"""Serve the demo page and the built widget on http://localhost:8080 (development only).

    cd adapters/widget && npm run build
    python tools/demo_site/serve.py

``/`` is this folder; ``/g2c/`` is the local copy of the pilot page
(``tools/g2c_demo``); ``/widget/`` is ``adapters/widget/dist``. The pages talk
to the API on http://127.0.0.1:8000; ``sites.json`` enrols this origin as the
"portal" site.
"""

from __future__ import annotations

import http.server
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
WIDGET = HERE.parents[1] / "adapters" / "widget" / "dist"
G2C = HERE.parent / "g2c_demo"
PORT = 8080


class Handler(http.server.SimpleHTTPRequestHandler):
    base = http.server.SimpleHTTPRequestHandler.extensions_map
    extensions_map = {**base, ".js": "text/javascript"}  # modules need a JavaScript type

    def translate_path(self, path: str) -> str:
        route = path.split("?", 1)[0].split("#", 1)[0]
        if route.startswith("/widget/"):
            return str(WIDGET / route[len("/widget/") :])
        if route.startswith("/g2c/"):
            return str(G2C / (route[len("/g2c/") :] or "index.html"))
        return str(HERE / (route.lstrip("/") or "index.html"))

    def end_headers(self) -> None:
        self.send_header("Cache-Control", "no-store")  # always the latest build
        super().end_headers()


def main() -> int:
    if not (WIDGET / "main.js").exists():
        print("build the widget first: cd adapters/widget && npm run build", file=sys.stderr)
        return 2
    with http.server.ThreadingHTTPServer(("127.0.0.1", PORT), Handler) as server:
        print(f"demo page on http://localhost:{PORT}/, pilot copy on http://localhost:{PORT}/g2c/")
        server.serve_forever()
    return 0


if __name__ == "__main__":
    sys.exit(main())
