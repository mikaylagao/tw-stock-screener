# -*- coding: utf-8 -*-
"""本機伺服器：py server.py 後開啟 http://localhost:8765"""
import json, os, socket, sys, threading, traceback, webbrowser
from http.server import ThreadingHTTPServer, SimpleHTTPRequestHandler
from urllib.parse import urlparse, parse_qs

import engine, fetch

BASE = os.path.dirname(os.path.abspath(__file__))
WEB = os.path.join(BASE, "web")
PORT = int(os.environ.get("PORT", "8765"))
# 預設開放同一區域網路（手機連線用）；加 --local-only 則只允許本機
HOST = "127.0.0.1" if "--local-only" in sys.argv else "0.0.0.0"


def lan_urls():
    ips = set()
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))  # 不會真的送出封包，只用來取得對外網卡 IP
        ips.add(s.getsockname()[0])
        s.close()
    except OSError:
        pass
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ip = info[4][0]
            if ip.startswith(("192.168.", "10.")) or (ip.startswith("172.") and 16 <= int(ip.split(".")[1]) <= 31):
                ips.add(ip)
    except OSError:
        pass
    return [f"http://{ip}:{PORT}" for ip in sorted(ips) if not ip.startswith("127.")]

state = {"engine": None, "lock": threading.Lock(), "job": {"running": False, "msg": "", "error": None}}


def get_engine():
    with state["lock"]:
        if state["engine"] is None:
            state["engine"] = engine.Engine()
        return state["engine"]


def do_update():
    job = state["job"]
    try:
        fetch.run(progress=lambda m: job.update(msg=m))
        job["msg"] = "重新計算指標…"
        e = engine.Engine()
        with state["lock"]:
            state["engine"] = e
        job["msg"] = "更新完成"
    except Exception as ex:
        job["error"] = repr(ex)
        traceback.print_exc()
    finally:
        job["running"] = False


class H(SimpleHTTPRequestHandler):
    extensions_map = {**SimpleHTTPRequestHandler.extensions_map,
                      ".webmanifest": "application/manifest+json", ".js": "text/javascript; charset=utf-8",
                      ".html": "text/html; charset=utf-8"}

    def end_headers(self):
        if self.path.split("?")[0] in ("/", "/index.html", "/sw.js"):
            self.send_header("Cache-Control", "no-cache")
        super().end_headers()

    def __init__(self, *a, **kw):
        super().__init__(*a, directory=WEB, **kw)

    def log_message(self, fmt, *args):
        pass

    def _json(self, obj, code=200):
        b = json.dumps(obj, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def do_GET(self):
        u = urlparse(self.path)
        q = parse_qs(u.query)
        try:
            if u.path == "/api/dates":
                return self._json({"dates": get_engine().dates(), "job": state["job"]})
            if u.path == "/api/screen":
                res = get_engine().screen(q.get("date", [""])[0])
                return self._json(res if res else {"error": "無此日期資料"}, 200 if res else 404)
            if u.path == "/api/update":
                return self._json(state["job"])
            if u.path == "/api/info":
                return self._json({"lan": lan_urls() if HOST == "0.0.0.0" else []})
        except Exception as ex:
            traceback.print_exc()
            return self._json({"error": repr(ex)}, 500)
        return super().do_GET()

    def do_POST(self):
        if urlparse(self.path).path == "/api/update":
            job = state["job"]
            if not job["running"]:
                job.update(running=True, msg="開始更新", error=None)
                threading.Thread(target=do_update, daemon=True).start()
            return self._json(job)
        self.send_error(404)


if __name__ == "__main__":
    print("載入資料與計算指標中…", flush=True)
    get_engine()
    srv = ThreadingHTTPServer((HOST, PORT), H)
    url = f"http://localhost:{PORT}"
    print(f"台股每日條件篩選平台已啟動：{url}（關閉此視窗即停止）", flush=True)
    for u in (lan_urls() if HOST == "0.0.0.0" else []):
        print(f"手機（同一個 Wi-Fi）請開啟：{u}", flush=True)
    if "--no-browser" not in sys.argv:
        webbrowser.open(url)
    srv.serve_forever()
