#!/usr/bin/env python3
# Tiny static server for the almanac overlay (index.html + wx.json) plus a
# /health endpoint for monitoring. Replaces `python -m http.server`.
#
#   /health -> JSON {status, dataAgeSec, station, temp, updateAvailable}
#     status: "ok"      data is fresh
#             "stale"   wx.json older than STALE_SEC (engine likely stuck)
#             "error"   wx.json missing/unreadable (engine down)
#   HTTP 200 when ok, 503 otherwise (so a monitor can alert on non-2xx).
#
# Bind stays on 127.0.0.1 by default (chromium is local; no data leaves the box).
# Set WFP_BIND=0.0.0.0 to expose /health (and the page) to the LAN for remote
# monitoring — note that also makes wx.json LAN-readable.
import http.server, socketserver, json, os, time, threading

PORT      = int(os.environ.get("WFP_PORT", "8137"))
WEB       = os.environ.get("WFP_WEB", ".")
BIND      = os.environ.get("WFP_BIND", "127.0.0.1")
DATA      = os.environ.get("WFP_DATA", "/tmp/wfp_data/wx.json")
STALE_SEC = int(os.environ.get("WFP_STALE_SEC", "20"))

# monotonic count of wx.json fetches (the page's render heartbeat). The watchdog
# reads this via /health instead of grepping the access log, which lets us drop
# the per-request log churn below.
_polls      = 0
_polls_lock = threading.Lock()


class Handler(http.server.SimpleHTTPRequestHandler):
    def __init__(self, *a, **k):
        super().__init__(*a, directory=WEB, **k)

    def do_GET(self):
        p = self.path.split("?")[0]
        if p == "/health":
            return self._health()
        if p == "/wx.json":
            global _polls
            with _polls_lock:
                _polls += 1
        return super().do_GET()

    def log_request(self, code="-", size="-"):
        # drop the ~2s wx.json/index poll churn (it grew the log unbounded on
        # tmpfs). Keep errors and any other path so real problems still surface.
        p = self.path.split("?")[0]
        if str(code) in ("200", "304") and p in ("/wx.json", "/index.html", "/health", "/"):
            return
        super().log_request(code, size)

    def _health(self):
        h = {"status": "ok", "polls": _polls}
        try:
            with open(DATA) as f:
                d = json.load(f)
            age = time.time() - float(d.get("ts", 0))
            h["dataAgeSec"]      = round(age, 1)
            h["station"]         = d.get("station")
            h["temp"]            = d.get("temp")
            h["updateAvailable"] = d.get("updateAvailable")
            if age > STALE_SEC:
                h["status"] = "stale"
        except Exception as e:                                            # noqa: BLE001
            h["status"] = "error"
            h["error"]  = str(e)
        body = json.dumps(h).encode()
        self.send_response(200 if h["status"] == "ok" else 503)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class Server(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


if __name__ == "__main__":
    with Server((BIND, PORT), Handler) as httpd:
        httpd.serve_forever()
