import contextlib
import importlib.util
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import wzry_updater


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("farm_build_release", ROOT / "packaging/build_release.py")
build_release = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(build_release)


class RuntimeReuseTests(unittest.TestCase):
    def setUp(self):
        self.stack = contextlib.ExitStack()
        self.addCleanup(self.stack.close)
        self.root = Path(self.stack.enter_context(tempfile.TemporaryDirectory()))
        self.app = self.root / "app"
        self.packs = self.root / "dist/packs"
        self.packs.mkdir(parents=True)
        self.stack.enter_context(patch.object(build_release, "ROOT", self.root))
        self.stack.enter_context(patch.object(build_release, "APP_DIR", self.app))
        self.stack.enter_context(patch.object(build_release, "PACKS_DIR", self.packs))
        self.stack.enter_context(patch.object(build_release, "log"))
        self.write("_internal/python.dll", b"unchanged-python")
        self.write("_internal/ucrtbase.dll", b"released-ucrt")
        self.write("_internal/api-ms-win-core-file-l1-1-0.dll", b"released-api")
        self.write("_internal/numpy-1.dist-info/RECORD", b"released-record")
        self.write("_internal/numpy/module.py", b"unchanged-module")
        self.pack = wzry_updater.make_pack(
            self.app, self.packs / "runtime_old.zip",
            wzry_updater.scan_release_files(self.app),
        )

    def write(self, rel, data):
        target = self.app / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)

    def test_only_system_dll_and_record_changes_reuse_original_pack(self):
        self.write("_internal/ucrtbase.dll", b"new-host-ucrt")
        self.write("_internal/numpy-1.dist-info/RECORD", b"new-install-record")
        extra = "_internal/api-ms-win-core-sysinfo-l1-2-0.dll"
        self.write(extra, b"new-host-api")
        self.assertTrue(build_release.reuse_runtime_build_noise(self.pack))
        self.assertFalse((self.app / extra).exists())
        self.assertEqual((self.app / "_internal/ucrtbase.dll").read_bytes(), b"released-ucrt")
        self.assertEqual(
            (self.app / "_internal/numpy-1.dist-info/RECORD").read_bytes(), b"released-record"
        )
        files = {rel: wzry_updater.file_meta(path) for rel, path in wzry_updater.scan_release_files(self.app).items()}
        state = {"runtime_key": wzry_updater.runtime_key(files), "runtime_pack": self.pack}
        manifest, created = wzry_updater.make_release_artifacts(
            self.app, self.packs, "new", 2, "", state,
        )
        self.assertEqual(manifest["packs"][1]["name"], "runtime_old.zip")
        self.assertEqual([path.name for path in created], ["app_new.zip"])

    def test_actual_dependency_change_is_kept(self):
        self.write("_internal/numpy/module.py", b"new-module")
        self.write("_internal/ucrtbase.dll", b"new-host-ucrt")
        self.assertFalse(build_release.reuse_runtime_build_noise(self.pack))
        self.assertEqual((self.app / "_internal/numpy/module.py").read_bytes(), b"new-module")
        self.assertEqual((self.app / "_internal/ucrtbase.dll").read_bytes(), b"new-host-ucrt")

    def test_added_dependency_is_not_removed(self):
        self.write("_internal/new.dll", b"new-dependency")
        self.assertFalse(build_release.reuse_runtime_build_noise(self.pack))
        self.assertTrue((self.app / "_internal/new.dll").is_file())

    def test_corrupt_cached_pack_is_not_applied(self):
        (self.packs / self.pack["name"]).write_bytes(b"corrupt")
        self.write("_internal/ucrtbase.dll", b"new-host-ucrt")
        self.assertFalse(build_release.reuse_runtime_build_noise(self.pack))
        self.assertEqual((self.app / "_internal/ucrtbase.dll").read_bytes(), b"new-host-ucrt")

    def test_cache_in_default_dist_is_used_for_isolated_build(self):
        self.stack.enter_context(patch.object(build_release, "PACKS_DIR", self.root / "other/packs"))
        self.write("_internal/ucrtbase.dll", b"new-host-ucrt")
        self.assertTrue(build_release.reuse_runtime_build_noise(self.pack))


if __name__ == "__main__":
    unittest.main()
