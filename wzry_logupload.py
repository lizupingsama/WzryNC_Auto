"""日志上传：把最近 50 轮务农日志连同排查信息打包，发到作者的日志服务器。

助手发给别人用，出了问题光靠对方口述很难排查。这里一次把排查要用的东西
收齐：最近 50 轮务农日志、界面日志、电脑名与 IP、手机型号与系统、adb
状态、脱敏后的设置、统计里最近 50 轮的起止时间与结果、最近的失败现场，
以及最近 5 次失败现场的截图和那一轮的日志。锁屏密码只报「有没有设置」，
日志里万一带出来也会抹掉。

截图不塞进报告：报告里只列出最近 5 个现场的名字，服务器回执说缺哪几张，
再逐张单独补传（upload_shots）。同一张截图上传过就不会再传、服务器也只存
一份，每小时自动上传不会把同样的图反复堆上去。

采集（collect_* / build_report）与上传（upload）分开：前者纯本地、可单测，
后者只管 HTTP。服务端见 server/log_server.py；上传地址可用环境变量
WZRY_LOG_UPLOAD_URL 或 gui_config.json 的 log_upload_url 覆盖。
"""

import gzip
import json
import os
import platform
import re
import socket
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path

SERVER_HOST = "47.108.49.28"
DEFAULT_URL = f"http://{SERVER_HOST}:8421/api/logs"
# 不是密钥：客户端代码谁都拿得到，只为挡掉扫端口的机器人往服务器乱投
UPLOAD_KEY = "9TjhkucIejJuUElCRpSE541a"
SCHEMA = 1

MAX_ROUNDS = 50
TAIL_READ_BYTES = 8 * 1024 * 1024   # 日志从不轮转，只读末尾这么多再切轮次
MAX_BODY_BYTES = 4 * 1024 * 1024    # 压缩后的上限，与服务端一致
GUI_LOG_LINES = 400
ERROR_LOG_TAIL = 32 * 1024
DIAGNOSTIC_LIMIT = 20
SHOT_COUNT = 5                      # 附带截图的失败现场个数（最近的几个）
SHOT_QUALITY = 85
MAX_SHOT_BYTES = 3 * 1024 * 1024    # 单张截图上限，与服务端一致
STARTUP_MAX_LINES = 80
FAILURE_LOG_MAX_LINES = 400         # 单个失败现场配的日志最多几行
FAILURE_LOG_HEAD = 30               # 超长的轮保留开头几行（轮次、唤醒、启动游戏）
FAILURE_LOG_AFTER = 20              # 保存现场那行之后再带几行（接着做了什么）

GAME_PKG = "com.tencent.tmgp.sgame"
DEVICE_ARCHIVE_FILE = "/sdcard/wzry_farm/state.json"

ROUND_MARK = re.compile(r"^# 第 (\d+) 轮务农", re.M)
BANNER = "王者荣耀农场自动化务农"
CLIENT_ID_RE = re.compile(r"^[0-9a-f]{32}$")
# 失败现场目录名：时间戳_步骤名（与 wzry_auto.save_diagnostic 一致）
DIAG_NAME_RE = re.compile(r"^\d{8}_\d{6}_[A-Za-z0-9_]{1,60}$")
SENSITIVE_KEY = re.compile(r"pwd|passw|token|secret", re.I)
# 上传前就地作废的配置键：纯内部簿记，对排查没用
INTERNAL_KEYS = ("log_client_id", "log_upload_fp")

# 手机属性：上传名 -> getprop 键。厂商系统版本各家键名不同，读不到的留空不报
PHONE_PROPS = (
    ("marketname", "ro.product.marketname"),
    ("model", "ro.product.model"),
    ("brand", "ro.product.brand"),
    ("android", "ro.build.version.release"),
    ("sdk", "ro.build.version.sdk"),
    ("build", "ro.build.display.id"),
    ("hyperos", "ro.mi.os.version.name"),
    ("miui", "ro.miui.ui.version.name"),
    ("coloros", "ro.build.version.opporom"),
    ("emui", "ro.build.version.emui"),
)
BATTERY_KEYS = ("level", "status", "AC powered", "USB powered", "temperature")


class UploadError(Exception):
    """上传失败，消息可直接给用户看。"""


def upload_url(config=None):
    return (
        os.environ.get("WZRY_LOG_UPLOAD_URL")
        or str((config or {}).get("log_upload_url") or "").strip()
        or DEFAULT_URL
    )


def short_code(client_id):
    """排查码：客户端 ID 前 8 位。用户报问题时报这个，作者后台按它搜。"""
    return str(client_id)[:8].upper()


def is_client_id(value):
    return bool(CLIENT_ID_RE.match(str(value or "")))


# ------------------------------------------------------------
# 运行日志：切出最近 N 轮
# ------------------------------------------------------------
def read_tail(path, limit):
    """读文件末尾至多 limit 字节并按 UTF-8 解码。

    返回 (文本, 文件大小, 是否截掉了开头)。从中间切开的半行直接丢掉。
    """
    size = os.path.getsize(path)
    with open(path, "rb") as handle:
        truncated = size > limit
        if truncated:
            handle.seek(size - limit)
        data = handle.read()
    if truncated:
        newline = data.find(b"\n")
        if newline >= 0:
            data = data[newline + 1:]
    return data.decode("utf-8", errors="replace"), size, truncated


def _line_start(text, pos):
    return text.rfind("\n", 0, pos) + 1


def _with_separator(text, start):
    """轮次标题、启动横幅上方那条 ==== 分隔线一并带上。"""
    if start <= 0:
        return start
    prev = _line_start(text, start - 1)
    line = text[prev:start].strip()
    return prev if line and not line.strip("=") else start


def extract_recent_rounds(text, max_rounds=MAX_ROUNDS):
    """截出最近 max_rounds 轮务农的日志（从那一轮的标题到末尾）。

    返回 (日志片段, 片段里的轮数, 启动信息块)。轮数不够就整段返回。
    启动信息块是最后一次启动时打印的设备、分辨率、存档接力等信息：那次
    启动要是早于截取起点（连挂几十轮没重启过），就单独补上，排查时才知道
    这台机器用的是哪套配置；启动在片段里的话它已经在了，返回空串。
    """
    marks = [m.start() for m in ROUND_MARK.finditer(text)]
    if len(marks) <= max_rounds:
        return text, len(marks), ""
    start = _with_separator(text, marks[-max_rounds])
    banner = text.rfind(BANNER)
    startup = ""
    if 0 <= banner < start:
        block_start = _with_separator(text, _line_start(text, banner))
        block_end = next((m for m in marks if m > banner), start)
        lines = text[block_start:block_end].rstrip().splitlines()
        if lines and not lines[-1].strip().strip("="):
            lines.pop()  # 属于下一轮标题的分隔线
        startup = "\n".join(lines[:STARTUP_MAX_LINES]).rstrip()
    return text[start:], max_rounds, startup


def collect_run_log(path, max_rounds=MAX_ROUNDS):
    """返回 (上传用的日志信息, 读到的整段日志末尾)；后者留给失败现场找对应日志。"""
    info = {"path": str(path)}
    try:
        text, size, truncated = read_tail(path, TAIL_READ_BYTES)
    except OSError as exc:
        info["error"] = f"读取失败: {exc}"
        return info, ""
    excerpt, rounds, startup = extract_recent_rounds(text, max_rounds)
    info.update(
        size=size,
        rounds=rounds,
        head_cut=truncated or len(excerpt) < len(text),
        startup=startup,
        text=excerpt,
    )
    return info, text


def failure_log(text, name):
    """失败现场对应的日志：保存现场时核心会打一行「已保存失败现场: …\\<现场名>」，
    按现场名找到这一行，截出它所在的整轮（从轮次标题到下一轮之前）。

    返回 (日志, 轮次号)；日志里找不到（被清过、或是别的助手存的现场）返回 (None, None)。
    等待循环刷屏的长轮只留开头和失败前的一大段，中间标明省略了几行。
    """
    pos = text.rfind(name)
    if pos < 0:
        return None, None
    marks = list(ROUND_MARK.finditer(text))
    before = [m for m in marks if m.start() < pos]
    after = [m for m in marks if m.start() > pos]
    start = (_with_separator(text, before[-1].start()) if before
             else _line_start(text, max(0, pos - 20000)))
    end = _with_separator(text, after[0].start()) if after else len(text)
    lines = text[start:end].rstrip().splitlines()
    if len(lines) > FAILURE_LOG_MAX_LINES:
        hit = max(i for i, line in enumerate(lines) if name in line)
        stop = min(len(lines), hit + FAILURE_LOG_AFTER + 1)
        tail_from = max(FAILURE_LOG_HEAD, stop - (FAILURE_LOG_MAX_LINES - FAILURE_LOG_HEAD))
        lines = (lines[:FAILURE_LOG_HEAD]
                 + [f"  ……（中间省略 {tail_from - FAILURE_LOG_HEAD} 行）……"]
                 + lines[tail_from:stop])
    return "\n".join(lines), int(before[-1].group(1)) if before else None


def attach_failure_logs(diagnostics, text):
    """给带截图的那几个失败现场配上日志，截图和日志放一起看才判断得了。"""
    for item in diagnostics:
        if not item.get("shot"):
            continue
        log, round_no = failure_log(text, item["name"])
        if log:
            item["log"] = scrub(log)
            if round_no is not None:
                item["round"] = round_no
    return diagnostics


def fingerprint(*paths):
    """日志文件的大小+修改时间。自动上传据此判断「上次传完之后有没有新东西」。"""
    parts = []
    for path in paths:
        try:
            st = os.stat(path)
            parts.append(f"{st.st_size}:{int(st.st_mtime)}")
        except OSError:
            parts.append("-")
    return "|".join(parts)


def scrub(text):
    """抹掉日志里可能带出的解锁密码（adb input text <密码>）。"""
    return re.sub(r"(input text )\S+", r"\1***", text or "")


# ------------------------------------------------------------
# 电脑 / adb / 手机
# ------------------------------------------------------------
def _primary_ip():
    """本机连外网走的那块网卡的地址。UDP connect 不发包，只让系统选一次路由。"""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.connect((SERVER_HOST, 80))
            return sock.getsockname()[0]
    except OSError:
        return ""


def _local_ips():
    ips = []
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ip = info[4][0]
            if ip not in ips:
                ips.append(ip)
    except OSError:
        pass
    return ips


def collect_pc_info():
    now = datetime.now().astimezone()
    return {
        "hostname": socket.gethostname(),
        "os": platform.platform(),
        "machine": platform.machine(),
        "primary_ip": _primary_ip(),
        "local_ips": _local_ips(),
        "time": now.strftime("%Y-%m-%d %H:%M:%S"),
        "utc_offset": now.strftime("%z"),
    }


def _run_text(adb_run, *args, timeout=10):
    """执行一条 adb 命令，返回 stdout；失败返回以 ! 开头的错误说明。"""
    try:
        result = adb_run(*args, timeout=timeout)
    except Exception as exc:  # 超时、adb 不存在……都只是少一项信息
        return f"!{type(exc).__name__}: {exc}"
    return result.stdout or ""


def collect_adb_info(adb_run, adb_path):
    version = _run_text(adb_run, "version")
    return {
        "path": str(adb_path),
        "version": " / ".join(version.strip().splitlines()[:2]),
        "devices": _run_text(adb_run, "devices", "-l").strip(),
    }


def collect_phone_info(adb_run, serial, state):
    """读手机型号、系统、分辨率、电量、游戏版本、时钟偏差和存档接力文件。"""
    info = {"serial": serial, "state": state}
    if state != "device":
        return info

    def shell(cmd, timeout=10):
        out = _run_text(adb_run, "-s", serial, "shell", cmd, timeout=timeout)
        return "" if out.startswith("!") else out

    props = "; ".join(f"echo {key}=$(getprop {prop})" for key, prop in PHONE_PROPS)
    for line in shell(props).splitlines():
        key, _, value = line.partition("=")
        if value.strip():
            info[key.strip()] = value.strip()
    wm = [line.strip() for line in shell("wm size; wm density").splitlines()]
    info["wm"] = " / ".join(line for line in wm if line)

    battery = {}
    for line in shell("dumpsys battery").splitlines():
        key, _, value = line.strip().partition(":")
        if key in BATTERY_KEYS:
            battery[key] = value.strip()
    info["battery"] = battery

    power = shell("dumpsys power | grep -m 1 mWakefulness=")
    info["wakefulness"] = power.strip().partition("=")[2]
    game = shell(f"dumpsys package {GAME_PKG} | grep -m 2 -E 'versionName|lastUpdateTime'")
    info["game"] = " ".join(game.split())

    # 两台电脑来回接力时，手机与电脑的时钟差是排查白浇水的关键线索
    epoch = shell("date +%s").strip()
    if epoch.isdigit():
        info["clock_skew_s"] = int(epoch) - int(time.time())
    info["archive"] = shell(f"cat {DEVICE_ARCHIVE_FILE} 2>/dev/null").strip()[:4000]
    return info


# ------------------------------------------------------------
# 本地文件：设置 / 统计 / 失败现场 / 错误日志
# ------------------------------------------------------------
def sanitize_config(config):
    """密码类字段只报「有没有设置」；内部簿记字段不传。"""
    clean = {}
    for key, value in (config or {}).items():
        if key in INTERNAL_KEYS:
            continue
        if SENSITIVE_KEY.search(str(key)):
            clean[key] = "（已设置）" if value else "（未设置）"
        else:
            clean[key] = value
    return clean


def collect_stats(path, max_rounds=MAX_ROUNDS):
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return {"error": f"读取失败: {exc}"}
    if not isinstance(data, dict):
        return {"error": "格式不对"}
    return {
        "updated": data.get("updated"),
        "start_time": data.get("start_time"),
        "totals": data.get("totals"),
        "next_wake": data.get("next_wake"),
        "rounds_log": (data.get("rounds_log") or [])[-max_rounds:],
    }


def collect_diagnostics(directory, limit=DIAGNOSTIC_LIMIT, shots=SHOT_COUNT):
    """最近的失败现场：目录名自带时间与步骤，context.json 很小顺手带上。

    最近 shots 个带截图的现场标上 shot=True，服务器据此决定要哪几张图。
    """
    try:
        names = sorted(
            p.name for p in Path(directory).iterdir()
            if p.is_dir() and DIAG_NAME_RE.match(p.name)
        )
    except OSError:
        return []
    items = []
    for name in names[-limit:]:
        item = {"name": name}
        context = Path(directory) / name / "context.json"
        try:
            if context.stat().st_size <= 8192:
                item["context"] = json.loads(context.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            pass
        items.append(item)
    for item in reversed(items):
        if shots <= 0:
            break
        if (Path(directory) / item["name"] / "screenshot.png").is_file():
            item["shot"] = True
            shots -= 1
    return items


def read_error_log(path, limit=ERROR_LOG_TAIL):
    try:
        return read_tail(path, limit)[0]
    except OSError:
        return ""


# ------------------------------------------------------------
# 组装 / 编码 / 上传
# ------------------------------------------------------------
def build_report(*, client_id, reason, app, gui, config, gui_log,
                 run_log_path, stats_path, error_log_path, diagnostics_dir,
                 adb=None, phone=None, nickname="", note=""):
    run_log, log_text = collect_run_log(run_log_path)
    for key in ("text", "startup"):
        if key in run_log:
            run_log[key] = scrub(run_log[key])
    diagnostics = attach_failure_logs(collect_diagnostics(diagnostics_dir), log_text)
    return {
        "schema": SCHEMA,
        "client_id": client_id,
        "code": short_code(client_id),
        "reason": reason,
        "nickname": str(nickname or "")[:40],
        "note": str(note or "")[:2000],
        "created_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "app": {
            "python": platform.python_version(),
            "executable": sys.executable,
            **(app or {}),
        },
        "pc": collect_pc_info(),
        "gui": gui or {},
        "adb": adb or {},
        "phone": phone or {},
        "config": sanitize_config(config),
        "stats": collect_stats(stats_path),
        "diagnostics": diagnostics,
        "run_log": run_log,
        "gui_log": scrub(gui_log),
        "error_log": scrub(read_error_log(error_log_path)),
    }


def encode_report(report, limit=MAX_BODY_BYTES):
    """JSON + gzip。超过上限就把运行日志从开头砍掉一半再试，最新的留着。"""
    while True:
        raw = json.dumps(report, ensure_ascii=False).encode("utf-8")
        body = gzip.compress(raw, 6)
        if len(body) <= limit:
            return body
        run_log = report.get("run_log") or {}
        text = run_log.get("text") or ""
        if len(text) < 4096:
            raise UploadError("日志压缩后仍超过上传上限")
        cut = text.find("\n", len(text) // 2)
        run_log["text"] = text[cut + 1:] if cut >= 0 else text[len(text) // 2:]
        run_log["head_cut"] = True


def upload(body, url, app_version="", timeout=30):
    """POST 报告到日志服务器，返回 JSON 回执（含 code、ip、need_shots）。"""
    return _post(url, body, {
        "Content-Type": "application/json",
        "Content-Encoding": "gzip",
    }, app_version, timeout)


def shots_url(report_url):
    """截图接口与报告接口同级：.../api/logs → .../api/shots。"""
    return report_url.rsplit("/", 1)[0] + "/shots"


def encode_shot(path, quality=SHOT_QUALITY, limit=MAX_SHOT_BYTES):
    """PNG 截图转 JPEG。保持原分辨率：日志里的点击坐标能直接对上图，
    必要时还能照着裁模板；超过上限才降质量、再不行才缩小。"""
    from io import BytesIO
    from PIL import Image

    with Image.open(path) as image:
        image = image.convert("RGB")
    for step in range(4):
        buffer = BytesIO()
        image.save(buffer, "JPEG", quality=quality, optimize=True)
        data = buffer.getvalue()
        if len(data) <= limit:
            return data
        if step == 0:
            quality = 70
        else:
            image = image.resize((image.width // 2, image.height // 2))
    raise UploadError("截图压缩后仍超过上限")


def upload_shots(report_url, client_id, names, diagnostics_dir, app_version="",
                 allowed=None, timeout=60):
    """逐张补传服务器说缺的截图。单张失败不影响其余，也不影响已传上去的报告。

    返回 (成功张数, [(现场名, 失败原因)])。allowed 是这次报告里标了 shot 的
    现场名：服务器要别的一概不给。
    """
    sent, failed = 0, []
    url = shots_url(report_url)
    for name in names or []:
        name = str(name)
        if not DIAG_NAME_RE.match(name) or (allowed is not None and name not in allowed):
            continue
        try:
            data = encode_shot(Path(diagnostics_dir) / name / "screenshot.png")
            query = urllib.parse.urlencode({"client": client_id, "name": name})
            _post(f"{url}?{query}", data, {"Content-Type": "image/jpeg"},
                  app_version, timeout)
            sent += 1
        except Exception as exc:  # 缺图、PIL 读不了、网络抖动……都只是少一张
            failed.append((name, str(exc) or type(exc).__name__))
    return sent, failed


def _post(url, body, headers, app_version, timeout):
    request = urllib.request.Request(
        url, data=body, method="POST",
        headers={
            **headers,
            "X-Wzry-Key": UPLOAD_KEY,
            "User-Agent": f"WzryFarm/{app_version or '?'}",
        },
    )
    # 直连不走系统代理：服务器在国内，代理只会多一跳；关了梯子系统代理却没清
    # 的电脑上，走代理反而一次都传不上
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(request, timeout=timeout) as response:
            payload = json.loads(response.read().decode("utf-8") or "{}")
    except urllib.error.HTTPError as exc:
        detail = ""
        try:
            detail = json.loads(exc.read().decode("utf-8")).get("error", "")
        except Exception:
            pass
        raise UploadError(
            f"服务器拒绝了上传（HTTP {exc.code}）" + (f"：{detail}" if detail else "")
        ) from None
    except (urllib.error.URLError, OSError) as exc:
        reason = getattr(exc, "reason", None) or exc
        raise UploadError(f"连不上日志服务器：{reason}") from None
    except ValueError:
        raise UploadError("服务器回执看不懂（不是 JSON）") from None
    if not isinstance(payload, dict) or not payload.get("ok"):
        message = payload.get("error") if isinstance(payload, dict) else ""
        raise UploadError(message or "服务器回执异常")
    return payload
