#!/usr/bin/env bash
# dzweb on the GovTech VM: the part that needs root. Run it ONCE, yourself:
#
#     sudo bash ~/dzweb/deploy/vm/setup-root.sh
#
# It installs PostgreSQL and Redis, locks them to this machine with generated
# passwords, sizes them for a small shared VM, and runs the API and worker as
# system services under your own account. Safe to run again: passwords and
# data are kept, configuration is rewritten.
#
# The VM is shared with other teams, so:
#   * Redis and PostgreSQL listen on localhost only, both password-protected
#     (Redis holds the GovTech access token);
#   * the generated passwords go to ~/dzweb/.env.infra, readable by you only;
#   * the API listens on 127.0.0.1:8000 -- nothing is exposed until GovTech's
#     gateway publishes it. Ports 80 and 443 belong to someone else: untouched.
set -euo pipefail

APP_USER="${SUDO_USER:?run this with sudo, as the account that owns ~/dzweb}"
APP_HOME="$(getent passwd "$APP_USER" | cut -d: -f6)"
APP_DIR="$APP_HOME/dzweb"
INFRA="$APP_DIR/.env.infra"
[ -d "$APP_DIR/.venv" ] || { echo "no $APP_DIR/.venv: set up the application first (deploy/vm/README.md)"; exit 1; }

echo "== packages"
apt-get update -q
DEBIAN_FRONTEND=noninteractive apt-get install -y -q postgresql redis-server

echo "== passwords (kept if already generated)"
if [ -f "$INFRA" ]; then
  # shellcheck disable=SC1090
  . "$INFRA"
  PG_PASS="${DZWEB_PG_DSN#postgresql://dzweb:}"; PG_PASS="${PG_PASS%%@*}"
  REDIS_PASS="${DZWEB_REDIS_URL#redis://:}"; REDIS_PASS="${REDIS_PASS%%@*}"
else
  PG_PASS="$(openssl rand -hex 24)"
  REDIS_PASS="$(openssl rand -hex 24)"
fi

echo "== Redis: localhost only, password, 64 MB, translations evicted first"
cat > /etc/redis/dzweb.conf <<EOF
bind 127.0.0.1 -::1
protected-mode yes
port 6379
requirepass $REDIS_PASS
maxmemory 64mb
maxmemory-policy volatile-lru
save ""
appendonly no
EOF
chown redis:redis /etc/redis/dzweb.conf
chmod 640 /etc/redis/dzweb.conf
grep -qxF "include /etc/redis/dzweb.conf" /etc/redis/redis.conf \
  || echo "include /etc/redis/dzweb.conf" >> /etc/redis/redis.conf
systemctl restart redis-server

echo "== PostgreSQL: role and database dzweb, small memory footprint"
sudo -u postgres psql -q -v ON_ERROR_STOP=1 <<EOF
DO \$\$ BEGIN
  IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'dzweb') THEN CREATE ROLE dzweb LOGIN; END IF;
END \$\$;
ALTER ROLE dzweb PASSWORD '$PG_PASS';
ALTER SYSTEM SET shared_buffers = '64MB';
ALTER SYSTEM SET max_connections = '30';
ALTER SYSTEM SET listen_addresses = 'localhost';
EOF
sudo -u postgres psql -tAq -c "SELECT 1 FROM pg_database WHERE datname = 'dzweb'" | grep -q 1 \
  || sudo -u postgres createdb -O dzweb dzweb
systemctl restart postgresql

echo "== $INFRA (yours only)"
umask 077
cat > "$INFRA" <<EOF
DZWEB_PG_DSN=postgresql://dzweb:$PG_PASS@127.0.0.1:5432/dzweb
DZWEB_REDIS_URL=redis://:$REDIS_PASS@127.0.0.1:6379/0
EOF
chown "$APP_USER:$APP_USER" "$INFRA"
chmod 600 "$INFRA" "$APP_DIR/.env"

echo "== services: dzweb-api, dzweb-worker"
unit() {  # name, description, command, memory limit
  cat > "/etc/systemd/system/$1.service" <<EOF
[Unit]
Description=$2
After=network-online.target postgresql.service redis-server.service
Wants=postgresql.service redis-server.service

[Service]
User=$APP_USER
Group=$APP_USER
WorkingDirectory=$APP_DIR
EnvironmentFile=$APP_DIR/deploy/vm/dzweb.env
EnvironmentFile=$INFRA
ExecStart=$3
Restart=on-failure
RestartSec=5
MemoryMax=$4
NoNewPrivileges=yes
PrivateTmp=yes
ProtectSystem=full

[Install]
WantedBy=multi-user.target
EOF
}
unit dzweb-api "dzweb translation API (127.0.0.1:8000)" \
  "$APP_DIR/.venv/bin/uvicorn orchestrator.main:create --factory --host 127.0.0.1 --port 8000 --workers 1" 350M
unit dzweb-worker "dzweb background translation worker" \
  "$APP_DIR/.venv/bin/python -m orchestrator.queue.run_worker" 250M
systemctl daemon-reload
systemctl enable --now dzweb-api dzweb-worker
sleep 3
systemctl --no-pager --lines=0 status dzweb-api dzweb-worker | grep -E "Active:|Loaded:"
echo "== done. Check: curl -s -H \"Authorization: Bearer \$(grep ^DZWEB_OPS_TOKEN ~/dzweb/.env | cut -d= -f2)\" http://127.0.0.1:8000/v1/health"
