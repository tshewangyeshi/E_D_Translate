# dzweb on the GovTech VM

`tyeshi@172.30.85.22` (GovNet; VPN from outside). Ubuntu 24.04, shared with
other teams: everything here listens on localhost only.

| Piece | Where |
|---|---|
| Code | `~/dzweb` (git clone, branch `feat/s0-s1-foundations`) |
| Python | `~/dzweb/.venv`, from the pinned `requirements.txt` |
| Settings | `deploy/vm/dzweb.env` (not secret, in git), `deploy/vm/sites.json` |
| Secrets | `~/dzweb/.env` (GovTech client, ops token, client-hash key), `~/dzweb/.env.infra` (database and Redis passwords): mode 600, never in git |
| API | `dzweb-api.service`, `127.0.0.1:8000`, memory limit 350 MB |
| Worker | `dzweb-worker.service`, memory limit 250 MB |
| PostgreSQL | database and role `dzweb`, localhost, password |
| Redis | localhost, password, 64 MB, translations evicted first |
| Backups | `dzweb-backup.timer`, nightly 02:30, `~/dzweb-backups`, 14 days (`deploy/vm/backup.sh`) |

## First setup (done 2026-10-07)

```bash
git clone --branch feat/s0-s1-foundations https://github.com/tshewangyeshi/E_D_Translate.git ~/dzweb
cd ~/dzweb && python3 -m venv --without-pip .venv
curl -sSf -o /tmp/get-pip.py https://bootstrap.pypa.io/get-pip.py && .venv/bin/python /tmp/get-pip.py
.venv/bin/pip install -r requirements.txt && .venv/bin/pip install --no-deps -e .
# ~/dzweb/.env: DZWEB_WSO2_* from GovTech, plus
#   DZWEB_OPS_TOKEN=$(openssl rand -hex 24) and DZWEB_CLIENT_HASH_KEY=$(openssl rand -hex 32)
sudo bash ~/dzweb/deploy/vm/setup-root.sh      # the only step that needs root
```

## Everyday

```bash
TOKEN=$(grep ^DZWEB_OPS_TOKEN ~/dzweb/.env | cut -d= -f2)
curl -s -H "Authorization: Bearer $TOKEN" http://127.0.0.1:8000/v1/health    # is it healthy?
curl -s -H "Authorization: Bearer $TOKEN" http://127.0.0.1:8000/v1/metrics   # counters
journalctl -u dzweb-api -u dzweb-worker --since "1 hour ago"                 # logs (sudo may be needed)
```

Backups:

```bash
systemctl list-timers dzweb-backup.timer              # when the next one runs
systemctl status dzweb-backup --no-pager              # did the last one work?
ls -lh ~/dzweb-backups                                # what is kept
sudo systemctl start dzweb-backup                     # one now (e.g. before an upgrade)
```

Restoring is in the header of `deploy/vm/backup.sh`. The copies are on the
same VM: they undo a bad migration or a deleted row, not the loss of the VM.

Update to the latest code:

```bash
cd ~/dzweb && git pull && .venv/bin/pip install -r requirements.txt && .venv/bin/pip install --no-deps -e .
sudo systemctl restart dzweb-api dzweb-worker      # migrations run at API start
```

## Demo page

http://172.30.85.22/dzweb/index.html is a copy of the G2C life-events page
with the widget, for showing dzweb before the portal adds the script tag. The
IP's own site (`/`) belongs to another team. Only ports 22, 80 and 443 reach the
VM and SSH forwarding is off, so it goes through nginx (`deploy/vm/demo-nginx.sh`).

Assemble `~/dzweb-demo` on a laptop (the VM cannot fetch the portal: its
certificate does not verify there), then copy it over:

```bash
python tools/g2c_demo/scrape.py && (cd adapters/widget && npm run build)
mkdir -p /tmp/dzweb-demo && cp -R tools/g2c_demo/data /tmp/dzweb-demo/data && cp -R adapters/widget/dist /tmp/dzweb-demo/widget
sed -e 's|src="/widget/main.js" data-dz-site="portal" data-dz-api="http://127.0.0.1:8000"|src="/dzweb/widget/main.js" data-dz-site="g2c" data-dz-api="http://172.30.85.22/dzweb"|' \
    tools/g2c_demo/index.html > /tmp/dzweb-demo/index.html
rsync -a --delete /tmp/dzweb-demo/ tyeshi@172.30.85.22:dzweb-demo/
```

Then, on the VM: `sudo bash ~/dzweb/deploy/vm/demo-nginx.sh` (`... undo` removes it).

- `http://172.30.85.22` is enrolled for `g2c` in `deploy/vm/sites.json`.
- Behind nginx every visitor is one client, so nothing reaches three distinct
  clients: pre-warm the page (`orchestrator.ops.prewarm`) before showing it.
- A browser that opened `/dzweb/` before the setup may keep the other team's
  app for it: use `/dzweb/index.html` or a private window.

## Not yet

- **Citizens cannot reach it.** GovTech's gateway must publish `127.0.0.1:8000`
  over HTTPS (backlog S2.3); then the widget's `data-dz-api` points there.
- **Set `DZWEB_TRUSTED_PROXIES`** in `deploy/vm/dzweb.env` to the gateway's
  address when it publishes the API (ask GovTech which address it connects
  from). Without it every citizen is one client: nothing reaches three distinct
  clients, so nothing is translated, and one reader's rate limit is everyone's.
- **Tier 1 rules are provisional** (`deploy/vm/sites.json`): the product owner
  confirms which portal pages and elements carry fees and legal text.
- **The nightly gate** (`tools/nightly_gate.py`) needs the frozen evaluation
  set copied here privately, then a timer.
- **Never run `tools/demo_site/serve.py` here**: it is for a laptop.
