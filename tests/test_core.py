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
        self.assertGreaterEqual(x1, 0.75)

    def test_dedicated_template_scales_are_bounded(self):
        scales = wzry_auto._template_scales(
            ROOT / "assets" / "templates" / "2400x1080",
            2400,
            1080,
        )
        self.assertEqual(scales, [0.9, 0.95, 1.0, 1.05, 1.1])


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
