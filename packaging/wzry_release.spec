# -*- mode: python ; coding: utf-8 -*-
"""王者农场助手发布打包：两个 exe 共享同一套 _internal 运行库。

- 农场助手.exe  图形界面（console=False，无窗口）
- wzry_core.exe 挂机核心（console=True：GUI 以隐藏控制台方式拉起，
  内部 adb 子进程附着隐藏控制台不弹黑框；直接双击等价终端模式）

由 packaging/build_release.py 调用，也可手动执行：
  venv\\Scripts\\python -m PyInstaller packaging\\wzry_release.spec --noconfirm
"""
import os

from PyInstaller.utils.hooks import collect_all, collect_dynamic_libs

ROOT = os.path.dirname(SPECPATH)  # noqa: F821  (SPECPATH 由 PyInstaller 注入)
ICON = os.path.join(SPECPATH, "app.ico")  # noqa: F821
if not os.path.exists(ICON):
    ICON = None

# rapidocr 的 onnx 模型与 config.yaml 是包内数据文件，必须显式收集；
# customtkinter 的主题 json / 字体同理
rapid_datas, rapid_bins, rapid_hidden = collect_all("rapidocr_onnxruntime")
ctk_datas, ctk_bins, ctk_hidden = collect_all("customtkinter")

a_gui = Analysis(
    [os.path.join(ROOT, "wzry_gui.py")],
    pathex=[ROOT],
    binaries=ctk_bins,
    datas=ctk_datas,
    hiddenimports=["pystray._win32"] + ctk_hidden,
)

a_core = Analysis(
    [os.path.join(ROOT, "wzry_auto.py")],
    pathex=[ROOT, os.path.join(ROOT, "scripts")],
    binaries=rapid_bins + collect_dynamic_libs("onnxruntime"),
    datas=rapid_datas,
    hiddenimports=rapid_hidden + ["stats_server"],
)

pyz_gui = PYZ(a_gui.pure)
pyz_core = PYZ(a_core.pure)

exe_gui = EXE(
    pyz_gui,
    a_gui.scripts,
    [],
    exclude_binaries=True,
    name="农场助手",
    console=False,
    icon=ICON,
    upx=False,
)

# ("u", None, "OPTION") 等价 python -u：日志不缓冲，实时推给 GUI
exe_core = EXE(
    pyz_core,
    a_core.scripts,
    [("u", None, "OPTION")],
    exclude_binaries=True,
    name="wzry_core",
    console=True,
    icon=ICON,
    upx=False,
)

COLLECT(
    exe_gui, a_gui.binaries, a_gui.datas,
    exe_core, a_core.binaries, a_core.datas,
    name="王者农场助手",
    upx=False,
)
