"""在线更新模块的端到端测试：目录更新源 → 检查 → 下载 → 应用。

不碰网络，用本地目录充当更新源；锁定文件的改名回退逻辑用 monkeypatch
模拟（真实场景中运行中的 exe 允许改名、不允许覆盖，已在 Windows 实测）。
"""
import json
import os
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

import wzry_updater


def write(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(data, str):
        data = data.encode("utf-8")
    path.write_bytes(data)


def build_app_dir(root, marker):
    """构造一个最小的发布目录结构。"""
    app = root / "app"
    write(app / "农场助手.exe", f"gui-{marker}")
    write(app / "wzry_core.exe", f"core-{marker}")
    write(app / "stats.html", f"stats-{marker}")
    write(app / "assets" / "templates" / "a.png", f"tpl-a-{marker}")
    write(app / "assets" / "templates" / "b.png", "tpl-b-stable")
    write(app / "_internal" / "python.dll", "runtime-stable")
    write(app / "platform-tools" / "adb.exe", "adb-stable")
    return app


def publish(app_dir, channel, version, build, prev_state=None, notes=""):
    """模拟 build_release 的目录发布：写 version.json → 打分包 → 出清单。"""
    write(app_dir / "version.json", json.dumps(
        {"version": version, "build": build}, ensure_ascii=False,
    ))
    packs_dir = channel / "packs_cache"
    manifest, new_paths = wzry_updater.make_release_artifacts(
        app_dir, packs_dir, version, build, notes, prev_state or {},
    )
    channel.mkdir(parents=True, exist_ok=True)
    for pack in manifest["packs"]:
        source = packs_dir / pack["name"]
        if source.exists():
            (channel / pack["name"]).write_bytes(source.read_bytes())
    (channel / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False), encoding="utf-8",
    )
    return manifest


class UpdaterFlowTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.logs = []

    def tearDown(self):
        self._tmp.cleanup()

    def make_updater(self, app_dir, channel):
        return wzry_updater.Updater(
            app_dir, str(channel), log=self.logs.append,
        )

    def test_full_update_flow(self):
        # 旧版安装（无 version.json，模拟第一批手动分发的包）
        old = build_app_dir(self.root, "old")
        # 运行时产生的文件不应被更新动到
        write(old / "assets" / "stats.json", "user-stats")
        write(old / "assets" / "gui_config.json", "user-config")
        # 旧版多出的过期模板应被清理
        write(old / "assets" / "templates" / "removed.png", "obsolete")

        new = build_app_dir(self.root / "newsrc", "new")
        channel = self.root / "channel"
        manifest = publish(new, channel, "20260827-1", 2026082701)

        updater = self.make_updater(old, channel)
        info = updater.check()
        self.assertIsNotNone(info)
        self.assertEqual(info.version, "20260827-1")
        # b.png / runtime / adb 内容没变，不应进差异集
        self.assertNotIn("assets/templates/b.png", info.need)
        self.assertNotIn("_internal/python.dll", info.need)
        # 运行库未变 → 只需下载应用分包
        self.assertEqual(
            [p["name"] for p in info.packs], ["app_20260827-1.zip"],
        )

        updater.download(info)
        new_exe = updater.apply(info)

        self.assertEqual(new_exe.name, "农场助手.exe")
        self.assertEqual((old / "农场助手.exe").read_text("utf-8"), "gui-new")
        self.assertEqual((old / "stats.html").read_text("utf-8"), "stats-new")
        # 版本标记已写入
        self.assertEqual(wzry_updater.read_version(old)["build"], 2026082701)
        # 用户数据原样保留
        self.assertEqual((old / "assets" / "stats.json").read_text("utf-8"), "user-stats")
        # 过期模板被清理
        self.assertFalse((old / "assets" / "templates" / "removed.png").exists())
        # 再查一次：已是最新
        self.assertIsNone(self.make_updater(old, channel).check())

    def test_runtime_change_downloads_runtime_pack(self):
        old = build_app_dir(self.root, "old")
        new = build_app_dir(self.root / "newsrc", "new")
        write(new / "_internal" / "python.dll", "runtime-CHANGED")
        channel = self.root / "channel"
        publish(new, channel, "20260827-2", 2026082702)

        updater = self.make_updater(old, channel)
        info = updater.check()
        self.assertEqual(
            sorted(p["name"] for p in info.packs),
            ["app_20260827-2.zip", "runtime_20260827-2.zip"],
        )
        updater.download(info)
        updater.apply(info)
        self.assertEqual(
            (old / "_internal" / "python.dll").read_text("utf-8"), "runtime-CHANGED",
        )

    def test_reused_runtime_pack_from_previous_release(self):
        """运行库没变时复用上一版分包记录，且清单能引用旧附件。"""
        new = build_app_dir(self.root / "newsrc", "v1")
        channel = self.root / "channel"
        m1 = publish(new, channel, "20260827-1", 2026082701)
        state = {
            "runtime_key": m1["runtime_key"],
            "runtime_pack": next(
                p for p in m1["packs"] if p["name"].startswith("runtime_")
            ),
        }
        write(new / "stats.html", "stats-v2")
        m2 = publish(new, channel, "20260827-3", 2026082703, prev_state=state)
        runtime2 = next(p for p in m2["packs"] if p["name"].startswith("runtime_"))
        self.assertEqual(runtime2["name"], "runtime_20260827-1.zip")

        old = build_app_dir(self.root, "old")
        updater = self.make_updater(old, channel)
        info = updater.check()
        updater.download(info)
        updater.apply(info)
        self.assertEqual((old / "stats.html").read_text("utf-8"), "stats-v2")

    def test_corrupted_download_rejected(self):
        old = build_app_dir(self.root, "old")
        new = build_app_dir(self.root / "newsrc", "new")
        channel = self.root / "channel"
        manifest = publish(new, channel, "20260827-1", 2026082701)
        pack_name = manifest["packs"][0]["name"]
        (channel / pack_name).write_bytes(b"corrupted bytes")

        updater = self.make_updater(old, channel)
        info = updater.check()
        with self.assertRaises(wzry_updater.UpdateError):
            updater.download(info)

    def test_locked_target_renamed_aside(self):
        """目标被占用时（运行中的 exe）：先改名挪进 trash 再放新文件。"""
        old = build_app_dir(self.root, "old")
        new = build_app_dir(self.root / "newsrc", "new")
        channel = self.root / "channel"
        publish(new, channel, "20260827-1", 2026082701)

        updater = self.make_updater(old, channel)
        info = updater.check()
        updater.download(info)

        locked = old / "农场助手.exe"
        real_replace = os.replace

        def replace_guard(src, dst, *args, **kwargs):
            # 模拟 Windows 对运行中 exe 的行为：覆盖被拒绝、改名放行
            if Path(dst) == locked and locked.exists():
                raise PermissionError(13, "in use", str(dst))
            return real_replace(src, dst, *args, **kwargs)

        with patch.object(wzry_updater.os, "replace", side_effect=replace_guard):
            updater.apply(info)

        self.assertEqual(locked.read_text("utf-8"), "gui-new")
        trashed = old / "_update" / "trash" / "农场助手.exe"
        self.assertEqual(trashed.read_text("utf-8"), "gui-old")
        # 下次启动清理残留
        wzry_updater.cleanup_leftovers(old)
        self.assertFalse((old / "_update").exists())

    def test_apply_failure_rolls_back(self):
        old = build_app_dir(self.root, "old")
        new = build_app_dir(self.root / "newsrc", "new")
        channel = self.root / "channel"
        publish(new, channel, "20260827-1", 2026082701)

        updater = self.make_updater(old, channel)
        info = updater.check()
        updater.download(info)

        victim = old / "wzry_core.exe"
        real_rename = os.rename
        real_replace = os.replace

        def explode(src, dst, *args, **kwargs):
            if Path(dst) == victim:
                raise PermissionError(13, "locked hard", str(dst))
            return real_replace(src, dst, *args, **kwargs)

        def rename_guard(src, dst, *args, **kwargs):
            if Path(src) == victim:
                raise PermissionError(13, "locked hard", str(src))
            return real_rename(src, dst, *args, **kwargs)

        with patch.object(wzry_updater.os, "replace", side_effect=explode), \
                patch.object(wzry_updater.os, "rename", side_effect=rename_guard):
            with self.assertRaises(wzry_updater.UpdateError):
                updater.apply(info)

        # 全部回滚到旧版
        self.assertEqual((old / "农场助手.exe").read_text("utf-8"), "gui-old")
        self.assertEqual((old / "stats.html").read_text("utf-8"), "stats-old")
        self.assertEqual(victim.read_text("utf-8"), "core-old")

    def test_uptodate_after_manual_unzip_heals_version_file(self):
        """手动解压过新包但没有 version.json：哈希一致时补写版本标记。"""
        new = build_app_dir(self.root / "newsrc", "same")
        channel = self.root / "channel"
        publish(new, channel, "20260827-1", 2026082701)

        installed = build_app_dir(self.root, "same")
        updater = self.make_updater(installed, channel)
        self.assertIsNone(updater.check())
        self.assertEqual(wzry_updater.read_version(installed)["build"], 2026082701)

    def test_manifest_path_traversal_rejected(self):
        old = build_app_dir(self.root, "old")
        channel = self.root / "channel"
        channel.mkdir(parents=True)
        (channel / "manifest.json").write_text(json.dumps({
            "schema": wzry_updater.SCHEMA,
            "version": "20260827-9", "build": 2026082799,
            "files": {"../evil.exe": {"sha256": "0" * 64, "size": 1}},
            "packs": [],
        }), encoding="utf-8")
        updater = self.make_updater(old, channel)
        with self.assertRaises(wzry_updater.UpdateError):
            updater.check()

    def test_missing_pack_coverage_reported(self):
        old = build_app_dir(self.root, "old")
        new = build_app_dir(self.root / "newsrc", "new")
        channel = self.root / "channel"
        manifest = publish(new, channel, "20260827-1", 2026082701)
        # 清单声称有文件但没有任何分包覆盖它
        manifest["files"]["ghost.bin"] = {"sha256": "0" * 64, "size": 3}
        (channel / "manifest.json").write_text(
            json.dumps(manifest, ensure_ascii=False), encoding="utf-8",
        )
        updater = self.make_updater(old, channel)
        with self.assertRaises(wzry_updater.UpdateError):
            updater.check()


class PackScanTests(unittest.TestCase):
    def test_scan_skips_runtime_junk_and_update_dir(self):
        with tempfile.TemporaryDirectory() as tmp:
            app = build_app_dir(Path(tmp), "x")
            write(app / "assets" / "stats.json", "volatile")
            write(app / "assets" / "current.png", "volatile")
            write(app / "_update" / "trash" / "old.exe", "trash")
            write(app / "diagnostics" / "shot.png", "debug")
            rels = set(wzry_updater.scan_release_files(app))
            self.assertIn("农场助手.exe", rels)
            self.assertIn("assets/templates/a.png", rels)
            self.assertIn("_internal/python.dll", rels)
            self.assertNotIn("assets/stats.json", rels)
            self.assertNotIn("assets/current.png", rels)
            self.assertNotIn("diagnostics/shot.png", rels)
            self.assertFalse(any(r.startswith("_update") for r in rels))

    def test_app_pack_uses_deflate_runtime_uses_lzma(self):
        with tempfile.TemporaryDirectory() as tmp:
            app = build_app_dir(Path(tmp), "x")
            write(app / "version.json", "{}")
            manifest, new_paths = wzry_updater.make_release_artifacts(
                app, Path(tmp) / "packs", "1", 1, "", {},
            )
            names = {p.name for p in new_paths}
            self.assertEqual(names, {"app_1.zip", "runtime_1.zip"})
            with zipfile.ZipFile(Path(tmp) / "packs" / "runtime_1.zip") as bundle:
                self.assertEqual(
                    bundle.infolist()[0].compress_type, zipfile.ZIP_LZMA,
                )
            app_files = next(
                p for p in manifest["packs"] if p["name"] == "app_1.zip"
            )["files"]
            self.assertIn("version.json", app_files)
            self.assertNotIn("_internal/python.dll", app_files)


if __name__ == "__main__":
    unittest.main()
