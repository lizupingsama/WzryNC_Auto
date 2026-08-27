#!/usr/bin/env python3
"""一键构建可分发的绿色版王者农场助手，并可发布在线更新。

在项目根目录用 venv 的 Python 运行：
  venv\\Scripts\\python packaging\\build_release.py             # 完整构建 + 打 zip
  venv\\Scripts\\python packaging\\build_release.py --publish   # 构建并发布到 Gitee
  venv\\Scripts\\python packaging\\build_release.py --publish-dir \\\\nas\\wzry
  venv\\Scripts\\python packaging\\build_release.py --no-zip    # 只构建目录
  venv\\Scripts\\python packaging\\build_release.py --skip-adb  # 不内置 adb

产物：
  dist/王者农场助手/                绿色目录（解压即用，含全部依赖与 adb）
  dist/王者农场助手_vYYYYMMDD.zip   可直接转发的压缩包（首次安装用）
  dist/packs/                       在线更新分包与 manifest.json

发布到 Gitee（老用户在助手里一键在线更新）需要一次性准备 token：
  Gitee 头像 → 设置 → 安全设置 → 私人令牌 → 生成（勾选 projects），
  然后设置环境变量 GITEE_TOKEN，或写入 packaging/gitee_token.txt（已被 gitignore）。
"""
import argparse
import json
import os
import shutil
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
import uuid
import zipfile
from datetime import datetime
from pathlib import Path

# Windows 中文控制台默认 GBK，打印自检输出中的 emoji 会崩
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

PACKAGING_DIR = Path(__file__).resolve().parent
ROOT = PACKAGING_DIR.parent
# WZRY_BUILD_DIST 可把构建输出改到其他目录（例如本机正从 dist 挂机时）
DIST = Path(os.environ.get("WZRY_BUILD_DIST") or ROOT / "dist")
APP_NAME = "王者农场助手"
APP_DIR = DIST / APP_NAME
PACKS_DIR = DIST / "packs"
SPEC = PACKAGING_DIR / "wzry_release.spec"
ICON = PACKAGING_DIR / "app.ico"
BUILD_VENV = PACKAGING_DIR / "buildvenv"
STATE_FILE = PACKAGING_DIR / "release_state.json"
TOKEN_FILE = PACKAGING_DIR / "gitee_token.txt"
DEFAULT_REPO = "lizupingsama/Farm-Automation-Assistant"

sys.path.insert(0, str(ROOT))
import wzry_updater  # noqa: E402  (清单/分包逻辑与客户端共用一份)

ADB_FILES_REQUIRED = ["adb.exe", "AdbWinApi.dll", "AdbWinUsbApi.dll"]
ADB_FILES_OPTIONAL = ["libwinpthread-1.dll", "NOTICE.txt", "source.properties"]


def log(msg):
    print(f"[build] {msg}", flush=True)


def _find_system_python():
    """用 py 启动器寻找 3.10.1+ 的解释器，优先新版本。"""
    launcher = shutil.which("py")
    if not launcher:
        return None
    for ver in ("3.13", "3.12", "3.11", "3.10"):
        probe = subprocess.run(
            [launcher, f"-{ver}", "-c",
             "import sys; print('%d.%d.%d' % sys.version_info[:3])"],
            capture_output=True, text=True,
        )
        if probe.returncode != 0:
            continue
        found = tuple(int(x) for x in probe.stdout.strip().split("."))
        if found >= (3, 10, 1):
            out = subprocess.run(
                [launcher, f"-{ver}", "-c", "import sys; print(sys.executable)"],
                capture_output=True, text=True,
            )
            return out.stdout.strip()
    return None


def ensure_build_interpreter():
    """PyInstaller 无法运行在 Python 3.10.0（CPython dis 模块 bug，3.10.1 修复）。

    运行环境 venv 保持不动；解释器过旧时自动创建/切换到
    packaging/buildvenv（3.10.1+，推荐 3.12）重跑本脚本。
    """
    if sys.version_info[:3] >= (3, 10, 1):
        return
    build_python = BUILD_VENV / "Scripts" / "python.exe"
    if not build_python.exists():
        system_python = _find_system_python()
        if not system_python:
            raise SystemExit(
                f"当前 Python {sys.version.split()[0]} 无法运行 PyInstaller"
                "（3.10.0 的 dis 模块 bug，3.10.1 已修复），且未找到其他可用解释器。\n"
                "请安装 Python 3.12（如 winget install Python.Python.3.12）后重跑本脚本。"
            )
        log(f"创建打包环境 buildvenv（基于 {system_python}）...")
        subprocess.run([system_python, "-m", "venv", str(BUILD_VENV)], check=True)
        index = os.environ.get("PIP_INDEX_URL", "https://mirrors.aliyun.com/pypi/simple/")
        subprocess.run(
            [str(build_python), "-m", "pip", "install",
             "-r", str(ROOT / "requirements.txt"), "pyinstaller", "-i", index],
            check=True,
        )
    log(f"当前解释器 {sys.version.split()[0]} 不支持 PyInstaller，切换到 {build_python}")
    result = subprocess.run([str(build_python), str(Path(__file__).resolve()), *sys.argv[1:]])
    raise SystemExit(result.returncode)


def ensure_pyinstaller():
    try:
        import PyInstaller
        log(f"PyInstaller {PyInstaller.__version__} 已安装")
        return
    except ImportError:
        pass
    log("安装 PyInstaller ...")
    index = os.environ.get("PIP_INDEX_URL", "https://mirrors.aliyun.com/pypi/simple/")
    subprocess.run(
        [sys.executable, "-m", "pip", "install", "pyinstaller", "-i", index],
        check=True,
    )


def make_icon():
    """绘制 256x256 幼苗图标（与 GUI 托盘图标同款的放大版）。"""
    from PIL import Image, ImageDraw
    s = 4  # 64 -> 256
    img = Image.new("RGBA", (64 * s, 64 * s), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    draw.ellipse((2 * s, 2 * s, 62 * s, 62 * s), fill=(76, 175, 80, 255))
    draw.line((32 * s, 54 * s, 32 * s, 30 * s), fill=(255, 255, 255, 255), width=6 * s)
    draw.ellipse((12 * s, 12 * s, 32 * s, 32 * s), fill=(232, 245, 233, 255))
    draw.ellipse((32 * s, 12 * s, 52 * s, 32 * s), fill=(255, 255, 255, 255))
    img.save(
        ICON, format="ICO",
        sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (256, 256)],
    )
    log(f"图标已生成: {ICON}")


def find_adb_dir():
    """定位本机 platform-tools 目录：WZRY_ADB > PATH > 注册表 PATH。"""
    explicit = os.environ.get("WZRY_ADB")
    if explicit and Path(explicit).exists():
        return Path(explicit).resolve().parent
    found = shutil.which("adb")
    if found:
        return Path(found).resolve().parent
    import winreg
    keys = [
        (winreg.HKEY_CURRENT_USER, r"Environment"),
        (winreg.HKEY_LOCAL_MACHINE,
         r"SYSTEM\CurrentControlSet\Control\Session Manager\Environment"),
    ]
    for hive, subkey in keys:
        try:
            with winreg.OpenKey(hive, subkey) as key:
                value, _ = winreg.QueryValueEx(key, "Path")
        except OSError:
            continue
        for directory in os.path.expandvars(value).split(";"):
            directory = directory.strip().strip('"')
            if not directory:
                continue
            candidate = Path(directory) / "adb.exe"
            try:
                if candidate.exists():
                    return candidate.parent
            except OSError:
                continue
    return None


def ensure_dist_not_running():
    """dist 里的助手正在运行时 PyInstaller 清理目录必然失败，提前拦截。"""
    if not APP_DIR.exists():
        return
    probe = subprocess.run(
        ["powershell", "-NoProfile", "-Command",
         "[Console]::OutputEncoding=[Text.Encoding]::UTF8; "
         "Get-Process 农场助手,wzry_core -ErrorAction SilentlyContinue "
         "| ForEach-Object { $_.Path }"],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    app_dir = str(APP_DIR).lower()
    hits = [
        line.strip() for line in (probe.stdout or "").splitlines()
        if line.strip() and line.strip().lower().startswith(app_dir)
    ]
    if hits:
        raise SystemExit(
            "构建目标目录正被运行中的助手占用，请先退出托盘里的农场助手"
            "（或设置 WZRY_BUILD_DIST 输出到其他目录）：\n  "
            + "\n  ".join(hits)
        )


def run_pyinstaller():
    log("PyInstaller 构建中（首次需要几分钟）...")
    env = dict(os.environ)
    # 固定构建时间戳（取 HEAD 提交时间）：代码不变则产物字节级一致，
    # 在线更新的哈希对比才不会把整个运行库误判为“有变化”
    if "SOURCE_DATE_EPOCH" not in env:
        probe = subprocess.run(
            ["git", "show", "-s", "--format=%ct", "HEAD"],
            capture_output=True, text=True, cwd=str(ROOT),
        )
        if probe.returncode == 0 and probe.stdout.strip().isdigit():
            env["SOURCE_DATE_EPOCH"] = probe.stdout.strip()
    subprocess.run(
        [sys.executable, "-m", "PyInstaller", str(SPEC),
         "--noconfirm", "--clean",
         "--distpath", str(DIST),
         "--workpath", str(PACKAGING_DIR / "build")],
        check=True, cwd=str(ROOT), env=env,
    )
    if not (APP_DIR / "农场助手.exe").exists() or not (APP_DIR / "wzry_core.exe").exists():
        raise SystemExit("PyInstaller 产物不完整，检查上方输出")
    normalize_base_library()


def normalize_base_library():
    """PyInstaller 写 base_library.zip 的条目顺序不定（内容/时间戳都一致），
    重排为字典序让未变的运行库两次构建字节一致，在线更新才能复用 runtime 分包。"""
    target = APP_DIR / "_internal" / "base_library.zip"
    if not target.exists():
        return
    with zipfile.ZipFile(target) as source:
        entries = [
            (info, source.read(info.filename))
            for info in sorted(source.infolist(), key=lambda info: info.filename)
        ]
    tmp = target.with_suffix(".tmp")
    with zipfile.ZipFile(tmp, "w") as out:
        for info, data in entries:
            out.writestr(info, data)
    os.replace(tmp, target)
    log("base_library.zip 条目已按字典序规范化")


def copy_data(adb_dir):
    src_templates = ROOT / "assets" / "templates"
    dst_templates = APP_DIR / "assets" / "templates"
    if dst_templates.exists():
        shutil.rmtree(dst_templates)
    shutil.copytree(src_templates, dst_templates)
    log(f"模板已复制: {len(list(dst_templates.rglob('*.png')))} 张")

    shutil.copy2(ROOT / "stats.html", APP_DIR / "stats.html")
    shutil.copy2(PACKAGING_DIR / "使用说明.txt", APP_DIR / "使用说明.txt")
    log("stats.html / 使用说明.txt 已复制")

    if adb_dir:
        dst = APP_DIR / "platform-tools"
        dst.mkdir(exist_ok=True)
        for name in ADB_FILES_REQUIRED:
            src = adb_dir / name
            if not src.exists():
                raise SystemExit(f"platform-tools 缺少 {name}: {adb_dir}")
            shutil.copy2(src, dst / name)
        for name in ADB_FILES_OPTIONAL:
            src = adb_dir / name
            if src.exists():
                shutil.copy2(src, dst / name)
        log(f"内置 adb 已复制（来自 {adb_dir}）")


def selftest():
    log("运行打包自检（依赖/模板/OCR/adb，不连接设备）...")
    result = subprocess.run(
        [str(APP_DIR / "wzry_core.exe"), "--selftest"],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        timeout=300,
    )
    print(result.stdout)
    if result.returncode != 0 or "SELFTEST OK" not in (result.stdout or ""):
        print(result.stderr)
        raise SystemExit("自检失败，终止发布")
    log("自检通过")


def make_zip():
    stamp = datetime.now().strftime("%Y%m%d")
    base = DIST / f"{APP_NAME}_v{stamp}"
    zip_path = base.with_suffix(".zip")
    if zip_path.exists():
        zip_path.unlink()
    log("压缩 zip 中 ...")
    archive = shutil.make_archive(str(base), "zip", root_dir=str(DIST), base_dir=APP_NAME)
    log(f"压缩包: {archive}（{Path(archive).stat().st_size / 1024 / 1024:.0f} MB）")


def dir_size_mb(path):
    return sum(f.stat().st_size for f in Path(path).rglob("*") if f.is_file()) / 1024 / 1024


# ----------------------------------------------------------------------
# 在线更新发布
# ----------------------------------------------------------------------
def load_state():
    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def save_state(state):
    STATE_FILE.write_text(
        json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8",
    )


def next_version(state):
    """版本号 = 日期-当日序号，构建号数值单调递增用于新旧比较。"""
    today = datetime.now().strftime("%Y%m%d")
    seq = 1
    last = str(state.get("last_version", ""))
    if last.startswith(today + "-"):
        seq = int(last.split("-")[1]) + 1
    version = f"{today}-{seq}"
    return version, int(f"{today}{seq:02d}")


def write_version_file(version, build):
    payload = {
        "version": version,
        "build": build,
        "built": datetime.now().strftime("%Y-%m-%d %H:%M"),
    }
    (APP_DIR / wzry_updater.VERSION_FILE).write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8",
    )
    log(f"版本号 v{version}（构建号 {build}）")


def release_notes(args, state):
    if args.notes:
        return args.notes
    since = state.get("last_commit")
    fmt_range = f"{since}..HEAD" if since else "-20"
    probe = subprocess.run(
        ["git", "log", fmt_range, "--pretty=format:- %s", "--no-merges"],
        capture_output=True, text=True, encoding="utf-8", cwd=str(ROOT),
    )
    lines = [l for l in (probe.stdout or "").splitlines() if l.strip()][:15]
    return "\n".join(lines) if lines else "- 常规更新"


def git_head():
    probe = subprocess.run(
        ["git", "rev-parse", "HEAD"], capture_output=True, text=True, cwd=str(ROOT),
    )
    return probe.stdout.strip() if probe.returncode == 0 else ""


def gitee_token():
    token = os.environ.get("GITEE_TOKEN", "").strip()
    if not token and TOKEN_FILE.exists():
        token = TOKEN_FILE.read_text(encoding="utf-8").strip()
    if not token:
        raise SystemExit(
            "缺少 Gitee 私人令牌：设置环境变量 GITEE_TOKEN，"
            f"或写入 {TOKEN_FILE}（该文件已被 gitignore）。\n"
            "令牌在 Gitee 头像 → 设置 → 安全设置 → 私人令牌 生成，勾选 projects"
        )
    return token


def _gitee_api(method, url, data=None, headers=None, timeout=60):
    request = urllib.request.Request(
        url, data=data, method=method,
        headers={"User-Agent": wzry_updater.USER_AGENT, **(headers or {})},
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")[:300]
        raise SystemExit(f"Gitee API 失败 {exc.code} {url}\n{detail}") from exc


def gitee_taken_tags(repo):
    """已存在的 tag 集合，用于避开重名发布。查询失败按空集处理。"""
    try:
        rows = _gitee_api(
            "GET", f"https://gitee.com/api/v5/repos/{repo}/tags", timeout=20,
        )
        return {row.get("name") for row in rows if isinstance(row, dict)}
    except (SystemExit, OSError, ValueError):
        return set()


def gitee_create_release(repo, token, tag, title, body):
    log(f"创建 Gitee 发布 {tag} ...")
    payload = urllib.parse.urlencode({
        "access_token": token,
        "tag_name": tag,
        "name": title,
        "body": body or "-",
        "target_commitish": git_head() or "main",
    }).encode("utf-8")
    data = _gitee_api(
        "POST", f"https://gitee.com/api/v5/repos/{repo}/releases", payload,
        {"Content-Type": "application/x-www-form-urlencoded"},
    )
    release_id = data.get("id")
    if not release_id:
        raise SystemExit(f"创建发布失败，响应异常: {data}")
    return release_id


def gitee_upload_asset(repo, token, release_id, tag, path):
    """multipart 上传发布附件，返回附件下载地址。"""
    path = Path(path)
    size_mb = path.stat().st_size / 1024 / 1024
    if size_mb > 95:
        log(f"⚠️ {path.name} 有 {size_mb:.0f} MB，可能超过 Gitee 附件上限（100MB）")
    log(f"上传附件 {path.name}（{size_mb:.1f} MB）...")
    boundary = uuid.uuid4().hex
    body = b"".join([
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="access_token"\r\n\r\n'.encode(),
        token.encode(), b"\r\n",
        f"--{boundary}\r\n".encode(),
        f'Content-Disposition: form-data; name="file"; filename="{path.name}"\r\n'
        "Content-Type: application/octet-stream\r\n\r\n".encode(),
        path.read_bytes(), b"\r\n",
        f"--{boundary}--\r\n".encode(),
    ])
    data = _gitee_api(
        "POST",
        f"https://gitee.com/api/v5/repos/{repo}/releases/{release_id}/attach_files",
        body, {"Content-Type": f"multipart/form-data; boundary={boundary}"},
        timeout=900,
    )
    url = data.get("browser_download_url")
    if not url:
        url = f"https://gitee.com/{repo}/releases/download/{tag}/{path.name}"
        log(f"⚠️ 响应未返回下载地址，按固定格式使用 {url}")
    return url


def ensure_runtime_pack_file(manifest):
    """复用的 runtime 分包在本地缓存缺失时（如换机器构建）重打一个。"""
    pack = next(p for p in manifest["packs"] if p["name"].startswith("runtime_"))
    cached = PACKS_DIR / pack["name"]
    if cached.exists() and wzry_updater.sha256_file(cached) == pack["sha256"]:
        return cached
    log(f"本地缓存缺少 {pack['name']}，重新打包 ...")
    rebuilt = wzry_updater.make_pack(APP_DIR, cached, pack["files"], zipfile.ZIP_LZMA)
    if rebuilt["sha256"] != pack["sha256"]:
        # 运行库内容与记录不符：按新包对待，修正清单
        log("⚠️ 重打的 runtime 包与历史记录不一致，按本次内容发布")
        pack.update(rebuilt)
        pack.pop("url", None)
    return cached


def publish_to_dir(manifest, target):
    target = Path(target)
    target.mkdir(parents=True, exist_ok=True)
    manifest = json.loads(json.dumps(manifest))  # 深拷贝，目录渠道按文件名引用分包
    for pack in manifest["packs"]:
        pack.pop("url", None)
        dest = target / pack["name"]
        if dest.exists() and wzry_updater.sha256_file(dest) == pack["sha256"]:
            continue  # 复用的 runtime 分包已在目录里
        source = PACKS_DIR / pack["name"]
        if not source.exists():
            if not pack["name"].startswith("runtime_"):
                raise SystemExit(f"分包缺失: {source}")
            source = ensure_runtime_pack_file(manifest)
        shutil.copy2(source, dest)
    (target / wzry_updater.MANIFEST_NAME).write_text(
        json.dumps(manifest, ensure_ascii=False, indent=1), encoding="utf-8",
    )
    log(f"已发布到目录 {target}")


def publish_to_gitee(manifest, new_paths, repo, state):
    token = gitee_token()
    tag = f"v{manifest['version']}"
    manifest = json.loads(json.dumps(manifest))  # 深拷贝，Gitee 渠道记录附件 url
    release_id = gitee_create_release(
        repo, token, tag, f"{APP_NAME} v{manifest['version']}", manifest["notes"],
    )
    packs_by_name = {p["name"]: p for p in manifest["packs"]}
    for path in new_paths:
        packs_by_name[Path(path).name]["url"] = gitee_upload_asset(
            repo, token, release_id, tag, path,
        )
    runtime = next(p for p in manifest["packs"] if p["name"].startswith("runtime_"))
    if not runtime.get("url"):
        # 复用的 runtime 包此前只发布过目录渠道，没有附件地址：补传一次
        runtime["url"] = gitee_upload_asset(
            repo, token, release_id, tag, ensure_runtime_pack_file(manifest),
        )
    manifest_path = PACKS_DIR / wzry_updater.MANIFEST_NAME
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=1), encoding="utf-8",
    )
    gitee_upload_asset(repo, token, release_id, tag, manifest_path)
    log(f"已发布到 Gitee: https://gitee.com/{repo}/releases/tag/{tag}")

    state["runtime_key"] = manifest["runtime_key"]
    state["runtime_pack"] = runtime
    return state


def main():
    parser = argparse.ArgumentParser(description="构建王者农场助手绿色发布包")
    parser.add_argument("--skip-adb", action="store_true", help="不内置 adb（接收方需自装）")
    parser.add_argument("--no-zip", action="store_true", help="只构建目录，不打 zip")
    parser.add_argument("--publish", action="store_true",
                        help="发布在线更新到 Gitee Releases（需 GITEE_TOKEN）")
    parser.add_argument("--publish-dir", metavar="DIR",
                        help="发布在线更新到目录（局域网共享 / 静态网站根目录）")
    parser.add_argument("--notes", help="本次更新说明；缺省取上次发布以来的 git log")
    parser.add_argument("--repo", help=f"Gitee 仓库 owner/repo（默认 {DEFAULT_REPO}）")
    parser.add_argument("--skip-build", action="store_true",
                        help="跳过构建，直接用现有 dist 目录发布（补发/调试用）")
    args = parser.parse_args()

    if os.name != "nt":
        raise SystemExit("发布包面向 Windows，请在 Windows 上构建")

    ensure_build_interpreter()

    state = load_state()
    repo = args.repo or state.get("gitee_repo") or DEFAULT_REPO
    if args.publish:
        gitee_token()  # 早失败：没配 token 就不必花几分钟构建了

    if args.skip_build:
        if not (APP_DIR / "农场助手.exe").exists():
            raise SystemExit(f"--skip-build 需要现成的 {APP_DIR}")
        log("跳过构建，使用现有 dist 目录")
    else:
        adb_dir = None
        if not args.skip_adb:
            adb_dir = find_adb_dir()
            if adb_dir is None:
                raise SystemExit(
                    "未找到 adb，无法内置。解决：安装 platform-tools 并加入 PATH，"
                    "或设置 WZRY_ADB 指向 adb.exe，或用 --skip-adb 构建不带 adb 的包"
                )
            log(f"将内置 adb: {adb_dir}")

        ensure_dist_not_running()
        ensure_pyinstaller()
        make_icon()
        run_pyinstaller()
        copy_data(adb_dir)
        selftest()

    version, build_num = next_version(state)
    if args.publish:
        taken = gitee_taken_tags(repo)
        while f"v{version}" in taken:
            date, seq = version.split("-")
            if int(seq) >= 99:
                raise SystemExit("当日发布序号已用尽（>99），请明天再发布")
            version = f"{date}-{int(seq) + 1}"
            build_num = int(f"{date}{int(seq) + 1:02d}")
    write_version_file(version, build_num)

    if args.publish or args.publish_dir:
        notes = release_notes(args, state)
        log(f"更新说明:\n{notes}")
        manifest, new_paths = wzry_updater.make_release_artifacts(
            APP_DIR, PACKS_DIR, version, build_num, notes, state, log=log,
        )
        if args.publish_dir:
            publish_to_dir(manifest, args.publish_dir)
        if args.publish:
            state = publish_to_gitee(manifest, new_paths, repo, state)
            state["gitee_repo"] = repo
        else:
            state["runtime_key"] = manifest["runtime_key"]
            state["runtime_pack"] = next(
                p for p in manifest["packs"] if p["name"].startswith("runtime_")
            )
        state["last_commit"] = git_head()

    state["last_version"] = version
    state["last_build"] = build_num
    save_state(state)

    log(f"发布目录: {APP_DIR}（{dir_size_mb(APP_DIR):.0f} MB）")
    if not args.no_zip:
        make_zip()
    if args.publish or args.publish_dir:
        log("完成。老用户在助手里点「检查更新」即可升级；新用户仍发完整 zip")
    else:
        log("完成。把 zip 发给对方，解压后双击 农场助手.exe 即可使用；"
            "加 --publish 可让老用户在线更新")


if __name__ == "__main__":
    main()
