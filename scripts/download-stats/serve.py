#!/usr/bin/env python3
"""Run the download stats page locally.

Records a snapshot of release download counts every 10 minutes (collect.py)
and serves the page and the recorded history on http://localhost:PORT/.
Nothing is published anywhere. History is kept in DATA_JSON, so a restart
continues the same timeline.

Usage: serve.py [DATA_JSON] [PORT] [REPO]
"""

from __future__ import annotations

import http.server
import os
import subprocess
import sys
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
INTERVAL = 10 * 60


def record(data_path: str, repo: str) -> None:
    try:
        subprocess.run(
            [sys.executable, "-I", os.path.join(HERE, "collect.py"), data_path, repo],
            check=True, timeout=120,
        )
    except Exception as exc:  # keep the loop alive through network blips
        print(f"record failed: {exc}", file=sys.stderr, flush=True)


def loop(data_path: str, repo: str) -> None:
    while True:
        record(data_path, repo)
        # Sleep to the next 10-minute boundary so points line up on the clock.
        time.sleep(INTERVAL - time.time() % INTERVAL)


class Handler(http.server.SimpleHTTPRequestHandler):
    data_path = ""

    def translate_path(self, path: str) -> str:
        path = path.split("?", 1)[0]
        if path == "/data.json":
            return self.data_path
        return os.path.join(HERE, "index.html") if path in ("/", "/index.html") else os.path.join(HERE, "__missing__")

    def end_headers(self) -> None:
        self.send_header("Cache-Control", "no-store")
        super().end_headers()

    def log_message(self, *args) -> None:
        pass


def main() -> None:
    data_path = os.path.abspath(sys.argv[1] if len(sys.argv) > 1 else os.path.join(HERE, "data.json"))
    port = int(sys.argv[2]) if len(sys.argv) > 2 else 8780
    repo = sys.argv[3] if len(sys.argv) > 3 else "stemdeckapp/stemdeck"
    Handler.data_path = data_path
    threading.Thread(target=loop, args=(data_path, repo), daemon=True).start()
    print(f"Serving http://localhost:{port}/  (history: {data_path})", flush=True)
    http.server.ThreadingHTTPServer(("0.0.0.0", port), Handler).serve_forever()


if __name__ == "__main__":
    main()
