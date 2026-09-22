"""customtkinter 的运行时性能补丁：拖窗口边框时别每一帧都重画。

两个热点（AutoMusic 项目上实测出来的，这边控件少一些但机制一样）：

1. 每个 CTk 控件收到 ``<Configure>`` 就同步 ``_draw()``。拖一下边框，
   几十个控件各自重画上百个 Canvas 图元，而中间尺寸转瞬即逝，画了白画。
   这里把 ``CTkBaseClass._update_dimensions_event`` 换成「先记下新尺寸、
   40ms 后合并成一次重画」：拖动过程中只累积脏控件，停手后一次画完。
2. ``CTkScrollbar._draw`` 末尾无条件 ``canvas.update_idletasks()``——它会把
   整个窗口所有挂起的布局/重画立刻同步跑完，等于每滚一次日志就强制全窗口
   刷新。Tk 自己的空闲重画机制完全够用，这一句直接禁掉。

必须在创建任何 CTk 控件之前 ``install()``（wzry_gui.py 导入 customtkinter
之后立刻调）。可重复调用；patch 失败只记日志，不影响程序启动。
"""

import logging
import time
import tkinter

log = logging.getLogger(__name__)

# 合并重画的等待时间：太短等于没合并，太长拖完窗口会明显「缩一下再弹开」
DRAW_DELAY_MS = 40

# 已验证过的 customtkinter 大版本：5.x / 6.x 的 CTkBaseClass 尺寸字段一致
SUPPORTED_MAJORS = (5, 6)

_installed = False
_install_result = {}

# 每个 Tk 根窗口一个待重画队列：{root: {"job": after_id|None, "deadline": float,
# "dirty": {widget: None}}}。dict 当有序集合用，保证重画顺序跟事件顺序一致
# （父容器先于子控件），避免子控件先画完又被父容器盖掉。
_pending = {}


def _ctk_major():
    try:
        import customtkinter
        return int(str(customtkinter.__version__).split(".")[0])
    except Exception:
        return None


def install(delay_ms=DRAW_DELAY_MS):
    """安装全部补丁。幂等：第二次及以后直接返回上次的结果。"""
    global _installed
    if _installed:
        return dict(_install_result)
    _installed = True

    major = _ctk_major()
    if major not in SUPPORTED_MAJORS:
        # 未知版本仍然尝试，但先留一条日志，出问题时好排查
        log.warning("customtkinter 版本 %s 未经验证，仍尝试安装性能补丁", major)

    for name, patch in (("throttle_draw", lambda: _patch_dimensions_event(delay_ms)),
                        ("scrollbar_idle", _patch_scrollbar_draw)):
        try:
            _install_result[name] = bool(patch())
        except Exception:                                  # pragma: no cover
            log.exception("性能补丁 %s 安装失败，跳过", name)
            _install_result[name] = False
    return dict(_install_result)


def installed():
    return dict(_install_result)


# ------------------------------------------------------------
# 1. 合并 / 节流控件重画
# ------------------------------------------------------------
def _patch_dimensions_event(delay_ms):
    from customtkinter.windows.widgets.core_widget_classes import CTkBaseClass

    if getattr(CTkBaseClass, "_perf_throttled", False):
        return True
    # 特性探测而不只看版本号：补丁依赖这几个内部名字
    for attr in ("_update_dimensions_event", "_reverse_widget_scaling", "_draw"):
        if not hasattr(CTkBaseClass, attr):
            log.warning("CTkBaseClass 缺少 %s，跳过重画节流补丁", attr)
            return False

    delay = max(int(delay_ms), 1)

    def _update_dimensions_event(self, event):
        # 尺寸没变就什么都不做（原版逻辑），只是把 _draw 换成排队
        try:
            width = self._reverse_widget_scaling(event.width)
            height = self._reverse_widget_scaling(event.height)
            if round(self._current_width) == round(width) and \
                    round(self._current_height) == round(height):
                return
            self._current_width = width
            self._current_height = height
        except Exception:
            return
        _schedule(self, delay)

    def _schedule(widget, delay):
        try:
            root = widget._root()
        except Exception:
            return
        entry = _pending.get(root)
        if entry is None:
            entry = _pending[root] = {"job": None, "deadline": 0.0, "dirty": {}}
        entry["dirty"][widget] = None
        # 不断拖动时不反复 after_cancel/after（每次都是两趟 Tcl 调用），
        # 只推后截止时间；定时器醒来发现还没到点就自己再睡一会
        entry["deadline"] = time.perf_counter() + delay / 1000.0
        if entry["job"] is None:
            try:
                entry["job"] = root.after(delay, _flush, root)
            except tkinter.TclError:
                entry["job"] = None

    def _flush(root):
        entry = _pending.get(root)
        if entry is None:
            return
        entry["job"] = None
        remaining = entry["deadline"] - time.perf_counter()
        if remaining > 0.002:
            try:
                entry["job"] = root.after(max(int(remaining * 1000), 1), _flush, root)
                return
            except tkinter.TclError:
                pass
        dirty, entry["dirty"] = entry["dirty"], {}
        for widget in dirty:
            try:
                if not widget.winfo_exists():
                    continue
                widget._draw(no_color_updates=True)
            except tkinter.TclError:
                # 控件在等待期间被销毁是常事（关掉配对/更新对话框），不算错误
                continue
            except Exception:
                log.debug("延迟重画 %r 失败", widget, exc_info=True)

    CTkBaseClass._perf_original_update_dimensions_event = \
        CTkBaseClass._update_dimensions_event
    CTkBaseClass._update_dimensions_event = _update_dimensions_event
    CTkBaseClass._perf_throttled = True
    return True


def flush_pending_draws():
    """立刻把排队的重画画完（截图/测试时用，正常运行不需要）。"""
    for root, entry in list(_pending.items()):
        if entry["job"] is not None:
            try:
                root.after_cancel(entry["job"])
            except Exception:
                pass
            entry["job"] = None
        entry["deadline"] = 0.0
        dirty, entry["dirty"] = entry["dirty"], {}
        for widget in dirty:
            try:
                if widget.winfo_exists():
                    widget._draw(no_color_updates=True)
            except Exception:
                continue


# ------------------------------------------------------------
# 2. 去掉 CTkScrollbar._draw 里的 update_idletasks
# ------------------------------------------------------------
def _noop(*_args, **_kwargs):
    return None


def _patch_scrollbar_draw():
    from customtkinter.windows.widgets.ctk_scrollbar import CTkScrollbar

    if getattr(CTkScrollbar, "_perf_no_idle", False):
        return True
    original_draw = CTkScrollbar._draw

    def _draw(self, no_color_updates=False):
        # 不复制原方法体（那样绑死某个版本的内部实现），而是把这个滚动条
        # 私有 canvas 的 update_idletasks 换成空函数：只有 _draw 末尾会调它，
        # 其它地方从不需要在滚动条 canvas 上同步刷新。
        canvas = getattr(self, "_canvas", None)
        if canvas is not None and "update_idletasks" not in canvas.__dict__:
            try:
                canvas.update_idletasks = _noop
            except Exception:
                pass
        return original_draw(self, no_color_updates)

    CTkScrollbar._perf_original_draw = original_draw
    CTkScrollbar._draw = _draw
    CTkScrollbar._perf_no_idle = True
    return True
