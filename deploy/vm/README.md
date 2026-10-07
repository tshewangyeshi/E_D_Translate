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

Update to the latest code:

```bash
cd ~/dzweb && git pull && .venv/bin/pip install -r requirements.txt && .venv/bin/pip install --no-deps -e .
sudo systemctl restart dzweb-api dzweb-worker      # migrations run at API start
```

## Not yet

- **Citizens cannot reach it.** GovTech's gateway must publish `127.0.0.1:8000`
  over HTTPS (backlog S2.3); then the widget's `data-dz-api` points there.
- **Tier 1 rules are provisional** (`deploy/vm/sites.json`): the product owner
  confirms which portal pages and elements carry fees and legal text.
- **The nightly gate** (`tools/nightly_gate.py`) needs the frozen evaluation
  set copied here privately, then a timer.
- **Never run `tools/demo_site/serve.py` here**: it is for a laptop.
