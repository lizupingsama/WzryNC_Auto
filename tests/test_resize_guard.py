"""拖窗口时「收起内容」的判定与还原。

用原生 tk 控件，不依赖 customtkinter（助手界面用 pack 排版，所以这里主要
盯着 pack 那条路：pack 没有 grid_remove 那种「记住参数再取消」，靠抄
pack_info() + pack_slaves() 的顺序还原，抄漏了就会错位）。
"""
import time
import tkinter as tk
import unittest

import wzry_resize_guard
from wzry_resize_guard import ResizeGuard


def make_root():
    """没有显示环境（CI、纯 SSH）时跳过整组测试。"""
    try:
        root = tk.Tk()
    except tk.TclError as exc:                        # pragma: no cover
        raise unittest.SkipTest(f"没有可用的显示环境: {exc}")
    root.geometry("400x300")
    root.update()
    return root


class ResizeGuardTests(unittest.TestCase):
    def setUp(self):
        self._real_pointer_down = wzry_resize_guard.pointer_button_down
        self._real_probe = wzry_resize_guard.probe_window_tax_us
        self._hold_button(False)                       # 默认「没按着」
        self._set_window_tax(2.0)                      # 默认「机器正常」
        self.root = make_root()
        self.body = tk.Frame(self.root, bg="#334455")
        self.body.pack(fill="both", expand=True, padx=7, pady=3)
        self.root.update()

    def tearDown(self):
        wzry_resize_guard.pointer_button_down = self._real_pointer_down
        wzry_resize_guard.probe_window_tax_us = self._real_probe
        try:
            self.root.destroy()
        except tk.TclError:
            pass

    def _hold_button(self, down):
        """假装鼠标键按着 / 松开；None = 问不出来（非 Windows）。"""
        wzry_resize_guard.pointer_button_down = lambda: down

    def _set_window_tax(self, us):
        """假装启动探针量到的单次窗口操作耗时；None 表示问不出来。

        不这么钉住的话，结果会取决于跑测试这台机器上有没有挂窗口钩子。
        """
        wzry_resize_guard.probe_window_tax_us = lambda _root: us

    def _resize(self, *sizes):
        for w, h in sizes:
            self.root.geometry(f"{w}x{h}")
            self.root.update()

    def _pump(self, seconds):
        end = time.perf_counter() + seconds
        while time.perf_counter() < end:
            self.root.update()
            time.sleep(0.01)

    # ---- 判定 ----
    def test_single_resize_keeps_content(self):
        """最大化 / 换屏改 DPI 这种一次性尺寸变化不该把界面收起来。"""
        guard = ResizeGuard(self.root, slow_layout_ms=-1).install()
        self._resize((480, 350))                       # 先让它记住当前尺寸
        self._pump(1.1)                                # 隔开，不算连续拖动
        self._resize((520, 380))
        self.assertFalse(guard.active)
        self.assertTrue(self.body.winfo_ismapped())

    def test_maximize_does_not_hide_even_right_after_a_drag(self):
        """刚拖完窗口紧接着点最大化：没按着鼠标就不算拖，别白闪一下。"""
        self._hold_button(True)
        guard = ResizeGuard(self.root, slow_layout_ms=-1).install()
        self._resize((420, 320), (440, 340))
        self.assertTrue(guard.active)
        self._hold_button(False)
        self._pump(0.3)
        self.assertFalse(guard.active)
        self._resize((900, 700))                       # 松手后紧接着最大化
        self.assertFalse(guard.active)

    def test_button_held_hides_on_first_frame(self):
        """按着鼠标键 = 在拖边框，第一帧就得收，否则头一下必顿。"""
        guard = ResizeGuard(self.root, slow_layout_ms=-1).install()
        self._resize((480, 350))
        self._pump(1.1)
        self._hold_button(True)
        self._resize((520, 380))
        self.assertTrue(guard.active)

    def test_probe_enables_before_first_drag(self):
        """探针一看就知道这机器动窗口贵，不用等拖动就该定下来。"""
        self._set_window_tax(wzry_resize_guard.HOOK_TAX_US * 10)
        guard = ResizeGuard(self.root).install()
        self.assertTrue(guard.enabled)
        self.assertIsNone(guard.layout_ms)             # 还没碰过真实重排
        self._hold_button(True)
        self._resize((480, 350))
        self.assertTrue(guard.active)                  # 头一帧就收起来了

    def test_probe_silent_leaves_decision_open(self):
        """探针说不贵也不能下结论——控件太多、CPU 太慢一样会卡。"""
        self._set_window_tax(2.0)
        self._hold_button(True)
        guard = ResizeGuard(self.root, slow_layout_ms=-1).install()
        self.assertIsNone(guard.enabled)
        self._resize((420, 320), (440, 340))
        self.assertTrue(guard.active)                  # 实测重排后才判出来

    def test_on_decide_gets_guard(self):
        """判定回调拿到的是 guard 本身：探针判出来时 layout_ms 还是空的，
        界面那边要据此换一种说法。"""
        seen = []
        self._set_window_tax(wzry_resize_guard.HOOK_TAX_US * 10)
        guard = ResizeGuard(self.root, on_decide=seen.append).install()
        self.assertEqual([guard], seen)
        self.assertTrue(seen[0].enabled)
        self.assertIsNone(seen[0].layout_ms)
        self.assertGreater(seen[0].tax_us, wzry_resize_guard.HOOK_TAX_US)

    def test_drag_hides_content_on_slow_machine(self):
        self._hold_button(True)
        guard = ResizeGuard(self.root, slow_layout_ms=-1).install()
        self._resize((420, 320), (440, 340), (460, 360))
        self.assertTrue(guard.active)
        self.assertFalse(self.body.winfo_ismapped())

    def test_fast_machine_never_hides(self):
        """重排够快就不该动界面——正常机器上这个补救是纯负担。"""
        guard = ResizeGuard(self.root, slow_layout_ms=10_000).install()
        self._resize((420, 320), (440, 340), (460, 360))
        self.assertFalse(guard.active)
        self.assertTrue(self.body.winfo_ismapped())
        self.assertIsNotNone(guard.layout_ms)

    def test_mode_off_does_not_bind(self):
        guard = ResizeGuard(self.root, mode="off", slow_layout_ms=-1).install()
        self._resize((420, 320), (440, 340), (460, 360))
        self.assertFalse(guard.active)
        self.assertTrue(self.body.winfo_ismapped())

    def test_slow_frames_still_count_as_drag(self):
        """问不出鼠标状态时走「连着变两次就算在拖」；
        慢机器上两帧能隔一秒多，别因为间隔大就判成「没在拖」。"""
        self._hold_button(None)
        guard = ResizeGuard(self.root, slow_layout_ms=-1).install()
        self._resize((420, 320))
        guard._enabled, guard._layout_ms = True, 800.0  # 假装已判定：一帧 800ms
        self._pump(1.2)
        self._resize((440, 340))
        self.assertTrue(guard.active)

    # ---- 还原 ----
    def test_settle_restores_pack_options(self):
        self._hold_button(None)
        guard = ResizeGuard(self.root, slow_layout_ms=-1, settle_ms=30).install()
        before = self.body.pack_info()
        self._resize((420, 320), (440, 340))
        self.assertTrue(guard.active)
        self._pump(0.4)
        self.assertFalse(guard.active)
        self.assertTrue(self.body.winfo_ismapped())
        self.assertEqual(before, self.body.pack_info())

    def test_settle_restores_pack_order(self):
        """助手主界面是一溜横条，顺序抄漏了拖一次窗口日志框就跑最上面去。"""
        rows = []
        for color in ("#a33", "#3a3", "#33a"):
            row = tk.Frame(self.root, bg=color, height=20)
            row.pack(fill="x", side="top")
            rows.append(row)
        self.root.update()
        want = list(self.root.pack_slaves())

        self._hold_button(None)
        guard = ResizeGuard(self.root, slow_layout_ms=-1, settle_ms=30).install()
        self._resize((420, 320), (440, 340))
        self.assertTrue(guard.active)
        self._pump(0.4)
        self.assertFalse(guard.active)
        self.assertEqual(want, list(self.root.pack_slaves()))
        for row in rows:
            self.assertTrue(row.winfo_ismapped())

    def test_grid_children_also_restored(self):
        """对话框里混用 grid，两种管理器都得认。"""
        self.body.pack_forget()
        celled = tk.Frame(self.root, bg="#552211")
        celled.grid(row=0, column=0, sticky="nsew", padx=5, pady=2)
        self.root.grid_rowconfigure(0, weight=1)
        self.root.grid_columnconfigure(0, weight=1)
        self.root.update()
        before = celled.grid_info()

        self._hold_button(None)
        guard = ResizeGuard(self.root, slow_layout_ms=-1, settle_ms=30).install()
        self._resize((420, 320), (440, 340))
        self.assertTrue(guard.active)
        self.assertFalse(celled.winfo_ismapped())
        self._pump(0.4)
        self.assertFalse(guard.active)
        self.assertTrue(celled.winfo_ismapped())
        self.assertEqual(before, celled.grid_info())

    def test_restored_geometry_matches_plain_resize(self):
        """收起再放回，控件位置尺寸要和直接改窗口大小一模一样。"""
        target = (520, 420)
        self._resize(target)
        self.root.update_idletasks()
        want = (self.body.winfo_x(), self.body.winfo_y(),
                self.body.winfo_width(), self.body.winfo_height())

        self._resize((400, 300))
        self._hold_button(None)
        guard = ResizeGuard(self.root, slow_layout_ms=-1, settle_ms=30).install()
        self._resize((440, 330), (480, 370), target)
        self.assertTrue(guard.active)
        self._pump(0.4)
        self.root.update_idletasks()
        got = (self.body.winfo_x(), self.body.winfo_y(),
               self.body.winfo_width(), self.body.winfo_height())
        self.assertEqual(want, got)

    def test_no_restore_while_button_held(self):
        """拖到一半停手但没松键，别放回来——放回来再收起去比不收还卡。"""
        guard = ResizeGuard(self.root, slow_layout_ms=-1, settle_ms=20).install()
        self._hold_button(True)
        self._resize((420, 320), (440, 340))
        self.assertTrue(guard.active)
        self._pump(0.3)
        self.assertTrue(guard.active)                  # 还按着，不还原
        self._hold_button(False)
        self._pump(0.3)
        self.assertFalse(guard.active)                 # 松手了才还原
        self.assertTrue(self.body.winfo_ismapped())

    def test_button_stuck_still_restores(self):
        """万一鼠标键状态读错了，也不能让内容永远不回来。"""
        guard = ResizeGuard(self.root, slow_layout_ms=-1, settle_ms=20).install()
        self._hold_button(True)
        self._resize((420, 320), (440, 340))
        self.assertTrue(guard.active)
        guard._last_change -= wzry_resize_guard.MAX_HOLD_MS / 1000.0 + 1
        self._pump(0.3)
        self.assertFalse(guard.active)

    def test_flush_restores_immediately(self):
        guard = ResizeGuard(self.root, slow_layout_ms=-1, settle_ms=5000).install()
        self._hold_button(True)
        self._resize((420, 320), (440, 340))
        self.assertTrue(guard.active)
        guard.flush()
        self.root.update()
        self.assertTrue(self.body.winfo_ismapped())

    def test_toplevel_children_untouched(self):
        """另开的对话框（配对、更新）不归它管，别一拖窗口就把对话框收了。"""
        dialog = tk.Toplevel(self.root)
        dialog.geometry("200x120")
        self.root.update()
        self._hold_button(True)
        guard = ResizeGuard(self.root, slow_layout_ms=-1).install()
        self._resize((420, 320), (440, 340))
        self.assertTrue(guard.active)
        self.assertTrue(dialog.winfo_exists())
        self.assertNotIn(dialog, [child for _m, child, _i in guard._hidden])
        dialog.destroy()

    # ---- 配置 ----
    def test_install_reads_smooth_resize_key(self):
        guard = wzry_resize_guard.install(self.root, {"smooth_resize": "off"})
        self.assertEqual("off", guard.mode)
        self._hold_button(True)
        self._resize((420, 320), (440, 340))
        self.assertFalse(guard.active)

    def test_install_defaults_to_auto(self):
        self.assertEqual("auto", wzry_resize_guard.install(self.root, {}).mode)


try:
    import customtkinter as ctk
except Exception:                                     # pragma: no cover
    ctk = None


@unittest.skipIf(ctk is None, "没装 customtkinter")
class CTkRestoreTests(unittest.TestCase):
    """CTk 控件的收放。它的 pack()/grid() 会把 padx/pady 乘上缩放系数再交给
    Tk，而 pack_info() 读回来的是乘过的值——照原样再 pack 一次就又乘一遍，
    收放几次边距肉眼可见地变宽。故意把缩放调到 1.5，好让这个问题必现。"""

    def setUp(self):
        self._real_pointer_down = wzry_resize_guard.pointer_button_down
        wzry_resize_guard.pointer_button_down = lambda: None
        self._scaling = getattr(ctk.ScalingTracker, "widget_scaling", 1.0)
        ctk.set_widget_scaling(1.5)
        try:
            self.root = ctk.CTk()
        except tk.TclError as exc:                    # pragma: no cover
            raise unittest.SkipTest(f"没有可用的显示环境: {exc}")
        self.root.geometry("500x360")
        self.card = ctk.CTkFrame(self.root, corner_radius=12)
        self.card.pack(fill="x", padx=14, pady=(0, 8))
        self.log = ctk.CTkTextbox(self.root, corner_radius=12)
        self.log.pack(fill="both", expand=True, padx=14, pady=(2, 14))
        self.root.update()

    def tearDown(self):
        wzry_resize_guard.pointer_button_down = self._real_pointer_down
        # 先把缩放调回去再销毁：ScalingTracker 是全局的，窗口没了再改缩放
        # 它会去 configure 已经不存在的控件
        try:
            ctk.set_widget_scaling(self._scaling)
        except tk.TclError:                           # pragma: no cover
            pass
        try:
            # CTk 自己挂了一串 after（标题栏图标、DPI 轮询），不先撤掉的话
            # 窗口销毁后它们还会在别的测试里醒来，刷一屏 invalid command name
            for job in self.root.tk.call("after", "info"):
                self.root.after_cancel(job)
            self.root.destroy()
        except tk.TclError:                           # pragma: no cover
            pass

    def test_restore_keeps_padding_and_scaling_record(self):
        # settle 拉长 + 手动 flush：CTk 画一遍要几十毫秒，等自动还原的话
        # 一次 update() 里就可能把定时器顺手跑了，测出来时快时慢
        guard = ResizeGuard(self.root, mode="on", settle_ms=5000).install()
        before = [w.pack_info() for w in self.root.pack_slaves()]
        self.root.geometry("520x380")
        self.root.update()
        self.root.geometry("540x400")
        self.root.update()
        self.assertTrue(guard.active)
        self.assertFalse(self.log.winfo_ismapped())

        guard.flush()
        self.root.update()
        self.assertFalse(guard.active)
        self.assertTrue(self.log.winfo_ismapped())
        self.assertEqual(before, [w.pack_info() for w in self.root.pack_slaves()])
        # CTk 靠这份记录在 DPI 变化时重摆控件，收放一轮不能把它抹掉
        self.assertIsNotNone(self.card._last_geometry_manager_call)


if __name__ == "__main__":
    unittest.main()
