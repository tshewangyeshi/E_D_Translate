#!/usr/bin/env bash
# dzweb on the GovTech VM: the demo page at http://172.30.85.22/dzweb/index.html
#
#     sudo bash ~/dzweb/deploy/vm/demo-nginx.sh          # set up, or refresh the page
#     sudo bash ~/dzweb/deploy/vm/demo-nginx.sh undo     # remove it again
#
# Serves ~/dzweb-demo (a copy of the G2C life-events page with the widget; the
# README says how to assemble it) and puts the API behind it at /dzweb/v1/.
#
# The VM is shared with other teams, so:
#   * port 80 belongs to another team's site; this adds ONE include line to its
#     default server and leaves everything else in that file alone
#   * the config is backed up first, checked with nginx -t, and put back if the
#     check fails
# Safe to run again.
set -euo pipefail

SITE=/etc/nginx/sites-available/default
SNIP=/etc/nginx/snippets/dzweb-demo.conf
LINE='	include snippets/dzweb-demo.conf;'
SRC="$(getent passwd "${SUDO_USER:-tyeshi}" | cut -d: -f6)/dzweb-demo"
WEB=/var/www/dzweb-demo

if [ "${1:-}" = undo ]; then
  sed -i '\|include snippets/dzweb-demo.conf;|d' "$SITE"
  rm -f "$SNIP"
  rm -rf "$WEB"
  nginx -t && systemctl reload nginx
  echo "removed"
  exit 0
fi

echo "== page: $SRC -> $WEB"
rsync -a --delete "$SRC/" "$WEB/"
chown -R root:root "$WEB"
chmod -R a+rX "$WEB"

echo "== nginx: /dzweb/ and /dzweb/v1/"
cat > "$SNIP" <<'CONF'
# dzweb demo: a copy of the G2C life-events page with the widget, and the API behind it.
location = /dzweb { return 301 /dzweb/; }
location ^~ /dzweb/v1/ {
    # The page and the API share this address, so browsers send no Origin on
    # GET; dzweb requires one (403). Fill in the page's own when it is missing.
    set $dzweb_origin $http_origin;
    if ($dzweb_origin = "") { set $dzweb_origin "http://$host"; }
    proxy_set_header Origin $dzweb_origin;
    proxy_pass http://127.0.0.1:8000/v1/;
    proxy_set_header Host $host;
    proxy_read_timeout 60s;
}
location ^~ /dzweb/ {
    alias /var/www/dzweb-demo/;
    index index.html;
    add_header Cache-Control "no-store";
}
CONF

cp -p "$SITE" "$SITE.bak-dzweb-$(date +%Y%m%d%H%M%S)"
# The include contains '/', so sed uses '|' as its separator.
grep -q 'include snippets/dzweb-demo.conf;' "$SITE" ||
  sed -i "0,\|^\s*server_name _;|s|^\(\s*server_name _;\)|\1\n$LINE|" "$SITE"
if nginx -t; then
  systemctl reload nginx
else
  sed -i '\|include snippets/dzweb-demo.conf;|d' "$SITE"
  rm -f "$SNIP"
  echo "nginx test failed; nothing changed"
  exit 1
fi

echo "== restart dzweb (picks up deploy/vm/sites.json)"
systemctl restart dzweb-api dzweb-worker
echo "Open http://172.30.85.22/dzweb/index.html"
