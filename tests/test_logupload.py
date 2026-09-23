"""日志上传：客户端采集（切轮次、脱敏、缩容）与服务端存储（上限、淘汰、后台页）。

服务端在本机随机端口起一个真实实例，客户端走真实 HTTP 上传，不碰外网。
"""
import gzip
import json
import os
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from unittest.mock import patch

import wzry_logupload as lu

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "server"))
import log_server  # noqa: E402

SEP = "=" * 60
CLIENT = "0123456789abcdef0123456789abcdef"


def direct_opener(*handlers):
    """本机开着系统代理时，127.0.0.1 的请求也可能被拐进代理；测试一律直连。"""
    return urllib.request.build_opener(urllib.request.ProxyHandler({}), *handlers)


def banner():
    return f"{SEP}\n王者荣耀农场自动化务农 v3\n{SEP}\n  📱 设备: 1.2.3.4:5555\n  📐 分辨率 3200x1440\n"


def round_block(num, body="  步骤…\n"):
    return f"\n{SEP}\n# 第 {num} 轮务农\n{SEP}\n{body}"


class ExtractRoundsTests(unittest.TestCase):
    def test_fewer_rounds_than_limit_returns_everything(self):
        text = banner() + round_block(1) + round_block(2)
        excerpt, rounds, startup = lu.extract_recent_rounds(text, 50)
        self.assertEqual(excerpt, text)
        self.assertEqual(rounds, 2)
        self.assertEqual(startup, "")

    def test_keeps_last_n_rounds_with_separator_and_startup_block(self):
        text = banner() + "".join(round_block(i, f"  第{i}轮内容\n") for i in range(1, 61))
        excerpt, rounds, startup = lu.extract_recent_rounds(text, 50)
        self.assertEqual(rounds, 50)
        self.assertTrue(excerpt.startswith(SEP + "\n# 第 11 轮务农"))
        self.assertNotIn("第10轮内容", excerpt)
        self.assertIn("第60轮内容", excerpt)
        # 启动早于截取起点：单独补上，但不带下一轮的分隔线
        self.assertIn("分辨率 3200x1440", startup)
        self.assertTrue(startup.startswith(SEP))
        self.assertNotIn("# 第", startup)
        self.assertFalse(startup.rstrip().endswith("="))

    def test_restart_inside_window_needs_no_separate_startup(self):
        text = (banner() + "".join(round_block(i) for i in range(1, 30))
                + banner() + "".join(round_block(i) for i in range(30, 70)))
        excerpt, rounds, startup = lu.extract_recent_rounds(text, 50)
        self.assertEqual(rounds, 50)
        self.assertIn("王者荣耀农场自动化务农", excerpt)
        self.assertEqual(startup, "")

    def test_read_tail_drops_partial_first_line(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "run.log"
            # 按字节写：write_text 在 Windows 上会把 \n 换成 \r\n，字节偏移就对不上了
            path.write_bytes("第一行很长很长\n第二行\n第三行\n".encode("utf-8"))
            # 共 42 字节：从第 27 字节（「二」的最后一个字节）开始读，半行丢掉
            text, size, truncated = lu.read_tail(path, 15)
            self.assertTrue(truncated)
            self.assertEqual(size, path.stat().st_size)
            self.assertEqual(text, "第三行\n")


class SanitizeTests(unittest.TestCase):
    def test_config_hides_password_and_internal_keys(self):
        clean = lu.sanitize_config({
            "unlock_pwd": "8888", "wireless_device": "1.2.3.4:5555",
            "log_client_id": CLIENT, "log_upload_fp": "1:2", "update_token": "",
        })
        self.assertEqual(clean["unlock_pwd"], "（已设置）")
        self.assertEqual(clean["update_token"], "（未设置）")
        self.assertEqual(clean["wireless_device"], "1.2.3.4:5555")
        self.assertNotIn("log_client_id", clean)
        self.assertNotIn("log_upload_fp", clean)

    def test_scrub_masks_input_text(self):
        self.assertEqual(lu.scrub("run input text 123456 done"), "run input text *** done")

    def test_encode_report_shrinks_log_to_fit(self):
        import random
        rng = random.Random(1)
        noise = "".join(f"{rng.getrandbits(64):016x}\n" for _ in range(40000))
        report = {"run_log": {"text": noise}}
        body = lu.encode_report(report, limit=200_000)
        self.assertLessEqual(len(body), 200_000)
        decoded = json.loads(gzip.decompress(body))
        self.assertTrue(decoded["run_log"]["head_cut"])
        self.assertTrue(noise.endswith(decoded["run_log"]["text"]))  # 留下的是最新的


class UploadUrlTests(unittest.TestCase):
    def test_url_file_env_and_config_precedence(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {}, clear=False):
            os.environ.pop("WZRY_LOG_UPLOAD_URL", None)
            self.assertEqual(lu.upload_url({}, tmp), "")            # 什么都没配：上传不可用
            # 记事本另存的 BOM、注释行、空行都不碍事
            (Path(tmp) / lu.URL_FILE).write_bytes(
                "﻿# 注释\n\n  http://file.example:8421/api/logs  \n".encode("utf-8"))
            self.assertEqual(lu.upload_url({}, tmp), "http://file.example:8421/api/logs")
            self.assertEqual(lu.upload_url({"log_upload_url": "http://cfg/api/logs"}, tmp),
                             "http://cfg/api/logs")
            os.environ["WZRY_LOG_UPLOAD_URL"] = "http://env/api/logs"
            self.assertEqual(lu.upload_url({"log_upload_url": "http://cfg/api/logs"}, tmp),
                             "http://env/api/logs")
            os.environ.pop("WZRY_LOG_UPLOAD_URL")

    def test_url_file_at_app_root_ships_with_online_update(self):
        # 在线更新只下发根目录文件和几棵受管目录；地址文件放 assets/ 下老用户就收不到
        import wzry_updater
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / lu.URL_FILE).write_text("http://x/api/logs", encoding="utf-8")
            self.assertIn(lu.URL_FILE, wzry_updater.scan_release_files(tmp))


class FakeAdb:
    """按命令关键字回放的假 adb。"""

    def __init__(self, devices="List of devices attached\nABC123 device product:x model:M2012\n"):
        self.devices = devices

    def __call__(self, *args, timeout=10):
        class Result:
            stdout = ""
        result = Result()
        joined = " ".join(args)
        if args[:1] == ("version",):
            result.stdout = "Android Debug Bridge version 1.0.41\nVersion 37.0.0\n"
        elif args[:1] == ("devices",):
            result.stdout = self.devices
        elif "getprop" in joined:
            result.stdout = "marketname=Redmi K40\nmodel=M2012K11AC\nandroid=13\nhyperos=\n"
        elif "wm size" in joined:
            result.stdout = "Physical size: 1080x2400\nPhysical density: 440\n"
        elif "dumpsys battery" in joined:
            result.stdout = "  AC powered: false\n  USB powered: true\n  level: 85\n  scale: 100\n"
        elif "mWakefulness" in joined:
            result.stdout = "  mWakefulness=Asleep\n"
        elif "date +%s" in joined:
            raise TimeoutError("timed out")
        elif "state.json" in joined:
            result.stdout = '{"wake": "2026-09-23 15:48:03"}\n'
        return result


class CollectTests(unittest.TestCase):
    def test_phone_info_survives_failing_commands(self):
        adb = FakeAdb()
        phone = lu.collect_phone_info(adb, "ABC123", "device")
        self.assertEqual(phone["marketname"], "Redmi K40")
        self.assertNotIn("hyperos", phone)          # 读到空值不报
        self.assertEqual(phone["battery"]["level"], "85")
        self.assertNotIn("scale", phone["battery"])
        self.assertEqual(phone["wakefulness"], "Asleep")
        self.assertNotIn("clock_skew_s", phone)     # date 超时只是少一项
        self.assertIn("15:48:03", phone["archive"])

    def test_offline_phone_is_not_queried(self):
        calls = []
        phone = lu.collect_phone_info(lambda *a, **k: calls.append(a), "X", "unauthorized")
        self.assertEqual(phone, {"serial": "X", "state": "unauthorized"})
        self.assertEqual(calls, [])

    def test_build_report_end_to_end(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "run.log").write_text(
                banner() + round_block(1, "  input text 9999\n"), encoding="utf-8")
            (root / "stats.json").write_text(json.dumps(
                {"totals": {"rounds": 1}, "rounds_log": [{"round": i} for i in range(80)]}),
                encoding="utf-8")
            diag = root / "diagnostics" / "20260923_094201_step3_start_game"
            diag.mkdir(parents=True)
            (diag / "context.json").write_text('{"step": "step3"}', encoding="utf-8")
            report = lu.build_report(
                client_id=CLIENT, reason="manual", nickname="小王", note="点不到",
                app={"version": "20260923-1"}, gui={"status": "挂机运行中"},
                config={"unlock_pwd": "9999"}, gui_log="[助手] input text 9999",
                run_log_path=root / "run.log", stats_path=root / "stats.json",
                error_log_path=root / "missing.log", diagnostics_dir=root / "diagnostics",
            )
        self.assertEqual(report["code"], "01234567")
        self.assertEqual(report["run_log"]["rounds"], 1)
        self.assertEqual(len(report["stats"]["rounds_log"]), 50)
        self.assertEqual(report["diagnostics"][0]["context"]["step"], "step3")
        blob = json.dumps(report, ensure_ascii=False)
        self.assertNotIn("9999", blob)
        self.assertEqual(report["error_log"], "")


def make_diagnostics(root, specs):
    """specs: [(现场名, 有没有截图)]，截图是一张小 RGBA PNG（和真机截图同格式）。

    没装 Pillow 的环境（服务器上跑服务端测试）写个占位文件，配合 fake_encode 用。
    """
    try:
        from PIL import Image
    except ImportError:
        Image = None
    folder = Path(root) / "diagnostics"
    for name, with_shot in specs:
        (folder / name).mkdir(parents=True)
        (folder / name / "context.json").write_text('{"step": "x"}', encoding="utf-8")
        if not with_shot:
            continue
        shot = folder / name / "screenshot.png"
        if Image is None:
            shot.write_bytes(b"placeholder")
        else:
            Image.new("RGBA", (320, 144), (40, 160, 90, 255)).save(shot)
    return folder


def fake_encode(path, **_kwargs):
    """服务端测试不关心压缩，给每张图一段不同的 JPEG 字节即可。"""
    return b"\xff\xd8\xff\xe0" + Path(path).parent.name.encode() * 50


class ShotClientTests(unittest.TestCase):
    def test_marks_latest_five_that_have_screenshots(self):
        with tempfile.TemporaryDirectory() as tmp:
            specs = [(f"20260923_0900{i:02d}_step3_start_game", True) for i in range(7)]
            specs.append(("20260923_090100_step7_oneclick", False))  # 最新一个没截图
            folder = make_diagnostics(tmp, specs)
            (folder / "手工放的").mkdir()
            items = lu.collect_diagnostics(folder)
        names = [item["name"] for item in items]
        self.assertNotIn("手工放的", names)
        flagged = [item["name"] for item in items if item.get("shot")]
        self.assertEqual(flagged, [f"20260923_0900{i:02d}_step3_start_game" for i in range(2, 7)])

    def test_encode_shot_keeps_resolution(self):
        from io import BytesIO
        from PIL import Image
        with tempfile.TemporaryDirectory() as tmp:
            folder = make_diagnostics(tmp, [("20260923_090000_step2_launch", True)])
            data = lu.encode_shot(folder / "20260923_090000_step2_launch" / "screenshot.png")
        self.assertTrue(data.startswith(b"\xff\xd8\xff"))
        self.assertEqual(Image.open(BytesIO(data)).size, (320, 144))

    def test_encode_shot_shrinks_when_over_limit(self):
        import random
        from io import BytesIO
        from PIL import Image
        rng = random.Random(3)
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "noise.png"
            Image.frombytes("RGB", (800, 400), bytes(rng.getrandbits(8) for _ in range(800 * 400 * 3))).save(path)
            data = lu.encode_shot(path, limit=40_000)
        self.assertLessEqual(len(data), 40_000)
        self.assertLess(Image.open(BytesIO(data)).size[0], 800)

    def test_failure_log_is_the_whole_round_that_saved_it(self):
        name = "20260923_094045_step3_start_game"
        text = banner() + round_block(13, "  第13轮\n") + round_block(
            14, f"  尝试 5/5...\n  ❌ 连续5次失败，返回步骤1\n  📁 已保存失败现场: D:\\x\\diagnostics\\{name}\n"
                "\n⚠️ 步骤3失败，重新开始...\n") + round_block(15, "  第15轮\n")
        log, round_no = lu.failure_log(text, name)
        self.assertEqual(round_no, 14)
        self.assertTrue(log.startswith(SEP + "\n# 第 14 轮务农"))
        self.assertIn(name, log)
        self.assertTrue(log.endswith("⚠️ 步骤3失败，重新开始..."))
        self.assertNotIn("第13轮", log)
        self.assertNotIn("第15轮", log)
        self.assertEqual(lu.failure_log(text, "20260101_000000_step2_launch"), (None, None))

    def test_failure_log_trims_long_waiting_rounds(self):
        name = "20260923_090000_step2_launch"
        waits = "".join(f"  ⏳ 等待游戏启动页，剩余 {i}秒\n" for i in range(2000))
        text = round_block(7, "  📱 屏幕已亮\n" + waits + f"  📁 已保存失败现场: {name}\n收尾\n")
        log, round_no = lu.failure_log(text, name)
        lines = log.splitlines()
        self.assertEqual(round_no, 7)
        self.assertLessEqual(len(lines), lu.FAILURE_LOG_MAX_LINES + 1)
        self.assertEqual(lines[1], "# 第 7 轮务农")              # 开头留着
        self.assertTrue(any("中间省略" in line for line in lines))
        self.assertIn(name, log)                                  # 失败那行一定在
        self.assertEqual(lines[-1], "收尾")

    def test_logs_attached_only_to_scenes_with_screenshots(self):
        with_shot, no_shot = "20260923_090000_step2_launch", "20260923_090100_step7_oneclick"
        with tempfile.TemporaryDirectory() as tmp:
            make_diagnostics(tmp, [(with_shot, True), (no_shot, False)])
            (Path(tmp) / "run.log").write_bytes((
                round_block(1, f"  📁 已保存失败现场: {with_shot}\n  input text 1234\n")
                + round_block(2, f"  📁 已保存失败现场: {no_shot}\n")).encode("utf-8"))
            report = lu.build_report(
                client_id=CLIENT, reason="auto", app={}, gui={}, config={}, gui_log="",
                run_log_path=Path(tmp) / "run.log", stats_path=Path(tmp) / "none.json",
                error_log_path=Path(tmp) / "none.log", diagnostics_dir=Path(tmp) / "diagnostics",
            )
        items = {d["name"]: d for d in report["diagnostics"]}
        self.assertEqual(items[with_shot]["round"], 1)
        self.assertIn("input text ***", items[with_shot]["log"])   # 配的日志也脱敏
        self.assertNotIn("log", items[no_shot])

    def test_upload_shots_only_sends_offered_names(self):
        calls = []
        with patch.object(lu, "_post", side_effect=lambda *a, **k: calls.append(a[0])):
            sent, failed = lu.upload_shots(
                "http://x/api/logs", CLIENT, ["20260923_090000_step2_launch", "../../etc/passwd"],
                "nowhere", allowed={"20260923_090001_step2_launch"},
            )
        self.assertEqual((sent, failed, calls), (0, [], []))


class ServerTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.data = self._tmp.name
        self.start_server()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self._tmp.cleanup()

    def start_server(self, max_bytes=50 * 1024 * 1024, client_max=20 * 1024 * 1024):
        store = log_server.Store(self.data, max_bytes, client_max)
        self.app = log_server.App(store, lu.UPLOAD_KEY, "admin-secret")
        self.server = log_server.Server(("127.0.0.1", 0), self.app)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"

    def post(self, report, key=lu.UPLOAD_KEY):
        with patch.object(lu, "UPLOAD_KEY", key):
            return lu.upload(lu.encode_report(report), self.base + "/api/logs", "test")

    def get(self, path, cookie=None, raw=False):
        request = urllib.request.Request(self.base + path)
        if cookie:
            request.add_header("Cookie", cookie)
        try:
            with direct_opener().open(request) as response:
                status, body = response.status, response.read()
        except urllib.error.HTTPError as exc:
            status, body = exc.code, exc.read()
        return status, body if raw else body.decode("utf-8")

    def post_shot(self, name, data, client=CLIENT):
        query = urllib.parse.urlencode({"client": client, "name": name})
        request = urllib.request.Request(
            f"{self.base}/api/shots?{query}", data=data, method="POST",
            headers={"X-Wzry-Key": lu.UPLOAD_KEY, "Content-Type": "image/jpeg"},
        )
        try:
            with direct_opener().open(request) as response:
                return response.status
        except urllib.error.HTTPError as exc:
            return exc.code

    def test_shots_requested_uploaded_deduped_and_shown(self):
        names = [f"20260923_09000{i}_step3_start_game" for i in range(3)]
        with tempfile.TemporaryDirectory() as tmp:
            folder = make_diagnostics(tmp, [(n, True) for n in names])
            diagnostics = lu.collect_diagnostics(folder)
            receipt = self.post(self.report(diagnostics=diagnostics))
            self.assertEqual(receipt["need_shots"], names)
            with patch.object(lu, "encode_shot", fake_encode):
                sent, failed = lu.upload_shots(
                    self.base + "/api/logs", CLIENT, receipt["need_shots"], folder,
                    allowed=set(names))
        self.assertEqual((sent, failed), (3, []))
        store = self.app.store
        self.assertEqual(store.shot_names(CLIENT), set(names))
        # 再传一份报告：图都有了，一张也不要
        again = self.post(self.report(diagnostics=diagnostics))
        self.assertEqual(again["need_shots"], [])
        # 截图算进占用，重启重扫后账目一致
        real = sum(store.disk_bytes(os.path.join(d, f))
                   for d, _, files in os.walk(self.data) for f in files)
        self.assertEqual(real, store.total)
        rescanned = log_server.Store(self.data, store.max_bytes, store.client_max_bytes)
        self.assertEqual(rescanned.total, store.total)
        self.assertEqual(rescanned.shot_names(CLIENT), set(names))
        # 后台：报告页贴图，图片要登录才能看
        cookie = "wzry_admin=admin-secret"
        status, body = self.get(f"/admin/r/{CLIENT}/{receipt['id']}", cookie)
        self.assertIn(f'<img src="/admin/s/{CLIENT}/{names[-1]}.jpg"', body)
        status, _ = self.get(f"/admin/s/{CLIENT}/{names[0]}.jpg")
        self.assertEqual(status, 401)
        status, data = self.get(f"/admin/s/{CLIENT}/{names[0]}.jpg", cookie, raw=True)
        self.assertEqual(status, 200)
        self.assertTrue(data.startswith(b"\xff\xd8\xff"))
        status, body = self.get("/admin", cookie)
        self.assertIn("2 份 · 3 图", body)   # 两份报告共用这 3 张图

    def test_failure_card_shows_round_log_highlighted_and_escaped(self):
        name = "20260923_094045_step3_start_game"
        log = (f"{SEP}\n# 第 14 轮务农\n{SEP}\n  ❌ 'start_game.png': 未匹配 (0.55 < 0.75)\n"
               f"  <script>alert(1)</script>\n  ❌ 连续5次失败，返回步骤1\n"
               f"  📁 已保存失败现场: D:\\x\\{name}")
        receipt = self.post(self.report(diagnostics=[
            {"name": name, "shot": True, "log": log, "round": 14},
        ]))
        cookie = "wzry_admin=admin-secret"
        _, body = self.get(f"/admin/r/{CLIENT}/{receipt['id']}", cookie)
        self.assertIn("09-23 09:40:45 · step3_start_game · 第 14 轮", body)
        self.assertIn("截图还没收到", body)                        # 图没传就先说明
        self.assertIn("<mark>  ❌ 连续5次失败，返回步骤1</mark>", body)
        self.assertIn(f"<mark>  📁 已保存失败现场: D:\\x\\{name}</mark>", body)
        self.assertNotIn("<mark>  ❌ &#x27;start_game.png&#x27;", body)  # 普通未匹配行不标
        self.assertNotIn("<script>alert", body)
        _, text = self.get(f"/admin/r/{CLIENT}/{receipt['id']}/log", cookie)
        self.assertIn(f"===== 失败现场 {name} =====", text)
        self.assertIn("连续5次失败", text)

    def test_unrequested_or_non_jpeg_shot_rejected(self):
        name = "20260923_090000_step2_launch"
        jpeg = b"\xff\xd8\xff\xe0" + b"0" * 100
        self.assertEqual(self.post_shot(name, jpeg), 409)          # 没传报告就塞图
        self.post(self.report(diagnostics=[{"name": name, "shot": True}]))
        self.assertEqual(self.post_shot(name, b"\x89PNG....."), 400)
        self.assertEqual(self.post_shot("../../x", jpeg), 400)
        self.assertEqual(self.post_shot(name, jpeg), 200)
        self.assertEqual(self.post_shot(name, jpeg), 200)          # 重传算成功，不重复存
        self.assertEqual(len(self.app.store.shot_names(CLIENT)), 1)
        self.assertEqual(self.post_shot("20260923_090001_step2_launch", jpeg), 409)

    def report(self, reason="manual", client=CLIENT, **extra):
        data = {"client_id": client, "reason": reason, "pc": {"hostname": "PC-1"},
                "phone": {"marketname": "Redmi K40"}, "app": {"version": "1"},
                "run_log": {"text": "# 第 1 轮务农\n"}}
        data.update(extra)
        return data

    def test_upload_stored_with_server_ip(self):
        receipt = self.post(self.report(note="点不到"))
        self.assertEqual(receipt["code"], "01234567")
        self.assertEqual(receipt["ip"], "127.0.0.1")
        stored = self.app.store.load(CLIENT, receipt["id"])
        self.assertEqual(stored["server"]["ip"], "127.0.0.1")
        meta = self.app.store.read_meta(CLIENT)
        self.assertEqual(meta["hostname"], "PC-1")
        self.assertEqual(meta["phone"], "Redmi K40")
        self.assertEqual(meta["recent"][0]["note"], "点不到")

    def test_wrong_key_rejected(self):
        with self.assertRaises(lu.UploadError) as ctx:
            self.post(self.report(), key="nope")
        self.assertIn("403", str(ctx.exception))

    def test_bad_client_id_rejected(self):
        with self.assertRaises(lu.UploadError) as ctx:
            self.post(self.report(client="../../etc"))
        self.assertIn("400", str(ctx.exception))

    def test_client_rate_limited(self):
        for _ in range(log_server.CLIENT_LIMIT[0]):
            self.post(self.report(reason="auto"))
        with self.assertRaises(lu.UploadError) as ctx:
            self.post(self.report(reason="auto"))
        self.assertIn("429", str(ctx.exception))

    def test_gzip_bomb_rejected(self):
        bomb = gzip.compress(b"{" + b" " * (log_server.MAX_JSON + 10) + b"}")
        request = urllib.request.Request(
            self.base + "/api/logs", data=bomb, method="POST",
            headers={"Content-Encoding": "gzip", "X-Wzry-Key": lu.UPLOAD_KEY},
        )
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            direct_opener().open(request)
        self.assertEqual(ctx.exception.code, 400)

    def test_cap_evicts_auto_before_manual_and_never_exceeds(self):
        import random
        rng = random.Random(7)
        store = self.app.store
        store.max_bytes = 700 * 1024
        store.client_max_bytes = store.max_bytes
        blob = lambda: "".join(f"{rng.getrandbits(64):016x}" for _ in range(12000))  # 约 96KB，压不动
        first_manual = self.post(self.report(reason="manual", run_log={"text": blob()}))["id"]
        other = "f" * 32
        for i in range(12):
            client = other if i % 2 else CLIENT
            with patch.object(self.app.client_limiter, "allow", return_value=True):
                self.post(self.report(reason="auto", client=client, run_log={"text": blob()}))
            self.assertLessEqual(store.total, store.max_bytes)
            real = sum(store.disk_bytes(os.path.join(d, f))
                       for d, _, files in os.walk(self.data) for f in files
                       if f != "admin_token.txt")
            self.assertEqual(real, store.total)
        self.assertIsNotNone(store.load(CLIENT, first_manual))  # 手动那份还在
        kinds = [r[2] for r in store.files.values()]
        self.assertEqual(kinds.count("manual"), 1)

    def test_restart_rescans_and_enforces_smaller_cap(self):
        for reason in ("auto", "manual", "auto"):
            self.post(self.report(reason=reason))
        self.server.shutdown()
        self.server.server_close()
        store = log_server.Store(self.data, 10, 10)
        self.assertEqual(len(store.files), 3)
        store._evict(0, None)
        self.assertEqual(store.files, {})
        self.assertEqual(store.total, 0)
        self.assertEqual(os.listdir(self.data), [])  # 空客户端目录一并清掉
        self.start_server()

    def test_admin_requires_token_and_escapes_content(self):
        receipt = self.post(self.report(nickname="<script>alert(1)</script>"))
        status, body = self.get("/admin")
        self.assertEqual(status, 401)
        self.assertNotIn("PC-1", body)
        status, _ = self.get("/admin?token=wrong")
        self.assertEqual(status, 401)
        cookie = "wzry_admin=admin-secret"
        status, body = self.get("/admin", cookie)
        self.assertEqual(status, 200)
        self.assertIn("01234567", body)
        self.assertNotIn("<script>alert", body)
        self.assertIn("&lt;script&gt;", body)
        status, body = self.get(f"/admin/r/{CLIENT}/{receipt['id']}", cookie)
        self.assertEqual(status, 200)
        self.assertIn("Redmi K40", body)
        status, body = self.get(f"/admin/r/{CLIENT}/{receipt['id']}/log", cookie)
        self.assertIn("# 第 1 轮务农", body)
        status, _ = self.get(f"/admin/r/{CLIENT}/..%2f..%2fetc", cookie)
        self.assertEqual(status, 404)

    def test_login_form_posts_token_in_body(self):
        class NoRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, *args, **kwargs):
                return None
        _, page_html = self.get("/admin")
        self.assertIn('method="post" action="/admin/login"', page_html)
        opener = direct_opener(NoRedirect)
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            opener.open(self.base + "/admin/login", data=b"token=admin-secret")
        self.assertEqual(ctx.exception.code, 303)
        self.assertEqual(ctx.exception.headers["Location"], "/admin")
        self.assertIn("wzry_admin=admin-secret", ctx.exception.headers["Set-Cookie"])
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            opener.open(self.base + "/admin/login", data=b"token=wrong")
        self.assertEqual(ctx.exception.code, 401)

    def test_token_login_sets_cookie_and_redirects(self):
        class NoRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, *args, **kwargs):
                return None
        opener = direct_opener(NoRedirect)
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            opener.open(self.base + "/admin?token=admin-secret&q=PC")
        self.assertEqual(ctx.exception.code, 303)
        self.assertEqual(ctx.exception.headers["Location"], "/admin?q=PC")
        self.assertIn("wzry_admin=admin-secret", ctx.exception.headers["Set-Cookie"])


if __name__ == "__main__":
    unittest.main()
