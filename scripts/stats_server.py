#!/usr/bin/env python3
"""王者农场挂机统计面板：零依赖本地 HTTP 服务。

页面本体是项目根目录的 stats.html（双击可离线打开）；
本服务提供同一页面的实时模式（/data 每10秒刷新）。
可被 wzry_auto.py 以后台线程启动（start_in_background），
也可独立运行：python scripts/stats_server.py --file assets/stats.json --port 8765
"""

import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

PAGE_FILE = Path(__file__).resolve().parent.parent / "stats.html"


class StatsHandler(BaseHTTPRequestHandler):
    stats_file = None   # stats.json 路径
    data_js = None      # stats_data.js 路径（离线快照数据）

    def do_GET(self):
        path = self.path.split("?")[0]
        if path in ("/", "/index.html"):
            try:
                body = PAGE_FILE.read_bytes()
            except OSError:
                self.send_error(404, "stats.html not found")
                return
            ctype = "text/html; charset=utf-8"
        elif path == "/data":
            try:
                body = Path(self.stats_file).read_bytes()
            except OSError:
                body = b"{}"
            ctype = "application/json; charset=utf-8"
        elif path == "/assets/stats_data.js":
            try:
                body = Path(self.data_js).read_bytes()
            except OSError:
                body = b"window.STATS = null;"
            ctype = "application/javascript; charset=utf-8"
        else:
            self.send_error(404)
            return
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass  # 静默访问日志，避免刷挂机终端


def _make_handler(stats_file):
    stats_path = Path(stats_file)
    return type("Handler", (StatsHandler,), {
        "stats_file": str(stats_path),
        "data_js": str(stats_path.parent / "stats_data.js"),
    })


def start_in_background(stats_file, port=8765, host="127.0.0.1"):
    """在后台守护线程中启动面板，返回 server 对象。"""
    server = ThreadingHTTPServer((host, port), _make_handler(stats_file))
    thread = threading.Thread(target=server.serve_forever, daemon=True, name="stats-server")
    thread.start()
    return server


def main(argv=None):
    import argparse
    parser = argparse.ArgumentParser(description="王者农场统计面板")
    parser.add_argument("--file", default="assets/stats.json", help="stats.json 路径")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--host", default="127.0.0.1")
    args = parser.parse_args(argv)
    server = ThreadingHTTPServer((args.host, args.port), _make_handler(args.file))
    print(f"统计面板: http://{args.host}:{args.port}  (数据: {args.file})")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
