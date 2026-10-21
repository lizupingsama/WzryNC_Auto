"""文字入口的坐标转换、点击流程和真实截图 OCR 回归。"""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import cv2
import numpy as np
import wzry_auto as w


SHOTS = Path(__file__).resolve().parents[1] / "assets" / "screenshots"


def row(text, score=0.99, x=20, y=30):
    return [[[x, y], [x + 100, y], [x + 100, y + 40], [x, y + 40]], text, score]


class TextButtonTests(unittest.TestCase):
    def setUp(self):
        image = np.zeros((1000, 2000, 3), dtype=np.uint8)
        self.image_patch = patch.object(w, "cv_imread", return_value=image)
        self.image_patch.start()
        self.addCleanup(self.image_patch.stop)

    def test_coordinates_include_roi_offset(self):
        with patch.object(w, "get_ocr", return_value=lambda image: ([row("开始游戏")], None)):
            result = w.find_text_button("start_game", "screen.png")
        self.assertEqual((result["x"], result["y"]), (570, 630))

    def test_whitespace_punctuation_and_english_subtitle_are_accepted(self):
        for text in ("开 始 游 戏", "开始游戏 START", "开始游戏！"):
            with self.subTest(text=text), patch.object(
                w, "get_ocr", return_value=lambda image: ([row(text)], None)
            ):
                self.assertIsNotNone(w.find_text_button("start_game", "screen.png"))

    def test_low_confidence_and_partial_mentions_are_not_clicked(self):
        for item in (row("开始游戏", 0.5), row("开始游戏前请阅读"), row("前往王者农场领取奖励")):
            with self.subTest(text=item[1]), patch.object(
                w, "get_ocr", return_value=lambda image: ([item], None)
            ), patch.object(w, "tap") as tap:
                self.assertFalse(w.click_text_button("start_game", "screen.png"))
                self.assertFalse(w.click_text_button("enter_farm", "screen.png"))
                tap.assert_not_called()

    def test_farm_aliases_and_highest_confidence_position(self):
        rows = [row("来农场干农活", 0.9), row("王者农场", 0.99, x=200)]
        with patch.object(w, "get_ocr", return_value=lambda image: (rows, None)), \
             patch.object(w, "tap") as tap:
            self.assertTrue(w.click_text_button("enter_farm", "screen.png"))
        tap.assert_called_once_with(550, 630, "王者农场")

    def test_ocr_failure_does_not_click(self):
        with patch.object(w, "get_ocr", side_effect=RuntimeError("OCR unavailable")), \
             patch.object(w, "tap") as tap:
            self.assertFalse(w.click_text_button("enter_farm", "screen.png"))
        tap.assert_not_called()

    def test_empty_ocr_result_does_not_click(self):
        with patch.object(w, "get_ocr", return_value=lambda image: (None, None)), \
             patch.object(w, "tap") as tap:
            self.assertFalse(w.click_text_button("start_game", "screen.png"))
        tap.assert_not_called()

    def test_missing_screenshot_does_not_run_ocr(self):
        with patch.object(w, "cv_imread", return_value=None), patch.object(w, "get_ocr") as ocr:
            self.assertIsNone(w.find_text_button("start_game", "screen.png"))
        ocr.assert_not_called()

    def test_launch_login_and_lobby_checks_all_use_ocr(self):
        responses = iter([
            ([row("开始游戏")], None),   # 步骤2：登录页已就绪
            ([row("开始游戏")], None),   # 步骤2b：启动弹窗已关完
            ([row("开始游戏")], None),   # 步骤3：点击登录
            ([row("王者农场")], None),   # 步骤3：大厅已就绪
            ([row("王者农场")], None),   # 步骤4：没有大厅弹窗
        ])
        with patch.object(w, "get_ocr", return_value=lambda image: next(responses)), \
             patch.object(w, "adb_shell"), patch.object(w, "screenshot"), \
             patch.object(w, "find_any_template", return_value=None), \
             patch.object(w, "click_template", return_value=False), \
             patch.object(w, "tap") as tap:
            self.assertTrue(w.step2_launch_game())
            self.assertTrue(w.step2b_close_startup_popups())
            self.assertTrue(w.step3_click_start_game())
            self.assertTrue(w.step4_close_popup())
        tap.assert_called_once_with(570, 630, "开始游戏")

    def test_enter_farm_closes_popup_before_ocr_click(self):
        actions = []
        popup = {"x": 1800, "y": 100, "template": "close_popup_event.png"}
        with patch.object(w, "screenshot"), \
             patch.object(w.time, "sleep"), \
             patch.object(w, "find_any_template", side_effect=[popup, None]), \
             patch.object(w, "get_ocr", return_value=lambda image: ([row("王者农场")], None)), \
             patch.object(w, "tap", side_effect=lambda x, y, label: actions.append((x, y))), \
             patch.object(w, "wait_for_page", return_value={"template": "refresh_pos.png"}):
            self.assertTrue(w.step5_enter_farm())
        self.assertEqual(actions, [(1800, 100), (370, 630)])


class RealTextButtonTests(unittest.TestCase):
    def test_new_and_old_login_and_farm_entry_text(self):
        cases = (
            ("start_game", "3200x1440_anniversary_login.png", (1600, 1125)),
            ("start_game", "3200x1440_login.png", (1590, 1120)),
            ("start_game", "wutanchuangzhuye.png", (635, 560)),
            ("enter_farm", "3200x1440_anniversary_lobby.png", (964, 1082)),
            ("enter_farm", "wutanchuangdating.png", (407, 470)),
        )
        for button, screenshot, (x, y) in cases:
            with self.subTest(button=button, screenshot=screenshot):
                result = w.find_text_button(button, str(SHOTS / screenshot))
                self.assertIsNotNone(result)
                self.assertAlmostEqual(result["x"], x, delta=12)
                self.assertAlmostEqual(result["y"], y, delta=12)

    def test_anniversary_buttons_across_resolutions(self):
        cases = (
            ("start_game", "3200x1440_anniversary_login.png", (1600, 1125)),
            ("enter_farm", "3200x1440_anniversary_lobby.png", (964, 1082)),
        )
        for width, height in ((2510, 1156), (2400, 1080)):
            scale = height / 1440
            for button, screenshot, (x, y) in cases:
                image = w.cv_imread(SHOTS / screenshot)
                scaled = cv2.resize(
                    image, (round(image.shape[1] * scale), height),
                    interpolation=cv2.INTER_AREA,
                )
                crop_x = (scaled.shape[1] - width) // 2
                with self.subTest(button=button, width=width), tempfile.TemporaryDirectory() as tmp:
                    path = Path(tmp) / "screen.png"
                    w.cv_imwrite(path, scaled[:, crop_x:crop_x + width])
                    result = w.find_text_button(button, str(path))
                    self.assertIsNotNone(result)
                    self.assertAlmostEqual(result["x"], x * scale - crop_x, delta=12)
                    self.assertAlmostEqual(result["y"], y * scale, delta=12)

    def test_buttons_absent_on_other_pages(self):
        for screenshot in (
            "3200x1440_agree_terms_popup.png", "3200x1440_fullscreen_activity.png",
            "3200x1440_farm_statue.png", "3200x1440_harvest_popup.png",
        ):
            for button in ("start_game", "enter_farm"):
                with self.subTest(button=button, screenshot=screenshot):
                    self.assertIsNone(w.find_text_button(button, str(SHOTS / screenshot)))

    def test_login_and_lobby_do_not_match_each_others_button(self):
        self.assertIsNone(w.find_text_button(
            "enter_farm", str(SHOTS / "3200x1440_anniversary_login.png")
        ))
        self.assertIsNone(w.find_text_button(
            "start_game", str(SHOTS / "3200x1440_anniversary_lobby.png")
        ))


if __name__ == "__main__":
    unittest.main()
