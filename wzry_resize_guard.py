"""拖窗口边框时的卡顿补救：拖动期间把内容收起来，停手再放回去。

为什么需要：Tk 的每个控件在 Windows 上都是一个真的 HWND。窗口尺寸一变，
Tk 就得对每个控件调一次 SetWindowPos 再重画一遍。本来这是几微秒的事，
但只要机器上装了会往所有进程注入 DLL 的东西（输入法皮肤/美化模块、录屏
浮层、某些安全软件），SetWindowPos 就会被钩子拦一道，实测能慢到 1ms 一个。
助手界面上百个控件，于是拖一下边框要几百毫秒 —— 看起来就是一顿一顿。

实测（AutoMusic 项目同一套补丁，200 个控件的合成窗口）：
    原样                          ~500ms / 次
    冻结布局（子控件不跟着变）      ~150ms / 次
    把内容整个收起来               ~16ms / 次

所以这里选「收起来」：拖动时窗口内只剩根窗口的底色，松手后一次性放回来并
重排。视觉上像老版 Windows 的「拖动时不显示窗口内容」，比现在这样半张脸
画到一半强。

两个容易踩的点，都靠「鼠标键还按着吗」解决：

* 第一帧也得收。要是等第二次尺寸变化才收，拖动的头一帧仍然是慢的那一帧，
  手感上就是「一按下去先顿一下」。但又不能见到尺寸变化就收——最大化、贴边、
  换屏幕改 DPI 都是按不住鼠标的一次性动作，收了只会白闪一下。按着鼠标键
  才收，正好把「人在拖边框」和这些分开；问不出鼠标状态的平台（非 Windows），
  以及键盘调整大小这类没有鼠标的路子，退回「短时间内连着变好几次就算在拖」
  （见 DRAG_STREAK）。
* 松手前别放回来。停手 160ms 就放回去的话，拖一半停顿一下就会「放回来
  （几百毫秒）→ 一动又收起去」，来回折腾比不收还卡。所以只要鼠标键还按着
  就一直等着（最多等 MAX_HOLD_MS，免得万一读错了键位内容永远不回来）。

只有慢机器才值得这么干，所以默认是 auto，分两步判：

1. 启动时用一个**从不映射**的空控件当探针，对它的 HWND 连调几次
   ``SetWindowPos`` 计时。没映射的窗口动起来没有任何视觉效果，但一样要过
   钩子那一道，所以能在不碰界面的前提下直接量出「这台机器每次动窗口要
   多少钱」。正常机器几微秒，被钩住的实测 1300µs，差两个数量级。
   探针说贵就直接启用——这样**第一次拖动的头一帧**就已经是收起状态，
   不会「一按下去先顿一下」。
2. 探针说不贵也不算完（也可能是控件太多、CPU 太慢）：第一次拖动时再实测
   一次真实重排耗时来定。之后每次把内容放回来时顺手复核，发现其实够快
   就自动关掉。

``assets/gui_config.json`` 里的 ``smooth_resize`` 可以写 "on" / "off" 强制。
"""

import logging
import time
import tkinter as tk

log = logging.getLogger(__name__)

# 一次布局超过这个数就认为「这机器画不动」，拖动时改成藏内容。
# 60fps 是 16.7ms/帧，25ms 已经明显掉帧了
SLOW_LAYOUT_MS = 25.0

# 启动探针：对一个不映射的控件连调几次 SetWindowPos 计时。单次超过这个数
# 就认定「窗口操作被钩子拦了」——正常机器几微秒，被钩住的实测 1300µs，
# 阈值放在 100µs 两边都不会踩线
HOOK_TAX_US = 100.0
PROBE_CALLS = 20
PROBE_BUDGET_MS = 6.0          # 已经慢得很明显就别继续量了，省启动时间

# 松手后多久把内容放回来。太短会在慢拖时反复藏/放，太长会觉得回来得慢
SETTLE_MS = 160

# 还按着鼠标键时的轮询间隔
POLL_MS = 60

# 按着鼠标键最多等这么久。正常拖动松手就结束了，这个数只是兜底：
# 万一键位读错（比如改了鼠标主键映射），别让内容永远不回来
MAX_HOLD_MS = 4000

# 隔多久之内的两次尺寸变化还算「连着变」。机器越慢两帧隔得越远，
# 所以实际判定窗口会按实测的一次布局耗时往上放宽（见 _drag_window_ms）
DRAG_WINDOW_MS = 900

# 连着变几次才认定是在拖边框，按鼠标键状态分三档：
#   按着     —— 就是在拖，第一帧就收，不然「一按下去先顿一下」
#   问不出来 —— 非 Windows，退回原来的「连着变两次」
#   明确没按 —— 最大化、贴边、换屏改 DPI 这类一次性动作，连着变三次
#                才认，免得给它们白闪一下
DRAG_STREAK = {True: 1, None: 2, False: 3}


# ------------------------------------------------------------
# 鼠标键状态
# ------------------------------------------------------------
try:                                                   # pragma: no cover
    import ctypes
    _user32 = ctypes.windll.user32
    _user32.GetAsyncKeyState.restype = ctypes.c_short
except Exception:                                      # pragma: no cover
    _user32 = None

# 左右键都算：主键被用户换过时拖边框用的是物理右键，而我们只在窗口
# 尺寸正在变的时候问这个，不存在「按着右键但没在拖窗口」的误判
_VK_BUTTONS = (0x01, 0x02)          # VK_LBUTTON / VK_RBUTTON


def pointer_button_down():
    """鼠标键是不是按着。问不出来（非 Windows）时返回 None。"""
    if _user32 is None:
        return None
    try:
        return any(_user32.GetAsyncKeyState(vk) & 0x8000 for vk in _VK_BUTTONS)
    except Exception:                                  # pragma: no cover
        return None


# SWP_NOZORDER | SWP_NOACTIVATE | SWP_NOREDRAW
_SWP_QUIET = 0x0004 | 0x0010 | 0x0008


def probe_window_tax_us(root):
    """量一次 SetWindowPos 要多少微秒。非 Windows / 出错返回 None。

    探针是一个从不 place/grid/pack 的空控件：``winfo_id()`` 会逼 Tk 把
    它的 HWND 真建出来，但因为从没映射过，改它的位置尺寸不会在屏幕上留下
    任何痕迹——却照样要走完窗口管理器和挂在上面的钩子，量到的数就是界面
    重排时每个子控件要付的单价。
    """
    if _user32 is None:
        return None
    ghost = None
    try:
        ghost = tk.Frame(root, width=1, height=1)
        handle = ghost.winfo_id()
        _user32.SetWindowPos(handle, 0, 0, 0, 1, 1, _SWP_QUIET)   # 预热
        start = time.perf_counter()
        done = 0
        for i in range(PROBE_CALLS):
            _user32.SetWindowPos(handle, 0, 0, 0, 1 + (i & 1), 1, _SWP_QUIET)
            done = i + 1
            if (time.perf_counter() - start) * 1000.0 >= PROBE_BUDGET_MS:
                break
        return (time.perf_counter() - start) / done * 1e6
    except Exception:                                  # pragma: no cover
        return None
    finally:
        if ghost is not None:
            try:
                ghost.destroy()
            except tk.TclError:                        # pragma: no cover
                pass


class ResizeGuard:
    """监视根窗口尺寸变化，拖动期间把顶层容器收起来。"""

    def __init__(self, root, mode="auto", settle_ms=SETTLE_MS,
                 slow_layout_ms=SLOW_LAYOUT_MS, on_decide=None, hint_font=None):
        self.root = root
        self.mode = str(mode or "auto").strip().lower()
        self.settle_ms = int(settle_ms)
        self.slow_layout_ms = float(slow_layout_ms)
        # 判定变化时回调 on_decide(guard)，给界面写日志用
        self.on_decide = on_decide
        self.hint_font = hint_font   # 收起时那行字的字体，留空用系统默认

        self._size = None            # 上一次见到的 (宽, 高)
        self._last_change = 0.0      # 上一次尺寸变化的时刻
        self._job = None             # 放回内容的 after id
        self._hidden = []            # 被收起来的控件 [(管理器, 控件, 参数)]
        self._hint = None            # 收起期间盖在窗口上的那行字
        self._streak = 0             # 连着变了几次尺寸
        self._layout_ms = None       # 实测的一次布局耗时
        self._tax_us = None          # 启动探针量到的单次 SetWindowPos 耗时
        # True/False = 已定；None = auto 模式还没判过
        self._enabled = True if self.mode == "on" else None

    # ------------------------------------------------------------
    def install(self):
        """开始监视。重复调用无害。"""
        if self.mode == "off":
            return self
        # 直接绑到 Misc 上：CTkFrame 之类会把 bind 转给内部 canvas，
        # 而且根窗口的绑定会收到所有子控件的 <Configure>，得自己过滤
        tk.Misc.bind(self.root, "<Configure>", self._on_configure, add="+")
        # 记下当前尺寸当基准。装的时候界面往往还没铺开（winfo_* 给 1），
        # 那就等第一次 <Configure> 补上，_on_configure 里会跳过那一次
        try:
            self._size = (self.root.winfo_width(), self.root.winfo_height())
        except tk.TclError:                            # pragma: no cover
            self._size = None
        if self._enabled is None:
            # 探针贵 = 这台机器动窗口本身就慢，直接启用，头一帧就不卡；
            # 探针不贵也不下结论，留给第一次拖动时的真实重排耗时去判
            self._tax_us = probe_window_tax_us(self.root)
            if self._tax_us is not None and self._tax_us > HOOK_TAX_US:
                log.info("窗口操作实测 %.0fµs/次（正常几 µs），"
                         "判定这台机器动窗口被钩子拖慢了", self._tax_us)
                self._decide(True)
        return self

    @property
    def active(self):
        """当前是否处于「内容已收起」状态。"""
        return bool(self._hidden)

    @property
    def enabled(self):
        """拖动时是否收起内容。None = auto 模式还没判过。"""
        return self._enabled

    @property
    def layout_ms(self):
        """实测的一次布局耗时（还没测过则为 None），日志里用。"""
        return self._layout_ms

    @property
    def tax_us(self):
        """启动探针量到的单次 SetWindowPos 耗时（µs），日志里用。"""
        return self._tax_us

    # ------------------------------------------------------------
    def _on_configure(self, event):
        if event.widget is not self.root:
            return                                   # 子控件的 Configure，不管
        size = (event.width, event.height)
        prev_size, prev_change = self._size, self._last_change
        if size == prev_size:
            return                                   # 只是移动窗口
        self._size = size
        self._last_change = time.perf_counter()
        if prev_size is None or prev_size == (1, 1):
            return                                   # 界面刚铺开，不是拖动

        gap_ms = (self._last_change - prev_change) * 1000.0
        self._streak = self._streak + 1 if gap_ms <= self._drag_window_ms() else 1

        if not self._hidden:
            if self._enabled is None:                # 启动时没量成，补量一次
                self._measure()
            need = DRAG_STREAK[pointer_button_down()]
            if self._enabled and self._streak >= need:
                self._hide()
        if not self._hidden:
            return                                   # 没收起来就没什么要还原的
        self._arm(self.settle_ms)

    def _arm(self, delay_ms):
        if self._job is not None:
            try:
                self.root.after_cancel(self._job)
            except Exception:
                pass
        try:
            self._job = self.root.after(int(delay_ms), self._settle)
        except tk.TclError:                            # pragma: no cover
            self._job = None

    def _drag_window_ms(self):
        """两次尺寸变化隔多久之内还算「连着变」。慢机器一帧就要几百毫秒，
        固定 900ms 会导致越慢越连不上，所以按实测耗时放宽。"""
        return max(DRAG_WINDOW_MS, 3.0 * (self._layout_ms or 0.0))

    # ------------------------------------------------------------
    def _measure(self, downgrade_only=False):
        """计一次 update_idletasks 的时间，据此决定要不要收内容。

        调用点都挑在「这遍布局本来就要做」的时候（拖动的第一帧、内容放回来），
        所以不算额外开销。快慢机器差两个数量级（几毫秒 vs 几百毫秒），
        量一次足够定性。``downgrade_only`` 用于放回内容时的复核：重新映射
        比单纯改尺寸贵，测出来偏大，所以只允许它把判定往「够快」改。
        """
        start = time.perf_counter()
        try:
            self.root.update_idletasks()
        except tk.TclError:
            return None
        self._layout_ms = (time.perf_counter() - start) * 1000.0
        slow = self._layout_ms > self.slow_layout_ms
        if not (downgrade_only and slow) and self.mode == "auto":
            self._decide(slow)
        return self._layout_ms

    def _decide(self, enabled):
        if enabled == self._enabled:
            return
        self._enabled = enabled
        log.info("拖动时%s内容（重排 %s，单次窗口操作 %s）",
                 "收起" if enabled else "不收起",
                 "未测" if self._layout_ms is None else f"{self._layout_ms:.0f}ms",
                 "未测" if self._tax_us is None else f"{self._tax_us:.0f}µs")
        if self.on_decide is not None:
            try:
                self.on_decide(self)
            except Exception:                          # pragma: no cover
                log.debug("resize 判定回调出错", exc_info=True)

    # ------------------------------------------------------------
    # 收起 / 放回。pack 和 grid 都要认：助手主界面是一溜 pack 的横条，
    # 统计卡片那一行内部用 grid。
    #
    # grid 有现成的 grid_remove（自己记住行列参数），pack 没有对应的东西，
    # 所以先把 pack_info() 抄下来，放回时按 pack_slaves() 的原顺序重新 pack。
    # 顺序不抄的话，拖一次窗口日志框就跑到最上面去了。
    #
    # 放回用 tk.Pack / tk.Grid 的原方法，不走控件自己的 pack()/grid()：
    # CTk 的重载会把 padx/pady 乘一遍界面缩放系数再交给 Tk，而 pack_info()
    # 读回来的已经是乘过的值，照原样再 pack 一次就又乘一遍——收放几次边距
    # 就肉眼可见地变宽。顺带也不会清掉 CTk 自己记的「上次是怎么摆的」，
    # 那份记录是它在 DPI 变化时重摆控件用的。
    # ------------------------------------------------------------
    @staticmethod
    def _pack_kwargs(info):
        """pack_info() 里的 'in' 键在 Python 里不是合法关键字，换成 in_。"""
        return {("in_" if key == "in" else key): value
                for key, value in info.items()}

    def _hide(self):
        """把根窗口下的顶层容器收起来（只是取消映射，不销毁）。"""
        packed = []
        try:
            order = list(self.root.pack_slaves())     # pack_slaves 就是显示顺序
        except tk.TclError:                           # pragma: no cover
            order = []
        for child in order:
            if isinstance(child, tk.Wm):
                continue                              # 另开的对话框不动
            try:
                if not child.winfo_ismapped():
                    continue
                info = self._pack_kwargs(child.pack_info())
            except tk.TclError:
                continue
            packed.append((child, info))
        for child, info in packed:
            try:
                tk.Pack.pack_forget(child)
            except tk.TclError:                       # pragma: no cover
                continue
            self._hidden.append(("pack", child, info))

        for child in self.root.winfo_children():
            if isinstance(child, tk.Wm):
                continue
            try:
                if child.winfo_manager() != "grid" or not child.winfo_ismapped():
                    continue
                tk.Grid.grid_remove(child)            # grid_remove 会记住行列参数
            except tk.TclError:
                continue
            self._hidden.append(("grid", child, None))

        if self._hidden:
            self._show_hint()

    def _settle(self, force=False):
        self._job = None
        if not self._hidden:
            return
        if not force and pointer_button_down() and \
                (time.perf_counter() - self._last_change) * 1000.0 < MAX_HOLD_MS:
            self._arm(POLL_MS)                       # 还按着，人没拖完
            return
        self._hide_hint()
        self._streak = 0                             # 这一轮拖动结束了
        hidden, self._hidden = self._hidden, []
        for manager, child, info in hidden:
            try:
                if manager == "grid":
                    tk.Grid.grid_configure(child)    # 按记住的参数放回去
                else:
                    tk.Pack.pack_configure(child, **info)  # 按抄下来的参数和顺序
            except tk.TclError:
                continue
        # 放回来这一遍是完整重排，顺手复核判定（只许往「够快」改）
        if self.mode == "auto":
            self._measure(downgrade_only=True)

    # ------------------------------------------------------------
    # 收起期间给一行字，免得看着像程序崩了。
    #
    # 两个刻意的选择：
    # * 用原生 tk.Label 而不是 CTkLabel——CTkLabel 是「框 + 画布 + 标签」
    #   三个窗口，这里每多一个窗口就多一份拖动开销；
    # * 不用时把它挪到窗口外，而不是 place_forget。取消映射再映射要重建
    #   显示状态，实测每次收起要多花 ~50ms（照样是钩子在收钱）；挪位置
    #   只是一次 SetWindowPos，1ms 不到。
    # ------------------------------------------------------------
    _HINT_PARKED = dict(relx=0, rely=0, x=-32000, y=-32000, anchor="nw")
    _HINT_CENTER = dict(relx=0.5, rely=0.5, x=0, y=0, anchor="center")

    def _show_hint(self):
        try:
            if self._hint is None:
                self._hint = tk.Label(self.root, text="调整窗口大小…", bd=0)
                if self.hint_font is not None:
                    self._hint.configure(font=self.hint_font)
                self._hint.place(**self._HINT_PARKED)
            self._hint.configure(bg=self.root.cget("bg"), fg="gray55")
            self._hint.place_configure(**self._HINT_CENTER)
        except tk.TclError:
            self._hint = None

    def _hide_hint(self):
        if self._hint is None:
            return
        try:
            self._hint.place_configure(**self._HINT_PARKED)
        except tk.TclError:
            self._hint = None

    # ------------------------------------------------------------
    def flush(self):
        """不管鼠标键，立刻把内容放回来（关窗口、缩托盘、测试时用）。"""
        if self._job is not None:
            try:
                self.root.after_cancel(self._job)
            except Exception:
                pass
            self._job = None
        self._settle(force=True)


def install(root, config=None, **kwargs):
    """按 gui_config.json 里的 smooth_resize 装上守卫，返回 ResizeGuard。"""
    mode = "auto"
    if config:
        mode = config.get("smooth_resize", "auto")
    return ResizeGuard(root, mode=mode, **kwargs).install()
