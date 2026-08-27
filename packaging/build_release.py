#!/usr/bin/env python3
"""一键构建可分发的绿色版王者农场助手。

在项目根目录用 venv 的 Python 运行：
  venv\\Scripts\\python packaging\\build_release.py            # 完整构建 + 打 zip
  venv\\Scripts\\python packaging\\build_release.py --no-zip   # 只构建目录
  venv\\Scripts\\python packaging\\build_release.py --skip-adb # 不内置 adb

产物：
  dist/王者农场助手/                绿色目录（解压即用，含全部依赖与 adb）
  dist/王者农场助手_vYYYYMMDD.zip   可直接转发的压缩包
"""
import argparse
import os
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path

# Windows 中文控制台默认 GBK，打印自检输出中的 emoji 会崩
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

PACKAGING_DIR = Path(__file__).resolve().parent
ROOT = PACKAGING_DIR.parent
DIST = ROOT / "dist"
APP_NAME = "王者农场助手"
APP_DIR = DIST / APP_NAME
SPEC = PACKAGING_DIR / "wzry_release.spec"
ICON = PACKAGING_DIR / "app.ico"
BUILD_VENV = PACKAGING_DIR / "buildvenv"

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


def run_pyinstaller():
    log("PyInstaller 构建中（首次需要几分钟）...")
    subprocess.run(
        [sys.executable, "-m", "PyInstaller", str(SPEC),
         "--noconfirm", "--clean",
         "--distpath", str(DIST),
         "--workpath", str(PACKAGING_DIR / "build")],
        check=True, cwd=str(ROOT),
    )
    if not (APP_DIR / "农场助手.exe").exists() or not (APP_DIR / "wzry_core.exe").exists():
        raise SystemExit("PyInstaller 产物不完整，检查上方输出")


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


def main():
    parser = argparse.ArgumentParser(description="构建王者农场助手绿色发布包")
    parser.add_argument("--skip-adb", action="store_true", help="不内置 adb（接收方需自装）")
    parser.add_argument("--no-zip", action="store_true", help="只构建目录，不打 zip")
    args = parser.parse_args()

    if os.name != "nt":
        raise SystemExit("发布包面向 Windows，请在 Windows 上构建")

    ensure_build_interpreter()

    adb_dir = None
    if not args.skip_adb:
        adb_dir = find_adb_dir()
        if adb_dir is None:
            raise SystemExit(
                "未找到 adb，无法内置。解决：安装 platform-tools 并加入 PATH，"
                "或设置 WZRY_ADB 指向 adb.exe，或用 --skip-adb 构建不带 adb 的包"
            )
        log(f"将内置 adb: {adb_dir}")

    ensure_pyinstaller()
    make_icon()
    run_pyinstaller()
    copy_data(adb_dir)
    selftest()
    log(f"发布目录: {APP_DIR}（{dir_size_mb(APP_DIR):.0f} MB）")
    if not args.no_zip:
        make_zip()
    log("完成。把 zip 发给对方，解压后双击 农场助手.exe 即可使用")


if __name__ == "__main__":
    main()
