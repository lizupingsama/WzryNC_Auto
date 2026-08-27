#!/usr/bin/env python3
"""王者农场助手在线更新 —— 清单/增量包的生成与客户端应用。

发布侧（packaging/build_release.py 调用）：
- 扫描发布目录生成 manifest.json：每个文件的 sha256/大小 + 分包信息
- 应用包 app_*.zip（exe、模板、页面，每次发布都上传，约 10MB）
- 运行库包 runtime_*.zip（_internal + platform-tools，约 240MB，
  仅当依赖变化导致内容变动时重新打包上传，平时复用旧版本的附件）

客户端（wzry_gui.py 调用）：
- 从更新源取 manifest，与本地文件哈希对比得出差异集
- 只下载覆盖差异集的分包、只解压需要的文件，逐文件校验 sha256
- 热替换：Windows 允许给运行中的 exe / 已加载的 dll 改名，把旧文件
  改名挪进 _update/trash 后写入新文件，重启后由 cleanup_leftovers 清理

更新源三种写法（gui_config.json 的 update_url / 环境变量 WZRY_UPDATE_URL）：
- gitee://owner/repo            Gitee Releases（默认）
- https://example.com/wzry/     静态 HTTP 目录（目录下放 manifest.json 与分包）
- \\\\nas\\share\\wzry 或本地路径  局域网共享 / 本地目录（内容同上）
"""

import hashlib
import json
import os
import shutil
import time
import urllib.error
import urllib.request
import zipfile
from pathlib import Path, PurePosixPath

SCHEMA = 1
DEFAULT_SOURCE = "gitee://lizupingsama/Farm-Automation-Assistant"
USER_AGENT = "WzryFarmUpdater/1"
VERSION_FILE = "version.json"
MANIFEST_NAME = "manifest.json"
UPDATE_DIR = "_update"          # 应用目录内的更新工作区（下载/暂存/回收站）
GUI_EXE = "农场助手.exe"
CORE_EXE = "wzry_core.exe"

# 运行库前缀：单独成包，依赖不变时复用旧附件
RUNTIME_PREFIXES = ("_internal/", "platform-tools/")
# 清单未列出即删除的托管目录（模板改名后旧文件会重复匹配，必须清）；
# 目录外的一切（assets/stats.json、gui_config.json、日志等）永不删除
MANAGED_CLEAN_PREFIXES = ("_internal/", "platform-tools/", "assets/templates/")

_CHUNK = 256 * 1024


class UpdateError(Exception):
    """更新流程中面向用户的可读错误。"""


class UpdateCancelled(Exception):
    """用户取消。"""


def _noop(*_args, **_kwargs):
    pass


def sha256_file(path):
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(_CHUNK), b""):
            digest.update(block)
    return digest.hexdigest()


def read_version(app_dir):
    """读应用目录的 version.json；不存在/损坏返回 {}（视为极旧版本）。"""
    try:
        data = json.loads((Path(app_dir) / VERSION_FILE).read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _safe_rel(rel):
    """校验清单里的相对路径，防止路径穿越。返回规范化的 posix 相对路径。"""
    pure = PurePosixPath(str(rel).replace("\\", "/"))
    if pure.is_absolute() or any(part in ("..", "") for part in pure.parts):
        raise UpdateError(f"清单包含非法路径: {rel!r}")
    if ":" in str(pure):
        raise UpdateError(f"清单包含非法路径: {rel!r}")
    return str(pure)


def scan_release_files(app_dir):
    """扫描发布目录中受管理的文件 -> {relpath: Path}。

    范围＝根目录文件 + assets/templates、_internal、platform-tools 三棵树；
    assets 下的其余内容（运行统计、配置、学习数据）与 _update 工作区不参与
    发布，也就永远不会被更新覆盖或删除。
    """
    app_dir = Path(app_dir)
    files = {}
    for item in app_dir.iterdir():
        if item.is_file():
            files[item.name] = item
    for tree in ("assets/templates", "_internal", "platform-tools"):
        base = app_dir / tree
        if not base.is_dir():
            continue
        for item in base.rglob("*"):
            if item.is_file():
                files[item.relative_to(app_dir).as_posix()] = item
    return files


def file_meta(path):
    return {"sha256": sha256_file(path), "size": path.stat().st_size}


def runtime_key(manifest_files):
    """运行库内容指纹：决定 runtime 包能否复用上一版的附件。"""
    rows = sorted(
        (rel, meta["sha256"])
        for rel, meta in manifest_files.items()
        if rel.startswith(RUNTIME_PREFIXES)
    )
    return hashlib.sha256(json.dumps(rows).encode("utf-8")).hexdigest()


def make_pack(app_dir, out_path, relpaths, compression=zipfile.ZIP_DEFLATED):
    """把指定文件打成 zip 分包，返回清单里的分包描述。"""
    app_dir = Path(app_dir)
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(out_path, "w", compression=compression) as bundle:
        for rel in sorted(relpaths):
            bundle.write(app_dir / rel, arcname=rel)
    return {
        "name": out_path.name,
        "sha256": sha256_file(out_path),
        "size": out_path.stat().st_size,
        "files": sorted(relpaths),
    }


def make_release_artifacts(app_dir, packs_dir, version, build, notes, prev_state,
                           log=_noop):
    """生成一次发布需要的 manifest 与分包。

    返回 (manifest, new_pack_paths)：new_pack_paths 是本次新打出的 zip
    （manifest["packs"] 里复用的 runtime 包不会出现在其中）。
    """
    app_dir = Path(app_dir)
    packs_dir = Path(packs_dir)
    log("计算发布文件哈希 ...")
    paths = scan_release_files(app_dir)
    manifest_files = {rel: file_meta(path) for rel, path in sorted(paths.items())}

    runtime_rels = [r for r in manifest_files if r.startswith(RUNTIME_PREFIXES)]
    app_rels = [r for r in manifest_files if not r.startswith(RUNTIME_PREFIXES)]
    key = runtime_key(manifest_files)

    new_paths = []
    prev_pack = (prev_state or {}).get("runtime_pack")
    if prev_pack and (prev_state or {}).get("runtime_key") == key:
        runtime_pack = dict(prev_pack)
        log(f"运行库未变化，复用分包 {runtime_pack['name']}")
    else:
        name = f"runtime_{version}.zip"
        log(f"运行库有变化，打包 {name}（LZMA 压缩，较慢）...")
        runtime_pack = make_pack(
            app_dir, packs_dir / name, runtime_rels, zipfile.ZIP_LZMA,
        )
        new_paths.append(packs_dir / name)
        log(f"{name}: {runtime_pack['size'] / 1024 / 1024:.0f} MB")

    app_name = f"app_{version}.zip"
    log(f"打包应用分包 {app_name} ...")
    app_pack = make_pack(app_dir, packs_dir / app_name, app_rels)
    new_paths.append(packs_dir / app_name)
    log(f"{app_name}: {app_pack['size'] / 1024 / 1024:.1f} MB")

    manifest = {
        "schema": SCHEMA,
        "version": version,
        "build": build,
        "notes": notes or "",
        "created": time.strftime("%Y-%m-%d %H:%M"),
        "files": manifest_files,
        "packs": [app_pack, runtime_pack],
        "runtime_key": key,
    }
    return manifest, new_paths


# ----------------------------------------------------------------------
# 客户端：更新源
# ----------------------------------------------------------------------
def _http_get(url, timeout=20):
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    return urllib.request.urlopen(request, timeout=timeout)


def _fetch_json(url, timeout=20):
    with _http_get(url, timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


class UpdateSource:
    """把三种更新源统一成：取 manifest + 解析分包地址。"""

    def __init__(self, spec):
        spec = (spec or DEFAULT_SOURCE).strip()
        self.spec = spec
        if spec.startswith("gitee://"):
            self.kind = "gitee"
            self.repo = spec[len("gitee://"):].strip("/")
            if self.repo.count("/") != 1:
                raise UpdateError(f"gitee 更新源应为 gitee://owner/repo: {spec}")
            self._tag = None
            self._assets = {}
        elif spec.startswith(("http://", "https://")):
            self.kind = "http"
            self.base = spec if spec.endswith("/") else spec + "/"
        else:
            self.kind = "dir"
            self.base = Path(spec)

    def describe(self):
        if self.kind == "gitee":
            return f"Gitee 仓库 {self.repo}"
        return str(self.base)

    def fetch_manifest(self):
        if self.kind == "gitee":
            api = f"https://gitee.com/api/v5/repos/{self.repo}/releases/latest"
            try:
                release = _fetch_json(api)
            except urllib.error.HTTPError as exc:
                if exc.code == 404:
                    raise UpdateError("更新源还没有发布过版本") from exc
                raise UpdateError(f"查询 Gitee 发布失败: HTTP {exc.code}") from exc
            tag = release.get("tag_name")
            if not tag:
                raise UpdateError("更新源还没有发布过版本")
            self._tag = tag
            for asset in release.get("assets") or []:
                name, url = asset.get("name"), asset.get("browser_download_url")
                if name and url:
                    self._assets[name] = url
            with _http_get(self._resolve_name(MANIFEST_NAME)) as resp:
                raw = resp.read()
        elif self.kind == "http":
            with _http_get(self.base + MANIFEST_NAME) as resp:
                raw = resp.read()
        else:
            path = self.base / MANIFEST_NAME
            if not path.is_file():
                raise UpdateError(f"更新源目录里没有 {MANIFEST_NAME}: {self.base}")
            raw = path.read_bytes()
        try:
            manifest = json.loads(raw.decode("utf-8"))
        except ValueError as exc:
            raise UpdateError(f"更新清单不是有效 JSON: {exc}") from exc
        if manifest.get("schema") != SCHEMA:
            raise UpdateError(
                f"更新清单版本不兼容（{manifest.get('schema')}），请获取最新完整包"
            )
        return manifest

    def _resolve_name(self, name):
        url = self._assets.get(name)
        if url:
            return url
        # 附件列表没给出时按 Gitee 附件的固定路径拼
        return f"https://gitee.com/{self.repo}/releases/download/{self._tag}/{name}"

    def pack_location(self, pack):
        """分包地址：清单里带 url 用 url（跨版本复用的 runtime 包），否则按名字解析。"""
        url = pack.get("url")
        if url:
            return url
        name = pack["name"]
        if self.kind == "gitee":
            return self._resolve_name(name)
        if self.kind == "http":
            return self.base + name
        return self.base / name


# ----------------------------------------------------------------------
# 客户端：检查 / 下载 / 应用
# ----------------------------------------------------------------------
class UpdateInfo:
    def __init__(self, manifest, need, packs, download_bytes, local_version):
        self.manifest = manifest
        self.need = need                      # 需要更新的相对路径列表
        self.packs = packs                    # 需要下载的分包描述
        self.download_bytes = download_bytes  # 需要下载的总字节数
        self.local_version = local_version    # 本地版本号字符串（可能为空）

    @property
    def version(self):
        return self.manifest.get("version", "?")

    @property
    def notes(self):
        return self.manifest.get("notes", "")


class Updater:
    def __init__(self, app_dir, source=None, log=_noop, progress=_noop,
                 cancel_event=None):
        self.app_dir = Path(app_dir)
        self.source = source if isinstance(source, UpdateSource) else UpdateSource(source)
        self.log = log
        self.progress = progress   # progress(stage, done, total, text)
        self.cancel_event = cancel_event
        self.work_dir = self.app_dir / UPDATE_DIR

    def _check_cancel(self):
        if self.cancel_event is not None and self.cancel_event.is_set():
            raise UpdateCancelled()

    # ---------------- 检查 ----------------
    def check(self):
        """对比清单与本地文件。返回 UpdateInfo；已是最新返回 None。"""
        local = read_version(self.app_dir)
        local_build = int(local.get("build") or 0)
        self.log(f"检查更新（{self.source.describe()}）...")
        manifest = self.source.fetch_manifest()
        remote_build = int(manifest.get("build") or 0)
        if remote_build <= local_build:
            self.log(f"已是最新版本 v{local.get('version', '?')}")
            return None

        files = manifest.get("files") or {}
        if not files:
            raise UpdateError("更新清单为空")
        need = []
        total = len(files)
        for index, (rel, meta) in enumerate(sorted(files.items())):
            self._check_cancel()
            rel = _safe_rel(rel)
            self.progress("check", index + 1, total, rel)
            if rel == VERSION_FILE:
                continue  # 版本标记由客户端更新成功后自行写入，不参与差异
            target = self.app_dir / rel
            try:
                stat = target.stat()
            except OSError:
                need.append(rel)
                continue
            if stat.st_size != int(meta["size"]) or sha256_file(target) != meta["sha256"]:
                need.append(rel)

        if not need:
            # 文件已一致（如刚手动解压过新包），补写版本号即可
            self._write_version(manifest)
            self.log(f"本地文件已与 v{manifest['version']} 一致，已更新版本标记")
            return None

        need_set = set(need)
        packs, covered = [], set()
        for pack in manifest.get("packs") or []:
            wanted = need_set & set(pack.get("files") or [])
            if wanted - covered:
                packs.append(pack)
                covered |= wanted
        missing = need_set - covered
        if missing:
            sample = "、".join(sorted(missing)[:3])
            raise UpdateError(
                f"更新源缺少 {len(missing)} 个文件的分包（如 {sample}），"
                "请联系作者重新发布或获取完整包"
            )
        download_bytes = sum(int(p["size"]) for p in packs)
        self.log(
            f"发现新版本 v{manifest['version']}：{len(need)} 个文件需更新，"
            f"需下载 {download_bytes / 1024 / 1024:.1f} MB"
        )
        runtime_need = [r for r in need if r.startswith(RUNTIME_PREFIXES)]
        if runtime_need:
            sample = "、".join(runtime_need[:3])
            self.log(f"运行库变动 {len(runtime_need)} 个文件（{sample} ...）")
        return UpdateInfo(manifest, need, packs, download_bytes,
                          local.get("version", ""))

    # ---------------- 下载 ----------------
    def download(self, info):
        """下载分包并解出需要的文件到暂存区，逐文件校验哈希。"""
        staging = self.work_dir / "staging"
        downloads = self.work_dir / "downloads"
        for directory in (staging, downloads):
            shutil.rmtree(directory, ignore_errors=True)
            directory.mkdir(parents=True, exist_ok=True)

        need_set = set(info.need)
        done_bytes = 0
        for pack in info.packs:
            self._check_cancel()
            location = self.source.pack_location(pack)
            dest = downloads / pack["name"]
            self.log(f"下载 {pack['name']}（{int(pack['size']) / 1024 / 1024:.1f} MB）...")
            self._fetch_verified(location, dest, pack["sha256"], int(pack["size"]),
                                 done_bytes, info.download_bytes)
            done_bytes += int(pack["size"])
            self._extract_needed(dest, pack, need_set, staging, info.manifest["files"])
            try:
                dest.unlink()
            except OSError:
                pass
        for rel in info.need:
            if not (staging / rel).is_file():
                raise UpdateError(f"分包中缺少文件: {rel}")
        self.log("下载与校验完成")
        return staging

    def _fetch_verified(self, location, dest, sha256, size, base_done, grand_total,
                        attempts=2):
        last_error = None
        for attempt in range(1, attempts + 1):
            try:
                self._fetch_once(location, dest, sha256, size, base_done, grand_total)
                return
            except (urllib.error.URLError, OSError, UpdateError) as exc:
                last_error = exc
                if attempt < attempts:
                    self.log(f"下载失败（{exc}），重试 {attempt}/{attempts - 1} ...")
                    time.sleep(2)
        raise UpdateError(f"下载 {dest.name} 失败: {last_error}")

    def _fetch_once(self, location, dest, sha256, size, base_done, grand_total):
        digest = hashlib.sha256()
        written = 0
        if isinstance(location, Path) or "://" not in str(location):
            stream = open(location, "rb")
        else:
            stream = _http_get(str(location), timeout=30)
        try:
            with open(dest, "wb") as out:
                while True:
                    self._check_cancel()
                    block = stream.read(_CHUNK)
                    if not block:
                        break
                    digest.update(block)
                    out.write(block)
                    written += len(block)
                    self.progress("download", base_done + written, grand_total,
                                  dest.name)
        finally:
            stream.close()
        if written != size or digest.hexdigest() != sha256:
            raise UpdateError(f"{dest.name} 校验失败（下载损坏）")

    def _extract_needed(self, zip_path, pack, need_set, staging, manifest_files):
        wanted = need_set & set(pack.get("files") or [])
        with zipfile.ZipFile(zip_path) as bundle:
            names = set(bundle.namelist())
            for rel in sorted(wanted):
                self._check_cancel()
                if rel not in names:
                    raise UpdateError(f"{pack['name']} 中缺少 {rel}")
                target = staging / _safe_rel(rel)
                target.parent.mkdir(parents=True, exist_ok=True)
                with bundle.open(rel) as src, open(target, "wb") as out:
                    shutil.copyfileobj(src, out, _CHUNK)
                meta = manifest_files[rel]
                if (target.stat().st_size != int(meta["size"])
                        or sha256_file(target) != meta["sha256"]):
                    raise UpdateError(f"{rel} 解压后校验失败")

    # ---------------- 应用 ----------------
    def apply(self, info):
        """把暂存区文件替换进应用目录。返回新 GUI exe 路径。

        运行中的 exe / 已加载 dll 无法覆盖但可以改名：先把旧文件挪进
        trash（同卷 rename，瞬间完成），再把新文件挪到位。任一步失败则
        按日志回滚，保证目录不会停在半新半旧状态。
        """
        staging = self.work_dir / "staging"
        trash = self.work_dir / "trash"
        shutil.rmtree(trash, ignore_errors=True)
        trash.mkdir(parents=True, exist_ok=True)

        # 数据文件在前，两个 exe 压轴（GUI 最后），中途失败影响面最小
        def order(rel):
            return {CORE_EXE: 1, GUI_EXE: 2}.get(rel, 0)

        ordered = sorted(info.need, key=lambda rel: (order(rel), rel))
        journal = []  # (rel, moved_to_trash)
        total = len(ordered)
        try:
            for index, rel in enumerate(ordered):
                self.progress("apply", index + 1, total, rel)
                source = staging / rel
                target = self.app_dir / rel
                target.parent.mkdir(parents=True, exist_ok=True)
                moved = False
                if target.exists():
                    backup = trash / rel
                    backup.parent.mkdir(parents=True, exist_ok=True)
                    os.rename(target, backup)
                    moved = True
                os.replace(source, target)
                journal.append((rel, moved))
        except OSError as exc:
            self.log(f"替换 {rel} 失败（{exc}），正在回滚 ...")
            self._rollback(journal, trash)
            raise UpdateError(
                f"替换文件失败: {exc}\n已回滚到原版本。"
                "若反复出现，请关闭占用程序（杀毒软件/资源管理器预览）后重试"
            ) from exc

        self._write_version(info.manifest)
        removed = self._clean_stale(info.manifest["files"])
        if removed:
            self.log(f"已清理 {removed} 个旧版本残留文件")
        shutil.rmtree(staging, ignore_errors=True)
        self.log(f"已更新到 v{info.version}")
        return self.app_dir / GUI_EXE

    def _rollback(self, journal, trash):
        for rel, moved in reversed(journal):
            target = self.app_dir / rel
            try:
                if target.exists():
                    os.replace(target, trash / (rel + ".failed_new"))
                if moved:
                    os.rename(trash / rel, target)
            except OSError:
                pass  # 尽力回滚；剩余不一致会被下次更新的哈希对比修复

    def _clean_stale(self, manifest_files):
        """删除托管目录里清单不再包含的文件（模板改名、依赖移除等）。"""
        removed = 0
        for prefix in MANAGED_CLEAN_PREFIXES:
            # 清单里该目录为空时不清理，避免异常清单清空整个目录
            if not any(rel.startswith(prefix) for rel in manifest_files):
                continue
            base = self.app_dir / prefix
            if not base.is_dir():
                continue
            for item in base.rglob("*"):
                if not item.is_file():
                    continue
                rel = item.relative_to(self.app_dir).as_posix()
                if rel not in manifest_files:
                    try:
                        item.unlink()
                        removed += 1
                    except OSError:
                        backup = self.work_dir / "trash" / rel
                        backup.parent.mkdir(parents=True, exist_ok=True)
                        try:
                            os.rename(item, backup)
                            removed += 1
                        except OSError:
                            self.log(f"无法清理旧文件: {rel}")
        return removed

    def _write_version(self, manifest):
        payload = {
            "version": manifest["version"],
            "build": manifest["build"],
            "updated": time.strftime("%Y-%m-%d %H:%M"),
        }
        (self.app_dir / VERSION_FILE).write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8",
        )


def cleanup_leftovers(app_dir):
    """启动时清理上次更新的回收站/暂存区。旧 exe 此刻已退出，可以删除；
    删不掉（另一个实例还活着）就留给下一次。"""
    work = Path(app_dir) / UPDATE_DIR
    if work.exists():
        shutil.rmtree(work, ignore_errors=True)
