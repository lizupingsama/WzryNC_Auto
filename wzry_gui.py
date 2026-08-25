#!/usr/bin/env python3
"""王者农场挂机助手 —— 图形界面 + 系统托盘外壳

以子进程方式运行 wzry_auto.py：
- 窗口内实时显示脚本日志、累计统计与下次唤醒倒计时
- 点击关闭按钮最小化到系统托盘，不占用任务栏
- 托盘菜单：显示主界面 / 启动挂机 / 停止挂机 / 统计面板 / 退出
- 停止时通过 stdin 管道通知脚本优雅退出（退出游戏、恢复手机亮度）；
  即使助手被强杀，子进程也会因管道 EOF 自行退出，不会留孤儿进程

请使用 start_gui.bat（首次运行，安装依赖）或 启动农场助手.vbs（日常静默启动）。
"""

import json
import os
import queue
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
from tkinter import messagebox, ttk

SCRIPT_DIR = Path(__file__).resolve().parent
ASSETS_DIR = SCRIPT_DIR / "assets"
MAIN_SCRIPT = SCRIPT_DIR / "wzry_auto.py"
STATS_FILE = ASSETS_DIR / "stats.json"
CONFIG_FILE = ASSETS_DIR / "gui_config.json"
ERROR_LOG = ASSETS_DIR / "gui_error.log"

APP_TITLE = "王者农场挂机助手"
STATS_PORT = int(os.environ.get("WZRY_STATS_PORT", "8765"))

# 亮度选项：显示文本 -> 传给脚本的 WZRY_BRIGHTNESS 值
BRIGHTNESS_OPTIONS = [
    ("保持当前亮度", "N"),
    ("最低亮度 (1)", "Y"),
    ("ROOT 亮度 0（全黑）", "R"),
    ("ROOT 亮度 1", "1"),
]

# 亮/暗主题配色（ttk 的 clam 主题全部颜色可配置）
THEMES = {
    "light": {
        "bg": "#f3f3f3",
        "fg": "#1f1f1f",
        "muted": "#767676",
        "accent": "#1565c0",
        "running": "#2e7d32",
        "stopping": "#e65100",
        "border": "#c8c8c8",
        "button_bg": "#e5e5e5",
        "button_active": "#d5d5d5",
        "entry_bg": "#ffffff",
        "text_bg": "#ffffff",
        "text_fg": "#1f1f1f",
        "select_bg": "#bcd8f5",
    },
    "dark": {
        "bg": "#202020",
        "fg": "#e6e6e6",
        "muted": "#9e9e9e",
        "accent": "#64b5f6",
        "running": "#81c784",
        "stopping": "#ffb74d",
        "border": "#3c3c3c",
        "button_bg": "#2d2d2d",
        "button_active": "#3a3a3a",
        "entry_bg": "#2b2b2b",
        "text_bg": "#171717",
        "text_fg": "#dcdcdc",
        "select_bg": "#264f78",
    },
}

# 状态种类 -> 主题色键
STATUS_COLOR_KEY = {"idle": "muted", "running": "running", "stopping": "stopping"}


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
    STATS_INTERVAL = 2000    # stats.json 刷新 (ms)
    MAX_LOG_LINES = 2000
    STOP_TIMEOUT = 40        # 点击停止后等待脚本清理现场的秒数
    QUIT_TIMEOUT = 25        # 退出助手时等待脚本清理的秒数

    def __init__(self, root):
        self.root = root
        self.proc = None
        self.cmd_queue = queue.Queue()
        self.user_stop = False
        self._running = False
        self._quitting = False
        self._quit_deadline = None
        self._restart_job = None
        self._tray = None
        self._tray_hint_shown = False

        self.config = self._load_config()
        if "dark_mode" in self.config:
            self._dark = bool(self.config["dark_mode"])
        else:
            self._dark = system_prefers_dark()
        self._status_kind = "idle"

        self._build_ui()
        self._apply_theme()
        self._init_window_icon()
        self._create_tray()
        self._resolve_adb()

        self.root.protocol("WM_DELETE_WINDOW", self.on_close_window)
        self.root.after(self.QUEUE_INTERVAL, self._drain_queue)
        self.root.after(300, self._refresh_stats)

        if self.config.get("start_minimized") and self._tray:
            self.root.after(200, self.hide_to_tray)
        if self.config.get("auto_start"):
            self.root.after(600, self.start_bot)

    # ------------------------------------------------------------
    # 界面构建
    # ------------------------------------------------------------
    def _build_ui(self):
        self.root.title(APP_TITLE)
        self.root.geometry("880x640")
        self.root.minsize(720, 480)
        pad = {"padx": 8, "pady": 4}

        # 顶部：状态 + 操作按钮
        top = ttk.Frame(self.root)
        top.pack(fill="x", **pad)
        self.status_var = tk.StringVar(value="● 未运行")
        self.status_label = ttk.Label(
            top, textvariable=self.status_var,
            font=("Microsoft YaHei UI", 12, "bold"),
        )
        self.status_label.pack(side="left")

        self.btn_stop = ttk.Button(top, text="停止挂机", command=self.stop_bot, state="disabled")
        self.btn_stop.pack(side="right", padx=4)
        self.btn_start = ttk.Button(top, text="启动挂机", command=self.start_bot)
        self.btn_start.pack(side="right", padx=4)
        ttk.Button(top, text="统计面板", command=self.open_stats_page).pack(side="right", padx=4)
        ttk.Button(top, text="缩到托盘", command=self.hide_to_tray).pack(side="right", padx=4)

        # 选项
        opts = ttk.LabelFrame(self.root, text="选项")
        opts.pack(fill="x", **pad)
        ttk.Label(opts, text="手机亮度:").grid(row=0, column=0, sticky="w", padx=(8, 2), pady=6)
        self.brightness_combo = ttk.Combobox(
            opts, state="readonly", width=18,
            values=[text for text, _ in BRIGHTNESS_OPTIONS],
        )
        saved = self.config.get("brightness", "N")
        index = next((i for i, (_, v) in enumerate(BRIGHTNESS_OPTIONS) if v == saved), 0)
        self.brightness_combo.current(index)
        self.brightness_combo.grid(row=0, column=1, sticky="w", pady=6)
        self.brightness_combo.bind("<<ComboboxSelected>>", self._save_config)

        self.auto_start_var = tk.BooleanVar(value=bool(self.config.get("auto_start")))
        self.start_min_var = tk.BooleanVar(value=bool(self.config.get("start_minimized")))
        self.auto_restart_var = tk.BooleanVar(value=bool(self.config.get("auto_restart")))
        ttk.Checkbutton(
            opts, text="打开助手后自动开始挂机",
            variable=self.auto_start_var, command=self._save_config,
        ).grid(row=0, column=2, padx=(16, 4), pady=6)
        ttk.Checkbutton(
            opts, text="启动时直接进托盘",
            variable=self.start_min_var, command=self._save_config,
        ).grid(row=0, column=3, padx=4, pady=6)
        ttk.Checkbutton(
            opts, text="脚本异常退出自动重启",
            variable=self.auto_restart_var, command=self._save_config,
        ).grid(row=0, column=4, padx=4, pady=6)
        self.dark_var = tk.BooleanVar(value=self._dark)
        ttk.Checkbutton(
            opts, text="深色模式",
            variable=self.dark_var, command=self._on_toggle_theme,
        ).grid(row=0, column=5, padx=4, pady=6)

        # 统计
        stats_frame = ttk.LabelFrame(self.root, text="统计")
        stats_frame.pack(fill="x", **pad)
        self.stats_var = tk.StringVar(value="暂无统计数据")
        ttk.Label(
            stats_frame, textvariable=self.stats_var, font=("Microsoft YaHei UI", 10),
        ).pack(anchor="w", padx=8, pady=(4, 0))
        self.wake_var = tk.StringVar(value="")
        self.wake_label = ttk.Label(
            stats_frame, textvariable=self.wake_var, font=("Microsoft YaHei UI", 10),
        )
        self.wake_label.pack(anchor="w", padx=8, pady=(0, 6))

        # 日志（tk.Text + ttk.Scrollbar：Windows 原生 tk 滚动条不吃深色配色）
        log_frame = ttk.LabelFrame(self.root, text="运行日志")
        log_frame.pack(fill="both", expand=True, **pad)
        log_body = ttk.Frame(log_frame)
        log_body.pack(fill="both", expand=True, padx=4, pady=4)
        self.log_text = tk.Text(
            log_body, state="disabled", wrap="word", font=("Consolas", 9),
            relief="flat", borderwidth=0, highlightthickness=0,
        )
        log_scroll = ttk.Scrollbar(log_body, orient="vertical", command=self.log_text.yview)
        self.log_text.configure(yscrollcommand=log_scroll.set)
        log_scroll.pack(side="right", fill="y")
        self.log_text.pack(side="left", fill="both", expand=True)

    def _init_window_icon(self):
        try:
            from PIL import ImageTk
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
            pystray.MenuItem("统计面板", lambda *a: self.cmd_queue.put(("stats", None))),
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
        self._cancel_restart()
        if not MAIN_SCRIPT.exists():
            messagebox.showerror(APP_TITLE, f"找不到脚本: {MAIN_SCRIPT}")
            return
        self._save_config()

        brightness = BRIGHTNESS_OPTIONS[self.brightness_combo.current()][1]
        env = {
            **os.environ,
            "PYTHONIOENCODING": "utf-8",
            "WZRY_GUI": "1",
            "WZRY_BRIGHTNESS": brightness,
        }
        creationflags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
        try:
            self.proc = subprocess.Popen(
                [self._child_python(), "-u", str(MAIN_SCRIPT)],
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

    def stop_bot(self):
        proc = self.proc
        if not proc or proc.poll() is not None:
            self._cancel_restart()
            return
        self._cancel_restart()
        self.user_stop = True
        self._set_status("stopping", "● 正在停止...")
        self.btn_stop.state(["disabled"])
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
        if self.proc and self.proc.poll() is None:
            self.show_window()
            if not messagebox.askyesno(
                APP_TITLE,
                "挂机脚本仍在运行。\n退出助手会停止挂机并恢复手机亮度设置。\n\n确认退出？",
            ):
                return
        self._quitting = True
        self._cancel_restart()
        self._set_status("stopping", "● 正在退出...")
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
                elif kind == "stats":
                    self.open_stats_page()
                elif kind == "quit":
                    self.quit_app()
        except queue.Empty:
            pass
        if not self._quitting:
            self.root.after(self.QUEUE_INTERVAL, self._drain_queue)

    def _append_log(self, text):
        widget = self.log_text
        at_bottom = widget.yview()[1] >= 0.99
        widget.configure(state="normal")
        widget.insert("end", text)
        line_count = int(widget.index("end-1c").split(".")[0])
        if line_count > self.MAX_LOG_LINES + 200:
            widget.delete("1.0", f"{line_count - self.MAX_LOG_LINES}.0")
        widget.configure(state="disabled")
        if at_bottom:
            widget.see("end")

    def _refresh_stats(self):
        data = None
        try:
            data = json.loads(STATS_FILE.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            pass
        if data:
            totals = data.get("totals", {})
            crops = totals.get("crops") or {}
            parts = [
                f"轮数 {totals.get('rounds', 0)}",
                f"收获 {totals.get('harvests', 0)} 次",
                f"经验 +{totals.get('exp', 0)}",
            ]
            crops_text = "  ".join(f"{k}×{v}" for k, v in list(crops.items())[:6])
            if crops_text:
                parts.append(crops_text)
            parts.append(f"更新于 {data.get('updated', '-')}")
            self.stats_var.set("    ".join(parts))
            self.wake_var.set(self._format_wake(data.get("next_wake") or {}))
        if not self._quitting:
            self.root.after(self.STATS_INTERVAL, self._refresh_stats)

    def _format_wake(self, wake):
        when = wake.get("wake")
        if not when:
            return ""
        reason = wake.get("reason") or ""
        try:
            dt = datetime.strptime(when, "%Y-%m-%d %H:%M:%S")
        except ValueError:
            return f"下次启动: {when} {reason}"
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
            return f"下次启动: {dt.strftime('%m-%d %H:%M:%S')}（{reason}）  还剩 {countdown}"
        return f"上次计划: {dt.strftime('%m-%d %H:%M:%S')}（{reason}）"

    def _set_running(self, running):
        self._running = running
        if running:
            self._set_status("running", "● 挂机运行中")
        else:
            self._set_status("idle", "● 未运行")
        self.btn_start.state(["disabled"] if running else ["!disabled"])
        self.btn_stop.state(["!disabled"] if running else ["disabled"])
        self.brightness_combo.configure(state="disabled" if running else "readonly")
        if self._tray:
            try:
                self._tray.icon = make_icon_image(running)
                self._tray.title = f"{APP_TITLE}（{'运行中' if running else '未运行'}）"
            except Exception:
                pass

    # ------------------------------------------------------------
    # 主题
    # ------------------------------------------------------------
    def _theme(self):
        return THEMES["dark" if self._dark else "light"]

    def _set_status(self, kind, text):
        self._status_kind = kind
        self.status_var.set(text)
        self.status_label.configure(foreground=self._theme()[STATUS_COLOR_KEY[kind]])

    def _on_toggle_theme(self):
        self._dark = bool(self.dark_var.get())
        self._apply_theme()
        self._save_config()

    def _apply_theme(self):
        t = self._theme()
        style = ttk.Style(self.root)
        # Windows 原生主题（vista）不响应配色，clam 全部颜色可配置
        style.theme_use("clam")

        style.configure(
            ".", background=t["bg"], foreground=t["fg"],
            fieldbackground=t["entry_bg"], troughcolor=t["bg"],
            bordercolor=t["border"], lightcolor=t["bg"], darkcolor=t["bg"],
            focuscolor=t["muted"], selectbackground=t["select_bg"],
            selectforeground=t["fg"],
        )
        style.configure("TFrame", background=t["bg"])
        style.configure("TLabel", background=t["bg"], foreground=t["fg"])
        style.configure(
            "TLabelframe", background=t["bg"], bordercolor=t["border"],
            lightcolor=t["bg"], darkcolor=t["bg"],
        )
        style.configure("TLabelframe.Label", background=t["bg"], foreground=t["muted"])
        style.configure(
            "TButton", background=t["button_bg"], foreground=t["fg"],
            bordercolor=t["border"], lightcolor=t["button_bg"],
            darkcolor=t["button_bg"], focuscolor=t["muted"], padding=(10, 4),
        )
        style.map(
            "TButton",
            background=[("disabled", t["bg"]), ("pressed", t["button_active"]),
                        ("active", t["button_active"])],
            foreground=[("disabled", t["muted"])],
        )
        style.configure(
            "TCheckbutton", background=t["bg"], foreground=t["fg"],
            indicatorbackground=t["entry_bg"], indicatorforeground=t["fg"],
            focuscolor=t["bg"],
        )
        style.map(
            "TCheckbutton",
            background=[("active", t["bg"])],
            indicatorbackground=[("pressed", t["button_active"]),
                                 ("selected", t["entry_bg"])],
        )
        style.configure(
            "TCombobox", fieldbackground=t["entry_bg"], background=t["button_bg"],
            foreground=t["fg"], arrowcolor=t["fg"], bordercolor=t["border"],
            lightcolor=t["entry_bg"], darkcolor=t["entry_bg"],
        )
        style.map(
            "TCombobox",
            fieldbackground=[("readonly", t["entry_bg"]), ("disabled", t["bg"])],
            foreground=[("disabled", t["muted"])],
            selectbackground=[("readonly", t["entry_bg"])],
            selectforeground=[("readonly", t["fg"])],
            arrowcolor=[("disabled", t["muted"])],
        )
        style.configure(
            "Vertical.TScrollbar", background=t["button_bg"], troughcolor=t["bg"],
            bordercolor=t["bg"], arrowcolor=t["fg"],
            lightcolor=t["button_bg"], darkcolor=t["button_bg"],
        )
        style.map("Vertical.TScrollbar", background=[("active", t["button_active"])])

        self.root.configure(bg=t["bg"])
        self.log_text.configure(
            bg=t["text_bg"], fg=t["text_fg"], insertbackground=t["fg"],
            selectbackground=t["select_bg"], selectforeground=t["text_fg"],
        )
        self.wake_label.configure(foreground=t["accent"])
        self.status_label.configure(foreground=t[STATUS_COLOR_KEY[self._status_kind]])

        # 下拉列表是 tk Listbox：option_add 影响未来创建，已创建的直接改
        self.root.option_add("*TCombobox*Listbox.background", t["entry_bg"])
        self.root.option_add("*TCombobox*Listbox.foreground", t["fg"])
        self.root.option_add("*TCombobox*Listbox.selectBackground", t["select_bg"])
        self.root.option_add("*TCombobox*Listbox.selectForeground", t["fg"])
        try:
            popdown = self.root.tk.call(
                "ttk::combobox::PopdownWindow", self.brightness_combo,
            )
            self.root.tk.call(
                f"{popdown}.f.l", "configure",
                "-background", t["entry_bg"], "-foreground", t["fg"],
                "-selectbackground", t["select_bg"], "-selectforeground", t["fg"],
            )
        except tk.TclError:
            pass

        self._apply_titlebar()

    def _apply_titlebar(self):
        """Windows 10/11：让标题栏跟随深浅色。失败无妨，只影响外观。"""
        if os.name != "nt":
            return
        try:
            import ctypes
            self.root.update_idletasks()
            hwnd = ctypes.windll.user32.GetParent(self.root.winfo_id())
            value = ctypes.c_int(1 if self._dark else 0)
            for attr in (20, 19):  # DWMWA_USE_IMMERSIVE_DARK_MODE（旧系统编号 19）
                if ctypes.windll.dwmapi.DwmSetWindowAttribute(
                    hwnd, attr, ctypes.byref(value), ctypes.sizeof(value),
                ) == 0:
                    break
            # SWP_NOSIZE|NOMOVE|NOZORDER|NOACTIVATE|FRAMECHANGED：立即重绘标题栏
            ctypes.windll.user32.SetWindowPos(hwnd, 0, 0, 0, 0, 0, 0x0037)
        except Exception:
            pass

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

    def _load_config(self):
        try:
            data = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            return {}

    def _save_config(self, *_event):
        cfg = {
            "brightness": BRIGHTNESS_OPTIONS[self.brightness_combo.current()][1],
            "auto_start": bool(self.auto_start_var.get()),
            "start_minimized": bool(self.start_min_var.get()),
            "auto_restart": bool(self.auto_restart_var.get()),
            "dark_mode": bool(self.dark_var.get()),
        }
        self.config = cfg
        try:
            CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
            CONFIG_FILE.write_text(
                json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8",
            )
        except OSError:
            pass


def acquire_single_instance():
    """端口绑定式单实例锁：进程退出自动释放。"""
    port = int(os.environ.get("WZRY_GUI_LOCK_PORT", "47251"))
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.bind(("127.0.0.1", port))
        sock.listen(1)
        return sock
    except OSError:
        sock.close()
        return None


def main():
    # 高分屏下让窗口文字清晰
    try:
        from ctypes import windll
        windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        pass

    lock = acquire_single_instance()
    root = tk.Tk()
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
