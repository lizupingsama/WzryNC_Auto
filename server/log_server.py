#!/usr/bin/env python3
"""王者农场助手 · 日志收集服务（只用标准库，兼容 Python 3.6+）

助手（wzry_logupload.py）手动或每小时自动 POST 一份 gzip 过的 JSON 报告到
/api/logs。这里校验后按客户端分目录存盘；总占用快到上限（默认 1 GB）就先删
最旧的自动上传和截图、再删最旧的手动上传——写入前先腾地方，磁盘上的日志任何
时刻都不超过上限。占用按实际磁盘块（st_blocks）算，小文件的块对齐开销也算进去。

失败现场截图单独走 /api/shots：报告里列出最近几个现场的名字，回执的 need_shots
告诉客户端缺哪几张，客户端再逐张补传。只收回执里要过的、同名只存一份。

浏览：http://<服务器>:8421/admin，输入管理口令。口令默认存在数据目录的
admin_token.txt（首次启动自动生成），也可用环境变量 WZRY_LOG_ADMIN_TOKEN 指定。

  python3 log_server.py --data /var/lib/wzry-logserver --port 8421
"""

import argparse
import collections
import gzip
import hmac
import html
import http.server
import json
import os
import re
import secrets
import socketserver
import sys
import threading
import time
import zlib
from datetime import datetime
from http.cookies import SimpleCookie
from urllib.parse import parse_qs, quote, unquote, urlencode, urlsplit

# 与客户端 wzry_logupload.UPLOAD_KEY 一致。不是密钥，只挡扫端口的机器人
DEFAULT_KEY = "9TjhkucIejJuUElCRpSE541a"
MAX_BODY = 4 * 1024 * 1024        # 压缩后
MAX_JSON = 32 * 1024 * 1024       # 解压后，防 gzip 炸弹
MAX_SHOT = 3 * 1024 * 1024        # 单张截图（JPEG）
SHOTS_PER_REPORT = 5              # 一份报告最多要几张截图
PENDING_TTL = 3600                # 回执里要的截图多久内补传有效（秒）
UPLOAD_SLOTS = 3                  # 同时处理的上传数（服务器内存小）
IP_LIMIT = (30, 3600)             # 每个 IP 每小时最多 30 次
SHOT_IP_LIMIT = (60, 3600)        # 截图另算：每个 IP 每小时最多 60 张
LOGIN_LIMIT = (20, 3600)          # 后台登录：每个 IP 每小时最多试 20 次
CLIENT_LIMIT = (12, 3600)         # 每个客户端每小时最多 12 次
RECENT_KEEP = 500                 # client.json 里记多少条上传摘要
COOKIE = "wzry_admin"

CLIENT_RE = re.compile(r"^[0-9a-f]{32}$")
FILE_RE = re.compile(r"^(\d{8}-\d{6})_(manual|auto)_([0-9a-f]{6})\.json\.gz$")
# 失败现场名：时间戳_步骤名（与 wzry_auto.save_diagnostic 一致）
SHOT_RE = re.compile(r"^\d{8}_\d{6}_[A-Za-z0-9_]{1,60}$")
REASON_TEXT = {"manual": "手动", "auto": "自动"}


def now_text():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def log(message):
    # 3.6 在 systemd 的 C 语言环境下 stdout 是 ASCII，print 中文直接抛异常；按 UTF-8 字节写
    line = "[{}] {}\n".format(now_text(), message).encode("utf-8")
    stream = getattr(sys.stdout, "buffer", None)
    if stream is None:
        print(line.decode("utf-8"), end="", flush=True)
        return
    stream.write(line)
    stream.flush()


def gunzip_limited(data, limit):
    decomp = zlib.decompressobj(16 + zlib.MAX_WBITS)
    out = decomp.decompress(data, limit + 1)
    if len(out) > limit or decomp.unconsumed_tail:
        raise ValueError("解压后超过 {} MB".format(limit // 1024 // 1024))
    return out


def same_secret(given, expected):
    """定长比较；compare_digest 碰到非 ASCII 的 str 会抛异常，统一转成字节再比。"""
    return hmac.compare_digest(
        str(given or "").encode("utf-8", "surrogateescape"), expected.encode("utf-8"),
    )


def dig(data, *keys, default=""):
    """沿着 keys 取嵌套字段；报告来自客户端，结构不可信，缺了就给 default。"""
    for key in keys:
        if not isinstance(data, dict):
            return default
        data = data.get(key)
    return default if data is None else data


def human_size(num):
    for unit in ("B", "KB", "MB", "GB"):
        if num < 1024 or unit == "GB":
            return "{:.0f} {}".format(num, unit) if unit == "B" else "{:.1f} {}".format(num, unit)
        num /= 1024.0


# ------------------------------------------------------------
# 存储
# ------------------------------------------------------------
class Store:
    """按客户端分目录存报告：<data>/<client_id>/<时间>_<类型>_<随机>.json.gz，
    截图存在 <data>/<client_id>/shots/<现场名>.jpg。

    每个客户端目录另有 client.json（最近一次的电脑名、手机、IP 与上传摘要），
    后台列表页只读它，不用把每份报告都解压一遍。截图和报告一起记账、一起淘汰
    （截图按收到的时间排队，与自动上传同一档）。
    """

    def __init__(self, root, max_bytes, client_max_bytes):
        self.root = root
        self.max_bytes = max_bytes
        self.client_max_bytes = client_max_bytes
        self.lock = threading.Lock()
        self.files = {}     # path -> [client_id, stamp, kind(manual/auto/shot), bytes]
        self.metas = {}     # client.json path -> bytes
        self.total = 0      # 报告 + 截图 + client.json 的磁盘占用，增删时同步记账
        self.client_bytes = collections.Counter()
        self.client_count = collections.Counter()
        os.makedirs(root, exist_ok=True)
        self._scan()

    @staticmethod
    def disk_bytes(path):
        st = os.stat(path)
        blocks = getattr(st, "st_blocks", None)
        return blocks * 512 if blocks is not None else st.st_size

    @staticmethod
    def _round_up(size):
        return (size + 4095) // 4096 * 4096

    def _scan(self):
        for client_id in os.listdir(self.root):
            folder = self._client_dir(client_id)
            if not CLIENT_RE.match(client_id) or not os.path.isdir(folder):
                continue
            for name in os.listdir(folder):
                path = os.path.join(folder, name)
                if name.endswith(".tmp"):
                    os.remove(path)  # 上次写到一半被杀
                    continue
                match = FILE_RE.match(name)
                if match:
                    self._track(path, client_id, match.group(1), match.group(2))
                elif name == "client.json":
                    self._track_meta(path)
                elif name == "shots" and os.path.isdir(path):
                    self._scan_shots(client_id, path)

    def _scan_shots(self, client_id, folder):
        for name in os.listdir(folder):
            path = os.path.join(folder, name)
            if name.endswith(".tmp"):
                os.remove(path)
            elif name.endswith(".jpg") and SHOT_RE.match(name[:-4]):
                stamp = datetime.fromtimestamp(os.stat(path).st_mtime).strftime("%Y%m%d-%H%M%S")
                self._track(path, client_id, stamp, "shot")

    def _client_dir(self, client_id):
        return os.path.join(self.root, client_id)

    def _track(self, path, client_id, stamp, kind):
        size = self.disk_bytes(path)
        self.files[path] = [client_id, stamp, kind, size]
        self.total += size
        self.client_bytes[client_id] += size
        self.client_count[client_id] += 1

    def _track_meta(self, path):
        size = self.disk_bytes(path)
        self.total += size - self.metas.get(path, 0)
        self.metas[path] = size

    def _delete(self, path):
        client_id, _, _, size = self.files.pop(path)
        self.total -= size
        self.client_bytes[client_id] -= size
        self.client_count[client_id] -= 1
        try:
            os.remove(path)
        except OSError:
            pass
        if self.client_count[client_id] > 0:
            return
        # 这台电脑什么都不剩了：摘要和目录一并清掉，别留空壳占块
        del self.client_bytes[client_id], self.client_count[client_id]
        folder = self._client_dir(client_id)
        meta = os.path.join(folder, "client.json")
        self.total -= self.metas.pop(meta, 0)
        for remove, target in ((os.remove, meta), (os.rmdir, os.path.join(folder, "shots")),
                               (os.rmdir, folder)):
            try:
                remove(target)
            except OSError:
                pass

    def _evict(self, need, client_id, protect=None):
        """腾出 need 字节：先压这台电脑自己的份额，再压总量。自动上传和截图先删。"""
        def order(path):
            _, stamp, kind, _ = self.files[path]
            return (kind == "manual", stamp)

        removed = 0
        if self.client_bytes[client_id] + need > self.client_max_bytes:
            mine = sorted((p for p, r in self.files.items()
                           if r[0] == client_id and p != protect), key=order)
            while mine and self.client_bytes[client_id] + need > self.client_max_bytes:
                self._delete(mine.pop(0))
                removed += 1
        if self.total + need > self.max_bytes:
            everyone = sorted((p for p in self.files if p != protect), key=order)
            while everyone and self.total + need > self.max_bytes:
                self._delete(everyone.pop(0))
                removed += 1
        return removed

    def add(self, client_id, kind, data, summary):
        """存一份报告并更新 client.json；返回 (文件名, 为腾地方删掉的份数)。"""
        with self.lock:
            folder = self._client_dir(client_id)
            meta_path = os.path.join(folder, "client.json")
            meta = self.read_meta(client_id)
            # 先腾地方再写：报告 + 新 client.json 的块数，减去旧 client.json 已占的
            meta_guess = len(self._meta_bytes(meta, summary)) + 512
            need = (self._round_up(len(data)) + self._round_up(meta_guess)
                    - self.metas.get(meta_path, 0))
            if need > min(self.max_bytes, self.client_max_bytes):
                raise ValueError("单份报告超过存储上限")
            removed = self._evict(need, client_id)
            os.makedirs(folder, exist_ok=True)
            stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
            name = "{}_{}_{}.json.gz".format(stamp, kind, secrets.token_hex(3))
            path = os.path.join(folder, name)
            self._write(path, data)
            self._track(path, client_id, stamp, kind)
            summary["name"] = name
            self._write(meta_path, self._meta_bytes(meta, summary))
            self._track_meta(meta_path)
            # 块数估算偏小时再兜一次，刚存的这份不删
            removed += self._evict(0, client_id, protect=path)
            return name, removed

    def shot_path(self, client_id, name):
        return os.path.join(self._client_dir(client_id), "shots", name + ".jpg")

    def has_shot(self, client_id, name):
        with self.lock:
            return self.shot_path(client_id, name) in self.files

    def add_shot(self, client_id, name, data):
        """存一张失败现场截图；返回为腾地方删掉的份数。"""
        with self.lock:
            path = self.shot_path(client_id, name)
            if path in self.files:
                return 0
            need = self._round_up(len(data))
            if need > min(self.max_bytes, self.client_max_bytes):
                raise ValueError("截图超过存储上限")
            removed = self._evict(need, client_id)
            os.makedirs(os.path.dirname(path), exist_ok=True)
            self._write(path, data)
            self._track(path, client_id, datetime.now().strftime("%Y%m%d-%H%M%S"), "shot")
            removed += self._evict(0, client_id, protect=path)
            return removed

    def shot_names(self, client_id):
        with self.lock:
            return {os.path.basename(p)[:-4] for p, r in self.files.items()
                    if r[0] == client_id and r[2] == "shot"}

    def read_shot(self, client_id, name):
        if not CLIENT_RE.match(client_id) or not SHOT_RE.match(name):
            return None
        path = self.shot_path(client_id, name)
        with self.lock:
            if path not in self.files:
                return None
        try:
            with open(path, "rb") as handle:
                return handle.read()
        except OSError:
            return None

    @staticmethod
    def _meta_bytes(meta, summary):
        meta = dict(meta)
        recent = [summary] + list(meta.get("recent") or [])
        meta.update({k: v for k, v in summary.items() if k != "note"})
        meta["uploads"] = int(meta.get("uploads") or 0) + 1
        meta["recent"] = recent[:RECENT_KEEP]
        return json.dumps(meta, ensure_ascii=False, indent=1).encode("utf-8")

    @staticmethod
    def _write(path, data):
        tmp = path + ".tmp"
        with open(tmp, "wb") as handle:
            handle.write(data)
        os.replace(tmp, path)

    def read_meta(self, client_id):
        try:
            with open(os.path.join(self._client_dir(client_id), "client.json"),
                      encoding="utf-8") as handle:
                data = json.load(handle)
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            return {}

    def clients(self):
        with self.lock:
            ids = list(self.client_count)
            reports = collections.Counter(r[0] for r in self.files.values() if r[2] != "shot")
            shots = collections.Counter(r[0] for r in self.files.values() if r[2] == "shot")
        rows = []
        for client_id in ids:
            meta = self.read_meta(client_id)
            meta["client_id"] = client_id
            meta["stored"] = reports[client_id]
            meta["shots"] = shots[client_id]
            rows.append(meta)
        rows.sort(key=lambda m: str(m.get("received_at") or ""), reverse=True)
        return rows

    def client_reports(self, client_id):
        """该客户端还在盘上的报告，新的在前：[(文件名, stamp, kind, bytes)]。"""
        with self.lock:
            rows = [(os.path.basename(p), r[1], r[2], r[3])
                    for p, r in self.files.items() if r[0] == client_id and r[2] != "shot"]
        return sorted(rows, key=lambda row: row[0], reverse=True)

    def load(self, client_id, name):
        if not CLIENT_RE.match(client_id) or not FILE_RE.match(name):
            return None
        path = os.path.join(self._client_dir(client_id), name)
        with self.lock:
            if path not in self.files:
                return None
        try:
            with open(path, "rb") as handle:
                return json.loads(gunzip_limited(handle.read(), MAX_JSON).decode("utf-8"))
        except (OSError, ValueError):
            return None

    def usage(self):
        """(总占用字节, 报告份数, 截图张数)"""
        with self.lock:
            shots = sum(1 for r in self.files.values() if r[2] == "shot")
            return self.total, len(self.files) - shots, shots


class RateLimiter:
    def __init__(self, limit, window):
        self.limit, self.window = limit, window
        self.hits = {}
        self.lock = threading.Lock()

    def allow(self, key):
        now = time.monotonic()
        with self.lock:
            if len(self.hits) > 10000:  # 防字典无限长：清掉窗口外的
                self.hits = {k: q for k, q in self.hits.items() if q and now - q[-1] < self.window}
            queue = self.hits.setdefault(key, collections.deque())
            while queue and now - queue[0] > self.window:
                queue.popleft()
            if len(queue) >= self.limit:
                return False
            queue.append(now)
            return True


# ------------------------------------------------------------
# 页面
# ------------------------------------------------------------
CSS = """
:root{--bg:#f6f7f9;--fg:#1d2127;--muted:#6b7280;--card:#fff;--line:#e3e6ea;--accent:#1f8f5f;--pre:#f1f3f5}
@media (prefers-color-scheme:dark){:root{--bg:#15181c;--fg:#e4e7eb;--muted:#9aa3ad;--card:#1d2126;--line:#2d333a;--accent:#3ccf8e;--pre:#111418}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);font:14px/1.55 -apple-system,"Segoe UI","Microsoft YaHei",sans-serif}
main{max-width:1180px;margin:0 auto;padding:20px 16px 60px}a{color:var(--accent);text-decoration:none}a:hover{text-decoration:underline}
h1{font-size:20px;margin:0 0 4px}h2{font-size:16px;margin:28px 0 8px}.muted{color:var(--muted)}
.card{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:12px 14px;overflow-x:auto}
table{border-collapse:collapse;width:100%}th,td{text-align:left;padding:6px 8px;border-bottom:1px solid var(--line);vertical-align:top}
th{color:var(--muted);font-weight:600;white-space:nowrap}td.k{color:var(--muted);white-space:nowrap;width:1%}
pre{background:var(--pre);border:1px solid var(--line);border-radius:8px;padding:10px;overflow:auto;max-height:70vh;font:12px/1.5 Consolas,"Cascadia Mono",monospace;white-space:pre-wrap;word-break:break-all;margin:0}
.code{font-family:Consolas,monospace;font-weight:700;letter-spacing:.5px}.tag{display:inline-block;padding:0 6px;border-radius:4px;border:1px solid var(--line);font-size:12px}
form{display:flex;gap:8px;margin:12px 0}input{flex:1;max-width:360px;padding:6px 10px;border:1px solid var(--line);border-radius:6px;background:var(--card);color:var(--fg)}
button{padding:6px 14px;border:0;border-radius:6px;background:var(--accent);color:#fff;cursor:pointer}
.nav{margin-bottom:14px}.nav a{margin-right:14px}
.fail{margin-bottom:14px}.fail h3{font-size:15px;margin:0 0 8px}
.fail img{max-width:100%;display:block;border:1px solid var(--line);border-radius:8px;margin-bottom:8px}
.fail pre{max-height:460px}mark{background:rgba(232,137,12,.28);color:inherit;border-radius:3px}
"""


def esc(value):
    return html.escape(str(value), quote=True)


def page(title, body):
    return ("<!doctype html><html lang=\"zh-CN\"><head><meta charset=\"utf-8\">"
            "<meta name=\"viewport\" content=\"width=device-width,initial-scale=1\">"
            "<title>{}</title><style>{}</style></head><body><main>{}</main></body></html>"
            ).format(esc(title), CSS, body)


def kv_table(rows):
    cells = "".join(
        "<tr><td class=\"k\">{}</td><td>{}</td></tr>".format(esc(k), esc(v))
        for k, v in rows if v not in ("", None, [], {})
    )
    return "<div class=\"card\"><table>{}</table></div>".format(cells)


def pre_block(title, text, note=""):
    if not text:
        return ""
    extra = " <span class=\"muted\">{}</span>".format(esc(note)) if note else ""
    return "<h2>{}{}</h2><pre>{}</pre>".format(esc(title), extra, esc(text))


def phone_name(report_or_meta):
    phone = dig(report_or_meta, "phone", default={})
    if not isinstance(phone, dict):
        return str(phone)
    return phone.get("marketname") or phone.get("model") or phone.get("serial") or ""


def render_login(message=""):
    body = ("<h1>日志后台</h1><p class=\"muted\">{}</p>"
            "<form method=\"post\" action=\"/admin/login\"><input type=\"password\" name=\"token\" "
            "placeholder=\"管理口令\" autofocus><button>进入</button></form>").format(
                esc(message or "输入管理口令（服务器数据目录下的 admin_token.txt）"))
    return page("日志后台", body)


def render_clients(store, query):
    used, count, shot_count = store.usage()
    rows = everyone = store.clients()
    if query:
        needle = query.lower()
        rows = [m for m in rows if needle in " ".join(str(m.get(k, "")) for k in (
            "client_id", "code", "nickname", "hostname", "phone", "ip", "primary_ip",
        )).lower()]
    lines = []
    for meta in rows:
        cid = meta["client_id"]
        lines.append(
            "<tr><td><a class=\"code\" href=\"/admin/c/{cid}\">{code}</a></td><td>{nick}</td>"
            "<td>{host}</td><td>{phone}</td><td>{ver}</td><td>{seen}</td><td>{ip}</td>"
            "<td>{stored}</td></tr>".format(
                cid=esc(cid), code=esc(meta.get("code") or cid[:8].upper()),
                nick=esc(meta.get("nickname", "")), host=esc(meta.get("hostname", "")),
                phone=esc(meta.get("phone", "")), ver=esc(meta.get("app_version", "")),
                seen=esc(meta.get("received_at", "")), ip=esc(meta.get("ip", "")),
                stored=esc("{} 份{}".format(
                    meta["stored"], " · {} 图".format(meta["shots"]) if meta["shots"] else "")),
            ))
    table = ("<div class=\"card\"><table><tr><th>排查码</th><th>称呼</th><th>电脑</th><th>手机</th>"
             "<th>版本</th><th>最近上传</th><th>公网 IP</th><th>存着</th></tr>{}</table></div>").format(
                 "".join(lines) or "<tr><td colspan=\"8\" class=\"muted\">还没有上传</td></tr>")
    body = ("<h1>王者农场助手 · 日志后台</h1>"
            "<div class=\"muted\">已用 {used} / {cap} · {clients} 台电脑 · {count} 份报告 · {shots} 张截图</div>"
            "<form method=\"get\" action=\"/admin\"><input name=\"q\" value=\"{q}\" "
            "placeholder=\"排查码 / 称呼 / 电脑名 / 手机 / IP\"><button>搜索</button></form>{table}"
            ).format(used=esc(human_size(used)), cap=esc(human_size(store.max_bytes)),
                     clients=len(everyone), count=count, shots=shot_count,
                     q=esc(query), table=table)
    return page("日志后台", body)


def render_client(store, client_id):
    meta = store.read_meta(client_id)
    notes = {}
    for item in meta.get("recent") or []:
        if isinstance(item, dict) and item.get("name"):
            notes[item["name"]] = item
    lines = []
    for name, stamp, kind, size in store.client_reports(client_id):
        item = notes.get(name, {})
        base = "/admin/r/{}/{}".format(esc(client_id), esc(name))
        lines.append(
            "<tr><td><a href=\"{base}\">{when}</a></td><td><span class=\"tag\">{kind}</span></td>"
            "<td>{size}</td><td>{ip}</td><td>{note}</td>"
            "<td><a href=\"{base}/log\">纯文本日志</a> · <a href=\"{base}/json\">JSON</a></td></tr>".format(
                base=base, when=esc(item.get("received_at") or stamp),
                kind=esc(REASON_TEXT.get(kind, kind)), size=esc(human_size(size)),
                ip=esc(item.get("ip", "")), note=esc(str(item.get("note") or "")[:80]),
            ))
    head = kv_table([
        ("排查码", meta.get("code") or client_id[:8].upper()),
        ("称呼", meta.get("nickname")), ("电脑名", meta.get("hostname")),
        ("手机", meta.get("phone")), ("助手版本", meta.get("app_version")),
        ("最近上传", meta.get("received_at")), ("公网 IP", meta.get("ip")),
        ("累计上传", meta.get("uploads")), ("客户端 ID", client_id),
    ])
    body = ("<div class=\"nav\"><a href=\"/admin\">← 全部电脑</a></div><h1>{title}</h1>{head}"
            "<h2>报告</h2><div class=\"card\"><table><tr><th>上传时间</th><th>类型</th><th>大小</th>"
            "<th>IP</th><th>问题描述</th><th></th></tr>{rows}</table></div>").format(
                title=esc(meta.get("nickname") or meta.get("hostname") or client_id[:8].upper()),
                head=head, rows="".join(lines) or "<tr><td colspan=\"6\" class=\"muted\">没有报告</td></tr>")
    return page("日志后台 · " + (meta.get("hostname") or client_id[:8]), body)


def render_rounds(stats):
    rounds = dig(stats, "rounds_log", default=[])
    if not isinstance(rounds, list) or not rounds:
        return ""
    lines = []
    for item in reversed(rounds):
        if not isinstance(item, dict):
            continue
        lines.append("<tr><td>{}</td><td>{}</td><td>{}</td><td>{}</td><td>{}</td><td>{}</td></tr>".format(
            *(esc(item.get(k) if item.get(k) is not None else "") for k in (
                "round", "start", "end", "status", "next_wake", "reason"))))
    return ("<h2>最近 {} 轮（统计记录）</h2><div class=\"card\"><table><tr><th>轮次</th><th>开始</th>"
            "<th>结束</th><th>结果</th><th>下次唤醒</th><th>原因</th></tr>{}</table></div>").format(
                len(lines), "".join(lines))


FAIL_WORDS = ("失败", "超时", "被系统拒绝")


def failure_title(name, round_no):
    """20260923_094045_step3_start_game → 09-23 09:40:45 · step3_start_game · 第 14 轮"""
    parts = [name]
    match = re.match(r"^\d{4}(\d\d)(\d\d)_(\d\d)(\d\d)(\d\d)_(.+)$", name)
    if match:
        parts = ["{}-{} {}:{}:{}".format(*match.groups()[:5]), match.group(6)]
    if isinstance(round_no, int):
        parts.append("第 {} 轮".format(round_no))
    return " · ".join(parts)


def highlight_log(text, name):
    """逐行转义；保存现场那行和带「失败/超时」的提示行标出来，一眼看到在哪儿断的。"""
    out = []
    for line in text.splitlines():
        safe = esc(line)
        stripped = line.strip()
        if name in line or (stripped[:1] in ("❌", "⚠") and any(w in line for w in FAIL_WORDS)):
            safe = "<mark>{}</mark>".format(safe)
        out.append(safe)
    return "\n".join(out)


def render_failures(client_id, diagnostics, stored):
    """最近几次失败：截图配上那一轮的日志（新的在前）。"""
    cards = []
    for item in reversed(diagnostics):
        if not isinstance(item, dict):
            continue
        name = item.get("name")
        if not isinstance(name, str) or not SHOT_RE.match(name):
            continue
        log_text = item.get("log") if isinstance(item.get("log"), str) else ""
        has_shot = name in stored
        if not (item.get("shot") or log_text or has_shot):
            continue
        if has_shot:
            src = "/admin/s/{}/{}.jpg".format(esc(client_id), esc(name))
            shot = "<a href=\"{0}\"><img src=\"{0}\" loading=\"lazy\" alt=\"{1}\"></a>".format(
                src, esc(name))
        else:
            shot = "<p class=\"muted\">截图还没收到（下次上传会补传）</p>"
        if log_text:
            log_html = "<pre>{}</pre>".format(highlight_log(log_text, name))
        else:
            log_html = "<p class=\"muted\">运行日志里没找到这次失败（日志可能被清过）</p>"
        cards.append("<div class=\"card fail\"><h3>{}</h3>{}{}</div>".format(
            esc(failure_title(name, item.get("round"))), shot, log_html))
    if not cards:
        return ""
    return ("<h2>最近的失败 <span class=\"muted\">{} 次 · 截图原分辨率可点开 · "
            "下面是那一轮的完整日志</span></h2>{}").format(len(cards), "".join(cards))


def render_report(client_id, name, report, stored_shots=()):
    phone = dig(report, "phone", default={})
    phone = phone if isinstance(phone, dict) else {}
    battery = phone.get("battery") if isinstance(phone.get("battery"), dict) else {}
    pc = dig(report, "pc", default={})
    pc = pc if isinstance(pc, dict) else {}
    run_log = dig(report, "run_log", default={})
    run_log = run_log if isinstance(run_log, dict) else {}
    skew = phone.get("clock_skew_s")
    system = " ".join(str(phone.get(k)) for k in ("hyperos", "miui", "coloros", "emui") if phone.get(k))
    summary = kv_table([
        ("上传时间", dig(report, "server", "received_at")),
        ("类型", REASON_TEXT.get(report.get("reason"), report.get("reason"))),
        ("排查码", report.get("code")), ("称呼", report.get("nickname")),
        ("问题描述", report.get("note")),
        ("公网 IP", dig(report, "server", "ip")),
        ("助手版本", "v{}{}".format(dig(report, "app", "version") or "?",
                                   "（打包版）" if dig(report, "app", "frozen") else "（源码）")),
        ("安装目录", dig(report, "app", "dir")),
        ("电脑名", pc.get("hostname")), ("系统", pc.get("os")),
        ("本机 IP", "{}（全部：{}）".format(
            pc.get("primary_ip", ""), ", ".join(str(ip) for ip in (pc.get("local_ips") or [])))),
        ("电脑时间", "{} {}".format(pc.get("time", ""), pc.get("utc_offset", ""))),
        ("助手状态", "{} · {}".format(dig(report, "gui", "status"), dig(report, "gui", "device_status"))),
    ])
    phone_rows = kv_table([
        ("手机", " / ".join(str(phone.get(k)) for k in ("marketname", "model", "brand") if phone.get(k))),
        ("adb 序列号", "{}（{}）".format(phone.get("serial", ""), phone.get("state", ""))
         if phone.get("serial") else "未发现设备"),
        ("Android", "{} (SDK {}) {}".format(phone.get("android", ""), phone.get("sdk", ""), system)
         if phone.get("android") else ""),
        ("系统版本号", phone.get("build")), ("屏幕", phone.get("wm")),
        ("电量", " · ".join("{} {}".format(k, v) for k, v in battery.items())),
        ("屏幕状态", phone.get("wakefulness")), ("游戏", phone.get("game")),
        ("时钟偏差", "手机比电脑{} {} 秒".format("快" if skew > 0 else "慢", abs(skew))
         if isinstance(skew, int) else ""),
        ("adb", "{} · {}".format(dig(report, "adb", "path"), dig(report, "adb", "version"))),
    ])
    diagnostics = dig(report, "diagnostics", default=[])
    diagnostics = diagnostics if isinstance(diagnostics, list) else []
    diag_lines = []
    for item in reversed(diagnostics):
        if not isinstance(item, dict):
            continue
        details = dig(item, "context", "details", default=None)
        diag_lines.append("{}  {}".format(
            item.get("name", ""), json.dumps(details, ensure_ascii=False) if details else ""))
    diag_text = "\n".join(diag_lines)
    config = dig(report, "config", default={})
    stats = dig(report, "stats", default={})
    totals = dig(stats, "totals", default={})
    body = [
        "<div class=\"nav\"><a href=\"/admin\">← 全部电脑</a><a href=\"/admin/c/{cid}\">← 这台电脑</a>"
        "<a href=\"/admin/r/{cid}/{name}/log\">纯文本日志</a><a href=\"/admin/r/{cid}/{name}/json\">原始 JSON</a></div>"
        .format(cid=esc(client_id), name=esc(name)),
        "<h1>{}</h1>".format(esc(report.get("nickname") or pc.get("hostname") or report.get("code") or name)),
        summary,
        "<h2>手机与 adb</h2>", phone_rows,
        pre_block("adb 设备列表", dig(report, "adb", "devices")),
        pre_block("手机存档接力", phone.get("archive")),
        render_rounds(stats),
        pre_block("累计统计 / 下次唤醒", json.dumps(
            {"totals": totals, "next_wake": dig(stats, "next_wake", default=None),
             "updated": dig(stats, "updated")}, ensure_ascii=False, indent=2) if stats else ""),
        render_failures(client_id, diagnostics, stored_shots),
        pre_block("失败现场清单", diag_text, "最近 {} 个".format(len(diag_lines))),
        pre_block("设置（已脱敏）", json.dumps(config, ensure_ascii=False, indent=2) if config else ""),
        pre_block("最后一次启动信息", run_log.get("startup"), "启动早于下面这段日志，单独补上"),
        pre_block("运行日志", run_log.get("text") or run_log.get("error"), "最近 {} 轮 · 日志文件 {}{}".format(
            run_log.get("rounds", "?"), human_size(int(run_log.get("size") or 0)),
            " · 开头已截断" if run_log.get("head_cut") else "")),
        pre_block("界面日志", report.get("gui_log"), "界面上最近的几百行（含助手自己的提示）"),
        pre_block("界面错误日志 gui_error.log", report.get("error_log")),
    ]
    return page("报告 · " + (pc.get("hostname") or name), "".join(body))


# ------------------------------------------------------------
# HTTP
# ------------------------------------------------------------
class App:
    def __init__(self, store, key, admin_token):
        self.store = store
        self.key = key
        self.admin_token = admin_token
        self.ip_limiter = RateLimiter(*IP_LIMIT)
        self.client_limiter = RateLimiter(*CLIENT_LIMIT)
        self.shot_limiter = RateLimiter(*SHOT_IP_LIMIT)
        self.login_limiter = RateLimiter(*LOGIN_LIMIT)
        self.slots = threading.BoundedSemaphore(UPLOAD_SLOTS)
        self.pending = {}   # (client_id, 现场名) -> 过期时刻：回执里要过、还没收到的截图
        self.pending_lock = threading.Lock()

    def expect_shots(self, client_id, names):
        now = time.monotonic()
        with self.pending_lock:
            self.pending = {k: t for k, t in self.pending.items() if t > now}
            for name in names:
                self.pending[(client_id, name)] = now + PENDING_TTL

    def take_pending(self, client_id, name):
        with self.pending_lock:
            deadline = self.pending.pop((client_id, name), 0)
        return deadline > time.monotonic()


class Handler(http.server.BaseHTTPRequestHandler):
    server_version = "WzryLogServer/1"
    timeout = 60  # 慢速连接最多占一个线程这么久

    @property
    def app(self):
        return self.server.app

    def log_message(self, fmt, *args):  # 默认会把每个请求打到 stderr，太吵
        pass

    def remote_ip(self):
        ip = self.client_address[0]
        if ip in ("127.0.0.1", "::1"):  # 只信本机反代给的头
            forwarded = (self.headers.get("X-Forwarded-For") or "").split(",")[0].strip()
            ip = self.headers.get("X-Real-IP") or forwarded or ip
        return ip

    def send_body(self, status, body, content_type, extra=None):
        data = body.encode("utf-8") if isinstance(body, str) else body
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Cache-Control", "no-store")
        for key, value in (extra or {}).items():
            self.send_header(key, value)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(data)

    def send_json(self, status, payload):
        self.send_body(status, json.dumps(payload, ensure_ascii=False), "application/json; charset=utf-8")

    def send_html(self, status, body, extra=None):
        extra = dict(extra or {})
        # 日志内容全是客户端给的，页面一律转义之外再禁掉脚本
        extra["Content-Security-Policy"] = (
            "default-src 'none'; style-src 'unsafe-inline'; img-src 'self'; form-action 'self'")
        self.send_body(status, body, "text/html; charset=utf-8", extra)

    # ---------------- 上传 ----------------
    def do_POST(self):
        parts = urlsplit(self.path)
        if parts.path == "/admin/login":
            return self._handle_login()
        if parts.path == "/api/logs":
            limit, limiter, handle = MAX_BODY, self.app.ip_limiter, self._handle_upload
        elif parts.path == "/api/shots":
            limit, limiter, handle = MAX_SHOT, self.app.shot_limiter, self._handle_shot
        else:
            return self.send_json(404, {"ok": False, "error": "not found"})
        if not same_secret(self.headers.get("X-Wzry-Key"), self.app.key):
            return self.send_json(403, {"ok": False, "error": "key 不对"})
        ip = self.remote_ip()
        try:
            length = int(self.headers.get("Content-Length") or "")
        except ValueError:
            return self.send_json(411, {"ok": False, "error": "缺 Content-Length"})
        if length <= 0 or length > limit:
            return self.send_json(413, {"ok": False, "error": "太大了（上限 {} MB）".format(
                limit // 1024 // 1024)})
        if not limiter.allow(ip):
            return self.send_json(429, {"ok": False, "error": "上传太频繁，稍后再试"})
        if not self.app.slots.acquire(timeout=15):
            return self.send_json(503, {"ok": False, "error": "服务器忙，稍后再试"})
        try:
            handle(ip, self.rfile.read(length))
        finally:
            self.app.slots.release()

    def _handle_upload(self, ip, body):
        try:
            if (self.headers.get("Content-Encoding") or "").lower() == "gzip":
                body = gunzip_limited(body, MAX_JSON)
            report = json.loads(body.decode("utf-8"))
        except (ValueError, UnicodeDecodeError, zlib.error, EOFError) as exc:
            return self.send_json(400, {"ok": False, "error": "报告解析失败: {}".format(exc)})
        if not isinstance(report, dict):
            return self.send_json(400, {"ok": False, "error": "报告格式不对"})
        client_id = str(report.get("client_id") or "")
        reason = report.get("reason")
        if not CLIENT_RE.match(client_id) or reason not in REASON_TEXT:
            return self.send_json(400, {"ok": False, "error": "缺客户端 ID 或上传类型"})
        if not self.app.client_limiter.allow(client_id):
            return self.send_json(429, {"ok": False, "error": "上传太频繁，稍后再试"})
        received = now_text()
        report["server"] = {"received_at": received, "ip": ip,
                            "user_agent": self.headers.get("User-Agent", "")}
        summary = {
            "received_at": received, "reason": reason, "ip": ip,
            "code": client_id[:8].upper(),
            "nickname": str(report.get("nickname") or "")[:40],
            "note": str(report.get("note") or "")[:200],
            "hostname": str(dig(report, "pc", "hostname"))[:80],
            "primary_ip": str(dig(report, "pc", "primary_ip"))[:64],
            "phone": str(phone_name(report))[:80],
            "app_version": str(dig(report, "app", "version"))[:40],
        }
        data = gzip.compress(json.dumps(report, ensure_ascii=False).encode("utf-8"), 6)
        try:
            name, removed = self.app.store.add(client_id, reason, data, summary)
        except (OSError, ValueError) as exc:
            log("存储失败 {} {}: {}".format(ip, client_id[:8], exc))
            return self.send_json(507, {"ok": False, "error": "服务器存储失败"})
        need = self._wanted_shots(client_id, report.get("diagnostics"))
        self.app.expect_shots(client_id, need)
        used, count, shots = self.app.store.usage()
        log("收到 {} {} {} [{}] {} · 要 {} 张截图 · 共 {} 份 {} 图 {}{}".format(
            REASON_TEXT[reason], summary["code"], ip, summary["hostname"], human_size(len(data)),
            len(need), count, shots, human_size(used),
            " · 腾出 {} 份旧文件".format(removed) if removed else ""))
        self.send_json(200, {"ok": True, "id": name, "code": summary["code"], "ip": ip,
                             "need_shots": need})

    def _wanted_shots(self, client_id, diagnostics):
        """报告里标了 shot 的现场，服务器还没有的，最多 SHOTS_PER_REPORT 张。"""
        need = []
        for item in diagnostics if isinstance(diagnostics, list) else []:
            if not isinstance(item, dict) or item.get("shot") is not True:
                continue
            name = item.get("name")
            if (isinstance(name, str) and SHOT_RE.match(name) and name not in need
                    and not self.app.store.has_shot(client_id, name)):
                need.append(name)
        return need[-SHOTS_PER_REPORT:]

    def _handle_shot(self, ip, body):
        query = parse_qs(urlsplit(self.path).query)
        client_id = (query.get("client") or [""])[0]
        name = (query.get("name") or [""])[0]
        if not CLIENT_RE.match(client_id) or not SHOT_RE.match(name):
            return self.send_json(400, {"ok": False, "error": "缺客户端 ID 或现场名"})
        if not body.startswith(b"\xff\xd8\xff"):
            return self.send_json(400, {"ok": False, "error": "不是 JPEG"})
        if self.app.store.has_shot(client_id, name):
            self.app.take_pending(client_id, name)
            return self.send_json(200, {"ok": True, "dup": True})
        if not self.app.take_pending(client_id, name):
            return self.send_json(409, {"ok": False, "error": "没要过这张截图（先传报告）"})
        try:
            removed = self.app.store.add_shot(client_id, name, body)
        except (OSError, ValueError) as exc:
            log("截图存储失败 {} {}: {}".format(ip, client_id[:8], exc))
            return self.send_json(507, {"ok": False, "error": "服务器存储失败"})
        used, count, shots = self.app.store.usage()
        log("收到截图 {} {} {} · 共 {} 份 {} 图 {}{}".format(
            client_id[:8].upper(), name, human_size(len(body)), count, shots, human_size(used),
            " · 腾出 {} 份旧文件".format(removed) if removed else ""))
        self.send_json(200, {"ok": True})

    # ---------------- 后台 ----------------
    def do_HEAD(self):
        self.do_GET()

    def do_GET(self):
        try:
            self._route_get()
        except Exception as exc:  # 报告结构千奇百怪，渲染炸了也要回个页面
            log("页面出错 {}: {!r}".format(self.path[:200], exc))
            self.send_html(500, page("出错了", "<p>页面渲染出错：{}</p>"
                                              "<p><a href=\"/admin\">返回</a></p>".format(esc(exc))))

    def _route_get(self):
        parts = urlsplit(self.path)
        path = parts.path.rstrip("/") or "/"
        query = parse_qs(parts.query)
        if path == "/api/ping":
            return self.send_json(200, {"ok": True})
        if path != "/admin" and not path.startswith("/admin/"):
            return self.send_body(404, "not found", "text/plain; charset=utf-8")

        token = (query.get("token") or [""])[0]
        if token:
            # 兼容 ?token= 的老写法；换成 cookie 后跳回不带口令的地址
            rest = {k: v for k, v in query.items() if k != "token"}
            return self._login(token, path + ("?" + urlencode(rest, doseq=True) if rest else ""))
        if not self._authorized():
            return self.send_html(401, render_login())

        store = self.app.store
        if path == "/admin":
            return self.send_html(200, render_clients(store, (query.get("q") or [""])[0].strip()))
        segs = path.split("/")[2:]
        if len(segs) == 3 and segs[0] == "s" and segs[2].endswith(".jpg"):
            data = store.read_shot(segs[1], segs[2][:-4])
            if data is None:
                return self.send_body(404, "not found", "text/plain; charset=utf-8")
            return self.send_body(200, data, "image/jpeg")
        if len(segs) == 2 and segs[0] == "c" and CLIENT_RE.match(segs[1]):
            return self.send_html(200, render_client(store, segs[1]))
        if len(segs) in (3, 4) and segs[0] == "r":
            report = store.load(segs[1], segs[2])
            if report is None:
                return self.send_html(404, page("找不到", "<p>这份报告不存在或已被清理。</p>"
                                                "<p><a href=\"/admin\">返回</a></p>"))
            view = segs[3] if len(segs) == 4 else ""
            if view == "json":
                return self.send_body(200, json.dumps(report, ensure_ascii=False, indent=2),
                                      "application/json; charset=utf-8")
            if view == "log":
                run_log = dig(report, "run_log", default={})
                failures = [
                    "===== 失败现场 {} =====\n{}".format(d.get("name"), d["log"])
                    for d in reversed(dig(report, "diagnostics", default=[]) or [])
                    if isinstance(d, dict) and isinstance(d.get("log"), str)
                ]
                text = "\n\n".join(t for t in [
                    dig(run_log, "startup"), dig(run_log, "text"), dig(report, "gui_log"),
                ] + failures if t)
                return self.send_body(200, text, "text/plain; charset=utf-8")
            if view == "":
                return self.send_html(200, render_report(
                    segs[1], segs[2], report, store.shot_names(segs[1])))
        return self.send_html(404, page("找不到", "<p><a href=\"/admin\">返回</a></p>"))

    def _handle_login(self):
        """登录表单走 POST：口令只在请求体里，不进地址栏、浏览历史。"""
        try:
            length = int(self.headers.get("Content-Length") or "0")
        except ValueError:
            length = 0
        if not 0 < length <= 2048:
            return self.send_html(400, render_login("请求不对"))
        form = parse_qs(self.rfile.read(length).decode("utf-8", "replace"))
        return self._login((form.get("token") or [""])[0].strip(), "/admin")

    def _login(self, token, target):
        if not self.app.login_limiter.allow(self.remote_ip()):
            return self.send_html(429, render_login("试得太多了，一小时后再来"))
        if not same_secret(token, self.app.admin_token):
            return self.send_html(401, render_login("口令不对"))
        cookie = "{}={}; Path=/admin; Max-Age=2592000; HttpOnly; SameSite=Strict".format(
            COOKIE, quote(token))
        return self.send_body(303, "", "text/plain", {"Location": target, "Set-Cookie": cookie})

    def _authorized(self):
        cookie = SimpleCookie()
        try:
            cookie.load(self.headers.get("Cookie") or "")
        except Exception:
            return False
        morsel = cookie.get(COOKIE)
        if morsel is None:
            return False
        return same_secret(unquote(morsel.value), self.app.admin_token)


class Server(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address, app):
        self.app = app
        http.server.HTTPServer.__init__(self, address, Handler)


def load_admin_token(data_dir):
    token = os.environ.get("WZRY_LOG_ADMIN_TOKEN", "").strip()
    if token:
        return token
    path = os.path.join(data_dir, "admin_token.txt")
    try:
        with open(path, encoding="utf-8") as handle:
            token = handle.read().strip()
    except OSError:
        token = ""
    if not token:
        token = secrets.token_urlsafe(18)
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(token + "\n")
        log("已生成管理口令，存在 {}".format(path))
    return token


def main(argv=None):
    parser = argparse.ArgumentParser(description="王者农场助手日志收集服务")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8421)
    parser.add_argument("--data", default=os.path.join(os.path.dirname(os.path.abspath(__file__)), "data"))
    parser.add_argument("--max-mb", type=float, default=1024, help="全部报告的磁盘占用上限")
    parser.add_argument("--client-max-mb", type=float, default=200, help="单台电脑的占用上限")
    parser.add_argument("--key", default=os.environ.get("WZRY_LOG_UPLOAD_KEY") or DEFAULT_KEY)
    args = parser.parse_args(argv)

    store = Store(args.data, int(args.max_mb * 1024 * 1024), int(args.client_max_mb * 1024 * 1024))
    removed = store._evict(0, None)  # 上限调小后重启：先压回上限内
    app = App(store, args.key, load_admin_token(args.data))
    server = Server((args.host, args.port), app)
    used, count, shots = store.usage()
    log("日志服务启动 {}:{} · 数据 {} · 已用 {} / {} · {} 份报告 {} 张截图{}".format(
        args.host, args.port, args.data, human_size(used), human_size(store.max_bytes), count, shots,
        " · 清掉超额 {} 份".format(removed) if removed else ""))
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    sys.exit(main())
