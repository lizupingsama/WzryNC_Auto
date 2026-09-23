#!/usr/bin/env python3
"""王者农场挂机助手 —— 图形界面 + 系统托盘外壳

以子进程方式运行挂机核心（脚本模式 wzry_auto.py / 打包模式 wzry_core.exe）：
- 窗口内实时显示脚本日志、累计统计与下次唤醒倒计时
- 点击关闭按钮最小化到系统托盘，不占用任务栏
- 托盘菜单：显示主界面 / 启动挂机 / 停止挂机 / 暂停挂机 / 统计面板 / 退出
- 停止时通过 stdin 管道通知脚本优雅退出（退出游戏、恢复手机亮度）；
  即使助手被强杀，子进程也会因管道 EOF 自行退出，不会留孤儿进程

界面基于 CustomTkinter（圆角控件、深浅色主题、跟随系统外观）。
请使用 start_gui.bat（首次运行，安装依赖）或 启动农场助手.vbs（日常静默启动）。
"""

import json
import os
import queue
import re
import shutil
import socket
import subprocess
import sys
import threading
import time
import traceback
import webbrowser
from datetime import datetime
from pathlib import Path

import tkinter as tk
from tkinter import font as tkfont, messagebox

import wzry_logupload
import wzry_updater

# PyInstaller 打包后 __file__ 指向内部资源目录，改用 exe 所在目录；
# 打包模式下挂机核心是同目录的 wzry_core.exe，脚本模式下仍是 wzry_auto.py。
IS_FROZEN = bool(getattr(sys, "frozen", False))
SCRIPT_DIR = (
    Path(sys.executable).resolve().parent if IS_FROZEN
    else Path(__file__).resolve().parent
)
ASSETS_DIR = SCRIPT_DIR / "assets"
MAIN_SCRIPT = SCRIPT_DIR / "wzry_auto.py"
CORE_EXE = SCRIPT_DIR / "wzry_core.exe"
STATS_FILE = ASSETS_DIR / "stats.json"
CONFIG_FILE = ASSETS_DIR / "gui_config.json"
ERROR_LOG = ASSETS_DIR / "gui_error.log"

APP_TITLE = "王者农场挂机助手"
APP_VERSION = wzry_updater.read_version(SCRIPT_DIR).get("version", "")
STATS_PORT = int(os.environ.get("WZRY_STATS_PORT", "8765"))

try:
    import customtkinter as ctk
except ImportError:
    _root = tk.Tk()
    _root.withdraw()
    messagebox.showerror(
        APP_TITLE, "缺少界面依赖 customtkinter。\n请运行 start_gui.bat 安装依赖后重试。",
    )
    raise

# 必须在创建任何 CTk 控件之前打补丁：合并拖窗口时的重画、去掉滚动条的
# update_idletasks，否则拖一下边框每个控件都要各自重画一遍（见 wzry_ctk_perf）
import wzry_ctk_perf
import wzry_resize_guard

wzry_ctk_perf.install()

# 亮度选项：显示文本 -> 传给脚本的 WZRY_BRIGHTNESS 值
BRIGHTNESS_OPTIONS = [
    ("保持当前亮度", "N"),
    ("最低亮度 (1)", "Y"),
    ("ROOT 亮度 0（全黑）", "R"),
    ("ROOT 亮度 1", "1"),
]

# 作物原始成熟时间档位：显示文本 -> 分钟数
CROP_CYCLE_OPTIONS = [
    ("1 小时", 60),
    ("8 小时", 480),
    ("16 小时", 960),
    ("32 小时", 1920),
]

# 唤醒提前量：显示文本 -> 分钟数。一轮要先启动游戏、关弹窗、进农场、走到土地
# 才点得到一键务农，提前量就是留给这段路的时间。选太大反而会早到：最后一次浇水
# 的减时最少（1 小时档只减 1 分钟），早几十秒就浇不熟，得多跑一轮才收得上。
# 实测这段耗时中位 1.8 分钟，默认 1 分钟最稳。
WAKE_LEAD_OPTIONS = [
    ("不提前", 0.0),
    ("0.5 分钟", 0.5),
    ("1 分钟（默认）", 1.0),
    ("1.5 分钟", 1.5),
    ("2 分钟", 2.0),
    ("3 分钟", 3.0),
]
WAKE_LEAD_DEFAULT = 1.0
WAKE_LEAD_DEFAULT_TEXT = WAKE_LEAD_OPTIONS[2][0]

# 外观选项：显示文本 -> customtkinter 外观模式
APPEARANCE_OPTIONS = [
    ("浅色", "light"),
    ("深色", "dark"),
    ("跟随系统", "system"),
]

# (浅色, 深色) 成对颜色，customtkinter 自动按当前外观取用
MUTED = ("gray45", "gray60")
ACCENT = ("#2FA572", "#2CC985")
DISABLED_BTN = ("gray80", "gray28")
STOP_FG = ("#D9534F", "#A94442")
STOP_HOVER = ("#C9302C", "#8B3634")
STATUS_COLORS = {
    "idle": MUTED,
    "running": ACCENT,
    "paused": ("#3D7BC7", "#5B9BE8"),
    "stopping": ("#E8890C", "#F0A030"),
}

# 设备状态指示色：已连接 / 需要处理（未授权、离线）/ 未连接
DEVICE_STATUS_COLORS = {
    "ok": ACCENT,
    "warn": STATUS_COLORS["stopping"],
    "off": MUTED,
}


def system_prefers_dark():
    """读取 Windows 深色模式偏好；读不到按浅色处理。"""
    try:
        import winreg
        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize",
        ) as key:
            value, _ = winreg.QueryValueEx(key, "AppsUseLightTheme")
        return value == 0
    except OSError:
        return False


def default_log_file():
    env = os.environ.get("WZRY_LOG_FILE")
    if env:
        return Path(env)
    if IS_FROZEN:
        # 打包版：日志放安装目录（与 exe 同级），不写系统临时目录
        return SCRIPT_DIR / "wzry_run.log"
    temp = os.environ.get("TEMP") or os.environ.get("TMP")
    return Path(temp) / "wzry_run.log" if temp else SCRIPT_DIR / "wzry_run.log"


def make_icon_image(running):
    """绘制托盘/窗口图标：绿色（运行中）或灰色（未运行）圆底上的幼苗。"""
    from PIL import Image, ImageDraw

    body = (76, 175, 80, 255) if running else (128, 128, 128, 255)
    img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    draw.ellipse((2, 2, 62, 62), fill=body)
    draw.line((32, 54, 32, 30), fill=(255, 255, 255, 255), width=6)
    draw.ellipse((12, 12, 32, 32), fill=(232, 245, 233, 255))
    draw.ellipse((32, 12, 52, 32), fill=(255, 255, 255, 255))
    return img


class FarmGui:
    QUEUE_INTERVAL = 100     # 日志/命令队列轮询 (ms)
    STATS_INTERVAL = 1000    # stats.json 与倒计时刷新 (ms)
    MAX_LOG_LINES = 2000
    STOP_TIMEOUT = 40        # 点击停止后等待脚本清理现场的秒数
    QUIT_TIMEOUT = 25        # 退出助手时等待脚本清理的秒数

    def __init__(self, root):
        self.root = root
        self.proc = None
        self.cmd_queue = queue.Queue()
        self.user_stop = False
        self._running = False
        self._paused = False
        self._quitting = False
        self._quit_deadline = None
        self._restart_job = None
        self._tray = None
        self._tray_hint_shown = False
        self._resize_guard = None
        self._adb_task = None
        self._pair_win = None
        self._device_poll_thread = None
        self._device_name_cache = {}
        self._log_groups = {}   # 日志合并：行文本 -> {"mark": 文本框标记名, "count": 次数}
        self._log_seq = 0
        self._update_state = "idle"   # idle/checking/found/waiting_stop/downloading/applying/restarting
        self._updater = None
        self._update_info = None
        self._upd_cancel = None
        self._upd_win = None
        self._log_upload_busy = False
        self._log_upload_win = None
        self._log_upload_failing = False   # 自动上传连续失败只在日志里提示一次

        self.config = self._load_config()
        self._appearance = self._initial_appearance()
        ctk.set_appearance_mode(self._appearance)
        self._status_kind = "idle"

        self._build_ui()
        # 拖边框时界面卡顿的补救（慢机器上才真正生效，见 wzry_resize_guard.py）
        self._resize_guard = wzry_resize_guard.install(
            self.root, self.config, on_decide=self._on_resize_decided,
            hint_font=self._tk_font(self.font_small),
        )
        self._init_window_icon()
        self._create_tray()
        self._resolve_adb()

        self.root.protocol("WM_DELETE_WINDOW", self.on_close_window)
        self.root.after(self.QUEUE_INTERVAL, self._drain_queue)
        self.root.after(300, self._refresh_stats)
        self.root.after(800, self._poll_device_status)

        if self.config.get("start_minimized") and self._tray:
            self.root.after(200, self.hide_to_tray)
        if self.config.get("auto_start"):
            self.root.after(600, self.start_bot)

        # 在线更新：清理上次更新残留；更新重启后报告结果；定时静默检查
        threading.Thread(
            target=wzry_updater.cleanup_leftovers, args=(SCRIPT_DIR,),
            daemon=True, name="update-cleanup",
        ).start()
        updated_from = os.environ.pop("WZRY_UPDATE_FROM", "")
        os.environ.pop("WZRY_UPDATE_RELAUNCH", None)
        if updated_from:
            self.root.after(400, lambda: self._append_log(
                f"[更新] ✅ 已从 v{updated_from or '?'} 更新到 v{APP_VERSION or '?'}\n"
            ))
        self._schedule_auto_update_check()
        self._schedule_auto_log_upload()

    def _initial_appearance(self):
        """新配置读 appearance；兼容旧配置的 dark_mode；都没有则跟随系统。"""
        appearance = self.config.get("appearance")
        if appearance in ("light", "dark", "system"):
            return appearance
        if "dark_mode" in self.config:
            return "dark" if self.config["dark_mode"] else "light"
        return "system"

    # ------------------------------------------------------------
    # 界面构建
    # ------------------------------------------------------------
    def _build_ui(self):
        self.root.title(f"{APP_TITLE} v{APP_VERSION}" if APP_VERSION else APP_TITLE)
        self.root.geometry("1060x680")
        self.root.minsize(880, 560)
        pad = 14

        yahei = "Microsoft YaHei UI"
        self.font_title = ctk.CTkFont(family=yahei, size=17, weight="bold")
        self.font_stat = ctk.CTkFont(family=yahei, size=20, weight="bold")
        self.font_body = ctk.CTkFont(family=yahei, size=13)
        self.font_small = ctk.CTkFont(family=yahei, size=12)
        self.font_mono = ctk.CTkFont(family="Consolas", size=12)

        # 顶部：状态 + 操作按钮
        header = ctk.CTkFrame(self.root, fg_color="transparent")
        header.pack(fill="x", padx=pad, pady=(pad, 8))
        self.status_var = tk.StringVar(value="●  未运行")
        self.status_label = ctk.CTkLabel(
            header, textvariable=self.status_var,
            font=self.font_title, text_color=STATUS_COLORS["idle"],
        )
        self.status_label.pack(side="left")
        # 设备名 + 连接状态，紧跟运行状态显示。控件在这里创建、但要等右侧
        # 按钮都 pack 完才 pack（见下面那行）：pack 按登记顺序分配空间，
        # 排在最后的才会在窗口被拉窄时先被裁掉——宁可截设备名，也别挤没按钮。
        self.device_status_var = tk.StringVar(value="●  检测设备中…")
        self.device_status_label = ctk.CTkLabel(
            header, textvariable=self.device_status_var,
            font=self.font_small, text_color=MUTED, anchor="w",
        )

        self.btn_start = ctk.CTkButton(
            header, text="启动挂机", width=104, font=self.font_body,
            command=self.start_bot, text_color_disabled=("gray45", "gray55"),
        )
        self.btn_start.pack(side="right", padx=(8, 0))
        self._start_fg = self.btn_start.cget("fg_color")
        self._start_hover = self.btn_start.cget("hover_color")
        self.btn_stop = ctk.CTkButton(
            header, text="停止挂机", width=104, font=self.font_body,
            command=self.stop_bot, state="disabled",
            fg_color=DISABLED_BTN, hover_color=STOP_HOVER,
            text_color_disabled=("gray45", "gray55"),
        )
        self.btn_stop.pack(side="right", padx=(8, 0))
        # 暂停/继续：核心会在最近的安全闸口停住，恢复后从原地接着跑
        self.btn_pause = ctk.CTkButton(
            header, text="暂停", width=84, font=self.font_body,
            command=self.toggle_pause, text_color_disabled=("gray45", "gray55"),
        )
        self._pause_fg = self.btn_pause.cget("fg_color")
        self._pause_hover = self.btn_pause.cget("hover_color")
        self.btn_pause.configure(state="disabled", fg_color=DISABLED_BTN)
        self.btn_pause.pack(side="right", padx=(8, 0))
        # 未运行 = 启动挂机并立即执行首轮；运行中 = 跳过等待提前开始新一轮
        self.btn_farm_now = ctk.CTkButton(
            header, text="立刻务农", width=104, font=self.font_body,
            command=self.farm_now, text_color_disabled=("gray45", "gray55"),
        )
        self.btn_farm_now.pack(side="right", padx=(8, 0))
        for text, cmd in (("缩到托盘", self.hide_to_tray), ("统计面板", self.open_stats_page)):
            ctk.CTkButton(
                header, text=text, width=92, font=self.font_body, command=cmd,
                fg_color="transparent", border_width=1,
                border_color=("gray60", "gray35"),
                text_color=("gray20", "gray80"),
                hover_color=("gray85", "gray25"),
            ).pack(side="right", padx=(8, 0))
        self.device_status_label.pack(side="left", padx=(18, 0))

        # 统计卡片：轮数 / 收获 / 经验 / 下次启动
        cards = ctk.CTkFrame(self.root, fg_color="transparent")
        cards.pack(fill="x", padx=pad, pady=(0, 4))
        for col in range(4):
            cards.grid_columnconfigure(col, weight=1, uniform="stat")

        self.rounds_var = tk.StringVar(value="0")
        self.harvest_var = tk.StringVar(value="0")
        self.exp_var = tk.StringVar(value="0")
        self.wake_big_var = tk.StringVar(value="—")
        self.wake_small_var = tk.StringVar(value="下次启动")

        def _card(col, var, caption_text=None, caption_var=None, value_color=None):
            frame = ctk.CTkFrame(cards, corner_radius=12)
            frame.grid(row=0, column=col, sticky="nsew", padx=(0 if col == 0 else 8, 0))
            ctk.CTkLabel(
                frame, textvariable=var, font=self.font_stat,
                text_color=value_color, anchor="w",
            ).pack(fill="x", padx=14, pady=(10, 0))
            ctk.CTkLabel(
                frame, text=caption_text, textvariable=caption_var,
                font=self.font_small, text_color=MUTED, anchor="w",
            ).pack(fill="x", padx=14, pady=(0, 10))
            return frame

        _card(0, self.rounds_var, caption_text="执行轮数")
        _card(1, self.harvest_var, caption_text="成熟收获")
        _card(2, self.exp_var, caption_text="累计经验")
        _card(3, self.wake_big_var, caption_var=self.wake_small_var, value_color=ACCENT)

        # 作物明细 + 更新时间
        self.crops_var = tk.StringVar(value="")
        ctk.CTkLabel(
            self.root, textvariable=self.crops_var,
            font=self.font_small, text_color=MUTED, anchor="w",
        ).pack(fill="x", padx=pad + 4, pady=(0, 4))

        # 选项行
        opts = ctk.CTkFrame(self.root, corner_radius=12)
        opts.pack(fill="x", padx=pad, pady=(0, 8))
        ctk.CTkLabel(opts, text="手机亮度", font=self.font_body).pack(
            side="left", padx=(14, 8), pady=10,
        )
        saved = self.config.get("brightness", "N")
        saved_text = next(
            (text for text, value in BRIGHTNESS_OPTIONS if value == saved),
            BRIGHTNESS_OPTIONS[0][0],
        )
        self.brightness_var = tk.StringVar(value=saved_text)
        self.brightness_menu = ctk.CTkOptionMenu(
            opts, values=[text for text, _ in BRIGHTNESS_OPTIONS],
            variable=self.brightness_var, width=170,
            font=self.font_body, dropdown_font=self.font_body,
            command=self._save_config,
        )
        self.brightness_menu.pack(side="left", pady=10)

        self.auto_start_var = tk.BooleanVar(value=bool(self.config.get("auto_start")))
        self.start_min_var = tk.BooleanVar(value=bool(self.config.get("start_minimized")))
        self.auto_restart_var = tk.BooleanVar(value=bool(self.config.get("auto_restart")))
        for text, var in (
            ("自动开始", self.auto_start_var),
            ("启动进托盘", self.start_min_var),
            ("异常自动重启", self.auto_restart_var),
        ):
            ctk.CTkSwitch(
                opts, text=text, variable=var, onvalue=True, offvalue=False,
                font=self.font_body, command=self._save_config,
            ).pack(side="left", padx=(16, 0), pady=10)

        self.appearance_seg = ctk.CTkSegmentedButton(
            opts, values=[text for text, _ in APPEARANCE_OPTIONS],
            command=self._on_appearance, font=self.font_small,
        )
        self.appearance_seg.set(
            next(text for text, value in APPEARANCE_OPTIONS if value == self._appearance)
        )
        self.appearance_seg.pack(side="right", padx=14, pady=10)

        if self._updates_supported():
            ctk.CTkButton(
                opts, text="检查更新", width=84, font=self.font_body,
                command=self.check_updates,
                fg_color="transparent", border_width=1,
                border_color=("gray60", "gray35"),
                text_color=("gray20", "gray80"),
                hover_color=("gray85", "gray25"),
            ).pack(side="right", padx=(8, 0), pady=10)

        # 作物档位必须由用户明确选择，避免浇水减时后按剩余时间误判档位。
        crop_opts = ctk.CTkFrame(self.root, corner_radius=12)
        crop_opts.pack(fill="x", padx=pad, pady=(0, 8))
        ctk.CTkLabel(
            crop_opts, text="当前作物时间档位", font=self.font_body,
        ).pack(side="left", padx=(14, 8), pady=10)
        saved_cycle = self.config.get("crop_cycle_min", 480)
        try:
            saved_cycle = int(saved_cycle)
        except (TypeError, ValueError):
            saved_cycle = 480
        saved_cycle_text = next(
            (
                text for text, value in CROP_CYCLE_OPTIONS
                if value == saved_cycle
            ),
            CROP_CYCLE_OPTIONS[1][0],
        )
        self.crop_cycle_var = tk.StringVar(value=saved_cycle_text)
        self.crop_cycle_menu = ctk.CTkOptionMenu(
            crop_opts, values=[text for text, _ in CROP_CYCLE_OPTIONS],
            variable=self.crop_cycle_var, width=130,
            font=self.font_body, dropdown_font=self.font_body,
            command=self._save_config,
        )
        self.crop_cycle_menu.pack(side="left", pady=10)
        ctk.CTkLabel(
            crop_opts,
            text="启动挂机前请选择作物的原始成熟时长",
            font=self.font_small, text_color=MUTED,
        ).pack(side="left", padx=(12, 0), pady=10)

        # 挂机进度（下次浇水时间 + 作物档位）写在手机自己身上，换台电脑连上
        # 同一部手机就接着等下一个浇水节点，而不是一连上就先白浇一次水。
        # 关掉则忽略手机存档，按界面选的档位从头务农。
        self.device_archive_var = tk.BooleanVar(
            value=bool(self.config.get("use_device_archive", True))
        )
        self.device_archive_switch = ctk.CTkSwitch(
            crop_opts, text="手机存档接力", variable=self.device_archive_var,
            onvalue=True, offvalue=False, font=self.font_body,
            command=self._save_config,
        )
        self.device_archive_switch.pack(side="right", padx=14, pady=10)

        # 唤醒提前量：先 pack 的开关在最右，这里再 pack 就落在它左边
        saved_lead = self.config.get("wake_lead_min", WAKE_LEAD_DEFAULT)
        try:
            saved_lead = float(saved_lead)
        except (TypeError, ValueError):
            saved_lead = WAKE_LEAD_DEFAULT
        saved_lead_text = next(
            (text for text, value in WAKE_LEAD_OPTIONS if value == saved_lead),
            WAKE_LEAD_DEFAULT_TEXT,
        )
        self.wake_lead_var = tk.StringVar(value=saved_lead_text)
        self.wake_lead_menu = ctk.CTkOptionMenu(
            crop_opts, values=[text for text, _ in WAKE_LEAD_OPTIONS],
            variable=self.wake_lead_var, width=124,
            font=self.font_body, dropdown_font=self.font_body,
            command=self._save_config,
        )
        self.wake_lead_menu.pack(side="right", pady=10)
        ctk.CTkLabel(crop_opts, text="唤醒提前", font=self.font_body).pack(
            side="right", padx=(16, 8), pady=10,
        )

        # 设备行：无线地址 + 连接 / USB转无线 / 配对 + 锁屏密码 + 连接状态
        conn = ctk.CTkFrame(self.root, corner_radius=12)
        conn.pack(fill="x", padx=pad, pady=(0, 8))
        ctk.CTkLabel(conn, text="无线ADB", font=self.font_body).pack(
            side="left", padx=(14, 8), pady=10,
        )
        self.device_entry = ctk.CTkEntry(
            conn, width=190, font=self.font_body,
            placeholder_text="IP:端口，留空自动选USB",
        )
        saved_device = str(self.config.get("wireless_device", "")).strip()
        if saved_device:
            self.device_entry.insert(0, saved_device)
        self.device_entry.pack(side="left", pady=10)
        self.device_entry.bind("<FocusOut>", self._save_config)
        self.btn_wireless_connect = ctk.CTkButton(
            conn, text="连接", width=64, font=self.font_body,
            command=self.connect_wireless,
        )
        self.btn_wireless_connect.pack(side="left", padx=(8, 0), pady=10)
        self.btn_usb_to_wifi = ctk.CTkButton(
            conn, text="USB转无线", width=96, font=self.font_body,
            command=self.usb_to_wireless,
            fg_color="transparent", border_width=1,
            border_color=("gray60", "gray35"),
            text_color=("gray20", "gray80"),
            hover_color=("gray85", "gray25"),
        )
        self.btn_usb_to_wifi.pack(side="left", padx=(8, 0), pady=10)
        self.btn_pair = ctk.CTkButton(
            conn, text="配对…", width=72, font=self.font_body,
            command=self.open_pair_dialog,
            fg_color="transparent", border_width=1,
            border_color=("gray60", "gray35"),
            text_color=("gray20", "gray80"),
            hover_color=("gray85", "gray25"),
        )
        self.btn_pair.pack(side="left", padx=(8, 0), pady=10)
        ctk.CTkLabel(conn, text="锁屏密码", font=self.font_body).pack(
            side="left", padx=(16, 8), pady=10,
        )
        self.pwd_entry = ctk.CTkEntry(
            conn, width=90, font=self.font_body, show="•",
            placeholder_text="无密码留空",
        )
        saved_pwd = str(self.config.get("unlock_pwd", ""))
        if saved_pwd:
            self.pwd_entry.insert(0, saved_pwd)
        self.pwd_entry.pack(side="left", pady=10)
        self.pwd_entry.bind("<FocusOut>", self._save_config)
        self.btn_pwd_eye = ctk.CTkButton(
            conn, text="👁", width=30, font=self.font_body,
            command=self._toggle_pwd_visible,
            fg_color="transparent",
            text_color=("gray20", "gray80"),
            hover_color=("gray85", "gray25"),
        )
        self.btn_pwd_eye.pack(side="left", padx=(2, 0), pady=10)

        # 日志（合并重复：同样内容的日志行只保留一行，行尾累加 ×N 计数）
        log_bar = ctk.CTkFrame(self.root, fg_color="transparent")
        log_bar.pack(fill="x", padx=pad + 4)
        ctk.CTkLabel(
            log_bar, text="运行日志", font=self.font_small,
            text_color=MUTED, anchor="w",
        ).pack(side="left")
        self.collapse_var = tk.BooleanVar(
            value=bool(self.config.get("log_collapse", True))
        )
        ctk.CTkSwitch(
            log_bar, text="合并重复", variable=self.collapse_var,
            onvalue=True, offvalue=False, font=self.font_small,
            switch_height=16, switch_width=36,
            command=self._on_collapse_toggle,
        ).pack(side="right")
        # 日志上传：出问题时点一下把最近 50 轮日志发给作者；另外每小时自动传一次
        self.auto_log_upload_var = tk.BooleanVar(
            value=bool(self.config.get("auto_log_upload", True))
        )
        ctk.CTkSwitch(
            log_bar, text="每小时自动上传", variable=self.auto_log_upload_var,
            onvalue=True, offvalue=False, font=self.font_small,
            switch_height=16, switch_width=36,
            command=self._save_config,
        ).pack(side="right", padx=(0, 16))
        self.btn_upload_log = ctk.CTkButton(
            log_bar, text="上传日志", width=76, height=24, font=self.font_small,
            command=self.open_log_upload_dialog,
            fg_color="transparent", border_width=1,
            border_color=("gray60", "gray35"),
            text_color=("gray20", "gray80"),
            hover_color=("gray85", "gray25"),
            text_color_disabled=("gray45", "gray55"),
        )
        self.btn_upload_log.pack(side="right", padx=(0, 12))
        self.log_text = ctk.CTkTextbox(
            self.root, corner_radius=12, font=self.font_mono,
            wrap="word", state="disabled",
        )
        self.log_text.pack(fill="both", expand=True, padx=pad, pady=(2, pad))

    def _tk_font(self, ctk_font):
        """把 CTkFont 转成原生 tk 控件能用、且一样按 DPI 缩放的字体。

        CTkFont 本身就是 tkinter.font.Font，但 CTk 控件画字时会乘上界面缩放
        系数，原生控件不会；不补这一步，高 DPI 屏上这行字会比旁边的小一号。
        """
        try:
            scale = ctk.ScalingTracker.get_widget_scaling(self.root)
        except Exception:
            scale = 1.0
        size = abs(int(ctk_font.cget("size"))) or 12
        return tkfont.Font(family=ctk_font.cget("family"),
                           size=-max(int(round(size * scale)), 1),
                           weight=ctk_font.cget("weight"))

    def _on_resize_decided(self, guard):
        """resize 守卫判定「这机器拖窗口画不动」时写进日志，方便排查。"""
        if not guard.enabled:
            return
        if guard.layout_ms is not None:
            why = f"重排一次窗口要 {guard.layout_ms:.0f}ms"
        else:
            why = f"每动一次窗口要 {guard.tax_us:.0f}µs（正常几 µs）"
        self._append_log(
            f"[界面] 这台机器{why}，拖动边框时先把内容收起来，松手就恢复\n"
        )
        self._append_log(
            "[界面] 多半是有软件往所有程序里注入了窗口钩子（输入法皮肤、"
            "录屏浮层一类）；不想收内容可在 assets/gui_config.json 里"
            "把 smooth_resize 改成 off\n"
        )

    def _init_window_icon(self):
        try:
            from PIL import ImageTk
            # 阻止 CTk 在 200ms 后覆盖为它自带的默认图标
            self.root._iconbitmap_method_called = True
            self._icon_photo = ImageTk.PhotoImage(make_icon_image(True))
            self.root.iconphoto(True, self._icon_photo)
        except Exception:
            pass

    def _resolve_adb(self):
        """定位 adb 并写入 WZRY_ADB 供子进程使用。

        助手可能从环境变量不完整的父进程启动（快捷方式、调度器等），
        进程 PATH 里没有 adb 时，回退到注册表里的用户/系统 PATH 搜索。
        """
        explicit = os.environ.get("WZRY_ADB")
        if explicit:
            if Path(explicit).exists():
                self._append_log(f"[助手] 使用 WZRY_ADB 指定的 adb: {explicit}\n")
                return
            self._append_log(
                f"[助手] ⚠️ WZRY_ADB 指向的文件不存在（{explicit}），尝试自动定位...\n"
            )
        bundled = SCRIPT_DIR / "platform-tools" / "adb.exe"
        if bundled.exists():
            os.environ["WZRY_ADB"] = str(bundled)
            self._append_log(f"[助手] 使用内置 adb: {bundled}\n")
            return
        found = shutil.which("adb") or self._find_adb_in_registry_path()
        if found:
            os.environ["WZRY_ADB"] = found
            self._append_log(f"[助手] 已定位 adb: {found}\n")
        else:
            self._append_log(
                "[助手] ⚠️ 未找到 adb：请将 adb 加入 PATH，"
                "或设置环境变量 WZRY_ADB 后重启助手\n"
            )

    @staticmethod
    def _find_adb_in_registry_path():
        """从注册表读取用户/系统 PATH，逐目录查找 adb.exe。"""
        if os.name != "nt":
            return None
        import winreg
        keys = [
            (winreg.HKEY_CURRENT_USER, r"Environment"),
            (winreg.HKEY_LOCAL_MACHINE,
             r"SYSTEM\CurrentControlSet\Control\Session Manager\Environment"),
        ]
        for hive, subkey in keys:
            try:
                with winreg.OpenKey(hive, subkey) as key:
                    value, _ = winreg.QueryValueEx(key, "Path")
            except OSError:
                continue
            for directory in os.path.expandvars(value).split(";"):
                directory = directory.strip().strip('"')
                if not directory:
                    continue
                candidate = Path(directory) / "adb.exe"
                try:
                    if candidate.exists():
                        return str(candidate)
                except OSError:
                    continue
        return None

    # ------------------------------------------------------------
    # 无线 ADB
    # ------------------------------------------------------------
    @staticmethod
    def _adb_run(*args, timeout=20):
        """助手侧直接执行 adb（不弹黑框），用于连接/配对等主机命令。"""
        adb = os.environ.get("WZRY_ADB") or "adb"
        return subprocess.run(
            [adb, *args], capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=timeout,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        )

    @staticmethod
    def _normalize_wireless_addr(addr):
        """无端口时补全 5555；adb 的 TCP 设备序列号总是带端口。"""
        addr = addr.strip()
        if addr and ":" not in addr:
            addr = f"{addr}:5555"
        return addr

    @staticmethod
    def _parse_wlan_ip(route_output):
        """从 `ip route` 输出提取 WiFi 网卡的本机地址（优先 wlan 行的 src）。"""
        fallback = None
        for line in route_output.splitlines():
            match = re.search(r"\bsrc\s+(\d+\.\d+\.\d+\.\d+)", line)
            if not match:
                continue
            if "wlan" in line:
                return match.group(1)
            fallback = fallback or match.group(1)
        return fallback

    def _start_adb_task(self, name, worker):
        """后台线程执行无线 ADB 操作，避免卡住界面；同一时间只跑一个。"""
        if self._adb_task is not None and self._adb_task.is_alive():
            self._append_log("[无线ADB] 上一个操作还在进行中，请稍候\n")
            return
        def run():
            try:
                worker()
            except subprocess.TimeoutExpired:
                self.cmd_queue.put(
                    ("log", f"[无线ADB] ❌ {name}超时，请确认手机与电脑在同一网络\n")
                )
            except Exception as exc:
                self.cmd_queue.put(("log", f"[无线ADB] ❌ {name}失败: {exc}\n"))
            finally:
                self.cmd_queue.put(("adb_task_done", None))
        self._adb_task = threading.Thread(target=run, daemon=True, name=f"adb-{name}")
        self._adb_task.start()
        self._refresh_adb_controls()

    def _refresh_adb_controls(self):
        busy = self._adb_task is not None and self._adb_task.is_alive()
        state = "disabled" if busy else "normal"
        self.btn_wireless_connect.configure(state=state)
        self.btn_pair.configure(state=state)
        # tcpip 会重启手机 adbd，挂机运行中切换会打断正在执行的命令
        self.btn_usb_to_wifi.configure(
            state="disabled" if (busy or self._running) else "normal"
        )
        entry_state = "disabled" if self._running else "normal"
        self.device_entry.configure(state=entry_state)
        self.pwd_entry.configure(state=entry_state)

    def _entry_text(self, entry_attr, config_key):
        """读输入框文本；退出流程中控件可能已销毁，回退到已保存配置。"""
        try:
            return getattr(self, entry_attr).get().strip()
        except (AttributeError, tk.TclError):
            return str(self.config.get(config_key, "")).strip()

    def _toggle_pwd_visible(self):
        """切换锁屏密码的明文/圆点显示。"""
        hidden = self.pwd_entry.cget("show") == "•"
        self.pwd_entry.configure(show="" if hidden else "•")
        self.btn_pwd_eye.configure(text="🙈" if hidden else "👁")

    def _set_device_entry(self, addr):
        """连接成功后回填设备地址并保存配置（兼容运行中被禁用的输入框）。"""
        entry = self.device_entry
        state = entry.cget("state")
        entry.configure(state="normal")
        entry.delete(0, "end")
        entry.insert(0, addr)
        entry.configure(state=state)
        self._save_config()

    # ------------------------------------------------------------
    # 设备状态显示
    # ------------------------------------------------------------
    DEVICE_POLL_INTERVAL = 5000  # 设备状态刷新 (ms)

    def _poll_device_status(self):
        """定时在后台线程刷新设备名与连接状态（adb devices 只查主机端，很轻）。"""
        if self._quitting:
            return
        if self._device_poll_thread is None or not self._device_poll_thread.is_alive():
            preferred = self._normalize_wireless_addr(
                self._entry_text("device_entry", "wireless_device")
            )
            def worker():
                self.cmd_queue.put(
                    ("device_status", self._query_device_status(preferred))
                )
            self._device_poll_thread = threading.Thread(
                target=worker, daemon=True, name="device-poll",
            )
            self._device_poll_thread.start()
        self.root.after(self.DEVICE_POLL_INTERVAL, self._poll_device_status)

    @staticmethod
    def _parse_adb_devices(output):
        """解析 `adb devices` 输出为 [(serial, state), ...]，保持顺序。"""
        rows = []
        for line in output.splitlines()[1:]:
            fields = line.split()
            if len(fields) >= 2:
                rows.append((fields[0], fields[1]))
        return rows

    @staticmethod
    def _choose_device(rows, preferred):
        """选出面板显示的设备：填了地址就看它；否则优先 USB，再看其余。"""
        if preferred:
            for serial, state in rows:
                if serial == preferred:
                    return serial, state
            return preferred, None
        for serial, state in rows:
            if ":" not in serial:
                return serial, state
        return rows[0] if rows else (None, None)

    def _query_device_status(self, preferred):
        """后台线程：返回 (状态文本, 颜色类别 ok/warn/off)。"""
        try:
            result = self._adb_run("devices", timeout=10)
        except Exception:
            return "●  ADB 不可用", "off"
        serial, state = self._choose_device(
            self._parse_adb_devices(result.stdout), preferred
        )
        if serial is None:
            return "●  未发现设备", "off"
        if state is None:
            return "●  未连接", "off"
        if state == "device":
            return f"●  {self._device_name(serial)} · 已连接", "ok"
        if state == "unauthorized":
            return "●  未授权，手机上点允许", "warn"
        if state == "offline":
            return "●  设备离线", "warn"
        return f"●  {state}", "warn"

    def _device_name(self, serial):
        """读取设备名（市场名优先，其次型号），按序列号缓存避免反复查询。"""
        cached = self._device_name_cache.get(serial)
        if cached:
            return cached
        try:
            result = self._adb_run(
                "-s", serial, "shell",
                "getprop ro.product.marketname; getprop ro.product.model",
                timeout=8,
            )
            lines = [line.strip() for line in result.stdout.splitlines()]
            name = next((line for line in lines if line), "") or serial
        except Exception:
            return serial
        if len(name) > 18:
            name = name[:17] + "…"
        self._device_name_cache[serial] = name
        return name

    def connect_wireless(self):
        addr = self._normalize_wireless_addr(self.device_entry.get())
        if not addr:
            self._append_log(
                "[无线ADB] 请先填写设备地址（手机 开发者选项 → 无线调试 页面显示的 IP:端口）\n"
            )
            return
        def worker():
            self.cmd_queue.put(("log", f"[无线ADB] 正在连接 {addr} ...\n"))
            result = self._adb_run("connect", addr)
            out = (result.stdout + result.stderr).strip()
            if "connected to" not in out:
                self.cmd_queue.put(("log", f"[无线ADB] ❌ 连接失败: {out or '无输出'}\n"))
                return
            # connect 成功不代表已授权，用 get-state 再确认一次
            state = self._adb_run("-s", addr, "get-state")
            if state.returncode == 0 and state.stdout.strip() == "device":
                self.cmd_queue.put(("log", f"[无线ADB] ✅ 已连接: {addr}\n"))
                self.cmd_queue.put(("wireless_ok", addr))
            else:
                err = (state.stdout + state.stderr).strip()
                hint = "，请在手机上允许调试授权弹窗" if "unauthorized" in err else ""
                self.cmd_queue.put(("log", f"[无线ADB] ⚠️ 已连接但设备不可用: {err}{hint}\n"))
        self._start_adb_task("连接", worker)

    def usb_to_wireless(self):
        """USB 连接的手机一键切换为无线：tcpip 5555 → 取 WiFi 地址 → connect。"""
        def worker():
            devices = self._adb_run("devices")
            usb = []
            for line in devices.stdout.splitlines()[1:]:
                fields = line.split()
                if len(fields) >= 2 and fields[1] == "device" and ":" not in fields[0]:
                    usb.append(fields[0])
            if not usb:
                self.cmd_queue.put(
                    ("log", "[无线ADB] ❌ 未检测到USB设备，请先用数据线连接手机并允许USB调试\n")
                )
                return
            if len(usb) > 1:
                self.cmd_queue.put(
                    ("log", f"[无线ADB] ❌ 检测到多台USB设备（{', '.join(usb)}），请只保留一台再试\n")
                )
                return
            serial = usb[0]
            route = self._adb_run("-s", serial, "shell", "ip", "route")
            ip = self._parse_wlan_ip(route.stdout)
            if not ip:
                inet = self._adb_run(
                    "-s", serial, "shell", "ip", "-f", "inet", "addr", "show", "wlan0",
                )
                match = re.search(r"inet\s+(\d+\.\d+\.\d+\.\d+)", inet.stdout)
                ip = match.group(1) if match else None
            if not ip:
                self.cmd_queue.put(
                    ("log", "[无线ADB] ❌ 未能获取手机 WiFi 地址，请确认手机已连接 WiFi\n")
                )
                return
            self.cmd_queue.put(("log", f"[无线ADB] 手机地址 {ip}，正在切换TCP模式...\n"))
            tcpip = self._adb_run("-s", serial, "tcpip", "5555")
            if "restarting" not in (tcpip.stdout + tcpip.stderr):
                out = (tcpip.stdout + tcpip.stderr).strip()
                self.cmd_queue.put(("log", f"[无线ADB] ❌ 切换TCP模式失败: {out or '无输出'}\n"))
                return
            time.sleep(2)  # 等手机 adbd 以 TCP 模式重启完成
            addr = f"{ip}:5555"
            result = self._adb_run("connect", addr)
            out = (result.stdout + result.stderr).strip()
            if "connected to" in out:
                self.cmd_queue.put(
                    ("log", f"[无线ADB] ✅ 已切换到无线连接 {addr}，"
                            "现在可以拔掉USB线（手机重启后需重新切换）\n")
                )
                self.cmd_queue.put(("wireless_ok", addr))
            else:
                self.cmd_queue.put(("log", f"[无线ADB] ❌ 无线连接失败: {out or '无输出'}\n"))
        self._start_adb_task("USB转无线", worker)

    def open_pair_dialog(self):
        """Android 11+ 无线调试配对，全程无需数据线。"""
        if self._pair_win is not None and self._pair_win.winfo_exists():
            self._pair_win.lift()
            self._pair_win.focus_force()
            return
        win = ctk.CTkToplevel(self.root)
        self._pair_win = win
        win.title("无线调试配对")
        win.resizable(False, False)
        win.transient(self.root)
        ctk.CTkLabel(
            win, justify="left", anchor="w", font=self.font_small,
            text=(
                "手机上打开 开发者选项 → 无线调试 → 使用配对码配对设备，\n"
                "将弹窗中的 IP:端口 与六位配对码填到下面。\n"
                "配对成功后，把无线调试主页面显示的 IP:端口（与配对端口不同）\n"
                "填入主界面的设备地址，点「连接」即可。"
            ),
        ).pack(fill="x", padx=16, pady=(14, 8))
        entries = {}
        for key, label, placeholder in (
            ("addr", "配对地址", "如 192.168.1.100:37123"),
            ("code", "配对码", "6位数字"),
        ):
            row = ctk.CTkFrame(win, fg_color="transparent")
            row.pack(fill="x", padx=16, pady=4)
            ctk.CTkLabel(row, text=label, width=64, anchor="w", font=self.font_body).pack(side="left")
            entry = ctk.CTkEntry(row, font=self.font_body, placeholder_text=placeholder)
            entry.pack(side="left", fill="x", expand=True)
            entries[key] = entry
        def submit():
            addr = entries["addr"].get().strip()
            code = entries["code"].get().strip()
            if not addr or not code:
                return
            win.destroy()
            def worker():
                self.cmd_queue.put(("log", f"[无线ADB] 正在配对 {addr} ...\n"))
                result = self._adb_run("pair", addr, code, timeout=30)
                out = (result.stdout + result.stderr).strip()
                if "Successfully paired" in out:
                    self.cmd_queue.put(
                        ("log", "[无线ADB] ✅ 配对成功。请在设备地址填入 无线调试 主页面"
                                "显示的 IP:端口 并点击「连接」\n")
                    )
                else:
                    self.cmd_queue.put(("log", f"[无线ADB] ❌ 配对失败: {out or '无输出'}\n"))
            self._start_adb_task("配对", worker)
        ctk.CTkButton(win, text="开始配对", width=120, font=self.font_body, command=submit).pack(
            pady=(10, 14),
        )
        win.after(120, win.focus_force)

    # ------------------------------------------------------------
    # 系统托盘
    # ------------------------------------------------------------
    def _create_tray(self):
        try:
            import pystray
        except Exception as exc:
            self._append_log(
                f"[助手] ⚠️ 托盘不可用（{exc}），关闭窗口将直接退出。"
                "请运行 start_gui.bat 安装依赖。\n"
            )
            return
        # 托盘菜单在独立线程回调，全部经 cmd_queue 转回主线程执行
        menu = pystray.Menu(
            pystray.MenuItem(
                "显示主界面", lambda *a: self.cmd_queue.put(("show", None)), default=True,
            ),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem(
                "启动挂机", lambda *a: self.cmd_queue.put(("start", None)),
                enabled=lambda *a: not self._running,
            ),
            pystray.MenuItem(
                "停止挂机", lambda *a: self.cmd_queue.put(("stop", None)),
                enabled=lambda *a: self._running,
            ),
            pystray.MenuItem(
                lambda *a: "继续挂机" if self._paused else "暂停挂机",
                lambda *a: self.cmd_queue.put(("pause", None)),
                enabled=lambda *a: self._running,
            ),
            pystray.MenuItem("立刻务农", lambda *a: self.cmd_queue.put(("farm_now", None))),
            pystray.MenuItem("统计面板", lambda *a: self.cmd_queue.put(("stats", None))),
            pystray.MenuItem("上传日志", lambda *a: self.cmd_queue.put(("upload_log", None))),
            pystray.MenuItem(
                "检查更新", lambda *a: self.cmd_queue.put(("check_update", None)),
                visible=self._updates_supported(),
            ),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("退出", lambda *a: self.cmd_queue.put(("quit", None))),
        )
        try:
            self._tray = pystray.Icon(
                "wzry_farm", make_icon_image(False), f"{APP_TITLE}（未运行）", menu,
            )
            self._tray.run_detached()
        except Exception as exc:
            self._tray = None
            self._append_log(f"[助手] ⚠️ 托盘初始化失败: {exc}\n")

    def hide_to_tray(self):
        if not self._tray:
            self.quit_app()
            return
        self.root.withdraw()
        if not self._tray_hint_shown:
            self._tray_hint_shown = True
            try:
                self._tray.notify("助手已缩到系统托盘，双击图标可恢复窗口", APP_TITLE)
            except Exception:
                pass

    def show_window(self):
        # 万一是拖到一半缩进托盘的，先把收起来的内容放回去，别露出一个空窗口
        if self._resize_guard is not None:
            self._resize_guard.flush()
        self.root.deiconify()
        self.root.lift()
        try:
            self.root.focus_force()
        except tk.TclError:
            pass

    def on_close_window(self):
        if self._tray:
            self.hide_to_tray()
        else:
            self.quit_app()

    # ------------------------------------------------------------
    # 子进程管理
    # ------------------------------------------------------------
    @staticmethod
    def _child_python():
        """GUI 可能运行在 pythonw 下；子进程改用 python.exe + 隐藏控制台，
        这样脚本内部调用的 adb 会附着到隐藏控制台，不会弹出黑框。"""
        exe = Path(sys.executable)
        if exe.name.lower() == "pythonw.exe":
            console = exe.with_name("python.exe")
            if console.exists():
                return str(console)
        return str(exe)

    def start_bot(self):
        if self._quitting or (self.proc and self.proc.poll() is None):
            return
        if self._update_state in ("waiting_stop", "downloading", "applying", "restarting"):
            self._append_log("[更新] 正在更新，更新完成重启后再启动挂机\n")
            return
        self._cancel_restart()
        if IS_FROZEN:
            if not CORE_EXE.exists():
                messagebox.showerror(APP_TITLE, f"找不到挂机核心: {CORE_EXE}")
                return
            cmd = [str(CORE_EXE)]
        else:
            if not MAIN_SCRIPT.exists():
                messagebox.showerror(APP_TITLE, f"找不到脚本: {MAIN_SCRIPT}")
                return
            cmd = [self._child_python(), "-u", str(MAIN_SCRIPT)]
        self._save_config()

        brightness = dict(BRIGHTNESS_OPTIONS).get(self.brightness_var.get(), "N")
        crop_cycle_min = dict(CROP_CYCLE_OPTIONS).get(
            self.crop_cycle_var.get(), 480
        )
        wake_lead_min = dict(WAKE_LEAD_OPTIONS).get(
            self.wake_lead_var.get(), WAKE_LEAD_DEFAULT
        )
        env = {
            **os.environ,
            "PYTHONIOENCODING": "utf-8",
            "WZRY_GUI": "1",
            "WZRY_BRIGHTNESS": brightness,
            "WZRY_CROP_CYCLE_MIN": str(crop_cycle_min),
            "WZRY_WAKE_LEAD_MIN": f"{wake_lead_min:g}",
            # 关掉则忽略手机上的存档，按界面选的档位从第一轮重新务农
            "WZRY_DEVICE_ARCHIVE": "1" if self.device_archive_var.get() else "0",
        }
        wireless = self._normalize_wireless_addr(self.device_entry.get())
        if wireless:
            # 挂机核心据此定位设备，掉线时也会用它自动 adb connect 重连
            env["WZRY_DEVICE"] = wireless
        unlock_pwd = self.pwd_entry.get().strip()
        if unlock_pwd:
            # 唤醒屏幕后上滑并输入该密码解锁（无密码留空则只上滑）
            env["WZRY_UNLOCK_PWD"] = unlock_pwd
        creationflags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
        try:
            self.proc = subprocess.Popen(
                cmd,
                cwd=str(SCRIPT_DIR),
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True, encoding="utf-8", errors="replace", bufsize=1,
                env=env, creationflags=creationflags,
            )
        except OSError as exc:
            messagebox.showerror(APP_TITLE, f"启动挂机脚本失败:\n{exc}")
            return

        self.user_stop = False
        threading.Thread(
            target=self._read_child, args=(self.proc,), daemon=True, name="child-reader",
        ).start()
        self._append_log(
            f"[助手] 已启动挂机脚本 (PID {self.proc.pid})，"
            f"日志文件: {default_log_file()}\n"
        )
        self._set_running(True)

    def _read_child(self, proc):
        """后台线程：转发子进程输出到界面队列，同时落盘日志文件。"""
        log_handle = None
        try:
            log_handle = open(default_log_file(), "a", encoding="utf-8")
        except OSError:
            pass
        try:
            assert proc.stdout is not None
            for line in proc.stdout:
                if log_handle is not None:
                    try:
                        log_handle.write(line)
                        log_handle.flush()
                    except OSError:
                        log_handle.close()
                        log_handle = None
                self.cmd_queue.put(("log", line))
        except (OSError, ValueError):
            pass
        finally:
            if log_handle is not None:
                try:
                    log_handle.close()
                except OSError:
                    pass
        code = proc.wait()
        self.cmd_queue.put(("exited", code))

    @staticmethod
    def _send_stop(proc):
        try:
            proc.stdin.write("stop\n")
            proc.stdin.flush()
        except (OSError, ValueError):
            pass

    def farm_now(self):
        """立刻开始一轮务农：未运行时直接启动挂机（首轮马上执行）；
        运行中则通过 stdin 发指令给核心——等待期跳过等待马上开始新一轮，
        务农步骤执行中由核心驳回（驳回原因显示在日志里）。"""
        proc = self.proc
        if not proc or proc.poll() is not None:
            self.start_bot()
            return
        if self.user_stop or self._quitting:
            self._append_log("[助手] 正在停止挂机，无法立刻务农\n")
            return
        if self._paused:
            self._append_log("[助手] 挂机已暂停，请先点「继续」再立刻务农\n")
            return
        try:
            proc.stdin.write("farm_now\n")
            proc.stdin.flush()
        except (OSError, ValueError):
            self._append_log("[助手] ⚠️ 立刻务农指令发送失败，请停止后重新启动挂机\n")

    def toggle_pause(self):
        """暂停 / 继续当前挂机。

        经 stdin 通知核心：核心会把当前这一步动作（一次点击、一次截图）做完，
        再在闸口停住，因此不会留下半个动作；恢复后从原地继续，不重跑本轮。
        暂停期间手机停在当前画面，等待中的下一轮唤醒时间也照旧按真实时间走。
        """
        proc = self.proc
        if not proc or proc.poll() is not None:
            return
        if self.user_stop or self._quitting:
            self._append_log("[助手] 正在停止挂机，无法暂停\n")
            return
        want_pause = not self._paused
        try:
            proc.stdin.write("pause\n" if want_pause else "resume\n")
            proc.stdin.flush()
        except (OSError, ValueError):
            self._append_log("[助手] ⚠️ 暂停指令发送失败，请停止后重新启动挂机\n")
            return
        self._set_paused(want_pause)

    def _set_paused(self, paused):
        self._paused = paused
        self.btn_pause.configure(text="继续" if paused else "暂停")
        if paused:
            self._set_status("paused", "●  已暂停")
            self._append_log("[助手] 已发送暂停指令，核心会在当前这一步动作做完后停住\n")
        else:
            self._set_status("running", "●  挂机运行中")
            self._append_log("[助手] 已发送恢复指令，从暂停处继续\n")
        self._refresh_tray()

    def stop_bot(self):
        proc = self.proc
        if not proc or proc.poll() is not None:
            self._cancel_restart()
            return
        self._cancel_restart()
        self.user_stop = True
        self._set_status("stopping", "●  正在停止...")
        self.btn_stop.configure(state="disabled", fg_color=DISABLED_BTN)
        self.btn_pause.configure(state="disabled", fg_color=DISABLED_BTN)
        self._append_log("[助手] 正在停止挂机（脚本会退出游戏并恢复手机亮度）...\n")
        self._send_stop(proc)
        threading.Thread(
            target=self._wait_or_kill, args=(proc,), daemon=True, name="stop-waiter",
        ).start()

    def _wait_or_kill(self, proc):
        try:
            proc.wait(timeout=self.STOP_TIMEOUT)
        except subprocess.TimeoutExpired:
            self.cmd_queue.put(("log", "[助手] 停止超时，强制结束脚本进程\n"))
            try:
                proc.kill()
            except OSError:
                pass

    def _on_child_exit(self, code):
        self.proc = None
        self._set_running(False)
        self._append_log(f"[助手] 挂机脚本已退出（代码 {code}）\n")
        if self._quitting:
            return
        if not self.user_stop and self.auto_restart_var.get():
            self._append_log("[助手] 已勾选自动重启，30秒后重新启动挂机...\n")
            self._restart_job = self.root.after(30_000, self.start_bot)
        self.user_stop = False

    def _cancel_restart(self):
        if self._restart_job is not None:
            try:
                self.root.after_cancel(self._restart_job)
            except (tk.TclError, ValueError):
                pass
            self._restart_job = None

    # ------------------------------------------------------------
    # 退出
    # ------------------------------------------------------------
    def quit_app(self):
        if self._quitting:
            return
        if self._update_state in ("applying", "restarting"):
            self._append_log("[更新] 正在替换文件，完成后会自动重启\n")
            return
        if self.proc and self.proc.poll() is None:
            self.show_window()
            if not messagebox.askyesno(
                APP_TITLE,
                "挂机脚本仍在运行。\n退出助手会停止挂机并恢复手机亮度设置。\n\n确认退出？",
            ):
                return
        self._quitting = True
        self._cancel_restart()
        self._set_status("stopping", "●  正在退出...")
        proc = self.proc
        if proc and proc.poll() is None:
            self._send_stop(proc)
            self._quit_deadline = time.monotonic() + self.QUIT_TIMEOUT
            self.root.after(300, self._quit_poll)
        else:
            self._finish_quit()

    def _quit_poll(self):
        proc = self.proc
        if proc and proc.poll() is None and time.monotonic() < self._quit_deadline:
            self.root.after(300, self._quit_poll)
            return
        if proc and proc.poll() is None:
            try:
                proc.kill()
            except OSError:
                pass
        self._finish_quit()

    def _finish_quit(self):
        self._save_config()
        if self._tray:
            try:
                self._tray.stop()
            except Exception:
                pass
        self.root.destroy()

    # ------------------------------------------------------------
    # 队列与刷新
    # ------------------------------------------------------------
    def _drain_queue(self):
        try:
            for _ in range(300):
                kind, payload = self.cmd_queue.get_nowait()
                if kind == "log":
                    self._append_log(payload)
                elif kind == "exited":
                    self._on_child_exit(payload)
                elif kind == "show":
                    self.show_window()
                elif kind == "start":
                    self.start_bot()
                elif kind == "stop":
                    self.stop_bot()
                elif kind == "pause":
                    self.toggle_pause()
                elif kind == "farm_now":
                    self.farm_now()
                elif kind == "stats":
                    self.open_stats_page()
                elif kind == "quit":
                    self.quit_app()
                elif kind == "check_update":
                    self.check_updates()
                elif kind == "upload_log":
                    self.open_log_upload_dialog()
                elif kind == "log_upload_done":
                    self._on_log_upload_done(*payload)
                elif kind == "upd_checked":
                    self._on_update_checked(*payload)
                elif kind == "upd_progress":
                    self._on_update_progress(*payload)
                elif kind == "upd_done":
                    self._on_update_done(payload)
                elif kind == "upd_fail":
                    self._on_update_fail(payload)
                elif kind == "adb_task_done":
                    self._refresh_adb_controls()
                elif kind == "wireless_ok":
                    self._set_device_entry(payload)
                elif kind == "device_status":
                    text, level = payload
                    self.device_status_var.set(text)
                    self.device_status_label.configure(
                        text_color=DEVICE_STATUS_COLORS.get(level, MUTED)
                    )
        except queue.Empty:
            pass
        if not self._quitting:
            self.root.after(self.QUEUE_INTERVAL, self._drain_queue)

    def _append_log(self, text):
        widget = self.log_text
        at_bottom = widget.yview()[1] >= 0.99
        widget.configure(state="normal")
        if not self._try_collapse_line(widget, text):
            self._insert_log_line(widget, text)
            line_count = int(widget.index("end-1c").split(".")[0])
            if line_count > self.MAX_LOG_LINES + 200:
                widget.delete("1.0", f"{line_count - self.MAX_LOG_LINES}.0")
        widget.configure(state="disabled")
        if at_bottom:
            widget.see("end")

    def _try_collapse_line(self, widget, text):
        """Unity 控制台风格合并：重复日志行不追加，在原行累加 ×N。

        每个已见过的行在文本框里用 mark 记住位置；顶部裁剪或错位后
        mark 所在行内容对不上，则放弃该组、按新行重新追加（自愈）。
        """
        if not self.collapse_var.get() or not text.endswith("\n"):
            return False
        key = text[:-1]
        if not key.strip() or "\n" in key:
            return False  # 空行与多行块不参与合并
        group = self._log_groups.get(key)
        if group is None:
            return False
        line = widget.index(group["mark"]).split(".")[0]
        shown = widget.get(f"{line}.0", f"{line}.0 lineend")
        expected = key if group["count"] == 1 else f"{key}  ×{group['count']}"
        if shown != expected:
            widget.mark_unset(group["mark"])
            del self._log_groups[key]
            return False
        group["count"] += 1
        widget.delete(f"{line}.0", f"{line}.0 lineend")
        widget.insert(f"{line}.0", f"{key}  ×{group['count']}")
        return True

    def _insert_log_line(self, widget, text):
        """追加日志；合并开启时为单行内容登记 mark，供后续重复行累加。"""
        collapsible = (
            self.collapse_var.get()
            and text.endswith("\n")
            and "\n" not in text[:-1]
            and text.strip()
            # 上一块以换行结尾（新块从行首开始）才能按整行登记
            and widget.index("end-1c") == widget.index("end-1c linestart")
        )
        if not collapsible:
            widget.insert("end", text)
            return
        if len(self._log_groups) > 2000:  # 防字典无限增长，整体重置
            self._reset_log_groups(widget)
        start_line = widget.index("end-1c").split(".")[0]
        widget.insert("end", text)
        self._log_seq += 1
        mark = f"loggrp{self._log_seq}"
        widget.mark_set(mark, f"{start_line}.0")
        widget.mark_gravity(mark, "left")
        self._log_groups[text[:-1]] = {"mark": mark, "count": 1}

    def _reset_log_groups(self, widget=None):
        widget = widget or self.log_text
        for group in self._log_groups.values():
            try:
                widget.mark_unset(group["mark"])
            except tk.TclError:
                pass
        self._log_groups = {}

    def _on_collapse_toggle(self):
        # 开关切换后旧分组作废：关闭时停止合并，重新打开时从当前位置重新统计
        self._reset_log_groups()
        self._save_config()

    def _refresh_stats(self):
        data = None
        try:
            data = json.loads(STATS_FILE.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            pass
        if data:
            totals = data.get("totals", {})
            crops = totals.get("crops") or {}
            self.rounds_var.set(str(totals.get("rounds", 0)))
            self.harvest_var.set(str(totals.get("harvests", 0)))
            self.exp_var.set(f"+{totals.get('exp', 0)}")
            crops_text = "   ".join(f"{k} ×{v}" for k, v in list(crops.items())[:8])
            updated = data.get("updated", "-")
            self.crops_var.set(
                f"作物  {crops_text}    更新于 {updated}" if crops_text
                else f"更新于 {updated}"
            )
            big, small = self._format_wake(data.get("next_wake") or {})
            self.wake_big_var.set(big)
            self.wake_small_var.set(small)
        if not self._quitting:
            self.root.after(self.STATS_INTERVAL, self._refresh_stats)

    @staticmethod
    def _format_wake(wake):
        """返回 (大字倒计时, 小字说明)。"""
        when = wake.get("wake")
        if not when:
            return "—", "下次启动"
        reason = wake.get("reason") or ""
        try:
            dt = datetime.strptime(when, "%Y-%m-%d %H:%M:%S")
        except ValueError:
            return when, f"下次启动 {reason}".strip()
        detail = f"下次启动 {dt.strftime('%m-%d %H:%M:%S')}"
        if reason:
            detail += f" · {reason}"
        remain = (dt - datetime.now()).total_seconds()
        if remain > 0:
            hours, rem = divmod(int(remain), 3600)
            minutes, seconds = divmod(rem, 60)
            if hours:
                countdown = f"{hours}小时{minutes}分"
            elif minutes:
                countdown = f"{minutes}分{seconds:02d}秒"
            else:
                countdown = f"{seconds}秒"
            return countdown, detail
        return "已到点", detail.replace("下次启动", "上次计划")

    def _set_running(self, running):
        self._running = running
        # 进程刚起或刚退，上一次的暂停态都已作废
        self._paused = False
        self.btn_pause.configure(text="暂停")
        if running:
            self._set_status("running", "●  挂机运行中")
            self.btn_start.configure(state="disabled", fg_color=DISABLED_BTN)
            self.btn_stop.configure(state="normal", fg_color=STOP_FG)
            self.btn_pause.configure(
                state="normal", fg_color=self._pause_fg, hover_color=self._pause_hover,
            )
        else:
            self._set_status("idle", "●  未运行")
            self.btn_start.configure(
                state="normal", fg_color=self._start_fg, hover_color=self._start_hover,
            )
            self.btn_stop.configure(state="disabled", fg_color=DISABLED_BTN)
            self.btn_pause.configure(state="disabled", fg_color=DISABLED_BTN)
        self.brightness_menu.configure(state="disabled" if running else "normal")
        self.crop_cycle_menu.configure(state="disabled" if running else "normal")
        self.wake_lead_menu.configure(state="disabled" if running else "normal")
        self.device_archive_switch.configure(
            state="disabled" if running else "normal"
        )
        self._refresh_adb_controls()
        self._refresh_tray()

    def _refresh_tray(self):
        if not self._tray:
            return
        if self._running:
            state = "已暂停" if self._paused else "运行中"
        else:
            state = "未运行"
        try:
            self._tray.icon = make_icon_image(self._running)
            self._tray.title = f"{APP_TITLE}（{state}）"
        except Exception:
            pass

    # ------------------------------------------------------------
    # 外观与状态
    # ------------------------------------------------------------
    def _set_status(self, kind, text):
        self._status_kind = kind
        self.status_var.set(text)
        self.status_label.configure(text_color=STATUS_COLORS[kind])

    def _on_appearance(self, choice_text):
        self._appearance = dict(APPEARANCE_OPTIONS).get(choice_text, "system")
        ctk.set_appearance_mode(self._appearance)
        self._save_config()

    # ------------------------------------------------------------
    # 其他
    # ------------------------------------------------------------
    @staticmethod
    def _stats_server_alive():
        """探测统计服务是否在线（GUI 子进程或终端实例开的都算）。"""
        try:
            with socket.create_connection(("127.0.0.1", STATS_PORT), timeout=0.4):
                return True
        except OSError:
            return False

    def open_stats_page(self):
        """打开统计面板：服务在线走实时模式，否则打开离线快照页。"""
        if self._stats_server_alive():
            webbrowser.open(f"http://localhost:{STATS_PORT}")
            self._append_log(
                f"[助手] 已打开统计面板（实时模式 http://localhost:{STATS_PORT}）；"
                "浏览器可能在后台打开，未弹出请查看浏览器窗口\n"
            )
            return
        page = SCRIPT_DIR / "stats.html"
        if page.exists():
            try:
                if os.name == "nt":
                    # 直接用 Unicode 路径交给系统默认程序，绕开 file:// URI 编码歧义
                    os.startfile(str(page))
                else:
                    webbrowser.open(page.as_uri())
                self._append_log(
                    "[助手] 统计服务未在线，已打开离线快照页 stats.html；"
                    "浏览器可能在后台打开，未弹出请查看浏览器窗口\n"
                )
                return
            except OSError as exc:
                self._append_log(f"[助手] ⚠️ 打开 stats.html 失败: {exc}\n")
        messagebox.showwarning(
            APP_TITLE,
            "统计面板不可用：本地统计服务未运行，且无法打开离线页面 stats.html。",
        )

    # ------------------------------------------------------------
    # 在线更新
    # ------------------------------------------------------------
    UPDATE_FIRST_CHECK = 8_000            # 启动后首次静默检查 (ms)
    UPDATE_CHECK_INTERVAL = 6 * 3600_000  # 之后每 6 小时静默检查 (ms)

    @staticmethod
    def _updates_supported():
        """在线更新面向打包版；源码模式用 git 管理（可用环境变量强开做测试）。"""
        return IS_FROZEN or bool(os.environ.get("WZRY_UPDATE_URL"))

    def _update_source_spec(self):
        return (
            os.environ.get("WZRY_UPDATE_URL")
            or str(self.config.get("update_url") or "").strip()
            or wzry_updater.DEFAULT_SOURCE
        )

    def _schedule_auto_update_check(self):
        if not self._updates_supported():
            return
        if not self.config.get("auto_update_check", True):
            return

        def periodic():
            if self._quitting:
                return
            self.check_updates(silent=True)
            self.root.after(self.UPDATE_CHECK_INTERVAL, periodic)

        self.root.after(
            self.UPDATE_FIRST_CHECK, lambda: self.check_updates(silent=True),
        )
        self.root.after(self.UPDATE_CHECK_INTERVAL, periodic)

    def _notify_tray(self, message):
        if self._tray:
            try:
                self._tray.notify(message, APP_TITLE)
            except Exception:
                pass

    def check_updates(self, silent=False):
        if self._quitting:
            return
        if not self._updates_supported():
            messagebox.showinfo(
                APP_TITLE, "源码模式请用 git pull 更新，在线更新仅打包版可用。",
            )
            return
        if self._update_state not in ("idle", "found"):
            if not silent:
                self._append_log("[更新] 更新流程进行中，请稍候\n")
            return
        self._update_state = "checking"
        if not silent:
            self._append_log("[更新] 正在检查更新...\n")
        self._upd_cancel = threading.Event()
        updater = wzry_updater.Updater(
            SCRIPT_DIR, self._update_source_spec(),
            log=lambda m: self.cmd_queue.put(("log", f"[更新] {m}\n")),
            progress=lambda *a: self.cmd_queue.put(("upd_progress", a)),
            cancel_event=self._upd_cancel,
        )

        def worker():
            try:
                info = updater.check()
                self.cmd_queue.put(("upd_checked", (updater, info, silent, None)))
            except Exception as exc:
                self.cmd_queue.put(("upd_checked", (updater, None, silent, exc)))

        threading.Thread(target=worker, daemon=True, name="update-check").start()

    def _on_update_checked(self, updater, info, silent, error):
        if error is not None:
            self._update_state = "idle"
            message = str(error) or type(error).__name__
            self._append_log(f"[更新] ⚠️ 检查更新失败: {message}\n")
            if not silent:
                messagebox.showerror(APP_TITLE, f"检查更新失败:\n{message}")
            return
        if info is None:
            self._update_state = "idle"
            if not silent:
                current = wzry_updater.read_version(SCRIPT_DIR).get("version", "?")
                messagebox.showinfo(APP_TITLE, f"已是最新版本 v{current}")
            return
        self._updater = updater
        self._update_info = info
        self._update_state = "found"
        if silent and self.config.get("auto_update"):
            self._notify_tray(f"发现新版本 v{info.version}，正在自动更新")
            self._append_log("[更新] 自动更新已开启，开始更新\n")
            self._begin_update(auto=True)
            return
        if silent and not self.root.winfo_viewable():
            self._notify_tray(
                f"发现新版本 v{info.version}，打开主界面点「检查更新」升级",
            )
            return
        self._show_update_dialog(info)

    def _show_update_dialog(self, info):
        if self._upd_win is not None and self._upd_win.winfo_exists():
            self._upd_win.lift()
            return
        self.show_window()
        win = ctk.CTkToplevel(self.root)
        self._upd_win = win
        win.title("发现新版本")
        win.geometry("480x400")
        win.transient(self.root)
        win.protocol("WM_DELETE_WINDOW", self._dismiss_update_dialog)
        win.after(150, win.lift)

        ctk.CTkLabel(
            win, text=f"v{info.local_version or '未知'}  →  v{info.version}",
            font=self.font_title,
        ).pack(pady=(18, 2))
        ctk.CTkLabel(
            win,
            text=f"共 {len(info.need)} 个文件，需下载 "
                 f"{info.download_bytes / 1024 / 1024:.1f} MB",
            font=self.font_small, text_color=MUTED,
        ).pack()
        notes = ctk.CTkTextbox(win, font=self.font_body, wrap="word")
        notes.pack(fill="both", expand=True, padx=16, pady=(10, 6))
        notes.insert("1.0", info.notes or "（未填写更新说明）")
        notes.configure(state="disabled")

        self._upd_progress = ctk.CTkProgressBar(win)
        self._upd_progress.set(0)
        self._upd_progress.pack(fill="x", padx=16)
        self._upd_status_var = tk.StringVar(value="")
        ctk.CTkLabel(
            win, textvariable=self._upd_status_var,
            font=self.font_small, text_color=MUTED,
        ).pack(pady=(2, 0))

        row = ctk.CTkFrame(win, fg_color="transparent")
        row.pack(pady=(4, 14))
        self._upd_btn_go = ctk.CTkButton(
            row, text="立即更新", width=120, font=self.font_body,
            command=self._begin_update,
        )
        self._upd_btn_go.pack(side="left", padx=8)
        self._upd_btn_later = ctk.CTkButton(
            row, text="稍后", width=90, font=self.font_body,
            command=self._dismiss_update_dialog,
            fg_color="transparent", border_width=1,
            border_color=("gray60", "gray35"),
            text_color=("gray20", "gray80"),
            hover_color=("gray85", "gray25"),
        )
        self._upd_btn_later.pack(side="left", padx=8)

    def _dismiss_update_dialog(self):
        if self._update_state == "downloading":
            # 下载中点「取消」：通知工作线程停止，界面等 upd_fail 收尾
            if self._upd_cancel is not None:
                self._upd_cancel.set()
            return
        if self._update_state in ("applying", "restarting", "waiting_stop"):
            return  # 替换阶段极快且不可中断，忽略关闭请求
        if self._upd_win is not None:
            try:
                self._upd_win.destroy()
            except tk.TclError:
                pass
            self._upd_win = None

    def _update_dialog_alive(self):
        return self._upd_win is not None and self._upd_win.winfo_exists()

    def _begin_update(self, auto=False):
        if self._update_state not in ("found",) or self._update_info is None:
            return
        self._cancel_restart()
        if self.proc and self.proc.poll() is None:
            if not auto and not messagebox.askyesno(
                APP_TITLE,
                "更新前需要停止挂机（脚本会退出游戏并恢复手机亮度）。\n\n"
                "停止挂机并开始更新？",
            ):
                return
            self._update_state = "waiting_stop"
            self._set_update_buttons(running=True, status="正在停止挂机 ...")
            self._append_log("[更新] 正在停止挂机，停止后自动开始更新\n")
            self.stop_bot()
            self._upd_wait_deadline = time.monotonic() + self.STOP_TIMEOUT + 15
            self.root.after(500, self._update_wait_stop)
            return
        self._start_update_download()

    def _update_wait_stop(self):
        if self.proc and self.proc.poll() is None:
            if time.monotonic() < self._upd_wait_deadline:
                self.root.after(500, self._update_wait_stop)
                return
            self._update_state = "found"
            self._set_update_buttons(running=False)
            self._append_log("[更新] ⚠️ 等待挂机停止超时，更新已取消\n")
            return
        self._start_update_download()

    def _set_update_buttons(self, running, status=""):
        if not self._update_dialog_alive():
            return
        try:
            self._upd_btn_go.configure(state="disabled" if running else "normal")
            self._upd_btn_later.configure(text="取消" if running else "稍后")
            self._upd_status_var.set(status)
        except tk.TclError:
            pass

    def _start_update_download(self):
        updater, info = self._updater, self._update_info
        if updater is None or info is None:
            self._update_state = "idle"
            return
        self._update_state = "downloading"
        self._set_update_buttons(running=True, status="开始下载 ...")

        def worker():
            try:
                updater.download(info)
                self.cmd_queue.put(("upd_progress", ("apply", 0, 1, "")))
                new_exe = updater.apply(info)
                self.cmd_queue.put(("upd_done", str(new_exe)))
            except wzry_updater.UpdateCancelled:
                self.cmd_queue.put(("upd_fail", None))
            except Exception as exc:
                self.cmd_queue.put(("upd_fail", exc))

        threading.Thread(target=worker, daemon=True, name="update-run").start()

    def _on_update_progress(self, stage, done, total, detail):
        if not self._update_dialog_alive():
            return
        try:
            if stage == "check":
                self._upd_status_var.set(f"比对本地文件 {done}/{total}")
            elif stage == "download":
                self._upd_progress.set(done / max(total, 1))
                self._upd_status_var.set(
                    f"下载中 {done / 1024 / 1024:.1f} / {total / 1024 / 1024:.1f} MB"
                )
            elif stage == "apply":
                self._update_state = "applying"
                self._upd_progress.set(1)
                self._upd_status_var.set(f"正在替换文件 {done}/{total}")
        except tk.TclError:
            pass

    def _on_update_fail(self, error):
        cancelled = error is None
        self._update_state = "found" if self._update_info is not None else "idle"
        if self._upd_cancel is not None:
            self._upd_cancel.clear()  # 允许重试，不然复用的取消标记会立即再次取消
        self._set_update_buttons(running=False, status="已取消" if cancelled else "更新失败")
        if cancelled:
            self._append_log("[更新] 已取消\n")
            self._dismiss_update_dialog()
            return
        message = str(error) or type(error).__name__
        self._append_log(f"[更新] ⚠️ 更新失败: {message}\n")
        if self._update_dialog_alive() or self.root.winfo_viewable():
            messagebox.showerror(APP_TITLE, f"更新失败:\n{message}")
        else:
            self._notify_tray("更新失败，详见主界面日志")

    def _on_update_done(self, new_exe):
        self._update_state = "restarting"
        info = self._update_info
        old_version = (info.local_version if info else "") or "?"
        self._append_log("[更新] ✅ 更新完成，正在重启助手 ...\n")
        env = {
            **os.environ,
            "WZRY_UPDATE_RELAUNCH": "1",
            "WZRY_UPDATE_FROM": old_version,
        }
        try:
            subprocess.Popen([new_exe], cwd=str(SCRIPT_DIR), env=env, close_fds=True)
        except OSError as exc:
            self._update_state = "idle"
            self._append_log(
                f"[更新] ⚠️ 自动重启失败（{exc}），请手动打开 农场助手.exe\n"
            )
            messagebox.showwarning(
                APP_TITLE, f"更新已完成，但自动重启失败:\n{exc}\n\n请手动重新打开助手。",
            )
            return
        self._quitting = True
        self._cancel_restart()
        self._finish_quit()

    # ------------------------------------------------------------
    # 日志上传（排查用，见 wzry_logupload.py）
    # ------------------------------------------------------------
    LOG_UPLOAD_FIRST = 5 * 60_000       # 启动后首次自动上传 (ms)
    LOG_UPLOAD_INTERVAL = 3600_000      # 之后每小时一次 (ms)

    def _log_client_id(self):
        """本机的上传身份，首次使用时生成并存进配置；排查码就是它的前 8 位。"""
        client_id = str(self.config.get("log_client_id") or "")
        if not wzry_logupload.is_client_id(client_id):
            # 不用 uuid：它会把 _uuid.pyd 带进运行库，在线更新就得整包重下 74 MB
            client_id = os.urandom(16).hex()
            self.config["log_client_id"] = client_id
            self._save_config()
        return client_id

    def _schedule_auto_log_upload(self):
        # 日志没变就不传（等下一轮浇水的几个小时里常是这样），见 _log_upload_worker
        def tick():
            if self._quitting:
                return
            if self.auto_log_upload_var.get():
                self.upload_logs(auto=True)
            self.root.after(self.LOG_UPLOAD_INTERVAL, tick)

        self.root.after(self.LOG_UPLOAD_FIRST, tick)

    def _log_upload_url(self):
        return wzry_logupload.upload_url(self.config, SCRIPT_DIR)

    def _warn_no_upload_url(self):
        message = (
            f"没有配置日志上传地址。\n\n在程序目录新建 {wzry_logupload.URL_FILE}，"
            "写上服务器的上传地址即可（发布包里自带，自己跑源码时才需要）。"
        )
        self._append_log(f"[日志上传] ❌ {message.splitlines()[0]}\n")
        messagebox.showerror(APP_TITLE, message)

    def open_log_upload_dialog(self):
        """手动上传：可以留个称呼和问题描述，传完把排查码发给作者。"""
        if not self._log_upload_url():
            self._warn_no_upload_url()
            return
        if self._log_upload_win is not None and self._log_upload_win.winfo_exists():
            self._log_upload_win.lift()
            self._log_upload_win.focus_force()
            return
        self.show_window()
        win = ctk.CTkToplevel(self.root)
        self._log_upload_win = win
        win.title("上传日志")
        win.resizable(False, False)
        win.transient(self.root)
        code = wzry_logupload.short_code(self._log_client_id())
        ctk.CTkLabel(
            win, justify="left", anchor="w", font=self.font_small,
            text=(
                "会把最近 50 轮务农日志、最近 5 次失败现场（游戏截图和那一轮\n"
                "的日志）和排查信息发给作者：电脑名、IP、手机型号与系统、adb 状态、\n"
                "助手设置（锁屏密码不会上传）。上传后把排查码告诉作者即可。"
            ),
        ).pack(fill="x", padx=16, pady=(14, 4))
        ctk.CTkLabel(
            win, text=f"排查码  {code}", anchor="w",
            font=self.font_title, text_color=ACCENT,
        ).pack(fill="x", padx=16, pady=(2, 8))

        row = ctk.CTkFrame(win, fg_color="transparent")
        row.pack(fill="x", padx=16, pady=4)
        ctk.CTkLabel(row, text="称呼", width=64, anchor="w", font=self.font_body).pack(side="left")
        nickname_entry = ctk.CTkEntry(
            row, font=self.font_body, placeholder_text="选填，方便作者认出你",
        )
        saved_nickname = str(self.config.get("log_nickname", "")).strip()
        if saved_nickname:
            nickname_entry.insert(0, saved_nickname)
        nickname_entry.pack(side="left", fill="x", expand=True)

        ctk.CTkLabel(
            win, text="遇到了什么问题（选填）", anchor="w", font=self.font_body,
        ).pack(fill="x", padx=16, pady=(8, 2))
        note_box = ctk.CTkTextbox(win, width=400, height=90, font=self.font_body, wrap="word")
        note_box.pack(fill="x", padx=16)

        def submit():
            nickname = nickname_entry.get().strip()[:40]
            note = note_box.get("1.0", "end").strip()[:2000]
            if nickname != saved_nickname:
                self.config["log_nickname"] = nickname
                self._save_config()
            win.destroy()
            self.upload_logs(auto=False, note=note)

        ctk.CTkButton(win, text="上传", width=120, font=self.font_body, command=submit).pack(
            pady=(12, 14),
        )
        win.after(120, win.focus_force)

    def upload_logs(self, auto=False, note=""):
        """收集并上传日志。界面上的东西在主线程取好，采集和上传放后台线程。"""
        if self._log_upload_busy:
            if not auto:
                self._append_log("[日志上传] 上一次上传还没结束，请稍候\n")
            return
        url = self._log_upload_url()
        if not url:
            if not auto:  # 自动上传没地址就安静跳过
                self._warn_no_upload_url()
            return
        self._log_upload_busy = True
        self.btn_upload_log.configure(state="disabled", text="上传中…")
        if not auto:
            self._append_log("[日志上传] 正在收集并上传日志 ...\n")
        snapshot = {
            "auto": auto,
            "url": url,
            "note": note,
            "client_id": self._log_client_id(),
            "nickname": str(self.config.get("log_nickname", "")),
            "config": dict(self.config),
            "last_fp": str(self.config.get("log_upload_fp", "")),
            "preferred": self._normalize_wireless_addr(
                self._entry_text("device_entry", "wireless_device")
            ),
            "gui_log": "\n".join(
                self.log_text.get("1.0", "end-1c").splitlines()[-wzry_logupload.GUI_LOG_LINES:]
            ),
            "gui": {
                "status": self.status_var.get().lstrip("● ").strip(),
                "device_status": self.device_status_var.get().lstrip("● ").strip(),
                "running": self._running,
                "paused": self._paused,
                "update_state": self._update_state,
            },
        }
        threading.Thread(
            target=self._log_upload_worker, args=(snapshot,), daemon=True, name="log-upload",
        ).start()

    def _log_upload_worker(self, snap):
        run_log = default_log_file()
        fp = wzry_logupload.fingerprint(run_log, ERROR_LOG)
        if snap["auto"] and fp == snap["last_fp"]:
            # 上次传完以后日志一行没多：再传一份一模一样的只是占服务器空间
            self.cmd_queue.put(("log_upload_done", (snap, None, None, None)))
            return
        try:
            adb_path = os.environ.get("WZRY_ADB") or "adb"
            adb = wzry_logupload.collect_adb_info(self._adb_run, adb_path)
            serial, state = self._choose_device(
                self._parse_adb_devices(adb.get("devices", "")), snap["preferred"],
            )
            phone = (
                wzry_logupload.collect_phone_info(self._adb_run, serial, state)
                if serial else {}
            )
            report = wzry_logupload.build_report(
                client_id=snap["client_id"],
                reason="auto" if snap["auto"] else "manual",
                nickname=snap["nickname"],
                note=snap["note"],
                app={"version": APP_VERSION, "frozen": IS_FROZEN, "dir": str(SCRIPT_DIR)},
                gui=snap["gui"],
                config=snap["config"],
                gui_log=snap["gui_log"],
                run_log_path=run_log,
                stats_path=STATS_FILE,
                error_log_path=ERROR_LOG,
                diagnostics_dir=SCRIPT_DIR / "diagnostics",
                adb=adb,
                phone=phone,
            )
            body = wzry_logupload.encode_report(report)
            url = snap["url"]
            receipt = wzry_logupload.upload(body, url, APP_VERSION)
        except Exception as exc:
            self.cmd_queue.put(("log_upload_done", (snap, None, None, exc)))
            return
        # 报告已经传上去了；截图按回执补传服务器还没有的，失败只记下不算整体失败
        allowed = {d["name"] for d in report["diagnostics"] if d.get("shot")}
        receipt["shots_sent"], receipt["shots_failed"] = wzry_logupload.upload_shots(
            url, snap["client_id"], receipt.get("need_shots"),
            SCRIPT_DIR / "diagnostics", APP_VERSION, allowed=allowed,
        )
        self.cmd_queue.put(("log_upload_done", (snap, receipt, fp, None)))

    def _on_log_upload_done(self, snap, receipt, fp, error):
        self._log_upload_busy = False
        try:
            self.btn_upload_log.configure(state="normal", text="上传日志")
        except tk.TclError:
            pass
        auto = snap["auto"]
        if error is not None:
            message = str(error) or type(error).__name__
            if not auto:
                self._append_log(f"[日志上传] ❌ 上传失败: {message}\n")
                messagebox.showerror(APP_TITLE, f"日志上传失败:\n{message}")
                return
            if not self._log_upload_failing:
                self._append_log(
                    f"[日志上传] ⚠️ 自动上传失败: {message}（之后照常每小时重试）\n"
                )
            self._log_upload_failing = True
            return
        if receipt is None:
            return  # 自动上传发现日志没变，跳过
        self._log_upload_failing = False
        self.config["log_upload_fp"] = fp
        self._save_config()
        code = receipt.get("code") or wzry_logupload.short_code(snap["client_id"])
        shots_sent = receipt.get("shots_sent", 0)
        shots_failed = receipt.get("shots_failed") or []
        if shots_failed:
            name, why = shots_failed[0]
            self._append_log(
                f"[日志上传] ⚠️ {len(shots_failed)} 张失败现场截图没传上（{name}: {why}），"
                "下次上传会再补\n"
            )
        if auto:
            # 文本固定不带时间，「合并重复」开着时每小时只在原行累加 ×N
            self._append_log("[日志上传] 已自动上传最近的运行日志\n")
            return
        shots_note = f"，附带 {shots_sent} 张新的失败截图" if shots_sent else ""
        self._append_log(f"[日志上传] ✅ 上传成功{shots_note}，排查码 {code}\n")
        try:
            self.root.clipboard_clear()
            self.root.clipboard_append(code)
        except tk.TclError:
            pass
        messagebox.showinfo(
            APP_TITLE,
            f"日志已上传。\n\n排查码：{code}（已复制到剪贴板）\n"
            "把排查码发给作者，就能查到这份日志。",
        )

    def _load_config(self):
        try:
            data = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            return {}

    def _save_config(self, *_event):
        # 在已有配置上合并，保留手工添加的键（update_url、auto_update 等）
        cfg = dict(self.config)
        cfg.update({
            "brightness": dict(BRIGHTNESS_OPTIONS).get(self.brightness_var.get(), "N"),
            "crop_cycle_min": dict(CROP_CYCLE_OPTIONS).get(
                self.crop_cycle_var.get(), 480
            ),
            "wake_lead_min": dict(WAKE_LEAD_OPTIONS).get(
                self.wake_lead_var.get(), WAKE_LEAD_DEFAULT
            ),
            "use_device_archive": bool(self.device_archive_var.get()),
            "auto_start": bool(self.auto_start_var.get()),
            "start_minimized": bool(self.start_min_var.get()),
            "auto_restart": bool(self.auto_restart_var.get()),
            "appearance": self._appearance,
            "wireless_device": self._entry_text("device_entry", "wireless_device"),
            "unlock_pwd": self._entry_text("pwd_entry", "unlock_pwd"),
            "log_collapse": bool(self.collapse_var.get()),
            "auto_log_upload": bool(self.auto_log_upload_var.get()),
            # 界面没有对应开关，写回来只是为了让这个键在配置文件里看得见，
            # 想强制开/关拖动时收内容的人知道该改哪儿（auto / on / off）
            "smooth_resize": str(self.config.get("smooth_resize", "auto")),
        })
        self.config = cfg
        try:
            CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
            CONFIG_FILE.write_text(
                json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8",
            )
        except OSError:
            pass


def acquire_single_instance(retry_seconds=0):
    """端口绑定式单实例锁：进程退出自动释放。

    在线更新重启时新实例先于旧实例退出而启动，锁会短暂被占，
    此时按 retry_seconds 轮询等待接管。
    """
    port = int(os.environ.get("WZRY_GUI_LOCK_PORT", "47251"))
    deadline = time.monotonic() + retry_seconds
    while True:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            sock.bind(("127.0.0.1", port))
            sock.listen(1)
            return sock
        except OSError:
            sock.close()
            if time.monotonic() >= deadline:
                return None
            time.sleep(0.5)


def main():
    relaunched = os.environ.get("WZRY_UPDATE_RELAUNCH") == "1"
    lock = acquire_single_instance(retry_seconds=20 if relaunched else 0)
    ctk.set_default_color_theme("green")
    root = ctk.CTk()
    if lock is None:
        root.withdraw()
        messagebox.showinfo(APP_TITLE, "助手已在运行（请查看系统托盘区图标）")
        root.destroy()
        return
    FarmGui(root)
    try:
        root.mainloop()
    finally:
        lock.close()


if __name__ == "__main__":
    try:
        main()
    except Exception:
        # pythonw 下没有控制台，异常写入文件便于排查
        try:
            ERROR_LOG.parent.mkdir(parents=True, exist_ok=True)
            with open(ERROR_LOG, "a", encoding="utf-8") as handle:
                handle.write(f"\n[{datetime.now()}] GUI 异常退出\n")
                handle.write(traceback.format_exc())
        except OSError:
            pass
        raise
