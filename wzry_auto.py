#!/usr/bin/env python3
"""
王者荣耀农场自动化务农 v3
完全基于模板匹配 + 摇杆操作
新增：浇水时间计算、统计功能、多分辨率支持、唤醒解锁、亮度控制
"""

import cv2
import numpy as np
import re
import subprocess
import time
import math
import os
import random
import signal
import socket
import sys
import shutil
import threading
from pathlib import Path
from datetime import datetime, timedelta

# Windows 中文系统下 stdout 走管道/重定向时默认 GBK，无法编码 emoji 输出；
# line_buffering 保证走管道时日志逐行实时到达 GUI（含 PyInstaller 打包后）。
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)

# ============================================================
# 配置
# ============================================================
# PyInstaller 打包后 __file__ 指向内部资源目录，改用 exe 所在目录，
# 让 assets、diagnostics、统计文件与可执行文件同级（绿色便携）。
IS_FROZEN = bool(getattr(sys, "frozen", False))
SCRIPT_DIR = Path(sys.executable).resolve().parent if IS_FROZEN else Path(__file__).parent
ASSETS_DIR = SCRIPT_DIR / "assets"
TEMPLATE_DIR = ASSETS_DIR / "templates"
SCREENSHOT_PATH = str(ASSETS_DIR / "current.png")
STATS_FILE = str(ASSETS_DIR / "stats.json")

# 等待超过该秒数时熄灭手机屏幕（下一轮开头会自动唤醒解锁）
SCREEN_OFF_WAIT_SECONDS = 180

GAME_PKG = "com.tencent.tmgp.sgame"
GAME_ACT = f"{GAME_PKG}/com.tencent.tmgp.sgame.SGameActivity"

# ADB 解析顺序：显式 WZRY_ADB > 随程序分发的 platform-tools > PATH > 常见安装位置
def _find_adb():
    explicit = os.environ.get("WZRY_ADB")
    if explicit:
        return explicit
    bundled = SCRIPT_DIR / "platform-tools" / ("adb.exe" if os.name == "nt" else "adb")
    if bundled.exists():
        return str(bundled)
    found = shutil.which("adb")
    if found:
        return found
    for candidate in ("/home/lili/android-tools/platform-tools/adb", "/tmp/platform-tools/adb"):
        if Path(candidate).exists():
            return candidate
    return "adb"

ADB = _find_adb()
DEVICE = os.environ.get("WZRY_DEVICE", "")
DEFAULT_DEVICE = os.environ.get("WZRY_DEFAULT_DEVICE", "")
UNLOCK_PWD = os.environ.get("WZRY_UNLOCK_PWD", "")  # 锁屏密码，为空则不输入密码
BASE_W, BASE_H = 1280, 720

# 摇杆配置（从测试结果）
JOYSTICK_CENTER = (160, 486)
JOYSTICK_RADIUS = 200

# 步骤6移动参数（按分辨率配置）
# 格式: (w,h): {"center": (cx,cy), "angle": 度, "distance": px, "duration": ms}
STEP6_CONFIG = {
    (1280, 720): {"center": (160, 486), "angle": 120, "distance": 200, "duration": 1500},
    (2400, 1080): {"center": (430, 755), "angle": 120, "distance": 250, "duration": 1500},
}

# 默认步骤6配置
_step6_cfg = {"center": (160, 486), "angle": 120, "distance": 200, "duration": 1500}

# 模板搜索区域使用归一化坐标 (x1, y1, x2, y2)，减少动态背景误匹配。
TEMPLATE_ROIS = {
    "start_game.png": (0.25, 0.55, 0.75, 1.00),
    # 左界须留到 0.70：版本更新公告类宽弹窗的 ✕ 中心在 0.78 屏宽处，
    # 模板主体会伸到 0.78 左侧，框到 0.78 会把它裁掉导致匹配不到。
    "close_popup.png": (0.70, 0.03, 0.96, 0.30),
    "close_popup_event.png": (0.70, 0.03, 0.96, 0.30),
    # 协议弹窗只框「同意」按钮（左边界 0.48 把 0.49 屏宽处的「拒绝」排除在外）
    "agree_terms.png": (0.48, 0.62, 0.82, 0.92),
    # 新版 UI 通用左上角返回箭头（活动页/农场页同款同位）
    "back_arrow.png": (0.04, 0.00, 0.19, 0.12),
    "lainongchang.png": (0.00, 0.55, 0.55, 1.00),
    "refresh_pos.png": (0.75, 0.70, 1.00, 1.00),
    "oneclick_farm.png": (0.45, 0.35, 0.80, 0.80),
    "harvest_continue.png": (0.30, 0.70, 0.70, 1.00),
}

TEMPLATE_THRESHOLDS = {
    "start_game.png": 0.75,
    "close_popup.png": 0.90,
    "close_popup_event.png": 0.78,
    "agree_terms.png": 0.85,
    # 白色箭头字形主导得分，换背景实测仍有 0.917（农场页），噪声上限 0.573
    "back_arrow.png": 0.80,
    "lainongchang.png": 0.75,
    "refresh_pos.png": 0.60,
    "oneclick_farm.png": 0.75,
    "harvest_continue.png": 0.85,
}

# ============================================================
# 统计数据
# ============================================================
class Stats:
    def __init__(self):
        self.rounds = 0          # 执行轮数
        self.harvests = 0        # 成熟收获次数
        self.total_exp = 0       # 累计获得经验
        self.total_crops = {}    # 累计收获作物 {作物名: 数量}
        self.start_time = datetime.now()
        self.rounds_log = []     # 每轮记录（最多保留最近200轮）
        self.next_wake = None    # 下一轮启动信息 {"wake", "target", "reason"}
        self.current = None      # 当前轮记录

    @staticmethod
    def _now():
        return datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    def load(self):
        """从磁盘恢复历史统计，实现跨会话累计"""
        try:
            with open(STATS_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, ValueError):
            return
        totals = data.get("totals", {})
        self.rounds = totals.get("rounds", 0)
        self.harvests = totals.get("harvests", 0)
        self.total_exp = totals.get("exp", 0)
        self.total_crops = totals.get("crops", {}) or {}
        self.rounds_log = data.get("rounds_log", []) or []
        self.next_wake = data.get("next_wake")
        # 上次会话残留的"进行中"轮次标记为中断
        for record in self.rounds_log:
            if record.get("status") == "进行中":
                record["status"] = "中断退出"

    def begin_round(self, round_num):
        """开始新一轮，写入进行中记录"""
        self.rounds = round_num
        self.current = {
            "round": round_num,
            "start": self._now(),
            "end": None,
            "status": "进行中",
            "exp": 0,
            "crops": {},
            "next_wake": None,
            "reason": None,
        }
        self.rounds_log.append(self.current)
        del self.rounds_log[:-200]
        self.save()

    def add_harvest(self, exp=0, crops=None):
        """记录一次收获"""
        self.harvests += 1
        if exp > 0:
            self.total_exp += exp
        if crops:
            for name, count in crops.items():
                self.total_crops[name] = self.total_crops.get(name, 0) + count
        if self.current is not None:
            self.current["exp"] += exp
            for name, count in (crops or {}).items():
                self.current["crops"][name] = self.current["crops"].get(name, 0) + count
        self.save()

    def set_next_wake(self, wake_time, target_time=None, reason=""):
        """记录下一轮启动时间（浇水/成熟/重试）"""
        info = {
            "wake": wake_time.strftime("%Y-%m-%d %H:%M:%S"),
            "target": target_time.strftime("%Y-%m-%d %H:%M:%S") if target_time else None,
            "reason": reason,
        }
        self.next_wake = info
        if self.current is not None:
            self.current["next_wake"] = info["wake"]
            self.current["reason"] = reason
        self.save()

    def finish_round(self, status="完成"):
        """结束当前轮；只覆盖仍为进行中的状态，避免重复标记"""
        if self.current is not None and self.current["status"] == "进行中":
            self.current["status"] = status
            self.current["end"] = self._now()
        self.save()

    def save(self):
        """原子写入 stats.json，供统计面板读取"""
        data = {
            "updated": self._now(),
            "start_time": self.start_time.strftime("%Y-%m-%d %H:%M:%S"),
            "totals": {
                "rounds": self.rounds,
                "harvests": self.harvests,
                "exp": self.total_exp,
                "crops": self.total_crops,
            },
            "next_wake": self.next_wake,
            "rounds_log": self.rounds_log,
        }
        try:
            tmp = STATS_FILE + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            os.replace(tmp, STATS_FILE)
            # 同步生成离线快照数据，供直接双击 stats.html 查看
            js_path = os.path.join(os.path.dirname(STATS_FILE), "stats_data.js")
            with open(js_path + ".tmp", "w", encoding="utf-8") as f:
                f.write("window.STATS = ")
                json.dump(data, f, ensure_ascii=False)
                f.write(";")
            os.replace(js_path + ".tmp", js_path)
        except OSError as exc:
            print(f"  ⚠️ 统计写入失败: {exc}")
    
    def summary(self):
        elapsed = datetime.now() - self.start_time
        hours = elapsed.total_seconds() / 3600
        print("\n" + "=" * 60)
        print("📊 务农统计")
        print("=" * 60)
        print(f"  执行轮数: {self.rounds}")
        print(f"  成熟收获: {self.harvests} 次")
        if self.total_exp > 0:
            print(f"  累计经验: +{self.total_exp}")
        if self.total_crops:
            for name, qty in self.total_crops.items():
                print(f"  {name}: {qty} 个")
        print(f"  运行时长: {hours:.1f} 小时")
        print(f"  开始时间: {self.start_time.strftime('%Y-%m-%d %H:%M:%S')}")
        print(f"  结束时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
        print("=" * 60)

stats = Stats()

def signal_handler(sig, frame):
    print("\n\n⚠️ 收到终止信号，正在退出...")
    stats.finish_round("中断退出")
    stats.summary()
    sys.exit(0)

signal.signal(signal.SIGINT, signal_handler)
signal.signal(signal.SIGTERM, signal_handler)
if hasattr(signal, "SIGBREAK"):
    # Windows 下收到 CTRL_BREAK / 控制台关闭时也走统一清理流程
    signal.signal(signal.SIGBREAK, signal_handler)

# “立刻务农”指令（GUI 经 stdin 发送）：只在轮次间等待时接受。
# _idle_wait 标记“当前正处于等待期”，务农步骤执行中收到的指令直接驳回。
_farm_now = threading.Event()
_idle_wait = threading.Event()

def wait_or_farm_now(seconds):
    """轮次之间的等待，可被“立刻务农”指令打断；返回 True 表示被打断。

    等待期间置位 _idle_wait，stdin 监听线程据此决定接受还是驳回指令。
    用分段 time.sleep 轮询而不是 Event.wait 整段等待：Windows 下锁等待
    不会被 SIGINT 唤醒，保住 time.sleep 才能让“停止挂机”继续即时生效。
    """
    end = time.monotonic() + seconds
    _idle_wait.set()
    try:
        while True:
            if _farm_now.is_set():
                _farm_now.clear()
                print("  ⚡ 立刻务农：跳过等待，马上开始新一轮")
                return True
            remaining = end - time.monotonic()
            if remaining <= 0:
                return False
            time.sleep(min(1.0, remaining))
    finally:
        _idle_wait.clear()

# ============================================================
# 浇水时间计算
# ============================================================
import json

# 最佳浇水节点表（单位：分钟）。实测最佳浇水时机为播种后
# 1h 档 0/20/40/44、8h 档 0/2h40/5h20/5h52、16h 档 0/5h20/10h40/11h44、
# 32h 档 0/10h40/21h20/23h12；换算成剩余时间刻度（成熟时长 - 播种后时刻）
# 即下表节点，剩余时间倒数到节点值的时刻浇水一次。播种后 0 分钟那次
# 由每轮一键务农自带，不列节点；节点 0 表示不再浇水、等成熟收获。
# 浇水会缩短剩余时间，下一轮按新剩余时间自然落入更低的节点/档位，
# 因此不能以"当前时刻 + 周期比例"重排（旧算法在剩余时间短时会把
# 浇水间隔越排越小）。
WATER_NODE_TIERS = [
    (60,   [40, 20, 16, 0]),
    (480,  [320, 160, 128, 0]),
    (960,  [640, 320, 256, 0]),
    (1920, [1280, 640, 528, 0]),
]

def calculate_next_water_time(show_mature_time, now=None):
    """按当前剩余成熟时间匹配档位，计算下次浇水（或收获）时间。

    档位匹配：剩余 ≤ 1h → 1h 档；1h~8h → 8h 档；8h~16h → 16h 档；
    其余 → 32h 档。档位内取小于等于剩余时间的最大节点，
    下次浇水时间 = 成熟时间 - 节点值。
    例：剩余 50 分钟 → 1h 档节点 40 → 10 分钟后（剩余 40 分钟时）浇水。
    :param show_mature_time: 本次浇水后 OCR 读到的成熟时间 (datetime)
    :param now: 当前时间，缺省取 datetime.now()（测试注入用）
    :return: dict with tier_min, node_min, next_water(None=等成熟), mature_time
    """
    if now is None:
        now = datetime.now()
    remain_min = (show_mature_time - now).total_seconds() / 60
    print(f"  💧 当前剩余成熟时间:{remain_min:.1f} 分钟")

    tier_min, nodes = next(
        ((d, ns) for d, ns in WATER_NODE_TIERS if remain_min <= d),
        WATER_NODE_TIERS[-1],
    )
    node_min = max((n for n in nodes if n <= remain_min), default=0)
    print(f"  🌱 匹配档位:{tier_min // 60} 小时档,浇水节点:剩余 {node_min} 分钟")

    if node_min <= 0:
        next_water = None
        print(f"  🌾 已过最后浇水节点,等待成熟收获:{show_mature_time.strftime('%m-%d %H:%M:%S')}")
    else:
        next_water = show_mature_time - timedelta(minutes=node_min)
        print(f"  💧 下次浇水时间:{next_water.strftime('%m-%d %H:%M:%S')}"
              f"(约 {remain_min - node_min:.1f} 分钟后)")

    return {
        "tier_min": tier_min,
        "node_min": node_min,
        "next_water": next_water,
        "mature_time": show_mature_time,
    }

# ============================================================
# ADB 基础操作
# ============================================================
def adb_shell(cmd, timeout=10):
    """执行设备端 shell 命令，不经过主机 shell。"""
    result = subprocess.run(
        [ADB, "-s", DEVICE, "shell", cmd],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        timeout=timeout,
    )
    return result.stdout

def adb_shell_root(cmd):
    """通过 su 执行设备端命令，重定向只在设备端解析。"""
    result = subprocess.run(
        [ADB, "-s", DEVICE, "shell", "su", "-c", cmd],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        timeout=15,
    )
    return result.stdout

def adb_command(*args, timeout=10):
    """执行 ADB 主机命令并返回 CompletedProcess。"""
    return subprocess.run(
        [ADB, "-s", DEVICE, *map(str, args)],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        timeout=timeout,
    )

def force_stop_game():
    """退出游戏；所有成功、失败和异常出口统一调用。"""
    if not DEVICE:
        return
    try:
        adb_shell(f"am force-stop {GAME_PKG}")
        _reapply_low_brightness()
    except Exception as exc:
        print(f"  ⚠️ 退出游戏失败: {exc}")

def wake_and_unlock(password=""):
    """唤醒屏幕并解锁"""
    global _original_brightness, _original_auto_brightness
    
    # 检测屏幕是否亮着
    out = adb_shell("dumpsys power | grep mHoldingDisplaySuspendBlocker")
    if "mHoldingDisplaySuspendBlocker=true" in out:
        print("  📱 屏幕已亮，无需唤醒")
        return True
    
    print("  🔆 唤醒屏幕...")
    
    # 如果之前设置了低亮度，先应用再唤醒（防止闪亮）
    if _original_brightness is not None:
        if _brightness_mode == 'root_zero':
            adb_shell_root("echo 0 > /sys/class/backlight/panel0-backlight/brightness")
        elif _brightness_mode == 'root_one':
            adb_shell_root("echo 1 > /sys/class/backlight/panel0-backlight/brightness")
        else:
            adb_shell("settings put system screen_brightness_mode 0")
            adb_shell("settings put system screen_brightness 1")
    
    # 唤醒屏幕
    adb_input("input keyevent KEYCODE_WAKEUP")
    time.sleep(1)
    
    # 按设备物理尺寸计算解锁手势，兼容手机和模拟器。
    import re
    size_out = adb_shell("wm size")
    match = re.search(r"(\d+)x(\d+)", size_out)
    if match:
        width, height = map(int, match.groups())
        portrait_w, portrait_h = min(width, height), max(width, height)
    else:
        portrait_w, portrait_h = 1080, 2400
    unlock_x = portrait_w // 2
    adb_input(
        f"input swipe {unlock_x} {int(portrait_h * 0.75)} "
        f"{unlock_x} {int(portrait_h * 0.25)} 300"
    )
    time.sleep(1)
    
    # 输入密码
    if password:
        print("  🔑 输入密码...")
        adb_input(f"input text {password}")
        time.sleep(0.5)
        adb_input("input keyevent 66")  # 回车确认
        time.sleep(2)
    
    print("  ✅ 屏幕已唤醒并解锁")
    return True

# ============================================================
# 屏幕亮度控制
# ============================================================
_original_brightness = None
_original_auto_brightness = None
_brightness_mode = None  # 'low' / 'root_zero' / 'root_one'

def get_brightness_settings():
    """获取当前亮度设置"""
    global _original_brightness, _original_auto_brightness
    
    # 获取当前亮度值 (0-255)
    out = adb_shell("settings get system screen_brightness")
    try:
        _original_brightness = int(out.strip())
    except:
        _original_brightness = 128
    
    # 获取自动亮度设置 (0=关闭, 1=开启)
    out = adb_shell("settings get system screen_brightness_mode")
    try:
        _original_auto_brightness = int(out.strip())
    except:
        _original_auto_brightness = 1
    
    print(f"  📊 当前亮度: {_original_brightness}/255, 自动亮度: {'开启' if _original_auto_brightness else '关闭'}")
    return _original_brightness, _original_auto_brightness

def set_brightness_low():
    """关闭自动亮度，将亮度降到最低"""
    print("  🔅 设置最低亮度...")
    # 关闭自动亮度
    adb_shell("settings put system screen_brightness_mode 0")
    # 设置亮度为最低 (1-255, 1为最低)
    adb_shell("settings put system screen_brightness 1")
    print("  ✅ 已关闭自动亮度，亮度设为最低")

def _reapply_low_brightness():
    """重新应用低亮度（杀游戏后调用，防止系统恢复亮度）"""
    if _original_brightness is None:
        return
    if _brightness_mode == 'root_zero':
        adb_shell_root("echo 0 > /sys/class/backlight/panel0-backlight/brightness")
    elif _brightness_mode == 'root_one':
        adb_shell_root("echo 1 > /sys/class/backlight/panel0-backlight/brightness")
    else:
        adb_shell("settings put system screen_brightness 1")

def set_brightness_zero_root():
    """使用ROOT权限将亮度设为0"""
    print("  🔅 使用ROOT权限设置亮度为0")
    # 关闭自动亮度
    adb_shell("settings put system screen_brightness_mode 0")
    # 使用ROOT权限直接写入亮度节点
    result = adb_shell_root("echo 0 > /sys/class/backlight/panel0-backlight/brightness")
    if result and ("Permission denied" in result or "error" in result.lower()):
        print(f"    ⚠️ 写入失败: {result}")
    else:
        # 验证是否写入成功
        verify = adb_shell_root("cat /sys/class/backlight/panel0-backlight/brightness")
        if verify.strip() == "0":
            print("    ✅ 已使用ROOT权限将亮度设为0")
        else:
            print(f"    ⚠️ 验证失败，当前亮度: {verify.strip()}")

def set_brightness_one_root():
    """使用ROOT权限将亮度设为1"""
    print("  🔅 使用ROOT权限设置亮度为1")
    # 关闭自动亮度
    adb_shell("settings put system screen_brightness_mode 0")
    # 使用ROOT权限直接写入亮度节点
    result = adb_shell_root("echo 1 > /sys/class/backlight/panel0-backlight/brightness")
    if result and ("Permission denied" in result or "error" in result.lower()):
        print(f"    ⚠️ 写入失败: {result}")
    else:
        # 验证是否写入成功
        verify = adb_shell_root("cat /sys/class/backlight/panel0-backlight/brightness")
        if verify.strip() == "1":
            print("    ✅ 已使用ROOT权限将亮度设为1")
        else:
            print(f"    ⚠️ 验证失败，当前亮度: {verify.strip()}")

def restore_brightness():
    """恢复原始亮度设置"""
    global _original_brightness, _original_auto_brightness, _brightness_mode
    
    if _original_brightness is None:
        return
    
    print("  🔆 恢复亮度设置...")
    
    # 如果使用了ROOT权限设置亮度，先尝试恢复节点
    if _brightness_mode in ('root_zero', 'root_one'):
        # 尝试恢复亮度节点
        adb_shell_root(f"echo {_original_brightness} > /sys/class/backlight/panel0-backlight/brightness")
        adb_shell_root(f"echo {_original_brightness} > /sys/class/backlight/lcd-backlight/brightness")
    
    # 恢复亮度值
    adb_shell(f"settings put system screen_brightness {_original_brightness}")
    # 恢复自动亮度设置
    adb_shell(f"settings put system screen_brightness_mode {_original_auto_brightness}")
    print(f"  ✅ 已恢复亮度: {_original_brightness}/255, 自动亮度: {'开启' if _original_auto_brightness else '关闭'}")
    
    # 清空全局变量
    _original_brightness = None
    _original_auto_brightness = None
    _brightness_mode = None

_INVALID_CHOICE = object()

def apply_brightness_choice(choice):
    """应用亮度选项；无法识别时返回 _INVALID_CHOICE。"""
    if choice in ('Y', 'YES', 'LOW'):
        get_brightness_settings()
        set_brightness_low()
        return 'low'
    if choice in ('R', 'ROOT', 'ROOT_ZERO'):
        get_brightness_settings()
        set_brightness_zero_root()
        return 'root_zero'
    if choice in ('1', 'ROOT_ONE'):
        get_brightness_settings()
        set_brightness_one_root()
        return 'root_one'
    if choice in ('N', 'NO', 'NONE', 'KEEP'):
        print("  ℹ️ 保持当前亮度设置")
        return None
    return _INVALID_CHOICE

def prompt_brightness_control():
    """选择亮度模式；GUI/无人值守场景用 WZRY_BRIGHTNESS 跳过交互。"""
    env_choice = os.environ.get("WZRY_BRIGHTNESS", "").strip().upper()
    if env_choice:
        mode = apply_brightness_choice(env_choice)
        if mode is not _INVALID_CHOICE:
            print(f"  💡 按 WZRY_BRIGHTNESS={env_choice} 设置亮度（跳过交互）")
            return mode
        print(f"  ⚠️ 无法识别 WZRY_BRIGHTNESS={env_choice}，保持当前亮度")
        return None

    print("\n" + "=" * 60)
    print("💡 是否降低屏幕亮度以减少烧屏风险？")
    print("=" * 60)
    print("  Y - 普通模式，亮度降至最低(1)")
    print("  R - ROOT权限，亮度设为0（屏幕全黑）")
    print("  1 - ROOT权限，亮度设为1（极低亮度）")
    print("  N - 保持当前亮度设置")
    print("=" * 60)

    while True:
        choice = input("请选择 (Y/R/1/N): ").strip().upper()
        mode = apply_brightness_choice(choice)
        if mode is not _INVALID_CHOICE:
            return mode
        print("  ⚠️ 请输入 Y、R、1 或 N")

def cv_imread(path):
    """读取图片；cv2.imread 在 Windows 上无法处理含中文的路径，改用 imdecode。"""
    try:
        data = np.fromfile(str(path), dtype=np.uint8)
    except OSError:
        return None
    if data.size == 0:
        return None
    return cv2.imdecode(data, cv2.IMREAD_COLOR)

def cv_imwrite(path, img):
    """写入图片；与 cv_imread 同理，改用 imencode 规避中文路径问题。"""
    ok, buf = cv2.imencode(Path(path).suffix or ".png", img)
    if ok:
        buf.tofile(str(path))
    return bool(ok)

def screenshot(path=SCREENSHOT_PATH):
    """截图；经 exec-out 流式读取由 Python 落盘。
    不能用 adb pull 写本地文件：platform-tools 35+ 在 Windows 上遇到含中文的
    本地路径会报 cannot create file/directory（且行为不稳定），打包目录名
    「王者农场助手」正好踩中。失败时清除旧图避免误用陈旧画面。"""
    try:
        result = subprocess.run(
            [ADB, "-s", DEVICE, "exec-out", "screencap", "-p"],
            capture_output=True, timeout=30,
        )
    except subprocess.TimeoutExpired:
        print("  ⚠️ 截图超时，跳过本次画面")
        Path(path).unlink(missing_ok=True)
        return path
    data = result.stdout
    if result.returncode != 0 or not data.startswith(b"\x89PNG"):
        err = result.stderr.decode("utf-8", "replace").strip()
        print(f"  ⚠️ 截图失败: {err or '设备无响应'}")
        Path(path).unlink(missing_ok=True)
        return path
    Path(path).write_bytes(data)
    # 自动旋转：竖屏截图 → 横屏
    img = cv_imread(path)
    if img is not None:
        h, w = img.shape[:2]
        if h > w:
            img = cv2.rotate(img, cv2.ROTATE_90_COUNTERCLOCKWISE)
            cv_imwrite(path, img)
    return path

def detect_resolution():
    """检测设备横屏分辨率"""
    import re
    out = adb_shell("wm size")
    match = re.search(r'(\d+)x(\d+)', out)
    if match:
        w, h = int(match.group(1)), int(match.group(2))
        if h > w:
            w, h = h, w
        return w, h
    return 1280, 720

def adb_input(cmd):
    """执行 input 注入并暴露失败原因。
    小米/红米未开启「USB调试（安全设置）」时 input 抛 SecurityException，
    但只写入 stderr，静默吞掉就会表现成"识别到但点不到"。"""
    result = adb_command("shell", cmd, timeout=10)
    noise = f"{result.stderr or ''}\n{result.stdout or ''}".strip()
    if "Exception" in noise or "INJECT_EVENTS" in noise or "permission" in noise.lower():
        print(f"  🚫 输入注入被系统拒绝: {noise.splitlines()[0][:120]}")
        print("     ➜ 小米/红米需在开发者选项开启「USB调试（安全设置）」（需登录小米账号+插SIM卡），开启后重新插拔数据线")
    return result

def tap(x, y, label=""):
    """点击屏幕"""
    if label:
        print(f"  👆 tap ({x}, {y}) [{label}]")
    adb_input(f"input tap {x} {y}")

def swipe(x1, y1, x2, y2, duration_ms=1000):
    """滑动"""
    adb_input(f"input swipe {x1} {y1} {x2} {y2} {duration_ms}")

def random_screen_fiddle():
    """退出游戏前在屏幕上随手乱划乱点几下，打散“收完立刻退出”的固定
    行为特征（防风控）。只在一轮务农成功完成后调用：此时还在农场场景、
    马上就要退出游戏，误触界面没有副作用。

    坐标限制在屏幕中部（横向 30%~70%、纵向 30%~65%），避开左下摇杆
    与四周的功能按钮；次数、位置、滑动时长、间隔全部随机。
    """
    try:
        w, h = detect_resolution()
        x_lo, x_hi = int(w * 0.30), int(w * 0.70)
        y_lo, y_hi = int(h * 0.30), int(h * 0.65)
        actions = random.randint(3, 6)
        print(f"  🖐️ 退出前随机滑动/点击 {actions} 次，模拟真人操作...")
        for _ in range(actions):
            x = random.randint(x_lo, x_hi)
            y = random.randint(y_lo, y_hi)
            if random.random() < 0.5:
                tap(x, y)
            else:
                swipe(x, y, random.randint(x_lo, x_hi), random.randint(y_lo, y_hi),
                      random.randint(200, 800))
            time.sleep(random.uniform(0.4, 1.2))
    except Exception as exc:
        # 尽力而为：随机操作失败不影响本轮结果与后续退出流程
        print(f"  ⚠️ 随机操作失败（忽略）: {exc}")

# ============================================================
# 模板匹配
# ============================================================
_RES_DIR_RE = re.compile(r"^(\d+)x(\d+)$")

def _template_dirs(img_w, img_h):
    """模板搜索路径：精确分辨率 > 其他分辨率目录（高度接近者优先）> 默认目录。"""
    res_dirs = []
    for entry in TEMPLATE_DIR.iterdir():
        match = _RES_DIR_RE.match(entry.name)
        if entry.is_dir() and match:
            res_dirs.append((entry, (int(match.group(1)), int(match.group(2)))))
    res_dirs.sort(key=lambda item: abs(item[1][1] - img_h))
    return res_dirs + [(TEMPLATE_DIR, None)]

def _template_scales(img_w, img_h, src_size):
    """返回有限且可解释的模板尺度，避免把按钮放大到整屏宽。"""
    if src_size is not None:
        # 游戏 UI 按屏幕高度等比缩放，宽度差异只是两侧留边，
        # 因此其他分辨率的模板按高度比例预缩放即可在新设备上复用。
        predicted = img_h / src_size[1]
        return [round(predicted * f, 3) for f in (0.90, 0.95, 1.0, 1.05, 1.10)]

    predicted = min(img_w / BASE_W, img_h / BASE_H)
    scales = {0.75, 1.0, 1.25, 1.5, 2.0}
    scales.update(
        round(predicted * factor, 3)
        for factor in (0.85, 0.925, 1.0, 1.075, 1.15)
    )
    return sorted(scales)

def find_template(template_name, screenshot_path, threshold=None, roi=None):
    """在限定区域内多尺度查找模板（分辨率专用模板优先）。"""
    img = cv_imread(screenshot_path)
    if img is None:
        return None
    
    img_h, img_w = img.shape[:2]
    threshold = TEMPLATE_THRESHOLDS.get(template_name, 0.6) if threshold is None else threshold
    roi = TEMPLATE_ROIS.get(template_name) if roi is None else roi
    if roi:
        x1, y1 = int(roi[0] * img_w), int(roi[1] * img_h)
        x2, y2 = int(roi[2] * img_w), int(roi[3] * img_h)
    else:
        x1, y1, x2, y2 = 0, 0, img_w, img_h
    search_img = img[y1:y2, x1:x2]
    
    best_score = -1
    best_loc = None
    best_tw, best_th = 0, 0

    for tdir, src_size in _template_dirs(img_w, img_h):
        template_path = tdir / template_name
        if not template_path.exists():
            continue

        tmpl = cv_imread(template_path)
        if tmpl is None:
            continue

        tmpl_h, tmpl_w = tmpl.shape[:2]
        scales = _template_scales(img_w, img_h, src_size)

        for s in scales:
            if abs(s - 1.0) < 0.01:
                t = tmpl
                tw, th = tmpl_w, tmpl_h
            else:
                nw, nh = int(tmpl_w * s), int(tmpl_h * s)
                if nw > search_img.shape[1] or nh > search_img.shape[0] or nw < 5 or nh < 5:
                    continue
                t = cv2.resize(tmpl, (nw, nh))
                tw, th = nw, nh
            
            if tw > search_img.shape[1] or th > search_img.shape[0]:
                continue
            result = cv2.matchTemplate(search_img, t, cv2.TM_CCOEFF_NORMED)
            _, max_val, _, max_loc = cv2.minMaxLoc(result)
            if max_val > best_score:
                best_score = max_val
                best_loc = (max_loc[0] + x1, max_loc[1] + y1)
                best_tw, best_th = tw, th

        # 高优先级目录已达标则停止，后面的目录只在未达标时兜底
        if best_score >= threshold:
            break

    if best_loc is None:
        print(f"  ❌ '{template_name}': 模板不存在")
        return None
    
    if best_score >= threshold:
        cx = best_loc[0] + best_tw // 2
        cy = best_loc[1] + best_th // 2
        print(f"  ✅ '{template_name}': score={best_score:.3f} @ ({cx},{cy})")
        return {
            "x": cx, "y": cy, "score": best_score,
            "template": template_name, "scale_size": (best_tw, best_th),
        }
    else:
        print(f"  ❌ '{template_name}': 未匹配 ({best_score:.3f} < {threshold})")
        return None

def has_template(template_name, screenshot_path, threshold=None):
    """检查模板是否存在"""
    return find_template(template_name, screenshot_path, threshold) is not None

def click_template(template_name, screenshot_path, threshold=None, label=""):
    """匹配并点击模板"""
    result = find_template(template_name, screenshot_path, threshold)
    if result:
        tap(result["x"], result["y"], label or template_name)
        return True
    return False

def find_any_template(template_names, screenshot_path, thresholds=None):
    """匹配同一功能的多个模板，返回超过各自阈值的最高分候选。"""
    best = None
    thresholds = thresholds or {}
    for name in template_names:
        result = find_template(name, screenshot_path, thresholds.get(name))
        if result and (best is None or result["score"] > best["score"]):
            best = result
    return best

def wait_for_any_template(template_names, timeout=60, interval=1, label="页面"):
    """轮询等待任一目标出现；成功后返回匹配结果。"""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        screenshot(SCREENSHOT_PATH)
        result = find_any_template(template_names, SCREENSHOT_PATH)
        if result:
            print(f"  ✅ {label}已就绪: {result['template']}")
            return result
        remaining = max(0, int(deadline - time.monotonic()))
        print(f"  ⏳ 等待{label}，剩余 {remaining}秒")
        time.sleep(min(interval, max(0, deadline - time.monotonic())))
    return None

def save_diagnostic(step, details=None):
    """保存失败现场，供游戏更新后离线复现。"""
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    output_dir = SCRIPT_DIR / "diagnostics" / f"{stamp}_{step}"
    output_dir.mkdir(parents=True, exist_ok=True)
    if Path(SCREENSHOT_PATH).exists():
        shutil.copy2(SCREENSHOT_PATH, output_dir / "screenshot.png")
    context = {
        "step": step,
        "time": datetime.now().isoformat(timespec="seconds"),
        "device": DEVICE,
        "details": details or {},
    }
    with open(output_dir / "context.json", "w", encoding="utf-8") as file:
        json.dump(context, file, ensure_ascii=False, indent=2)
    print(f"  📁 已保存失败现场: {output_dir}")
    return output_dir

# ============================================================
# 摇杆操作
# ============================================================
def move_joystick(angle_deg, distance=200, duration_ms=1500, center=None):
    """向指定角度推动摇杆"""
    cx, cy = center or JOYSTICK_CENTER
    angle_rad = math.radians(angle_deg)
    tx = int(cx + distance * math.cos(angle_rad))
    ty = int(cy - distance * math.sin(angle_rad))
    swipe(cx, cy, tx, ty, duration_ms)
    print(f"  🎮 摇杆 ({cx},{cy})→({tx},{ty}) {angle_deg}° {duration_ms}ms")

def check_at_initial_position():
    """检查角色是否在初始位置（石盘）"""
    screenshot(SCREENSHOT_PATH)
    # 检查是否有refresh_pos按钮（在初始位置才会显示）
    return has_template("refresh_pos.png", SCREENSHOT_PATH)

def reset_position():
    """刷新站位重置角色位置"""
    print("  🔄 刷新站位...")
    if click_template("refresh_pos.png", SCREENSHOT_PATH, label="刷新站位"):
        print("  ⏳ 等待3秒...")
        time.sleep(3)
        return True
    return False

# ============================================================
# OCR 成熟时间
# ============================================================
_ocr_engine = None

def get_ocr():
    global _ocr_engine
    if _ocr_engine is None:
        from rapidocr_onnxruntime import RapidOCR
        _ocr_engine = RapidOCR()
    return _ocr_engine

def parse_relative_maturity(text):
    """解析"X小时Y分钟后成熟"类相对时间，返回剩余分钟数；不匹配返回 None。"""
    import re
    match = re.search(r'(?:(\d+)\s*小时)?\s*(?:(\d+)\s*分钟?)?\s*后成熟', text)
    if match and (match.group(1) or match.group(2)):
        hours = int(match.group(1) or 0)
        minutes = int(match.group(2) or 0)
        return hours * 60 + minutes
    if re.search(r'\d+\s*秒后成熟', text):
        return 1  # 秒级剩余按1分钟处理
    return None

def read_maturity_time(screenshot_path):
    """从截图中读取成熟时间
    支持绝对时间（"18:25成熟"）与相对时间（"17分钟后成熟"）两种界面格式。
    返回: (maturity_dt, is_mature)
        - 识别到时间: (datetime, False)，完整日期时间，跨天信息不丢失
        - 作物已成熟可收获: (None, True)
        - 无法识别: (None, False)
    """
    import re

    img = cv_imread(screenshot_path)
    if img is None:
        return None, False

    # 裁剪成熟时间区域（左侧面板，覆盖"xx:xx成熟"和"可收获"区域）
    h, w = img.shape[:2]
    roi = img[h//3:2*h//3, 0:w//3]

    ocr = get_ocr()
    result, _ = ocr(roi)

    if result:
        all_text = " ".join([line[1] for line in result])
        print(f"  📝 OCR文本: {all_text}")
        
        # 检查是否显示"可收获"或"已成熟"（作物已经成熟）
        if "可收获" in all_text or "已成熟" in all_text:
            return None, True

        # 相对时间格式（如"17分钟后成熟"、"1小时30分钟后成熟"）
        rel_min = parse_relative_maturity(all_text)
        if rel_min is not None:
            if rel_min <= 0:
                return None, True
            target = datetime.now() + timedelta(minutes=rel_min)
            print(f"  🕐 OCR识别: {rel_min}分钟后成熟 → {target.strftime('%m-%d %H:%M')}")
            return target, False

        # 匹配成熟时间（如 "18:25成熟"、"明天00：02成熟"，兼容全角冒号）
        for line in result:
            text = line[1]
            time_match = re.search(r'(\d{1,2})[:\uff1a](\d{2})', text)
            if time_match:
                hour = int(time_match.group(1))
                minute = int(time_match.group(2))
                if hour >= 24 or minute >= 60:
                    continue
                now = datetime.now()
                target = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
                # 带"明天"前缀或时刻已过均按次日处理
                if "明天" in text or target <= now:
                    target += timedelta(days=1)
                print(f"  🕐 OCR识别: {target.strftime('%m-%d %H:%M')}成熟")
                return target, False

    return None, False

def parse_harvest_exp(all_text):
    """从OCR文本解析经验值；支持整数与 "27.50万" 这类小数+万单位格式。"""
    import re
    patterns = (
        r'XP\s*(\d+(?:\.\d+)?)(万?)',
        r'(\d+(?:\.\d+)?)(万?)\s*XP',
        r'(\d+(?:\.\d+)?)(万?)\s*农场[经経]验',
        r'[+＋](\d+(?:\.\d+)?)(万?)\s*[经経]验',
        r'[经経]验\s*[+＋](\d+(?:\.\d+)?)(万?)',
    )
    for pattern in patterns:
        match = re.search(pattern, all_text)
        if match:
            value = float(match.group(1))
            if match.group(2):
                value *= 10000
            return int(value)
    return 0

def read_harvest_info(screenshot_path):
    """从收获弹窗截图中OCR识别收获信息
    返回: {"exp": int, "crops": {作物名: 数量}} 或 None
    """
    import re

    img = cv_imread(screenshot_path)
    if img is None:
        return None
    
    h, w = img.shape[:2]
    # 覆盖多行奖励卡片；旧范围会截掉第二行的作物名称。
    roi = img[int(h*0.15):int(h*0.92), int(w*0.15):int(w*0.85)]
    
    try:
        ocr = get_ocr()
        result, _ = ocr(roi)
    except Exception as e:
        print(f"  ⚠️ OCR失败: {e}")
        return None
    
    if not result:
        return None
    
    all_text = " ".join([line[1] for line in result])
    print(f"  📝 OCR文本: {all_text}")
    
    harvest = {"exp": 0, "crops": {}}
    
    # 识别经验
    harvest["exp"] = parse_harvest_exp(all_text)
    
    # 识别作物名和数量
    # OCR输出格式: 作物名和数字可能在不同行，按x坐标排序配对
    # 作物名按"1-4个汉字且非界面固定词"识别，避免每出新作物就要维护清单
    exclude_words = {"农场经验", "点击继续", "恭喜您获得", "已成熟", "可收获"}
    
    # 提取所有文字及其位置
    items = []
    for line in result:
        text = line[1]
        x = sum(point[0] for point in line[0]) / 4
        y = sum(point[1] for point in line[0]) / 4
        items.append({"text": text, "x": x, "y": y})
    
    # 按y坐标排序，找数字行和作物名行的配对
    numbers = []  # [{"x", "y", "value", "used"}]
    crops_found = []  # [{"x", "y", "name"}]
    
    for item in items:
        text = item["text"].strip()
        if re.fullmatch(r'[一-鿿]{1,4}', text) and text not in exclude_words:
            crops_found.append({"x": item["x"], "y": item["y"], "name": text})
        elif text.isdigit() and int(text) > 0:
            numbers.append({
                "x": item["x"], "y": item["y"],
                "value": int(text), "used": False,
            })

    # 每张奖励卡片的数量位于作物名上方。二维配对且每个数字只能使用一次。
    for crop in sorted(crops_found, key=lambda item: item["y"]):
        candidates = []
        for number in numbers:
            dy = crop["y"] - number["y"]
            dx = abs(crop["x"] - number["x"])
            if not number["used"] and 20 <= dy <= 180 and dx <= 140:
                candidates.append((dx + dy * 0.15, number))
        if not candidates:
            continue
        _, best = min(candidates, key=lambda pair: pair[0])
        best["used"] = True
        name = crop["name"]
        harvest["crops"][name] = harvest["crops"].get(name, 0) + best["value"]
    
    if harvest["exp"] > 0 or harvest["crops"]:
        return harvest
    return None

# ============================================================
# 步骤1: 检测状态
# ============================================================
def step1_check_status():
    """步骤1: 检测APP是否在前台"""
    print("\n[步骤1] 检测状态...")
    
    found = False
    found2 = False
    found3 = False
    for attempt in range(3):
        # 方法1: 精确匹配ResumedActivity行（不在历史/缓存中误匹配）
        out = adb_shell("dumpsys activity activities")
        found = False
        for line in out.splitlines():
            stripped = line.strip()
            if ("ResumedActivity" in stripped or "topResumedActivity" in stripped) and GAME_PKG in stripped:
                found = True
                break
        if found:
            break
        # 方法2: mCurrentFocus
        out2 = adb_shell("dumpsys window")
        found2 = False
        for line in out2.splitlines():
            stripped = line.strip()
            if ("mCurrentFocus" in stripped or "mFocusedApp" in stripped) and GAME_PKG in stripped:
                found2 = True
                break
        if found2:
            break
        # 方法3: top activity
        out3 = adb_shell("dumpsys activity top")
        found3 = False
        for line in out3.splitlines():
            stripped = line.strip()
            if "ACTIVITY" in stripped and GAME_PKG in stripped:
                found3 = True
                break
        if found3:
            break
        # dumpsys 有正常输出且三种方式均未命中，说明游戏确实不在前台。
        if out.strip() or out2.strip() or out3.strip():
            break
        print(f"  ⚠️ 检测失败，重试 {attempt+1}/3...")
        time.sleep(2)
    
    in_foreground = found or found2 or found3
    if in_foreground:
        print("  🎮 王者荣耀在前台，退出...")
        adb_shell(f"am force-stop {GAME_PKG}")
        print("  ⏳ 等待2秒...")
        time.sleep(2)
        return True
    else:
        print(f"  ✅ 王者荣耀不在前台")
        return False

# ============================================================
# 步骤2: 启动游戏
# ============================================================
def step2_launch_game():
    """步骤2: 启动游戏"""
    print("\n[步骤2] 启动游戏...")
    adb_shell(f"am start -n {GAME_ACT}")
    print("  ⏳ 等待登录页或启动弹窗...")
    return wait_for_any_template(
        ["start_game.png", "close_popup.png", "close_popup_event.png",
         "agree_terms.png"],
        timeout=60, interval=1, label="游戏启动页",
    ) is not None


# ============================================================
# 步骤2b: 关闭启动弹窗
# ============================================================
def step2b_close_startup_popups():
    """步骤2b: 启动后关闭弹窗（逻辑与步骤4一致）"""
    print("\n[步骤2b] 关闭启动弹窗...")

    miss_count = 0
    for i in range(10):  # 最多处理10个弹窗
        screenshot(SCREENSHOT_PATH)
        result = find_any_template(
            ["close_popup.png", "close_popup_event.png", "agree_terms.png"],
            SCREENSHOT_PATH,
        )

        if result:
            x, y = result["x"], result["y"]
            action = "同意协议" if result["template"] == "agree_terms.png" else "关闭启动弹窗"
            tap(x, y, f"{action}/{result['template']}")
            miss_count = 0
            print("  ⏳ 等待2秒...")
            time.sleep(2)
        else:
            if find_template("start_game.png", SCREENSHOT_PATH):
                print("  ✅ 已到登录页，无需继续处理启动弹窗")
                break
            miss_count += 1
            print(f"  ⚠️ 未找到弹窗，未匹配 {miss_count}/3")

        # 连续3次未匹配，认为弹窗全部关闭
        if miss_count >= 3:
            print("  ✅ 启动弹窗处理完毕（连续3次未匹配）")
            break

    return True


# ============================================================
# 步骤3: 点击开始游戏
# ============================================================
def step3_click_start_game():
    """步骤3: 点击开始游戏"""
    print("\n[步骤3] 点击开始游戏...")
    
    for attempt in range(5):
        print(f"  尝试 {attempt+1}/5...")
        screenshot(SCREENSHOT_PATH)
        
        if click_template("start_game.png", SCREENSHOT_PATH, label="开始游戏"):
            print("  ⏳ 等待大厅...")
            deadline = time.monotonic() + 90
            while time.monotonic() < deadline:
                screenshot(SCREENSHOT_PATH)
                if find_any_template(
                    ["close_popup.png", "close_popup_event.png", "lainongchang.png"],
                    SCREENSHOT_PATH,
                ):
                    print("  ✅ 大厅已就绪")
                    return True
                # 登录后可能盖全屏活动页（如回归福利，无 ✕ 只有返回），点返回退出
                if click_template("back_arrow.png", SCREENSHOT_PATH, label="退出活动页"):
                    time.sleep(2)
                    continue
                remaining = max(0, int(deadline - time.monotonic()))
                print(f"  ⏳ 等待大厅，剩余 {remaining}秒")
                time.sleep(1)
            save_diagnostic("step3_lobby_timeout")
            return False

        # 协议条款更新后首启弹「拒绝/同意」确认框（无 ✕），点掉后登录页才出现
        if click_template("agree_terms.png", SCREENSHOT_PATH, label="同意协议"):
            print("  ⏳ 已同意协议，等待2秒...")
            time.sleep(2)
            continue

        if attempt < 4:
            print("  ⏳ 等待2秒...")
            time.sleep(2)
    
    print("  ❌ 连续5次失败，返回步骤1")
    save_diagnostic("step3_start_game")
    return False

# ============================================================
# 步骤4: 关闭弹窗
# ============================================================
def step4_close_popup():
    """步骤4: 关闭弹窗（多个弹窗依次关闭）"""
    print("\n[步骤4] 关闭弹窗...")

    miss_count = 0
    for i in range(10):  # 最多处理10个弹窗
        screenshot(SCREENSHOT_PATH)
        result = find_any_template(
            ["close_popup.png", "close_popup_event.png"], SCREENSHOT_PATH
        )

        if result:
            x, y = result["x"], result["y"]
            tap(x, y, f"关闭弹窗/{result['template']}")
            miss_count = 0
            print("  ⏳ 等待2秒...")
            time.sleep(2)
        else:
            if find_template("lainongchang.png", SCREENSHOT_PATH):
                print("  ✅ 已进入大厅主页，弹窗处理完毕")
                break
            miss_count += 1
            print(f"  ⚠️ 未找到弹窗，未匹配 {miss_count}/3")

        # 连续3次未匹配，认为弹窗全部关闭
        if miss_count >= 3:
            print("  ✅ 弹窗处理完毕（连续3次未匹配）")
            break

    return True

# ============================================================
# 步骤5: 进入农场
# ============================================================
def step5_enter_farm():
    """步骤5: 进入农场"""
    print("\n[步骤5] 进入农场...")

    for attempt in range(10):  # 最多尝试10次
        screenshot(SCREENSHOT_PATH)

        if click_template("lainongchang.png", SCREENSHOT_PATH, label="进入农场"):
            print("  ⏳ 等待农场加载...")
            if wait_for_any_template(
                ["refresh_pos.png"], timeout=60, interval=1, label="农场"
            ):
                return True
            save_diagnostic("step5_farm_timeout")
            return False

        print(f"  ⏳ 等待2秒... ({attempt+1}/10)")
        time.sleep(2)

    print("  ❌ 连续10次未找到进入农场按钮")
    save_diagnostic("step5_enter_farm")
    return False

# ============================================================
# 步骤6: 移动到雕像
# ============================================================
def step6_move_to_statue():
    """步骤6: 移动到雕像"""
    print("\n[步骤6] 移动到雕像...")
    
    # 检查是否在初始位置
    if check_at_initial_position():
        print("  ✅ 在初始位置")
    else:
        print("  🔄 不在初始位置，刷新站位...")
        reset_position()
    
    # 使用分辨率专属配置或默认参数
    cfg = _step6_cfg
    move_joystick(cfg["angle"], cfg["distance"], cfg["duration"], center=cfg["center"])
    print(f"  ⏳ 等待移动...")
    time.sleep(2)
    return True

# ============================================================
# 步骤7: 一键务农
# ============================================================
def step7_oneclick_farm():
    """步骤7: 一键务农，返回 (是否成功, 实际点击时间)。"""
    print("\n[步骤7] 一键务农...")
    
    screenshot(SCREENSHOT_PATH)
    
    if has_template("oneclick_farm.png", SCREENSHOT_PATH):
        print("  ✅ 找到一键务农按钮")
        print("  ⏳ 等待1秒...")
        time.sleep(1)

        if not click_template("oneclick_farm.png", SCREENSHOT_PATH, label="一键务农"):
            return False, None
        farm_time = datetime.now()
        print(f"  🕐 一键务农时间: {farm_time.strftime('%H:%M:%S')}")
        print("  ⏳ 等待2秒...")
        time.sleep(2)
        return True, farm_time
    else:
        print("  ❌ 未找到一键务农，返回步骤6")
        save_diagnostic("step7_oneclick")
        return False, None

# ============================================================
# 步骤8: 关闭收获弹窗
# ============================================================
def step8_close_harvest():
    """步骤8: 关闭收获弹窗，返回 (success, harvested)"""
    print("\n[步骤8] 关闭收获弹窗...")
    harvested = False
    
    for attempt in range(2):
        screenshot(SCREENSHOT_PATH)

        if has_template("harvest_continue.png", SCREENSHOT_PATH):
            print("  ✅ 找到收获弹窗")
            harvested = True
            
            # OCR识别收获信息
            harvest_info = read_harvest_info(SCREENSHOT_PATH)
            if harvest_info:
                exp = harvest_info["exp"]
                crops = harvest_info["crops"]
                detail = []
                if exp > 0:
                    detail.append(f"经验+{exp}")
                for cname, ccount in crops.items():
                    detail.append(f"{cname}×{ccount}")
                print(f"  🎉 收获: {' '.join(detail)}")
                stats.add_harvest(exp=exp, crops=crops)
            else:
                stats.add_harvest()
            
            print("  ⏳ 等待1秒...")
            time.sleep(1)

            click_template("harvest_continue.png", SCREENSHOT_PATH, label="继续")
            print("  ⏳ 等待2秒...")
            time.sleep(2)
            return True, harvested
        else:
            if attempt == 0:
                print("  ⚠️ 未找到收获弹窗，等待2秒后重试 (1/2)")
                time.sleep(2)

    print("  ⚠️ 连续2次未找到收获弹窗，进入步骤9")
    return False, False

# ============================================================
# 步骤9: 移动到土地
# ============================================================
def step9_move_to_farmland():
    """步骤9: 移动到土地，读取成熟时间，计算下次浇水时间

    :return: (result, maturity_dt, is_mature)
    """
    print("\n[步骤9] 移动到土地...")

    # 向上方移动
    move_joystick(90, 200, 1200)  # 90度是正上方
    print("  ⏳ 等待移动...")
    time.sleep(2)

    # 截图OCR；识别失败原地快速重试，避免直接落入步骤10的5分钟重试惩罚
    maturity_dt, is_mature = None, False
    for attempt in range(3):
        screenshot(SCREENSHOT_PATH)
        maturity_dt, is_mature = read_maturity_time(SCREENSHOT_PATH)
        if maturity_dt or is_mature:
            break
        if attempt < 2:
            print(f"  ⚠️ 未识别到成熟时间，2秒后重试 ({attempt + 1}/3)")
            time.sleep(2)

    # 如果作物已成熟（可收获），不需要等待
    if is_mature:
        print("  🌾 作物已成熟，无需等待")
        return None, None, True

    # 计算下次浇水时间
    result = None
    if maturity_dt:
        result = calculate_next_water_time(maturity_dt)

    return result, maturity_dt, False

# ============================================================
# 步骤10: 计算等待时间
# ============================================================
def step10_calculate_wait(result, maturity_dt, is_mature=False):
    """步骤10: 根据成熟时间和浇水时间计算等待时间

    :param result: 浇水计算结果 dict
    :param maturity_dt: 成熟时间 (datetime)
    :param is_mature: 作物已成熟可收获（与"识别失败"明确区分）
    :return: wake_time (下次唤醒时间)
    """
    print("\n[步骤10] 计算等待时间...")

    # 作物已成熟可收获，直接重启收割；识别失败则走下方5分钟重试分支
    if is_mature:
        print("  🌾 作物已成熟，退出游戏重新进入收割...")
        random_screen_fiddle()
        adb_shell(f"am force-stop {GAME_PKG}")
        _reapply_low_brightness()
        stats.set_next_wake(datetime.now(), None, "作物已成熟，立即收割")
        time.sleep(1)
        return None
    
    # 先随机划拉几下再杀掉游戏
    random_screen_fiddle()
    print("  🛑 退出王者荣耀...")
    adb_shell(f"am force-stop {GAME_PKG}")
    _reapply_low_brightness()
    
    now = datetime.now()
    
    # 确定唤醒时间
    wake_time = None
    reason = ""
    
    if result and maturity_dt:
        # 获取下次浇水时间（None 表示已过最后节点，等成熟）
        next_watering = result.get("next_water")
        
        if next_watering and next_watering < maturity_dt:
            wake_time = next_watering
            reason = "浇水"
            print(f"  💧 下次浇水时间: {next_watering.strftime('%m-%d %H:%M:%S')} (早于成熟时间)")
        else:
            wake_time = maturity_dt
            reason = "成熟"
            print(f"  🌾 成熟时间: {maturity_dt.strftime('%m-%d %H:%M:%S')} (早于浇水时间)")
    elif maturity_dt:
        wake_time = maturity_dt
        reason = "成熟"
        print(f"  🌾 成熟时间: {maturity_dt.strftime('%m-%d %H:%M:%S')}")
    else:
        print("  ⚠️ 无法识别时间，5分钟后重试...")
        retry_time = now + timedelta(minutes=5)
        stats.set_next_wake(retry_time, None, "识别失败重试")
        return retry_time
    
    # 提前2分钟唤醒
    wake_time -= timedelta(minutes=2)
    
    if wake_time <= now:
        print(f"  ⚠️ {reason}时间已到，立即重新启动")
        stats.set_next_wake(now, wake_time + timedelta(minutes=2), reason)
        return now
    
    wait_seconds = int((wake_time - now).total_seconds())
    hours = wait_seconds // 3600
    minutes = (wait_seconds % 3600) // 60
    seconds = wait_seconds % 60
    
    print(f"  🎯 目标时间: {(wake_time + timedelta(minutes=2)).strftime('%H:%M:%S')} ({reason})")
    print(f"  🔔 提前2分钟唤醒: {wake_time.strftime('%m-%d %H:%M:%S')}")
    print(f"  ⏳ 等待 {hours}小时{minutes}分{seconds:02d}秒")
    stats.set_next_wake(wake_time, wake_time + timedelta(minutes=2), reason)
    return wake_time
# ============================================================
# 主流程
# ============================================================
def is_wireless_device(serial=None):
    """网络设备地址形如 ip:port；USB 序列号不含冒号。"""
    return ":" in (serial if serial is not None else DEVICE)

def adb_host_command(*args, timeout=15):
    """执行不针对具体设备的 adb 主机命令（connect/disconnect 等）。"""
    return subprocess.run(
        [ADB, *map(str, args)],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        timeout=timeout,
    )

def _device_state_ok():
    try:
        result = adb_command("get-state")
        return result.returncode == 0 and result.stdout.strip() == "device"
    except (OSError, subprocess.TimeoutExpired):
        return False

def ensure_device_connected(max_attempts=6, retry_interval=10):
    """确认设备在线；无线设备掉线时自动 adb connect 重连。

    作物成熟等待动辄数小时，手机省电或路由器常把空闲的 WiFi ADB 断开，
    因此每轮开始前都要调用，尽力恢复连接而不是让整轮流程失败。
    """
    for attempt in range(1, max_attempts + 1):
        if _device_state_ok():
            if attempt > 1:
                print(f"  ✅ 设备重连成功: {DEVICE}")
            return True
        if not is_wireless_device():
            print(f"  ❌ USB 设备离线: {DEVICE}，请检查数据线连接")
            return False
        print(f"  🔄 无线设备离线，尝试重连 ({attempt}/{max_attempts}): {DEVICE}")
        try:
            # 先断开残留的半死连接，否则 connect 可能直接返回 already connected
            adb_host_command("disconnect", DEVICE)
            out = adb_host_command("connect", DEVICE).stdout
        except (OSError, subprocess.TimeoutExpired):
            out = ""
        if "connected to" in out and _device_state_ok():
            print(f"  ✅ 设备重连成功: {DEVICE}")
            return True
        if attempt < max_attempts:
            time.sleep(retry_interval)
    print(f"  ❌ 无法恢复设备连接: {DEVICE}")
    return False

def check_adb_connection():
    """启动前检查ADB连接状态，未连接则自动连接（无线设备带重试）。"""
    print("🔍 检查ADB连接...")
    if not DEVICE:
        print("  ❌ 未选择设备")
        return False
    if ensure_device_connected(max_attempts=3, retry_interval=5):
        print(f"  ✅ 设备已连接: {DEVICE}")
        return True
    return False

def resolve_device():
    """优先使用环境变量；否则单设备自动选择，多设备要求显式指定。"""
    global DEVICE
    if DEVICE:
        return True
    try:
        result = subprocess.run(
            [ADB, "devices"], capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=10,
        )
        devices = []
        for line in result.stdout.splitlines()[1:]:
            fields = line.split()
            if len(fields) >= 2 and fields[1] == "device":
                devices.append(fields[0])
        if len(devices) == 1:
            DEVICE = devices[0]
            print(f"  📱 自动选择唯一设备: {DEVICE}")
            return True
        if len(devices) > 1:
            print(f"  ❌ 检测到多个设备: {', '.join(devices)}")
            print("  请设置 WZRY_DEVICE 指定目标设备")
            return False
    except Exception as exc:
        print(f"  ⚠️ 枚举ADB设备失败: {exc}")

    if DEFAULT_DEVICE:
        DEVICE = DEFAULT_DEVICE
        print(f"  📱 未发现在线设备，尝试默认无线设备: {DEVICE}")
        return True
    print("  ❌ 未发现在线设备，请先连接 ADB 或设置 WZRY_DEFAULT_DEVICE")
    return False

_instance_lock = None

def acquire_instance_lock():
    """端口绑定式单实例锁：原子、进程退出自动释放、按设备区分。

    同一设备只允许一个脚本实例；多设备并行时各实例的 WZRY_DEVICE
    不同，锁端口互不冲突。可用 WZRY_LOCK_PORT 显式指定。
    """
    global _instance_lock
    import zlib
    port = int(os.environ.get("WZRY_LOCK_PORT", "0"))
    if not port:
        port = 46000 + zlib.crc32((DEVICE or "default").encode("utf-8")) % 1000
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.bind(("127.0.0.1", port))
        sock.listen(1)
    except OSError:
        sock.close()
        print(f"  ❌ 本设备已有脚本实例在运行（锁端口 {port} 被占用），本实例退出")
        print("     如需多设备并行，请为每个实例设置不同的 WZRY_DEVICE")
        return False
    _instance_lock = sock
    return True

def start_stats_server():
    """后台线程启动本地统计面板；失败不影响挂机。"""
    try:
        sys.path.insert(0, str(SCRIPT_DIR / "scripts"))
        import stats_server
        port = int(os.environ.get("WZRY_STATS_PORT", "8765"))
        stats_server.start_in_background(STATS_FILE, port)
        print(f"  📊 统计面板: http://localhost:{port}")
    except Exception as exc:
        print(f"  ⚠️ 统计面板启动失败（不影响挂机）: {exc}")

def main():
    """主流程"""
    global JOYSTICK_CENTER, _step6_cfg
    print("=" * 60)
    print("王者荣耀农场自动化务农 v3")
    print("=" * 60)
    # 显示支持的分辨率及参数
    for (w, h), cfg in STEP6_CONFIG.items():
        print(f"  📐 {w}x{h}: 中心{cfg['center']} {cfg['angle']}° {cfg['distance']}px {cfg['duration']}ms")

    # 本地统计面板（先恢复历史统计，实现跨会话累计）
    stats.load()
    stats.save()
    start_stats_server()

    # 启动前选择并检查ADB连接
    if not resolve_device() or not check_adb_connection():
        return

    # 单实例锁：防止两个脚本同时操作同一台设备
    if not acquire_instance_lock():
        return
    
    # 询问是否降低亮度
    global _brightness_mode
    _brightness_mode = prompt_brightness_control()
    
    # 检测设备分辨率
    dev_w, dev_h = detect_resolution()
    print(f"  📐 分辨率 {dev_w}x{dev_h}")
    
    # 加载分辨率专属步骤6配置
    res_key = (dev_w, dev_h)
    if res_key in STEP6_CONFIG:
        _step6_cfg = STEP6_CONFIG[res_key].copy()
        JOYSTICK_CENTER = _step6_cfg["center"]
        print(f"  🎯 步骤6使用专属配置: 中心{JOYSTICK_CENTER} 角度{_step6_cfg['angle']}° 距离{_step6_cfg['distance']}px 时间{_step6_cfg['duration']}ms")
    else:
        if dev_w != BASE_W or dev_h != BASE_H:
            sx = dev_w / BASE_W
            sy = dev_h / BASE_H
            JOYSTICK_CENTER = (int(160 * sx), int(486 * sy))
            move_scale = min(sx, sy)
        else:
            move_scale = 1.0
        _step6_cfg = {
            "center": JOYSTICK_CENTER,
            "angle": 120,
            "distance": max(100, int(200 * move_scale)),
            "duration": 1500,
        }
        print(f"  🎯 步骤6使用缩放配置: {_step6_cfg}")
    
    round_num = stats.rounds  # 接续历史轮次编号
    while True:
        # 作废竞态窗口（等待刚结束、新一轮将启）漏进来的立刻务农指令，
        # 否则它会原样打断下一次等待、多跑一轮
        _farm_now.clear()
        round_num += 1
        stats.begin_round(round_num)
        print(f"\n{'='*60}")
        print(f"# 第 {round_num} 轮务农")
        print(f"{'='*60}")
        
        try:
            # 长等待后无线 ADB 常被省电策略断开，先确保连接可用再操作。
            # 设备不在线（如带手机外出）只记一轮失败，之后原地每5分钟重试，
            # 恢复后自动继续挂机，不把离线期间刷成一长串失败轮次。
            if not ensure_device_connected():
                stats.finish_round("失败：设备离线")
                print("  ⏳ 设备离线，每30秒重试，恢复连接后自动继续...")
                while True:
                    retry_at = datetime.now() + timedelta(seconds=30)
                    stats.set_next_wake(retry_at, retry_at, "设备离线，等待重连")
                    wait_or_farm_now(30)
                    if ensure_device_connected(max_attempts=1):
                        print("  ✅ 设备恢复连接，继续挂机")
                        break
                continue

            # 唤醒屏幕并解锁（首轮启动或长等待后手机可能处于息屏状态）
            wake_and_unlock(UNLOCK_PWD)

            # 步骤1: 检测状态
            step1_check_status()
        
            # 步骤2: 启动游戏
            if not step2_launch_game():
                print("\n⚠️ 游戏启动页超时，重新开始...")
                stats.finish_round("失败：游戏启动页超时")
                save_diagnostic("step2_launch")
                force_stop_game()
                wait_or_farm_now(30)
                continue

            # 步骤2b: 关闭启动弹窗
            step2b_close_startup_popups()

            # 步骤3: 点击开始游戏
            if not step3_click_start_game():
                print("\n⚠️ 步骤3失败，重新开始...")
                stats.finish_round("失败：未能点击开始游戏")
                force_stop_game()
                continue
        
            # 步骤4: 关闭弹窗
            step4_close_popup()
        
            # 步骤5: 进入农场
            if not step5_enter_farm():
                print("\n⚠️ 步骤5失败，重新开始...")
                stats.finish_round("失败：未能进入农场")
                force_stop_game()
                continue
        
            # 步骤6: 移动到雕像
            step6_move_to_statue()
        
            # 步骤7: 一键务农
            farm_ok, _ = step7_oneclick_farm()
            if not farm_ok:
                print("\n⚠️ 步骤7失败，返回步骤6...")
                step6_move_to_statue()
                farm_ok, _ = step7_oneclick_farm()
            if not farm_ok:
                print("\n❌ 步骤7连续失败，本轮结束")
                stats.finish_round("失败：未找到一键务农")
                force_stop_game()
                wait_or_farm_now(30)
                continue
        
            # 步骤8: 关闭收获弹窗
            step8_close_harvest()

            # 步骤9: 移动到土地，读取成熟时间，计算下次浇水时间
            result, maturity_dt, is_mature = step9_move_to_farmland()

            # 步骤10: 计算等待时间，返回唤醒时间
            wake_time = step10_calculate_wait(result, maturity_dt, is_mature)
        
            if wake_time is None:
                # 作物已成熟，立即重新开始
                stats.finish_round("完成：作物已成熟，立即收割")
                continue

            stats.finish_round("完成")
            # 等待到唤醒时间
            now = datetime.now()
            if wake_time > now:
                wait_seconds = int((wake_time - now).total_seconds())
                if wait_seconds > SCREEN_OFF_WAIT_SECONDS:
                    print("  🌙 等待较长，熄灭手机屏幕")
                    adb_input("input keyevent KEYCODE_SLEEP")
                print(f"\n⏳ 等待到 {wake_time.strftime('%m-%d %H:%M:%S')} 唤醒...")
                wait_or_farm_now(wait_seconds)
            # 醒屏和解锁在下一轮循环开头统一处理
        except subprocess.TimeoutExpired as exc:
            # 设备偶发无响应（USB抖动、无线掉线、高负载卡顿）不应终止挂机，
            # 放弃本轮稍后重试；无线掉线常表现为命令超时，先尝试重连。
            print(f"\n⚠️ ADB 命令超时，放弃本轮，30秒后重试: {exc}")
            stats.finish_round("失败：ADB命令超时")
            ensure_device_connected(max_attempts=3, retry_interval=10)
            force_stop_game()
            wait_or_farm_now(30)

def _start_gui_stop_watcher():
    """GUI 模式（WZRY_GUI=1）：监听 stdin。收到 stop 或管道断开时优雅退出；
    收到 farm_now 且正处于轮次间等待时置位 _farm_now 马上开始新一轮，
    务农步骤执行中则驳回（避免把指令攒到本轮结束后多跑一轮）。

    图形助手（wzry_gui.py）以管道接管本进程 stdin。即使助手被强制结束，
    管道 EOF 也会触发这里的退出流程，避免留下无人管理的挂机进程。
    raise_signal(SIGINT) 经 C 层处理器唤醒主线程（包括长 time.sleep），
    走既有的 signal_handler → 清理 → 恢复亮度路径；
    Windows + Python 3.10 下 _thread.interrupt_main 无法唤醒 sleep，不要换回。
    """
    if os.environ.get("WZRY_GUI") != "1":
        return

    def watch():
        try:
            for line in sys.stdin:
                cmd = line.strip().lower()
                if cmd == "stop":
                    break
                if cmd == "farm_now":
                    if _idle_wait.is_set():
                        print("\n⚡ 收到立刻务农指令，马上开始新一轮")
                        _farm_now.set()
                    else:
                        print("\n⚠️ 正在执行务农流程，立刻务农已驳回（等待下一轮期间才可用）")
        except (OSError, ValueError):
            pass
        print("\n⚠️ 收到助手停止指令（或助手已关闭），正在退出...")
        signal.raise_signal(signal.SIGINT)

    threading.Thread(target=watch, daemon=True, name="gui-stop-watcher").start()

def run_main():
    """运行主流程，确保退出时恢复亮度"""
    _start_gui_stop_watcher()
    try:
        main()
    except KeyboardInterrupt:
        print("\n\n⚠️ 用户中断 (Ctrl+C)")
    except Exception as e:
        print(f"\n\n❌ 发生错误: {e}")
    finally:
        force_stop_game()
        # 恢复亮度设置
        restore_brightness()
        print("\n👋 脚本已退出")

def run_selftest():
    """打包/环境自检：验证依赖、模板、OCR 与 adb 可用（不连设备、不动游戏）。"""
    import traceback
    failures = []
    print("=" * 60)
    print("王者农场助手 自检")
    print(f"  Python {sys.version.split()[0]}  frozen={IS_FROZEN}")
    print(f"  程序目录: {SCRIPT_DIR}")
    print(f"  OpenCV {cv2.__version__} / NumPy {np.__version__}")

    templates = sorted(TEMPLATE_DIR.rglob("*.png")) if TEMPLATE_DIR.exists() else []
    print(f"  模板目录: {TEMPLATE_DIR}（{len(templates)} 张）")
    if not templates:
        failures.append(f"模板缺失: {TEMPLATE_DIR}")
    else:
        sample = cv_imread(templates[0])
        if sample is None:
            failures.append(f"模板无法解码: {templates[0]}")
        else:
            print(f"  模板解码正常: {templates[0].name} {sample.shape[1]}x{sample.shape[0]}")

    try:
        canvas = np.full((64, 200, 3), 255, dtype=np.uint8)
        cv2.putText(canvas, "12:34", (10, 44), cv2.FONT_HERSHEY_SIMPLEX, 1.2, (0, 0, 0), 2)
        get_ocr()(canvas)
        print("  OCR 引擎初始化并推理成功")
    except Exception as exc:
        failures.append(f"OCR 引擎异常: {exc}")
        traceback.print_exc()

    print(f"  adb 路径: {ADB}")
    try:
        result = subprocess.run(
            [ADB, "version"], capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=15,
        )
        first = (result.stdout or result.stderr).strip().splitlines()
        print(f"  adb 可执行: {first[0] if first else '(无输出)'}")
    except (OSError, subprocess.TimeoutExpired) as exc:
        failures.append(f"adb 不可执行: {exc}")

    try:
        sys.path.insert(0, str(SCRIPT_DIR / "scripts"))
        import stats_server  # noqa: F401
        print("  统计面板模块可导入")
    except Exception as exc:
        failures.append(f"统计面板模块导入失败: {exc}")

    print("=" * 60)
    if failures:
        for item in failures:
            print(f"❌ {item}")
        return 1
    print("✅ SELFTEST OK")
    return 0

if __name__ == "__main__":
    if "--selftest" in sys.argv:
        sys.exit(run_selftest())
    run_main()
