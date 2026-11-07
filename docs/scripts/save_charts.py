"""Save every chart of the rendered site pages as SVG and PNG, as the Download buttons would.

Backup copies, one folder per page: training pages go to docs/assets/training/, the case-study
pages to docs/assets/java/, the xdssat pages and the home page to docs/assets/xdssat/. Needs the
rendered site served locally and Google Chrome:

    python -m http.server 8765 --directory docs/_site          # in another shell
    conda run --no-capture-output -n climate python docs/scripts/save_charts.py

Each chart is saved in its default state (the values its inputs start with). Uses chartImages()
of docs/assets/js/dl.js through the Chrome DevTools Protocol (package websocket-client).
"""
from __future__ import annotations

import argparse
import base64
import json
import subprocess
import tempfile
import time
import urllib.request
from pathlib import Path

import websocket

DOCS = Path(__file__).resolve().parents[1]
CHROME = next((p for p in [r"C:\Program Files\Google\Chrome\Application\chrome.exe",
                           r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe"] if Path(p).exists()), "chrome")
PAGES = (["index", "xdssat/index", "xdssat/gridding", "xdssat/implementation",
          "java/index", "java/data", "java/science", "learn/index"]
         + [f"learn/module{n}" for n in range(1, 7)])
GROUP = {"learn": "training", "java": "java", "xdssat": "xdssat"}


def target(page: str) -> tuple[str, str]:
    """(group folder under assets/, page folder) for a page path such as java/science."""
    if "/" not in page:
        return "xdssat", "home"
    section, name = page.split("/", 1)
    return GROUP.get(section, section), name.replace("/", "-")


class Tab:
    def __init__(self, port: int, url: str):
        req = urllib.request.Request(f"http://127.0.0.1:{port}/json/new?{url}", method="PUT")
        info = json.load(urllib.request.urlopen(req))
        self.ws = websocket.create_connection(info["webSocketDebuggerUrl"], timeout=120)
        self.id, self.n = info["id"], 0

    def call(self, method: str, **params):
        self.n += 1
        self.ws.send(json.dumps({"id": self.n, "method": method, "params": params}))
        while True:
            msg = json.loads(self.ws.recv())
            if msg.get("id") == self.n:
                if "error" in msg:
                    raise RuntimeError(msg["error"])
                return msg["result"]

    def js(self, expr: str, wait: bool = False):
        r = self.call("Runtime.evaluate", expression=expr, awaitPromise=wait, returnByValue=True)
        if "exceptionDetails" in r:
            raise RuntimeError(r["exceptionDetails"])
        return r["result"].get("value")


def wait_for_charts(tab: Tab, settle: float = 4.0, limit: float = 90.0) -> int:
    """Wait until the number of charts on the page stops changing."""
    t0, last, since = time.time(), -1, time.time()
    while time.time() - t0 < limit:
        n = tab.js("document.querySelectorAll('.dl-chart').length")
        if n != last:
            last, since = n, time.time()
        elif n > 0 and time.time() - since > settle:
            return n
        time.sleep(0.5)
    return last


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--base", default="http://localhost:8765")
    ap.add_argument("--pages", nargs="*", default=PAGES)
    ap.add_argument("--out", default=str(DOCS / "assets"), help="assets folder; each page goes to <group>/<page>/")
    ap.add_argument("--port", type=int, default=9333)
    a = ap.parse_args()
    out = Path(a.out)
    profile = tempfile.mkdtemp(prefix="xdssat-charts-")
    chrome = subprocess.Popen([CHROME, "--headless=new", "--disable-gpu", f"--remote-debugging-port={a.port}",
                               f"--user-data-dir={profile}", "--window-size=1280,1600", "about:blank"],
                              stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        for _ in range(50):
            try:
                urllib.request.urlopen(f"http://127.0.0.1:{a.port}/json/version", timeout=1)
                break
            except OSError:
                time.sleep(0.2)
        index = {}
        for page in a.pages:
            tab = Tab(a.port, f"{a.base}/{page}.html")
            n = wait_for_charts(tab)
            charts = tab.js("import('/assets/js/dl.js').then(m => m.chartImages(2))", wait=True) or []
            group, name0 = target(page)
            folder = out / group / name0
            folder.mkdir(parents=True, exist_ok=True)
            seen = {}
            for c in charts:
                name = c["name"]
                seen[name] = seen.get(name, 0) + 1
                if seen[name] > 1:
                    name = f"{name}-{seen[name]}"
                (folder / f"{name}.svg").write_text(c["svg"], encoding="utf-8")
                if c.get("png"):
                    (folder / f"{name}.png").write_bytes(base64.b64decode(c["png"].split(",", 1)[1]))
                index.setdefault(group, []).append({"page": page, "file": f"{folder.name}/{name}"})
            print(f"{page}: {n} charts on the page, {len(charts)} saved to {folder}")
            tab.ws.close()
        for group, rows in index.items():   # merge into the group's index: pages not rerun keep their entries
            f = out / group / "index.json"
            old = json.loads(f.read_text(encoding="utf-8")) if f.exists() else []
            done = {r["page"] for r in rows}
            f.write_text(json.dumps([r for r in old if r["page"] not in done] + rows, indent=1), encoding="utf-8")
    finally:
        chrome.terminate()


if __name__ == "__main__":
    main()
