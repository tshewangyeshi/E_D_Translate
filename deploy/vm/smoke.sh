#!/usr/bin/env bash
# dzweb smoke test on the VM: health, a page request, and one real translation.
#   ssh tyeshi@172.30.85.22 ~/dzweb/deploy/vm/smoke.sh
set -euo pipefail
cd "$(dirname "$0")/../.."
set -a; . deploy/vm/dzweb.env; . ./.env.infra; set +a
PYTHONIOENCODING=utf-8 exec .venv/bin/python deploy/vm/smoke.py 2> >(grep -v "HTTP Request" >&2)
