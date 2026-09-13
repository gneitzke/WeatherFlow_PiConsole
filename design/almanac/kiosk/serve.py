#!/usr/bin/env python3
# Tiny static server for the almanac overlay (index.html + wx.json) plus a
# /health endpoint for monitoring. Replaces `python -m http.server`.
#
#   /health -> JSON {status, reason, dataAgeSec, obsAgeSec, renders, polls, ...}
#     status: "ok"        engine heartbeat AND observation both fresh
#             "stale"     wx.json older than STALE_SEC (engine stalled)
#             "degraded"  wx.json fresh, but the newest observation is older
#                         than OBS_STALE_SEC (sensor silent — the engine is
#                         faithfully republishing a dead reading)
#             "error"     wx.json missing/unreadable (engine down)
#     reason: "engine stalled" | "sensor silent" | the read error
#   HTTP 200 when ok, 503 otherwise (so a monitor can alert on non-2xx).
#
# Bind stays on 127.0.0.1 by default (chromium is local; no data leaves the box).
# Set WFP_BIND=0.0.0.0 to expose /health (and the page) to the LAN for remote
# monitoring — note that also makes wx.json LAN-readable.
import http.server, socketserver, json, math, os, time, threading

PORT      = int(os.environ.get("WFP_PORT", "8137"))
WEB       = os.environ.get("WFP_WEB", ".")
BIND      = os.environ.get("WFP_BIND", "127.0.0.1")
DATA      = os.environ.get("WFP_DATA", "/tmp/wfp_data/wx.json")
STALE_SEC = int(os.environ.get("WFP_STALE_SEC", "20"))
# A station can go silent for minutes while the engine keeps emitting. Long
# enough not to trip on one dropped Tempest report (they arrive ~60 s apart).
OBS_STALE_SEC = int(os.environ.get("WFP_OBS_STALE_SEC", "300"))

LOOPBACK = ("127.0.0.1", "::1", "::ffff:127.0.0.1")

# Two counters, and the difference matters. `polls` counts wx.json REQUESTS
# from anyone — a LAN viewer, a curl, a renderer that fetched and then threw.
# `renders` counts frames the KIOSK actually painted: the page reports the
# previous frame's success by adding r=1 to its next poll, and only a loopback
# client is believed. The watchdog reads `renders`, because a growing request
# count never proved anything reached the screen.
_polls      = 0
_renders    = 0
_count_lock = threading.Lock()


class Handler(http.server.SimpleHTTPRequestHandler):
    def __init__(self, *a, **k):
        super().__init__(*a, directory=WEB, **k)

    def do_GET(self):
        path, _, query = self.path.partition("?")
        if path == "/health":
            return self._health()
        if path == "/wx.json":
            global _polls, _renders
            rendered = "r=1" in query.split("&") and self.client_address[0] in LOOPBACK
            viewed_radar = "view=radar" in query.split("&") and self.client_address[0] in LOOPBACK
            with _count_lock:
                _polls += 1
                if rendered:
                    _renders += 1
                if viewed_radar:
                    # Share only a timestamp with the emitter. Serialize writers
                    # and replace atomically so it never reads a partial epoch.
                    marker = os.path.join(os.path.dirname(DATA), "radar_viewed")
                    tmp = f"{marker}.tmp.{os.getpid()}"
                    try:
                        with open(tmp, "w") as f:
                            f.write(str(time.time()))
                        os.replace(tmp, marker)
                    except OSError:
                        pass  # an optional demand hint must never break polling
                    finally:
                        try:
                            os.unlink(tmp)
                        except OSError:
                            pass
        return super().do_GET()

    def log_request(self, code="-", size="-"):
        # drop the ~2s wx.json/index poll churn (it grew the log unbounded on
        # tmpfs). Keep errors and any other path so real problems still surface.
        p = self.path.split("?")[0]
        if str(code) in ("200", "304") and p in ("/wx.json", "/index.html", "/health", "/"):
            return
        super().log_request(code, size)

    def _health(self):
        h = {"status": "ok", "polls": _polls, "renders": _renders}
        try:
            with open(DATA) as f:
                d = json.load(f)
            age = time.time() - float(d.get("ts", 0))
            h["dataAgeSec"]      = round(age, 1)
            h["station"]         = d.get("station")
            h["temp"]            = d.get("temp")
            h["updateAvailable"] = d.get("updateAvailable")
            obs_age = d.get("obsAgeSec")
            # a bool is not an age, nor is NaN/inf: treat those as absent
            if isinstance(obs_age, bool) or not isinstance(obs_age, (int, float)) or not math.isfinite(obs_age):
                obs_age = None
            else:
                obs_age = float(obs_age) + max(age, 0.0)
            h["obsAgeSec"] = round(obs_age, 1) if obs_age is not None else None
            # Order matters: a stalled engine is the bigger fault, and its stale
            # file makes every observation in it look old too.
            if age > STALE_SEC:
                h["status"], h["reason"] = "stale", "engine stalled"
            elif obs_age is not None and obs_age > OBS_STALE_SEC:
                h["status"], h["reason"] = "degraded", "sensor silent"
        except Exception as e:                                            # noqa: BLE001
            h["status"] = "error"
            h["reason"] = h["error"] = str(e)
        try:
            body = json.dumps(h, allow_nan=False).encode()
        except ValueError as e:                                          # a non-finite crept in
            h["status"] = "error"
            body = json.dumps({"status": "error", "reason": str(e)}).encode()
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
