import contextlib
import io
import json
import os
import subprocess
import tempfile
import threading
import time
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

import numpy as np
import wzry_auto


ROOT = Path(__file__).resolve().parents[1]


class TemplateMatchingTests(unittest.TestCase):
    def test_event_popup_close_matches_in_safe_region(self):
        template = wzry_auto.cv_imread(
            ROOT / "assets" / "templates" / "2400x1080"
            / "close_popup_event.png"
        )
        canvas = np.zeros((1080, 2400, 3), dtype=np.uint8)
        canvas[78:160, 2060:2142] = template
        with tempfile.TemporaryDirectory() as directory:
            screenshot = Path(directory) / "event.png"
            wzry_auto.cv_imwrite(screenshot, canvas)
            result = wzry_auto.find_template(
                "close_popup_event.png", str(screenshot)
            )
        self.assertIsNotNone(result)
        self.assertGreaterEqual(result["score"], 0.78)
        self.assertEqual((result["x"], result["y"]), (2101, 119))

    def test_popup_roi_excludes_top_center_navigation(self):
        x1, _, _, _ = wzry_auto.TEMPLATE_ROIS["close_popup.png"]
        self.assertGreaterEqual(x1, 0.65)

    def test_wide_announcement_popup_close_matches_at_roi_left_edge(self):
        # 回归：版本更新公告类宽弹窗的 ✕ 中心在 0.78 屏宽处（实测 3200x1440
        # 失败截图位于 (2495,199)），ROI 左界取 0.78 会裁掉模板主体导致
        # step2 等不到弹窗而超时
        template = wzry_auto.cv_imread(
            ROOT / "assets" / "templates" / "3200x1440" / "close_popup.png"
        )
        th, tw = template.shape[:2]
        canvas = np.zeros((1440, 3200, 3), dtype=np.uint8)
        canvas[161:161 + th, 2458:2458 + tw] = template
        with tempfile.TemporaryDirectory() as directory:
            screenshot = Path(directory) / "announcement.png"
            wzry_auto.cv_imwrite(screenshot, canvas)
            result = wzry_auto.find_template("close_popup.png", str(screenshot))
        self.assertIsNotNone(result)
        self.assertEqual(
            (result["x"], result["y"]), (2458 + tw // 2, 161 + th // 2)
        )

    def test_kpl_popup_close_matches_at_real_position(self):
        # KPL 观赛直播弹窗（比赛期间每次登录必弹）关闭钮是右上角海星样式 ✕，
        # 模板取自 3200x1440 失败现场，中心位于 (2807,157)
        result = wzry_auto.find_template(
            "close_popup_kpl.png",
            str(ROOT / "assets" / "screenshots" / "3200x1440_kpl_live_popup.png"),
        )
        self.assertIsNotNone(result)
        self.assertEqual((result["x"], result["y"]), (2807, 157))

    def test_kpl_popup_close_absent_elsewhere(self):
        # 全部参考截图噪声上限 0.544（event_popup），阈值 0.80 不得误报
        for name in ("3200x1440_event_popup.png", "3200x1440_login.png"):
            result = wzry_auto.find_template(
                "close_popup_kpl.png",
                str(ROOT / "assets" / "screenshots" / name),
            )
            self.assertIsNone(result, name)

    def test_dialog_confirm_matches_on_rest_reminder(self):
        # 健康系统「呵护双眼」休息提醒只有 确定/帮助/前往营地 三键、无 ✕；
        # 模板取自 3200x1440 失败现场，「确定」中心位于 (1595,928)
        result = wzry_auto.find_template(
            "dialog_confirm.png",
            str(ROOT / "assets" / "screenshots" / "3200x1440_rest_reminder_popup.png"),
        )
        self.assertIsNotNone(result)
        self.assertEqual((result["x"], result["y"]), (1595, 928))

    def test_dialog_confirm_roi_excludes_sibling_buttons(self):
        # 「帮助」从 0.553 屏宽起、「前往营地」更靠右；搜索区右界必须
        # 卡在 0.56 以内，让同排按钮连完整匹配窗口都放不进去
        _, _, x2, _ = wzry_auto.TEMPLATE_ROIS["dialog_confirm.png"]
        self.assertLessEqual(x2, 0.56)

    def test_dialog_confirm_absent_on_agree_popup(self):
        # 协议弹窗的「同意」也是同款蓝色按钮，但位置在 ROI 之外，不得误报
        # （误点「同意」无害，但说明 ROI 失守）
        result = wzry_auto.find_template(
            "dialog_confirm.png",
            str(ROOT / "assets" / "screenshots" / "3200x1440_agree_terms_popup.png"),
        )
        self.assertIsNone(result)

    def test_new_popup_templates_reusable_across_resolutions(self):
        # 新模板只存于 3200x1440 目录，其余设备靠高度比例预缩放复用。
        # 按「UI 随屏高等比缩放、宽度只是两侧留边」模拟现役另两档分辨率，
        # 实测 KPL✕ 0.987/0.994、确定钮 0.987/0.975，均远超阈值
        cases = [
            ("close_popup_kpl.png", "3200x1440_kpl_live_popup.png", (2807, 157)),
            ("dialog_confirm.png", "3200x1440_rest_reminder_popup.png", (1595, 928)),
        ]
        for width, height in ((2510, 1156), (2400, 1080)):
            scale = height / 1440
            for template, shot, (src_x, src_y) in cases:
                img = wzry_auto.cv_imread(
                    ROOT / "assets" / "screenshots" / shot
                )
                import cv2
                scaled = cv2.resize(
                    img, (round(img.shape[1] * scale), height),
                    interpolation=cv2.INTER_AREA,
                )
                crop_x = (scaled.shape[1] - width) // 2
                sim = scaled[:, crop_x:crop_x + width]
                with tempfile.TemporaryDirectory() as directory:
                    screenshot = Path(directory) / "sim.png"
                    wzry_auto.cv_imwrite(screenshot, sim)
                    result = wzry_auto.find_template(template, str(screenshot))
                label = f"{template} @ {width}x{height}"
                self.assertIsNotNone(result, label)
                self.assertAlmostEqual(
                    result["x"], src_x * scale - crop_x, delta=4, msg=label
                )
                self.assertAlmostEqual(
                    result["y"], src_y * scale, delta=4, msg=label
                )

    def test_lobby_popup_closers_cover_new_popups(self):
        # 步骤3/4/5 共用的弹窗关闭清单必须包含两类新弹窗
        self.assertIn("close_popup_kpl.png", wzry_auto.LOBBY_POPUP_CLOSERS)
        self.assertIn("dialog_confirm.png", wzry_auto.LOBBY_POPUP_CLOSERS)

    def test_agree_terms_matches_at_real_position(self):
        # 协议条款更新弹窗只有「拒绝/同意」两键、无 ✕；模板取自 3200x1440
        # 实机截图，「同意」按钮中心位于 (1851,1089)
        template = wzry_auto.cv_imread(
            ROOT / "assets" / "templates" / "3200x1440" / "agree_terms.png"
        )
        th, tw = template.shape[:2]
        canvas = np.zeros((1440, 3200, 3), dtype=np.uint8)
        canvas[1038:1038 + th, 1630:1630 + tw] = template
        with tempfile.TemporaryDirectory() as directory:
            screenshot = Path(directory) / "agree.png"
            wzry_auto.cv_imwrite(screenshot, canvas)
            result = wzry_auto.find_template("agree_terms.png", str(screenshot))
        self.assertIsNotNone(result)
        self.assertEqual(
            (result["x"], result["y"]), (1630 + tw // 2, 1038 + th // 2)
        )

    def test_agree_roi_excludes_refuse_button(self):
        # 「拒绝」按钮占 0.35~0.49 屏宽，搜索区左界必须足够靠右，
        # 保证「拒绝」永远无法被完整框进搜索区而误点
        x1, _, _, _ = wzry_auto.TEMPLATE_ROIS["agree_terms.png"]
        self.assertGreaterEqual(x1, 0.45)

    def test_back_arrow_matches_at_real_position(self):
        # 登录后可能盖全屏活动页（如回归福利），无 ✕ 只有左上角返回箭头；
        # 模板取自 3200x1440 实机截图，箭头中心位于 (307,76)
        template = wzry_auto.cv_imread(
            ROOT / "assets" / "templates" / "3200x1440" / "back_arrow.png"
        )
        th, tw = template.shape[:2]
        canvas = np.zeros((1440, 3200, 3), dtype=np.uint8)
        canvas[40:40 + th, 236:236 + tw] = template
        with tempfile.TemporaryDirectory() as directory:
            screenshot = Path(directory) / "activity.png"
            wzry_auto.cv_imwrite(screenshot, canvas)
            result = wzry_auto.find_template("back_arrow.png", str(screenshot))
        self.assertIsNotNone(result)
        self.assertEqual(
            (result["x"], result["y"]), (236 + tw // 2, 40 + th // 2)
        )

    def test_back_arrow_generalizes_across_backgrounds(self):
        # 新版 UI 各页面共用同款返回箭头：农场页参考截图背景不同，
        # 也必须达标，证明活动页背景变化不影响识别
        result = wzry_auto.find_template(
            "back_arrow.png",
            str(ROOT / "assets" / "screenshots" / "3200x1440_farm_statue.png"),
        )
        self.assertIsNotNone(result)

    def test_back_arrow_absent_on_login_page(self):
        # 登录页没有返回箭头，不得误报（步骤3等待大厅时会点它）
        result = wzry_auto.find_template(
            "back_arrow.png",
            str(ROOT / "assets" / "screenshots" / "3200x1440_login.png"),
        )
        self.assertIsNone(result)

    def test_dedicated_template_scales_are_bounded(self):
        # 精确匹配的分辨率目录：预测比例为 1，尺度限制在 ±10%
        scales = wzry_auto._template_scales(2400, 1080, (2400, 1080))
        self.assertEqual(scales, [0.9, 0.95, 1.0, 1.05, 1.1])

    def test_cross_resolution_scales_follow_height_ratio(self):
        # 跨分辨率目录：按截图高度/模板源高度预缩放（2510x1156 ← 2400x1080）
        scales = wzry_auto._template_scales(2510, 1156, (2400, 1080))
        predicted = 1156 / 1080
        self.assertIn(round(predicted, 3), scales)
        self.assertEqual(len(scales), 5)
        self.assertLess(max(scales), predicted * 1.2)
        self.assertGreater(min(scales), predicted * 0.8)

    def test_template_dirs_prefer_exact_then_nearest_height(self):
        dirs = [d.name for d, _ in wzry_auto._template_dirs(2510, 1156)]
        self.assertEqual(dirs[0], "2400x1080")  # 高度 1080 比 1440 更接近 1156
        self.assertEqual(dirs[-1], "templates")  # 默认目录兜底


class AdbTests(unittest.TestCase):
    @patch("wzry_auto.subprocess.run")
    def test_adb_command_does_not_use_host_shell(self, run):
        run.return_value = subprocess.CompletedProcess([], 0, "device\n", "")
        previous = wzry_auto.DEVICE
        try:
            wzry_auto.DEVICE = "example:5555"
            wzry_auto.adb_command("get-state")
        finally:
            wzry_auto.DEVICE = previous

        args, kwargs = run.call_args
        self.assertEqual(
            args[0][-3:],
            ["-s", "example:5555", "get-state"],
        )
        self.assertNotIn("shell", kwargs)


class WirelessReconnectTests(unittest.TestCase):
    @staticmethod
    def _completed(returncode=0, stdout=""):
        return subprocess.CompletedProcess([], returncode, stdout, "")

    @patch("wzry_auto.time.sleep")
    @patch("wzry_auto.subprocess.run")
    def test_wireless_device_reconnects_after_drop(self, run, _sleep):
        run.side_effect = [
            self._completed(1),                                       # get-state 掉线
            self._completed(0),                                       # disconnect
            self._completed(0, "connected to 192.168.1.10:5555\n"),   # connect
            self._completed(0, "device\n"),                           # get-state 确认
        ]
        previous = wzry_auto.DEVICE
        try:
            wzry_auto.DEVICE = "192.168.1.10:5555"
            self.assertTrue(
                wzry_auto.ensure_device_connected(max_attempts=2, retry_interval=0)
            )
        finally:
            wzry_auto.DEVICE = previous
        self.assertEqual(run.call_count, 4)
        connect_args = run.call_args_list[2][0][0]
        self.assertEqual(connect_args[-2:], ["connect", "192.168.1.10:5555"])

    @patch("wzry_auto.time.sleep")
    @patch("wzry_auto.subprocess.run")
    def test_usb_device_offline_fails_without_connect(self, run, _sleep):
        run.return_value = self._completed(1)
        previous = wzry_auto.DEVICE
        try:
            wzry_auto.DEVICE = "USB1234"
            self.assertFalse(wzry_auto.ensure_device_connected(max_attempts=3))
        finally:
            wzry_auto.DEVICE = previous
        self.assertEqual(run.call_count, 1)  # 只查了一次状态，USB 设备不该尝试 connect


class WirelessGuiHelperTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        try:
            import wzry_gui
        except Exception as exc:  # 无 GUI 依赖的环境跳过
            raise unittest.SkipTest(f"GUI 依赖不可用: {exc}")
        cls.gui_module = wzry_gui
        cls.FarmGui = wzry_gui.FarmGui

    def test_crop_cycle_options_cover_supported_tiers(self):
        self.assertEqual(
            dict(self.gui_module.CROP_CYCLE_OPTIONS),
            {"1 小时": 60, "8 小时": 480, "16 小时": 960, "32 小时": 1920},
        )

    def test_wake_lead_options_match_core_default(self):
        values = dict(self.gui_module.WAKE_LEAD_OPTIONS)
        # 界面默认项必须就是挂机核心的默认提前量，否则两边会各说一套
        self.assertEqual(
            values[self.gui_module.WAKE_LEAD_DEFAULT_TEXT],
            wzry_auto.DEFAULT_WAKE_LEAD_MIN,
        )
        self.assertEqual(
            self.gui_module.WAKE_LEAD_DEFAULT, wzry_auto.DEFAULT_WAKE_LEAD_MIN
        )
        for text, minutes in values.items():
            with self.subTest(option=text):
                self.assertGreaterEqual(minutes, 0)
                self.assertLessEqual(minutes, wzry_auto.MAX_WAKE_LEAD_MIN)
                # 下拉框选项要能原样被核心解析回同一个数
                with patch.dict(
                    os.environ, {"WZRY_WAKE_LEAD_MIN": f"{minutes:g}"}
                ):
                    self.assertEqual(wzry_auto.wake_lead_minutes(), minutes)

    def test_normalize_appends_default_port(self):
        self.assertEqual(
            self.FarmGui._normalize_wireless_addr("192.168.1.5"), "192.168.1.5:5555"
        )
        self.assertEqual(
            self.FarmGui._normalize_wireless_addr(" 192.168.1.5:40001 "),
            "192.168.1.5:40001",
        )
        self.assertEqual(self.FarmGui._normalize_wireless_addr(""), "")

    def test_parse_wlan_ip_prefers_wlan_interface(self):
        route = (
            "10.0.0.0/24 dev rmnet0 proto kernel scope link src 10.0.0.5\n"
            "192.168.1.0/24 dev wlan0 proto kernel scope link src 192.168.1.100\n"
        )
        self.assertEqual(self.FarmGui._parse_wlan_ip(route), "192.168.1.100")

    def test_parse_wlan_ip_falls_back_to_any_src(self):
        route = "172.16.0.0/16 dev eth0 proto kernel scope link src 172.16.0.9\n"
        self.assertEqual(self.FarmGui._parse_wlan_ip(route), "172.16.0.9")

    def test_parse_adb_devices_skips_header_and_blank_lines(self):
        output = (
            "List of devices attached\n"
            "USB1234\tdevice\n"
            "192.168.1.10:5555\toffline\n"
            "\n"
        )
        self.assertEqual(
            self.FarmGui._parse_adb_devices(output),
            [("USB1234", "device"), ("192.168.1.10:5555", "offline")],
        )

    def test_choose_device_prefers_filled_address(self):
        rows = [("USB1234", "device"), ("192.168.1.10:5555", "device")]
        self.assertEqual(
            self.FarmGui._choose_device(rows, "192.168.1.10:5555"),
            ("192.168.1.10:5555", "device"),
        )
        # 填了地址但不在线：状态为 None 表示未连接
        self.assertEqual(
            self.FarmGui._choose_device([], "192.168.1.10:5555"),
            ("192.168.1.10:5555", None),
        )

    def test_choose_device_auto_prefers_usb(self):
        rows = [("192.168.1.10:5555", "device"), ("USB1234", "device")]
        self.assertEqual(
            self.FarmGui._choose_device(rows, ""), ("USB1234", "device")
        )
        # 只有无线设备时展示无线设备；空列表返回 (None, None)
        self.assertEqual(
            self.FarmGui._choose_device([("192.168.1.10:5555", "device")], ""),
            ("192.168.1.10:5555", "device"),
        )
        self.assertEqual(self.FarmGui._choose_device([], ""), (None, None))


class HarvestParsingTests(unittest.TestCase):
    def test_exp_with_wan_unit_and_decimal(self):
        self.assertEqual(wzry_auto.parse_harvest_exp("XP 27.50万 农场经验"), 275000)

    def test_exp_without_xp_label_falls_back_to_farm_exp(self):
        self.assertEqual(
            wzry_auto.parse_harvest_exp("27.50万 农场经验 梨子 407"), 275000
        )

    def test_exp_plain_integer(self):
        self.assertEqual(wzry_auto.parse_harvest_exp("XP 407 农场经验"), 407)

    def test_exp_absent(self):
        self.assertEqual(wzry_auto.parse_harvest_exp("恭喜您获得 梨子 407"), 0)


class MaturityParsingTests(unittest.TestCase):
    def test_relative_minutes(self):
        self.assertEqual(wzry_auto.parse_relative_maturity("17分钟后成熟"), 17)

    def test_relative_hours_and_minutes(self):
        self.assertEqual(wzry_auto.parse_relative_maturity("1小时30分钟后成熟"), 90)

    def test_relative_hours_only(self):
        self.assertEqual(wzry_auto.parse_relative_maturity("2小时后成熟"), 120)

    def test_relative_seconds_rounds_up(self):
        self.assertEqual(wzry_auto.parse_relative_maturity("45秒后成熟"), 1)

    def test_absolute_time_not_matched(self):
        self.assertIsNone(wzry_auto.parse_relative_maturity("18:25成熟"))

    @staticmethod
    def _read_maturity_with(ocr_text, fake_now):
        """用假 OCR 结果与固定当前时间驱动 read_maturity_time。"""

        class _FixedDatetime(datetime):
            @classmethod
            def now(cls, tz=None):
                return cls.fromtimestamp(fake_now.timestamp(), tz)

        fake_engine = lambda roi: ([[None, ocr_text, 0.9]], 0.1)
        img = np.zeros((900, 2400, 3), dtype=np.uint8)
        with patch.object(wzry_auto, "datetime", _FixedDatetime), \
             patch.object(wzry_auto, "get_ocr", return_value=fake_engine), \
             patch.object(wzry_auto, "cv_imread", return_value=img):
            return wzry_auto.read_maturity_time("fake.png")

    def test_cross_day_relative_maturity_keeps_date(self):
        # 回归：29小时19分钟（1759分钟）跨天，此前丢日期导致 32 小时作物被判成 8 小时
        fake_now = datetime(2026, 8, 26, 13, 7, 0)
        maturity_dt, is_mature = self._read_maturity_with("29小时19分钟后成熟", fake_now)
        self.assertFalse(is_mature)
        self.assertEqual(maturity_dt, fake_now + timedelta(minutes=1759))

    def test_absolute_time_past_midnight_rolls_to_next_day(self):
        fake_now = datetime(2026, 8, 26, 23, 50, 0)
        maturity_dt, _ = self._read_maturity_with("00：02成熟", fake_now)
        self.assertEqual(maturity_dt, datetime(2026, 8, 27, 0, 2, 0))

    def test_absolute_time_with_tomorrow_prefix(self):
        # "明天14:00" 晚于当前时刻也必须按次日处理
        fake_now = datetime(2026, 8, 26, 13, 0, 0)
        maturity_dt, _ = self._read_maturity_with("明天14:00成熟", fake_now)
        self.assertEqual(maturity_dt, datetime(2026, 8, 27, 14, 0, 0))

    def _calculate_water_time(self, cycle_min, remain_min):
        now = datetime(2026, 8, 26, 10, 0, 0)
        with tempfile.TemporaryDirectory() as directory:
            cycle_file = Path(directory) / "crop_cycle.json"
            cycle_file.write_text(
                json.dumps({"crop_name": "作物", "cycle_min": cycle_min}),
                encoding="utf-8",
            )
            with patch.object(wzry_auto, "CYCLE_FILE", str(cycle_file)), \
                 patch.dict(
                     wzry_auto.os.environ, {"WZRY_CROP_CYCLE_MIN": ""}
                 ):
                result = wzry_auto.calculate_next_water_time(
                    now + timedelta(minutes=remain_min), now=now
                )
        return now, result

    def test_water_reduction_nodes_for_all_crop_tiers(self):
        cases = {
            60: [(55, 35, 20), (30, 10, 20), (5, 1, 4)],
            480: [(440, 280, 160), (240, 80, 160), (40, 8, 32)],
            960: [(880, 560, 320), (480, 160, 320), (80, 16, 64)],
            1920: [(1760, 1120, 640), (960, 320, 640), (160, 32, 128)],
        }
        for cycle_min, stages in cases.items():
            for remain_min, node_min, wait_min in stages:
                with self.subTest(cycle_min=cycle_min, remain_min=remain_min):
                    now, result = self._calculate_water_time(
                        cycle_min, remain_min
                    )
                    self.assertEqual(result["tier_min"], cycle_min)
                    self.assertEqual(result["node_min"], node_min)
                    self.assertEqual(
                        result["next_water"], now + timedelta(minutes=wait_min)
                    )

    def test_new_crop_infers_and_saves_original_cycle(self):
        now = datetime(2026, 8, 26, 10, 0, 0)
        with tempfile.TemporaryDirectory() as directory:
            cycle_file = Path(directory) / "crop_cycle.json"
            with patch.object(wzry_auto, "CYCLE_FILE", str(cycle_file)):
                result = wzry_auto.calculate_next_water_time(
                    now + timedelta(minutes=440),
                    now=now,
                    save_if_fresh=True,
                )
                stored = json.loads(cycle_file.read_text(encoding="utf-8"))
        self.assertEqual(result["tier_min"], 480)
        self.assertEqual(result["node_min"], 280)
        self.assertEqual(stored["cycle_min"], 480)

    def test_original_tier_is_kept_after_remaining_time_drops(self):
        # 16小时作物第二次浇水后剩480分钟，仍须使用16小时档节点160，
        # 不能按剩余时间错误切换到8小时档。
        now, result = self._calculate_water_time(960, 480)
        self.assertEqual(result["tier_min"], 960)
        self.assertEqual(result["node_min"], 160)
        self.assertEqual(result["next_water"], now + timedelta(minutes=320))

    def test_gui_selected_cycle_overrides_stored_cycle(self):
        now = datetime(2026, 8, 26, 10, 0, 0)
        with tempfile.TemporaryDirectory() as directory:
            cycle_file = Path(directory) / "crop_cycle.json"
            cycle_file.write_text(
                json.dumps({"crop_name": "作物", "cycle_min": 480}),
                encoding="utf-8",
            )
            with patch.object(wzry_auto, "CYCLE_FILE", str(cycle_file)), \
                 patch.dict(
                     wzry_auto.os.environ,
                     {"WZRY_CROP_CYCLE_MIN": "960"},
                 ):
                result = wzry_auto.calculate_next_water_time(
                    now + timedelta(minutes=480), now=now
                )
        self.assertEqual(result["tier_min"], 960)
        self.assertEqual(result["node_min"], 160)

    def test_past_last_node_waits_for_mature(self):
        now, result = self._calculate_water_time(60, 0.5)
        self.assertIsNone(result["next_water"])
        self.assertEqual(
            result["mature_time"], now + timedelta(minutes=0.5)
        )


class StatsTests(unittest.TestCase):
    def test_round_lifecycle_persists_json(self):
        with tempfile.TemporaryDirectory() as directory:
            stats_file = str(Path(directory) / "stats.json")
            with patch.object(wzry_auto, "STATS_FILE", stats_file):
                tracker = wzry_auto.Stats()
                tracker.begin_round(1)
                tracker.add_harvest(exp=275000, crops={"梨子": 526})
                wake = datetime(2026, 8, 25, 15, 0, 0)
                tracker.set_next_wake(wake, wake + timedelta(minutes=2), "浇水")
                tracker.finish_round("完成")
                tracker.finish_round("中断退出")  # 已完成的轮次不应被覆盖
                data = json.loads(Path(stats_file).read_text(encoding="utf-8"))
                snapshot = (Path(directory) / "stats_data.js").read_text(encoding="utf-8")

        self.assertTrue(snapshot.startswith("window.STATS = "))
        self.assertEqual(data["totals"]["exp"], 275000)
        self.assertEqual(data["totals"]["crops"]["梨子"], 526)
        self.assertEqual(data["next_wake"]["reason"], "浇水")
        self.assertEqual(data["next_wake"]["wake"], "2026-08-25 15:00:00")
        record = data["rounds_log"][0]
        self.assertEqual(record["status"], "完成")
        self.assertEqual(record["exp"], 275000)
        self.assertEqual(record["next_wake"], "2026-08-25 15:00:00")

    def test_load_restores_totals_and_marks_stale_rounds(self):
        with tempfile.TemporaryDirectory() as directory:
            stats_file = str(Path(directory) / "stats.json")
            with patch.object(wzry_auto, "STATS_FILE", stats_file):
                first = wzry_auto.Stats()
                first.begin_round(3)
                first.add_harvest(exp=100, crops={"梨子": 7})
                # 不结束本轮，模拟上次会话异常退出

                second = wzry_auto.Stats()
                second.load()

        self.assertEqual(second.rounds, 3)
        self.assertEqual(second.harvests, 1)
        self.assertEqual(second.total_exp, 100)
        self.assertEqual(second.total_crops, {"梨子": 7})
        self.assertEqual(second.rounds_log[-1]["status"], "中断退出")


class PauseTests(unittest.TestCase):
    """暂停/恢复：闸口必须挡在动作之前，且停止时一定要放行。"""

    def tearDown(self):
        wzry_auto._paused.clear()
        wzry_auto._farm_now.clear()
        wzry_auto._idle_wait.clear()

    @staticmethod
    def _call_async(func, *args):
        """在后台线程里跑一个会被闸口挡住的调用，返回 (线程, 完成标志)。"""
        done = threading.Event()

        def run():
            func(*args)
            done.set()

        thread = threading.Thread(target=run, daemon=True)
        thread.start()
        return thread, done

    def _feed_watcher(self, *commands):
        """喂一串指令给 GUI 指令监听线程，跑到 EOF 为止；返回它打印的内容。"""
        script = "".join(f"{cmd}\n" for cmd in commands)
        buffer = io.StringIO()
        with contextlib.ExitStack() as stack:
            stack.enter_context(patch.dict(os.environ, {"WZRY_GUI": "1"}))
            stack.enter_context(
                patch.object(wzry_auto.sys, "stdin", io.StringIO(script))
            )
            raise_signal = stack.enter_context(
                patch.object(wzry_auto.signal, "raise_signal")
            )
            stack.enter_context(contextlib.redirect_stdout(buffer))
            wzry_auto._start_gui_stop_watcher()
            # 监听线程没有句柄可 join（且 StringIO 读到 EOF 快到抓不住），
            # 就等它走完最后一步——发信号；那之前的清理必然已经做完
            deadline = time.monotonic() + 5
            while not raise_signal.called and time.monotonic() < deadline:
                time.sleep(0.01)
        raise_signal.assert_called_once_with(wzry_auto.signal.SIGINT)
        return buffer.getvalue()

    def test_gate_blocks_until_resumed(self):
        wzry_auto._paused.set()
        with contextlib.redirect_stdout(io.StringIO()):
            thread, done = self._call_async(wzry_auto.pause_gate)
            self.assertFalse(done.wait(0.5), "暂停期间闸口不该放行")
            wzry_auto._paused.clear()
            self.assertTrue(done.wait(3), "恢复后闸口应立刻放行")
            thread.join(timeout=3)

    def test_input_injection_waits_behind_the_gate(self):
        """暂停后不能再有指令打到手机——闸口必须挡在 adb_command 之前。"""
        wzry_auto._paused.set()
        with patch("wzry_auto.adb_command") as adb_command:
            adb_command.return_value = subprocess.CompletedProcess([], 0, "", "")
            with contextlib.redirect_stdout(io.StringIO()):
                thread, done = self._call_async(wzry_auto.adb_input, "input tap 1 2")
                self.assertFalse(done.wait(0.5))
                adb_command.assert_not_called()
                wzry_auto._paused.clear()
                self.assertTrue(done.wait(3))
                thread.join(timeout=3)
            adb_command.assert_called_once()

    def test_pause_does_not_extend_the_wait(self):
        """作物成熟只认墙上时钟：暂停期间等待照常走完，恢复后不补等。"""
        wzry_auto._paused.set()
        with contextlib.redirect_stdout(io.StringIO()):
            thread, done = self._call_async(wzry_auto.wait_or_farm_now, 0.3)
            time.sleep(1.0)          # 等待时长早已走完，但闸口还挡着
            self.assertFalse(done.is_set())
            resumed_at = time.monotonic()
            wzry_auto._paused.clear()
            self.assertTrue(done.wait(3))
            thread.join(timeout=3)
        self.assertLess(time.monotonic() - resumed_at, 1.0)

    def test_stop_releases_the_gate(self):
        """暂停中点停止：必须先放行闸口，主线程才走得到清理与退出。"""
        output = self._feed_watcher("pause", "stop")
        self.assertFalse(wzry_auto._paused.is_set())
        self.assertIn("收到暂停指令", output)

    def test_pipe_eof_releases_the_gate(self):
        """助手被强杀时管道 EOF 同样要放行，别留下卡死的挂机进程。"""
        self._feed_watcher("pause")
        self.assertFalse(wzry_auto._paused.is_set())

    def test_resume_clears_the_gate(self):
        output = self._feed_watcher("pause", "resume")
        self.assertIn("收到恢复指令", output)

    def test_farm_now_rejected_while_paused(self):
        wzry_auto._idle_wait.set()
        output = self._feed_watcher("pause", "farm_now")
        self.assertFalse(wzry_auto._farm_now.is_set())
        self.assertIn("已驳回", output)

    def test_farm_now_still_accepted_when_not_paused(self):
        wzry_auto._idle_wait.set()
        self._feed_watcher("farm_now")
        self.assertTrue(wzry_auto._farm_now.is_set())


class KeepAwakeTests(unittest.TestCase):
    """挂机期间阻止电脑睡眠：等待是进程内倒计时，系统一睡就到点不醒。"""

    @staticmethod
    def _windows_env(keep_awake=""):
        """伪装成 Windows 并清掉开关，让测试在任何平台上结果一致。"""
        stack = contextlib.ExitStack()
        stack.enter_context(patch.object(wzry_auto.os, "name", "nt"))
        stack.enter_context(
            patch.dict(os.environ, {"WZRY_KEEP_AWAKE": keep_awake})
        )
        stack.enter_context(contextlib.redirect_stdout(io.StringIO()))
        return stack

    def test_request_keeps_system_up_but_lets_screen_sleep(self):
        with self._windows_env(),              patch.object(
                 wzry_auto, "_set_execution_state", return_value=True
             ) as call:
            self.assertTrue(wzry_auto.keep_system_awake())
        # 必须只有 CONTINUOUS|SYSTEM_REQUIRED：多带 ES_DISPLAY_REQUIRED
        # 会让屏幕常亮，挂机一整夜没人愿意
        call.assert_called_once_with(
            wzry_auto.ES_CONTINUOUS | wzry_auto.ES_SYSTEM_REQUIRED
        )

    def test_release_clears_the_request(self):
        with self._windows_env(),              patch.object(
                 wzry_auto, "_set_execution_state", return_value=True
             ) as call:
            wzry_auto.allow_system_sleep()
        call.assert_called_once_with(wzry_auto.ES_CONTINUOUS)

    def test_env_switch_disables_the_request(self):
        with self._windows_env(keep_awake="0"),              patch.object(wzry_auto, "_set_execution_state") as call:
            self.assertFalse(wzry_auto.keep_system_awake())
        call.assert_not_called()

    def test_non_windows_is_a_no_op(self):
        with patch.object(wzry_auto.os, "name", "posix"),              patch.dict(os.environ, {"WZRY_KEEP_AWAKE": ""}),              patch("ctypes.WinDLL", create=True) as windll:
            self.assertFalse(wzry_auto.keep_system_awake())
            wzry_auto.allow_system_sleep()
        windll.assert_not_called()

    def test_failed_request_does_not_stop_farming(self):
        with self._windows_env(),              patch.object(
                 wzry_auto, "_set_execution_state", return_value=False
             ):
            self.assertFalse(wzry_auto.keep_system_awake())


class DeviceArchiveTests(unittest.TestCase):
    """手机端存档：换电脑接力挂机，不再一连上设备就先白浇一次水。"""

    DEVICE_ID = "fef7b108fd526415"

    def setUp(self):
        # 存档相关的全局量与真实的统计/周期文件都要隔离，测试不能动用户数据
        for name, value in (
            ("DEVICE", "192.168.1.10:5555"),
            ("DEVICE_ID", self.DEVICE_ID),
            ("_archive_tier_min", None),
        ):
            patcher = patch.object(wzry_auto, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        for name in ("save_crop_cycle", "adb_input"):
            patcher = patch.object(wzry_auto, name)
            patcher.start()
            self.addCleanup(patcher.stop)
        tracker = patch.object(wzry_auto, "stats")
        self.stats = tracker.start()
        self.stats.rounds = 7
        self.addCleanup(tracker.stop)
        env = patch.dict(os.environ, {"WZRY_DEVICE_ARCHIVE": "1"})
        env.start()
        self.addCleanup(env.stop)

    @staticmethod
    def _archive(**overrides):
        data = {
            "schema": 1,
            "device_id": DeviceArchiveTests.DEVICE_ID,
            "host": "OTHER-PC",
            "tier_min": 960,
            "reason": "浇水",
            # 故意写一个早就过期的本机时间串：唤醒时刻应以手机时钟为准
            "wake_time": "2000-01-01 00:00:00",
            "wake_device_epoch": 1_758_403_600,
            "updated": "2026-09-21 09:00:00",
            "updated_device_epoch": 1_758_400_000,
        }
        data.update(overrides)
        return data

    def test_archive_write_carries_next_water_and_tier(self):
        wake = datetime.now() + timedelta(hours=2)
        result = {
            "tier_min": 480,
            "next_water": wake + timedelta(minutes=2),
            "mature_time": wake + timedelta(hours=1),
        }
        with patch.object(wzry_auto, "_device_epoch", return_value=1_758_400_000), \
             patch("wzry_auto.subprocess.run") as run:
            run.return_value = subprocess.CompletedProcess([], 0, b"", b"")
            self.assertTrue(
                wzry_auto.save_device_archive(
                    wake, wake + timedelta(minutes=2), "浇水", result
                )
            )

        argv, kwargs = run.call_args
        payload = json.loads(kwargs["input"].decode("utf-8"))
        self.assertEqual(payload["device_id"], self.DEVICE_ID)
        self.assertEqual(payload["tier_min"], 480)
        self.assertEqual(
            payload["next_water"],
            (wake + timedelta(minutes=2)).strftime("%Y-%m-%d %H:%M:%S"),
        )
        # 唤醒时刻换算到手机时钟上，换台电脑读回来不受两边系统时钟偏差影响
        self.assertAlmostEqual(
            payload["wake_device_epoch"], 1_758_400_000 + 7200, delta=2
        )
        # 回归：platform-tools 35+ 在含中文的本地路径下 pull/push 会失败，
        # 存档读写必须全程不碰本地文件（同 screenshot 的处理）
        self.assertNotIn("push", argv[0])
        self.assertNotIn("pull", argv[0])
        # 原子写：先落临时名再改名，中途断连不会留下半截存档
        self.assertIn("cat > ", argv[0][-1])
        self.assertIn("mv -f", argv[0][-1])

    def test_missing_archive_starts_fresh(self):
        # 文件不存在时设备端报错会并进 stdout 且退出码为 0，只能按内容判断
        missing = b"cat: /sdcard/wzry_farm/state.json: No such file or directory\n"
        with patch.object(wzry_auto, "_read_device_file", return_value=missing):
            self.assertIsNone(wzry_auto.load_device_archive())
            with patch.object(wzry_auto, "wait_or_farm_now") as wait:
                self.assertFalse(wzry_auto.restore_device_archive())
        wait.assert_not_called()

    def test_restore_waits_until_archived_water_time(self):
        with patch.object(
                 wzry_auto, "load_device_archive", return_value=self._archive()
             ), \
             patch.object(wzry_auto, "_device_epoch", return_value=1_758_400_000), \
             patch.object(wzry_auto, "wait_or_farm_now") as wait:
            self.assertTrue(wzry_auto.restore_device_archive())

        # 手机时钟距唤醒点还有 3600 秒，就等 3600 秒，而不是按过期的时间串立刻开干
        self.assertAlmostEqual(wait.call_args[0][0], 3600, delta=2)
        self.assertEqual(wzry_auto._archive_tier_min, 960)
        self.assertIn("浇水", self.stats.set_next_wake.call_args[0][2])

    def test_restore_falls_back_to_written_time_without_phone_clock(self):
        wake = datetime.now() + timedelta(minutes=30)
        archive = self._archive(
            wake_time=wake.strftime("%Y-%m-%d %H:%M:%S"),
            wake_device_epoch=None,
        )
        with patch.object(wzry_auto, "load_device_archive", return_value=archive), \
             patch.object(wzry_auto, "_device_epoch", return_value=None), \
             patch.object(wzry_auto, "wait_or_farm_now") as wait:
            self.assertTrue(wzry_auto.restore_device_archive())
        self.assertAlmostEqual(wait.call_args[0][0], 1800, delta=2)

    def test_expired_archive_farms_immediately(self):
        archive = self._archive(wake_device_epoch=1_758_390_000)
        with patch.object(wzry_auto, "load_device_archive", return_value=archive), \
             patch.object(wzry_auto, "_device_epoch", return_value=1_758_400_000), \
             patch.object(wzry_auto, "wait_or_farm_now") as wait:
            self.assertFalse(wzry_auto.restore_device_archive())
        wait.assert_not_called()
        # 档位仍按存档恢复，立刻务农的这一轮也不会用错节点
        self.assertEqual(wzry_auto._archive_tier_min, 960)

    def test_long_expired_archive_is_ignored_entirely(self):
        # 关掉开关挂了几天、或中途手动收过菜，陈年存档的档位不能再覆盖界面选择
        archive = self._archive(wake_device_epoch=1_758_400_000 - 25 * 3600)
        with patch.object(wzry_auto, "load_device_archive", return_value=archive), \
             patch.object(wzry_auto, "_device_epoch", return_value=1_758_400_000), \
             patch.object(wzry_auto, "wait_or_farm_now") as wait:
            self.assertFalse(wzry_auto.restore_device_archive())
        wait.assert_not_called()
        self.assertIsNone(wzry_auto._archive_tier_min)

    def test_reconnect_reread_waits_for_the_other_pc_water_time(self):
        """离线重连：手机刚在另一台电脑上挂过，接着等存档里的浇水点。

        过去重连后直接进下一轮务农，存档只在启动时读一次；手机在两地
        来回挂时，这一下就会在不该浇的时刻白浇一次水。
        """
        other = self._archive(host="OTHER-PC")
        with patch.object(wzry_auto, "load_device_archive", return_value=other),              patch.object(wzry_auto, "_device_epoch", return_value=1_758_400_000),              patch.object(wzry_auto, "wait_or_farm_now") as wait:
            self.assertTrue(wzry_auto.restore_device_archive(reread=True))
        self.assertAlmostEqual(wait.call_args[0][0], 3600, delta=2)

    def test_reconnect_reread_retries_flaky_read_before_giving_up(self):
        """刚重连上的 adb 抖一下不能算"没有存档"，否则照样白浇一次。"""
        payload = json.dumps(self._archive()).encode("utf-8")
        with patch.object(
                 wzry_auto, "_read_device_file", side_effect=[None, None, payload]
             ) as read,              patch.object(wzry_auto, "_device_epoch", return_value=1_758_400_000),              patch.object(wzry_auto.time, "sleep"),              patch.object(wzry_auto, "wait_or_farm_now") as wait:
            self.assertTrue(wzry_auto.restore_device_archive(reread=True))
        self.assertEqual(read.call_count, 3)
        self.assertAlmostEqual(wait.call_args[0][0], 3600, delta=2)

    def test_missing_file_is_not_retried(self):
        """文件确实不存在时读到的是 No such file（退出码 0），重试纯属浪费。"""
        missing = b"cat: /sdcard/wzry_farm/state.json: No such file or directory\n"
        with patch.object(
                 wzry_auto, "_read_device_file", return_value=missing
             ) as read:
            self.assertIsNone(wzry_auto.load_device_archive(retries=3))
        self.assertEqual(read.call_count, 1)

    def test_archive_of_another_phone_is_ignored(self):
        archive = self._archive(device_id="0123456789abcdef")
        with patch.object(wzry_auto, "load_device_archive", return_value=archive), \
             patch.object(wzry_auto, "wait_or_farm_now") as wait:
            self.assertFalse(wzry_auto.restore_device_archive())
        wait.assert_not_called()
        self.assertIsNone(wzry_auto._archive_tier_min)

    def test_switch_off_skips_archive_entirely(self):
        with patch.dict(os.environ, {"WZRY_DEVICE_ARCHIVE": "0"}), \
             patch.object(wzry_auto, "load_device_archive") as load, \
             patch.object(wzry_auto, "_write_device_file") as write:
            self.assertFalse(wzry_auto.restore_device_archive())
            self.assertFalse(
                wzry_auto.save_device_archive(datetime.now(), None, "浇水")
            )
        load.assert_not_called()
        write.assert_not_called()

    def test_archived_tier_beats_gui_selection(self):
        # 换电脑接力时，新电脑界面上残留的默认 8 小时档不能把 16 小时作物带偏
        now = datetime(2026, 8, 26, 10, 0, 0)
        with patch.object(wzry_auto, "_archive_tier_min", 960), \
             patch.dict(os.environ, {"WZRY_CROP_CYCLE_MIN": "480"}):
            result = wzry_auto.calculate_next_water_time(
                now + timedelta(minutes=480), now=now
            )
        self.assertEqual(result["tier_min"], 960)
        self.assertEqual(result["node_min"], 160)

    def test_device_id_falls_back_to_serial_number(self):
        with patch.object(wzry_auto, "adb_shell", side_effect=["null\n", "YP9TU\n"]):
            self.assertEqual(wzry_auto.get_device_id(), "YP9TU")

    def test_device_id_prefers_android_id_over_wireless_address(self):
        # 无线地址 ip:port 会随路由器重新分配而改变，不能拿来当身份
        with patch.object(wzry_auto, "adb_shell", return_value=f"{self.DEVICE_ID}\n"):
            self.assertEqual(wzry_auto.get_device_id(), self.DEVICE_ID)


class WakeLeadTests(unittest.TestCase):
    """唤醒提前量：留给"启动游戏→进农场→走到土地"这段路的时间，可配置。"""

    @staticmethod
    def _lead(raw):
        with patch.dict(os.environ, {"WZRY_WAKE_LEAD_MIN": raw}),              contextlib.redirect_stdout(io.StringIO()):
            return wzry_auto.wake_lead_minutes()

    def test_default_is_one_minute(self):
        self.assertEqual(wzry_auto.DEFAULT_WAKE_LEAD_MIN, 1.0)
        self.assertEqual(self._lead(""), 1.0)

    def test_accepts_fraction_and_zero(self):
        self.assertEqual(self._lead("1.5"), 1.5)
        self.assertEqual(self._lead("0"), 0.0)

    def test_out_of_range_is_clamped(self):
        self.assertEqual(self._lead("-3"), 0.0)
        self.assertEqual(self._lead("99"), wzry_auto.MAX_WAKE_LEAD_MIN)

    def test_garbage_falls_back_to_default(self):
        self.assertEqual(self._lead("一分钟"), 1.0)

    @staticmethod
    def _run_step10(lead, node_min=1):
        """跑一遍步骤10，返回 (唤醒时刻, 节点时刻, 记录下来的参数)。"""
        now = datetime.now().replace(microsecond=0)
        mature = now + timedelta(minutes=30)
        next_water = mature - timedelta(minutes=node_min)
        result = {
            "tier_min": 60, "node_min": node_min,
            "next_water": next_water, "mature_time": mature,
        }
        with contextlib.ExitStack() as stack:
            stack.enter_context(
                patch.dict(os.environ, {"WZRY_WAKE_LEAD_MIN": lead})
            )
            for name in (
                "random_screen_fiddle", "adb_shell", "_reapply_low_brightness",
            ):
                stack.enter_context(patch.object(wzry_auto, name))
            record = stack.enter_context(
                patch.object(wzry_auto, "record_next_wake")
            )
            stack.enter_context(contextlib.redirect_stdout(io.StringIO()))
            wake = wzry_auto.step10_calculate_wait(result, mature)
        return wake, next_water, record.call_args[0]

    def test_step10_wakes_ahead_by_configured_lead(self):
        wake, node_at, recorded = self._run_step10("1.5")
        self.assertEqual(wake, node_at - timedelta(minutes=1.5))
        # 面板与手机存档记的是"唤醒时刻 + 未打折的节点时刻"
        self.assertEqual(recorded[0], wake)
        self.assertEqual(recorded[1], node_at)

    def test_step10_zero_lead_starts_right_on_the_node(self):
        wake, node_at, recorded = self._run_step10("0")
        self.assertEqual(wake, node_at)
        self.assertEqual(recorded[1], node_at)


class StatueApproachTests(unittest.TestCase):
    """走到石像跟前这一段：刷新站位的语义、补步重试、推杆参数可调。"""

    def test_refresh_button_shows_even_when_walked_past_the_statue(self):
        # 回归：refresh_pos 曾被当成"站在石盘上"的判据，可它在农场里始终
        # 可见——人走过头站到土地上照样匹配得到。判据失真的后果是步骤7失败
        # 后从不刷新站位，直接在落点上再推一次杆，越走越远
        shot = str(ROOT / "assets" / "screenshots" / "juesezhanzaitudishang.png")
        with contextlib.redirect_stdout(io.StringIO()):
            refresh = wzry_auto.find_template("refresh_pos.png", shot)
            oneclick = wzry_auto.find_template("oneclick_farm.png", shot)
        self.assertIsNotNone(refresh, "走到土地上时刷新站位按钮仍在")
        self.assertIsNone(oneclick, "离开石像后一键务农就不弹了")

    @staticmethod
    def _walk(reset_first):
        """跑一遍步骤6，返回 (是否刷新过站位, 推杆调用列表)。"""
        with contextlib.ExitStack() as stack:
            reset = stack.enter_context(
                patch.object(wzry_auto, "reset_position", return_value=True)
            )
            move = stack.enter_context(patch.object(wzry_auto, "move_joystick"))
            stack.enter_context(
                patch.object(wzry_auto, "in_farm_scene", return_value=True)
            )
            stack.enter_context(patch.object(wzry_auto.time, "sleep"))
            stack.enter_context(contextlib.redirect_stdout(io.StringIO()))
            wzry_auto.step6_move_to_statue(reset_first=reset_first)
        return reset.called, move.call_args_list

    def test_first_walk_starts_from_the_platform_without_resetting(self):
        reset_called, moves = self._walk(reset_first=False)
        self.assertFalse(reset_called)
        self.assertEqual(len(moves), 1)

    def test_retry_refreshes_position_before_walking_again(self):
        reset_called, moves = self._walk(reset_first=True)
        self.assertTrue(reset_called, "重走前必须先把人拉回石盘")
        self.assertEqual(len(moves), 1)

    @staticmethod
    def _farm(hits, nudge=True):
        """跑一遍步骤7，has_template 依次返回 hits；返回 (成功?, 推杆调用)。"""
        seen = list(hits)
        with contextlib.ExitStack() as stack:
            stack.enter_context(patch.object(wzry_auto, "screenshot"))
            stack.enter_context(
                patch.object(wzry_auto, "has_template", side_effect=seen)
            )
            stack.enter_context(
                patch.object(wzry_auto, "click_template", return_value=True)
            )
            stack.enter_context(patch.object(wzry_auto, "save_diagnostic"))
            move = stack.enter_context(patch.object(wzry_auto, "move_joystick"))
            stack.enter_context(patch.object(wzry_auto.time, "sleep"))
            stack.enter_context(contextlib.redirect_stdout(io.StringIO()))
            ok, _ = wzry_auto.step7_oneclick_farm(nudge=nudge)
        return ok, move.call_args_list

    def test_button_found_at_once_needs_no_nudge(self):
        ok, moves = self._farm([True])
        self.assertTrue(ok)
        self.assertEqual(moves, [])

    def test_a_few_steps_short_is_recovered_by_nudging(self):
        # 第一眼没弹，补一小步就出来了——这一轮不该白白重启游戏
        ok, moves = self._farm([False, True])
        self.assertTrue(ok)
        self.assertEqual(len(moves), 1)

    def test_nudging_stops_after_the_configured_tries(self):
        misses = [False] * (1 + len(wzry_auto.STEP7_NUDGE_RATIOS))
        ok, moves = self._farm(misses)
        self.assertFalse(ok)
        self.assertEqual(len(moves), len(wzry_auto.STEP7_NUDGE_RATIOS))

    def test_each_nudge_is_shorter_than_a_full_walk(self):
        # 补步得是"挪一点"，跟步骤6一样长就等于再走一整段，直接冲过石像
        full = wzry_auto._step6_cfg["duration"]
        ok, moves = self._farm([False] * (1 + len(wzry_auto.STEP7_NUDGE_RATIOS)))
        self.assertFalse(ok)
        for call in moves:
            self.assertLess(call.args[2], full)

    def test_second_attempt_after_refresh_does_not_nudge(self):
        # 刷新站位后已经从石盘完整重走过，再补步只会越补越远
        ok, moves = self._farm([False], nudge=False)
        self.assertFalse(ok)
        self.assertEqual(moves, [])


class Step6EnvOverrideTests(unittest.TestCase):
    """推杆参数可用环境变量微调，不必改代码重新打包。"""

    BASE = {"center": (400, 972), "angle": 120, "distance": 400, "duration": 1500}

    def _tuned(self, **env):
        with patch.dict(os.environ, env), contextlib.redirect_stdout(io.StringIO()):
            return wzry_auto.apply_step6_env_overrides(self.BASE)

    def test_no_env_keeps_resolution_defaults(self):
        self.assertEqual(self._tuned(), self.BASE)

    def test_longer_hold_walks_further(self):
        self.assertEqual(self._tuned(WZRY_STEP6_DURATION="1800")["duration"], 1800)

    def test_angle_and_distance_are_tunable(self):
        tuned = self._tuned(WZRY_STEP6_ANGLE="115", WZRY_STEP6_DISTANCE="450")
        self.assertEqual((tuned["angle"], tuned["distance"]), (115, 450))

    def test_garbage_and_out_of_range_keep_the_default(self):
        self.assertEqual(self._tuned(WZRY_STEP6_DURATION="久一点")["duration"], 1500)
        self.assertEqual(self._tuned(WZRY_STEP6_DURATION="99999")["duration"], 1500)
        self.assertEqual(self._tuned(WZRY_STEP6_DISTANCE="0")["distance"], 400)

    def test_override_does_not_mutate_the_input(self):
        self._tuned(WZRY_STEP6_DURATION="1800")
        self.assertEqual(self.BASE["duration"], 1500)


class DiagnosticRetentionTests(unittest.TestCase):
    """失败现场保留上限：一夜失败循环不该把盘写满，也不该冲掉稀有现场。"""

    @staticmethod
    def _make(root, names):
        """在临时 diagnostics 下造出这些现场目录。"""
        folder = root / "diagnostics"
        folder.mkdir(exist_ok=True)
        for name in names:
            (folder / name).mkdir()
        return folder

    @staticmethod
    def _stamped(step, count, day="20260826"):
        """同一步骤的连号现场，名字里的时间戳即新旧顺序。"""
        return [f"{day}_{10000 + i:06d}_{step}" for i in range(count)]

    @contextlib.contextmanager
    def _sandbox(self, names):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            folder = self._make(root, names)
            with patch.object(wzry_auto, "SCRIPT_DIR", root), \
                 contextlib.redirect_stdout(io.StringIO()):
                yield folder

    def test_under_the_cap_nothing_is_removed(self):
        names = self._stamped("step2_launch", 10)
        with self._sandbox(names) as folder:
            self.assertEqual(wzry_auto.prune_diagnostics(), 0)
            self.assertEqual(len(list(folder.iterdir())), 10)

    def test_over_the_cap_is_trimmed_back_to_it(self):
        names = self._stamped("step2_launch", wzry_auto.MAX_DIAGNOSTIC_DIRS + 7)
        with self._sandbox(names) as folder:
            self.assertEqual(wzry_auto.prune_diagnostics(), 7)
            self.assertEqual(
                len(list(folder.iterdir())), wzry_auto.MAX_DIAGNOSTIC_DIRS
            )

    def test_a_flood_of_one_step_cannot_evict_the_rare_ones(self):
        # 2026-09-21 定位步骤7的 bug 靠的就是被 505 个 step2_launch 埋着的
        # 5 个 step7_oneclick；一律删全局最旧会把它们先冲掉
        rare = self._stamped("step7_oneclick", 5, day="20260825")
        flood = self._stamped("step2_launch", 505, day="20260826")
        with self._sandbox(rare + flood) as folder:
            wzry_auto.prune_diagnostics()
            left = sorted(p.name for p in folder.iterdir())
        self.assertTrue(set(rare).issubset(left), "稀有现场必须留住")
        self.assertEqual(len(left), wzry_auto.MAX_DIAGNOSTIC_DIRS)

    def test_within_a_step_the_oldest_go_first(self):
        names = self._stamped("step2_launch", wzry_auto.MAX_DIAGNOSTIC_DIRS + 3)
        with self._sandbox(names) as folder:
            wzry_auto.prune_diagnostics()
            left = sorted(p.name for p in folder.iterdir())
        self.assertEqual(left, names[3:])

    def test_hand_made_folders_are_left_alone(self):
        names = self._stamped("step2_launch", wzry_auto.MAX_DIAGNOSTIC_DIRS + 5)
        with self._sandbox(names + ["我自己放的参考图"]) as folder:
            wzry_auto.prune_diagnostics()
            left = sorted(p.name for p in folder.iterdir())
        self.assertIn("我自己放的参考图", left)
        # 上限只管现场目录，人手目录不占额度
        self.assertEqual(len(left), wzry_auto.MAX_DIAGNOSTIC_DIRS + 1)

    def test_undeletable_folder_does_not_break_the_run(self):
        names = self._stamped("step2_launch", wzry_auto.MAX_DIAGNOSTIC_DIRS + 2)
        with self._sandbox(names):
            with patch.object(
                wzry_auto.shutil, "rmtree", side_effect=OSError("被占用")
            ):
                self.assertEqual(wzry_auto.prune_diagnostics(), 0)

    def test_saving_a_capture_also_trims(self):
        names = self._stamped("step2_launch", wzry_auto.MAX_DIAGNOSTIC_DIRS)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            folder = self._make(root, names)
            with patch.object(wzry_auto, "SCRIPT_DIR", root), \
                 patch.object(wzry_auto, "SCREENSHOT_PATH", str(root / "none.png")), \
                 contextlib.redirect_stdout(io.StringIO()):
                fresh = wzry_auto.save_diagnostic("step7_oneclick")
            left = sorted(p.name for p in folder.iterdir())
        self.assertEqual(len(left), wzry_auto.MAX_DIAGNOSTIC_DIRS)
        self.assertIn(fresh.name, left, "刚存的现场不能被自己挤掉")


if __name__ == "__main__":
    unittest.main()
