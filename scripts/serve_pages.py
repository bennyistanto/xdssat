"""Serve the rendered site the way GitHub Pages does, to check it locally.

The site appears under /xdssat/ (as at bennyistanto.github.io/xdssat/), and any address that
has no file gets docs/_site/404.html with status 404. Plain ``python -m http.server`` does
neither, so it cannot show whether the 404 page works.

    python scripts/serve_pages.py [port]      # default 8766, then open http://localhost:8766/xdssat/
"""
from __future__ import annotations

import sys
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

SITE = Path(__file__).resolve().parents[1] / "docs" / "_site"
PREFIX = "/xdssat"


class PagesHandler(SimpleHTTPRequestHandler):
    def translate_path(self, path: str) -> str:
        path = path.split("?", 1)[0].split("#", 1)[0]
        if path == PREFIX or path.startswith(PREFIX + "/"):
            path = path[len(PREFIX):] or "/"
        else:
            path = "/__outside_the_site__"
        return super().translate_path(path)

    def send_error(self, code, message=None, explain=None):
        page = SITE / "404.html"
        if code != 404 or not page.exists():
            return super().send_error(code, message, explain)
        body = page.read_bytes()
        self.send_response(404)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)


if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8766
    handler = partial(PagesHandler, directory=str(SITE))
    print(f"http://localhost:{port}{PREFIX}/")
    ThreadingHTTPServer(("localhost", port), handler).serve_forever()
