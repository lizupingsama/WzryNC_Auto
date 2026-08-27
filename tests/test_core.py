import json
import subprocess
import tempfile
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
        cls.FarmGui = wzry_gui.FarmGui

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

    def test_cross_day_remaining_matches_32h_crop(self):
        with tempfile.TemporaryDirectory() as directory:
            cycle_file = Path(directory) / "crop_cycle.json"
            with patch.object(wzry_auto, "CYCLE_FILE", str(cycle_file)):
                first_water = datetime(2026, 8, 26, 13, 7, 0)
                result = wzry_auto.calculate_plant_cycle_and_water_time(
                    first_water, first_water + timedelta(minutes=1759),
                    save_if_fresh=True,
                )
        self.assertEqual(result["plant_cycle_min"], 1920)

    def test_stale_stored_cycle_is_ignored(self):
        with tempfile.TemporaryDirectory() as directory:
            cycle_file = Path(directory) / "crop_cycle.json"
            cycle_file.write_text(
                json.dumps({"crop_name": "作物", "cycle_min": 60}),
                encoding="utf-8",
            )
            with patch.object(wzry_auto, "CYCLE_FILE", str(cycle_file)):
                first_water = datetime(2026, 8, 25, 10, 0, 0)
                # 剩余300分钟 > 存储周期60分钟，应忽略存储值重新匹配到480
                result = wzry_auto.calculate_plant_cycle_and_water_time(
                    first_water, first_water + timedelta(minutes=300)
                )
        self.assertEqual(result["plant_cycle_min"], 480)


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


if __name__ == "__main__":
    unittest.main()
