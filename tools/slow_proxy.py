"""A stand-in for the computer: forwards to the real server, then goes away.

    python -u .tmp/slow_proxy.py --port 8900 --fail-after-batches 1 --delay 1.5

Point the app at this port and a run is interrupted at a *known* point: the
first N batches are tagged normally, every later one gets a 503, which is the
"电脑端没了" case the app reports as `出错：`. The line per batch on stdout is the
record of how many images the computer had already tagged when it vanished.

`--delay` makes each batch take long enough that the interruption lands while the
run is visibly in progress.
"""
import argparse
import json
import sys
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

for stream in (sys.stdout, sys.stderr):
    try:
        stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

ARGS = None
LOCK = threading.Lock()
STATE = {"batches": 0, "images": 0}


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *a):  # keep stdout for our own lines only
        pass

    def _forward(self, method):
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length else None
        images = 0
        if method == "POST" and body:
            try:
                payload = json.loads(body.decode("utf-8"))
                images = len(payload) if isinstance(payload, list) else len(payload.get("images", []))
            except Exception:
                images = -1

        if method == "POST":
            with LOCK:
                STATE["batches"] += 1
                batch = STATE["batches"]
            if batch > ARGS.fail_after_batches:
                print(f"  batch {batch}: {images} images -> 503 (the computer is gone)", flush=True)
                self.send_response(503)
                self.send_header("Content-Type", "text/plain; charset=utf-8")
                self.send_header("Content-Length", "14")
                self.end_headers()
                self.wfile.write(b"server is gone")
                return
            time.sleep(ARGS.delay)
            with LOCK:
                STATE["images"] += max(0, images)
            print(f"  batch {batch}: {images} images -> tagged (running total {STATE['images']})", flush=True)

        url = ARGS.upstream.rstrip("/") + self.path
        request = urllib.request.Request(url, data=body, method=method)
        for name in ("X-Albumcat-Token", "Content-Type"):
            if self.headers.get(name):
                request.add_header(name, self.headers[name])
        try:
            with urllib.request.urlopen(request, timeout=600) as response:
                payload = response.read()
                code = response.status
                ctype = response.headers.get("Content-Type", "application/json")
        except urllib.error.HTTPError as error:
            payload = error.read()
            code = error.code
            ctype = error.headers.get("Content-Type", "application/json")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def do_GET(self):
        self._forward("GET")

    def do_POST(self):
        self._forward("POST")


def main():
    global ARGS
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8900)
    ap.add_argument("--upstream", default="http://127.0.0.1:8899")
    ap.add_argument("--fail-after-batches", type=int, default=1)
    ap.add_argument("--delay", type=float, default=1.5)
    ARGS = ap.parse_args()

    httpd = ThreadingHTTPServer(("0.0.0.0", ARGS.port), Handler)
    print(f"slow_proxy on :{ARGS.port} -> {ARGS.upstream}; "
          f"the first {ARGS.fail_after_batches} batch(es) get through, later ones 503", flush=True)
    httpd.serve_forever()


if __name__ == "__main__":
    main()
