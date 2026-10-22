#!/usr/bin/env bash
# Start one part of the local demo in this window (development only).
#
#     bash tools/demo_site/run-demo.sh api      # the dzweb API on 127.0.0.1:8000
#     bash tools/demo_site/run-demo.sh worker   # the background translation worker
#
# Needs Docker's PostgreSQL and Redis (docker compose up -d) and the GovTech
# credentials in the git-ignored .env. Settings: tools/demo_site/demo.env.
# Close the window, or press Ctrl+C, to stop it.
set -euo pipefail
cd "$(dirname "$0")/../.."
set -a
. tools/demo_site/demo.env
. ./.env
set +a
case "${1:-}" in
  api) exec .venv/Scripts/python.exe -m uvicorn orchestrator.main:create --factory --host 127.0.0.1 --port 8000 ;;
  worker) exec .venv/Scripts/python.exe -m orchestrator.queue.run_worker ;;
  *) echo "usage: run-demo.sh api|worker" >&2; exit 2 ;;
esac
