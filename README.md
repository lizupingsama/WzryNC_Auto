# 王者荣耀农场自动化工具

通过 ADB、OpenCV 模板匹配和 RapidOCR，自动完成王者荣耀农场的启动、务农、成熟时间识别、定时等待与下一轮执行。

当前支持：

- 安卓实体手机与雷电模拟器
- Windows、Linux
- 任意分辨率：已有模板按屏幕高度比例自动缩放复用（内置 1280×720、2400×1080、3200×1440 模板）
- 单设备自动选择，多设备显式指定
- 普通亮度、ROOT 亮度 0/1、退出时恢复亮度
- 游戏更新后的多模板、ROI 和有限尺度匹配
- 失败现场自动保存

## 工作流程

```text
检查设备与游戏状态
        ↓
启动王者荣耀并等待登录页
        ↓
关闭启动弹窗 → 点击开始游戏
        ↓
关闭大厅活动弹窗
        ↓
进入王者农场
        ↓
刷新站位并移动到雕像
        ↓
一键务农（收获 / 播种 / 浇水）
        ↓
识别收获信息与成熟时间
        ↓
计算下一次浇水时间
        ↓
退出游戏并等待唤醒
```

每轮务农后按剩余成熟时间匹配浇水档位：剩余 ≤ 1 小时 → 1h 档，1~8 小时 → 8h 档，8~16 小时 → 16h 档，其余 → 32h 档。在档位的最佳浇水节点表（如 1h 档为剩余 44/40/20 分钟）中取小于等于剩余时间的最大节点，剩余时间倒数到节点值时浇水一次（例：剩余 50 分钟 → 节点 44 → 6 分钟后浇水）；低于最后节点则直接等成熟收获。浇水缩短剩余时间后，下一轮会自然落入更低节点，不会因反复以当前时刻重排导致浇水间隔越来越小。

## 项目结构

```text
WzryNC_Auto/
├── wzry_auto.py                 # 主程序
├── wzry_gui.py                  # 图形助手（窗口 + 系统托盘）
├── stats.html                   # 统计面板页面（可直接双击打开）
├── start.bat                    # Windows 一键启动（终端模式）
├── start_gui.bat                # Windows 图形助手启动（首次运行/装依赖）
├── 启动农场助手.vbs             # 图形助手静默启动（日常使用，无任何窗口闪现）
├── start.sh                     # Linux 一键启动
├── monitor.sh                   # Linux 状态检查
├── realtime_monitor.sh          # Linux 实时日志
├── requirements.txt
├── assets/
│   ├── screenshots/             # 模板源截图
│   └── templates/
│       ├── *.png                # 1280×720 默认模板
│       └── 2400x1080/*.png      # 2400×1080 专用模板
├── scripts/
│   ├── check_requirements.py    # 依赖版本检查
│   ├── run_with_log.py          # 跨平台终端与文件双路日志
│   └── stats_server.py          # 本地统计面板（HTTP）
├── packaging/
│   ├── build_release.py         # 一键构建绿色分发包
│   ├── wzry_release.spec        # PyInstaller 配置（双 exe 共享运行库）
│   └── 使用说明.txt             # 随包分发给接收方的说明
└── tests/
    └── test_core.py             # 离线核心测试
```

运行过程中会自动生成：

- `assets/current.png`：最近一次设备截图
- `assets/stats.json`：统计数据（统计面板数据源）
- `assets/stats_data.js`：面板离线快照数据
- `assets/gui_config.json`：图形助手的选项记忆
- `diagnostics/`：关键步骤失败现场
- `/tmp/wzry_run.log`：Linux 默认日志
- `%TEMP%\wzry_run.log`：Windows 默认日志

这些文件不会提交到 Git。

## 环境要求

- Python 3.11–3.13
- Android platform-tools（ADB）
- 已开启 USB 调试或无线调试的安卓设备
- Windows 使用 PowerShell 5+ 完成多实例检查
- ROOT 亮度模式需要设备已 ROOT

Python 依赖（见 `requirements.txt`）：

```text
opencv-python
numpy
rapidocr-onnxruntime
pystray
Pillow
customtkinter
```

## Windows 图形助手（推荐）

不想在任务栏挂一个终端窗口时，使用图形助手：

1. 首次运行双击 `start_gui.bat`（自动创建 venv 并安装依赖，之后拉起界面）。
2. 日常使用双击 `启动农场助手.vbs`，全程无控制台窗口。

界面基于 CustomTkinter（圆角卡片、开关控件、深浅色主题），提供：

- 启动 / 停止挂机按钮，实时滚动日志（同时写入日志文件）
- 亮度模式下拉选择（代替终端的 Y/R/1/N 交互）
- 无线 ADB：设备地址连接、USB 一键转无线、Android 11+ 无线配对（见下节）
- 设备状态实时显示（标题栏）：设备名（市场名/型号）+ 已连接 / 未连接 / 未授权 / 离线
- 锁屏密码：预留手机锁屏密码，唤醒屏幕后自动上滑并输入解锁，旁边的 👁 按钮可切换明文显示
  （无密码留空；密码明文保存在本机 `assets/gui_config.json`，介意请改用环境变量 `WZRY_UNLOCK_PWD`）
- 日志合并重复（默认开，类似 Unity 控制台 Collapse）：内容相同的日志行只保留一行、
  行尾累加 ×N 计数，离线重连等周期性日志不再刷屏；可用日志标题右侧开关关闭
- 统计卡片：轮数、收获、经验、下一轮启动倒计时与作物明细
- 外观三档切换：浅色 / 深色 / 跟随系统（含标题栏，首次启动跟随系统）
- 选项记忆：自动开始、启动进托盘、异常自动重启、外观模式、无线设备地址、锁屏密码、日志合并

点击窗口关闭按钮或"缩到托盘"后，助手隐藏到系统托盘（不占任务栏）；
双击托盘图标恢复窗口，托盘右键菜单可直接启动/停止挂机或退出。
托盘图标绿色表示挂机运行中，灰色表示未运行。

停止挂机时助手会通知脚本优雅退出：先退出游戏、恢复手机亮度，再结束进程；
即使助手进程被强制结束，挂机脚本也会因管道断开而自行退出，不会残留后台进程。

## 无线 ADB（免数据线挂机）

手机和电脑连同一 WiFi 即可无线挂机，图形助手的"无线ADB"一行提供三种上手方式：

- **USB转无线**：手机先用数据线连接并允许 USB 调试，点击按钮后自动完成
  `adb tcpip 5555` → 读取手机 WiFi 地址 → 无线连接 → 回填设备地址，
  之后即可拔掉数据线（手机重启后需重新执行一次）
- **连接**：已知地址时直接在输入框填 `IP:端口`（不填端口默认 5555）点击连接；
  连接成功的地址会记住，下次启动挂机自动使用
- **配对…**（Android 11+，全程免数据线）：手机 开发者选项 → 无线调试 →
  使用配对码配对设备，把弹窗中的 IP:端口 和六位配对码填入对话框完成配对；
  再把无线调试主页面显示的 IP:端口（与配对端口不同）填入设备地址点"连接"

设备地址留空时保持原有行为：自动选择唯一的 USB 设备。

终端模式通过 `WZRY_DEVICE=192.168.1.100:5555` 指定无线设备（见下文配置环境变量）。

无线连接容易被手机省电策略或路由器断开，挂机核心已内置自动重连：
每轮务农开始前检查设备状态，掉线时自动 `adb connect` 重试（约 1 分钟内 6 次）；
仍失败（如带手机外出）只记一轮失败，之后每 30 秒重试、恢复后自动继续挂机，
界面"下次启动"卡片会显示重连倒计时；ADB 命令超时后也会先尝试重连再进入下一轮。

长期无线挂机建议：用「USB转无线」方式（端口固定 5555），并在路由器里给手机
绑定固定 IP（DHCP 静态租约），否则手机重新入网拿到新 IP 后将无法自动重连。
Android 11+ 的"无线调试"开关在离开网络后常被系统自动关闭且端口随机变化，
适合临时调试，不适合长期挂机。

## 打包分发（发给别人一键使用）

把项目打成绿色免安装包，接收方无需安装 Python、依赖和 ADB，解压双击即用：

```cmd
venv\Scripts\python packaging\build_release.py
```

构建脚本会自动：

- 检查打包解释器：PyInstaller 不支持 Python 3.10.0（dis 模块 bug，3.10.1 修复），
  过旧时自动用系统里 3.10.1+ 的 Python 创建 `packaging\buildvenv` 并切换
- 安装 PyInstaller（仅首次）
- 把 `wzry_gui.py`、`wzry_auto.py` 打成 `农场助手.exe` + `wzry_core.exe`（共享一套运行库）
- 复制识别模板、`stats.html`、`使用说明.txt`
- 内置本机的 adb（`platform-tools/`，可用 `--skip-adb` 跳过）
- 运行打包自检（依赖 / 模板 / OCR / adb，不连接设备）
- 生成 `dist\王者农场助手\` 目录和 `dist\王者农场助手_vYYYYMMDD.zip`

把 zip 发给对方，解压后双击 `农场助手.exe` 即可。接收方只需要一台
Windows 10/11 64 位电脑和开了 USB 调试的安卓手机（或雷电模拟器）。

### 在线更新（改完代码不用再挨个发压缩包）

完整 zip 只在第一次给新用户；之后每次改动用一条命令发布，老用户的助手会
自动发现新版本，点一下就完成增量下载、文件替换和重启：

```cmd
venv\Scripts\python packaging\build_release.py --publish
```

原理与产物：

- 构建后按文件 sha256 生成 `manifest.json`，并打两个分包：
  `app_版本.zip`（两个 exe + 模板 + 页面，约 10MB，每次发布都上传）、
  `runtime_版本.zip`（`_internal` + `platform-tools`，约 80MB，
  仅依赖变化导致内容变动时才重新上传，平时直接复用旧版本的附件）
- 发布即在 Gitee 仓库创建一个 Release（tag 如 `v20260827-1`），
  把 manifest 和分包传为附件；客户端匿名走 Gitee API 检查最新版
- 客户端逐文件对比哈希，只下载覆盖差异的分包、只解压需要的文件，
  校验通过后热替换（运行中的 exe 先改名挪进 `_update\trash`，重启后清理），
  任一步失败自动回滚，不会出现半新半旧
- 版本状态记录在 `packaging\release_state.json`（建议随代码提交，
  换机器构建也能接着发布）；`--publish-dir \\nas\wzry` 可发布到局域网
  共享目录，接收方在 `assets\gui_config.json` 写 `"update_url"` 指向它即可

一次性准备：Gitee 头像 → 设置 → 安全设置 → 私人令牌，生成一个勾选
`projects` 的 token，设为环境变量 `GITEE_TOKEN`（或写入
`packaging\gitee_token.txt`，该文件已被 gitignore，绝不会入库）。

客户端行为：启动 8 秒后与每 6 小时静默检查一次（可在 `gui_config.json`
里 `"auto_update_check": false` 关闭）；发现新版本时弹窗展示更新说明，
「立即更新」会先优雅停止挂机再更新重启。把 `"auto_update": true` 写进
配置则发现即自动更新（适合长期无人值守的挂机机器）。更新说明缺省取
上次发布以来的 `git log`，也可用 `--notes "文字"` 指定。

打包版说明：

- 两个 exe 与 `_internal/` 运行库同级，模板在包内 `assets\templates\` 下可直接替换；
  统计、作物周期、失败现场等运行数据同样生成在包目录中（绿色便携）
- `wzry_core.exe` 由助手自动调用；直接双击运行等价终端模式
- exe 未签名，首次运行可能出现 SmartScreen 提示（更多信息 → 仍要运行），
  部分杀毒软件可能误报，需要添加信任
- 验证某个已解压的包是否完好：`wzry_core.exe --selftest`

## Windows 一键启动（终端模式）

1. 安装 Python，并勾选 `Add Python to PATH`。
2. 安装 Android platform-tools，并将 ADB 加入 PATH。
3. 连接手机或启动模拟器：

```cmd
adb devices
```

4. 双击 `start.bat`。

PowerShell 中运行：

```powershell
.\start.bat
```

启动器会自动：

- 创建或复用 `venv`
- 检查并安装缺失/过旧的依赖
- 阻止重复启动
- 使用 Windows Terminal（可用时）
- 将日志写入 `%TEMP%\wzry_run.log`

## Linux 一键启动

```bash
chmod +x start.sh
./start.sh
```

启动器会按以下顺序复用虚拟环境：

1. `WZRY_VENV_DIR`
2. `.venv`
3. `venv`
4. 新建 `.venv`

Linux 状态检查：

```bash
./monitor.sh
```

实时查看日志：

```bash
./realtime_monitor.sh
```

## 配置环境变量

### 指定设备

只有一个在线设备时会自动选择。多个设备同时在线时必须指定：

Windows：

```cmd
set WZRY_DEVICE=192.168.1.100:5555
start.bat
```

Linux：

```bash
WZRY_DEVICE=192.168.1.100:5555 ./start.sh
```

### 其他配置

| 变量 | 作用 |
|---|---|
| `WZRY_DEVICE` | ADB 设备序列号、IP 或模拟器地址 |
| `WZRY_DEFAULT_DEVICE` | 没有在线设备时尝试连接的默认无线设备 |
| `WZRY_ADB` | 自定义 ADB 可执行文件路径 |
| `WZRY_VENV_DIR` | 自定义虚拟环境目录 |
| `WZRY_LOG_FILE` | 自定义日志路径 |
| `WZRY_UNLOCK_PWD` | 锁屏密码；无密码时留空 |
| `WZRY_STATS_PORT` | 统计面板端口，默认 8765 |
| `WZRY_LOCK_PORT` | 单实例锁端口；默认按设备号自动分配 |
| `WZRY_BRIGHTNESS` | 亮度选项（Y/R/1/N），设置后跳过启动时的交互询问 |
| `WZRY_GUI_LOCK_PORT` | 图形助手单实例锁端口，默认 47251 |
| `PYTHON_BIN` | Linux 创建虚拟环境所用的 Python |

## 亮度模式

终端模式启动时交互选择，图形助手在下拉框中选择（也可用 `WZRY_BRIGHTNESS` 预设）：

```text
Y - 普通模式，关闭自动亮度并设置为 1
R - ROOT 模式，将背光节点设置为 0
1 - ROOT 模式，将背光节点设置为 1
N - 不修改亮度
```

正常退出、异常和 Ctrl+C 中断都会尝试退出游戏并恢复原始亮度。

等待时间超过 3 分钟时会自动熄灭手机屏幕，下一轮开始时自动唤醒并解锁（有锁屏密码需设置 `WZRY_UNLOCK_PWD`）。

脚本内置按设备区分的单实例锁：同一台设备重复启动会直接退出，多设备并行需为每个实例指定不同的 `WZRY_DEVICE`。

## 统计面板

两种打开方式，页面相同（右上角标注当前模式）：

- **实时模式**：脚本运行时自动启动本机服务，浏览器访问 `http://localhost:8765`，每 10 秒自动刷新；
- **离线模式**：直接双击项目根目录的 `stats.html`，无需任何服务在运行，展示最近一次保存的快照。

面板展示累计统计（轮数、收获、经验、作物）、每一轮的完成状态与收获明细、下一轮启动时间（浇水或成熟）及实时倒计时。数据持久化在 `assets/stats.json`（离线快照为 `assets/stats_data.js`），脚本重启自动恢复历史并继续累计；Ctrl+C 退出时终端同样会打印汇总。端口可用 `WZRY_STATS_PORT` 修改，面板启动失败不影响挂机。

## 模板匹配

当前主要阈值：

| 目标 | 阈值 |
|---|---:|
| 开始游戏 | 0.75 |
| 王者农场入口 | 0.75 |
| 常规弹窗关闭 | 0.90 |
| 赛事弹窗关闭 | 0.78 |
| 刷新站位 | 0.60 |
| 一键务农 | 0.75 |
| 收获继续 | 0.85 |

匹配不只依赖分数，还会限制搜索区域和模板尺度，避免将大厅图标误认为弹窗关闭按钮。游戏更新导致 UI 变化时，应优先根据失败截图更新模板，不建议直接大幅降低阈值。

模板目录按优先级搜索：`assets/templates/<宽>x<高>/`（与截图分辨率完全一致）> 其他分辨率目录（游戏 UI 按屏幕高度等比缩放，模板会按高度比例预缩放后复用，高度最接近的目录优先）> 根目录默认模板。因此换新分辨率的设备通常无需截新模板；只有当所有目录都匹配不到时，再为该分辨率新建专属目录。

条款更新后的《游戏许可及服务协议》确认弹窗只有"拒绝/同意"两个按钮（无 ✕），
挂机会自动点击"同意"（模板 `agree_terms.png`，搜索区刻意排除"拒绝"按钮）。
如不希望自动同意协议，删除各模板目录下的 `agree_terms.png` 即可——
该弹窗将不再被自动处理，出现时需人工在手机上点击。

登录后偶尔会盖一层全屏活动页（如"回归福利"，只有左上角返回箭头、无 ✕）。
等待大厅期间若检测到返回箭头（`back_arrow.png`，新版 UI 各页面通用样式）
会自动点击退出活动页，直到大厅出现。

## 故障诊断

关键步骤失败时会创建：

```text
diagnostics/YYYYMMDD_HHMMSS_step_name/
├── screenshot.png
└── context.json
```

常用检查：

```bash
adb devices -l
tail -100 /tmp/wzry_run.log
./monitor.sh
```

常见设备坑：

- **小米/红米「识别到但点不到」**：HyperOS/MIUI 默认拦截 ADB 模拟点击，`input tap` 抛 SecurityException（日志会打印 🚫 提示）。需在 开发者选项 → 开启「USB调试（安全设置）」（要求登录小米账号并插入 SIM 卡），开启后重新插拔数据线。
- **中文路径截图失败**：platform-tools 35+ 的 `adb pull` 在 Windows 上写入含中文的本地路径会报 `cannot create file/directory`。本项目已改用 `adb exec-out` 流式截图绕开，若自行调用 adb 请避免给它传中文本地路径。

## 测试

Linux：

```bash
venv/bin/python -m unittest discover -s tests -v
bash -n start.sh monitor.sh realtime_monitor.sh
```

Windows：

```cmd
venv\Scripts\python -m unittest discover -s tests -v
```

离线测试不会启动游戏或操作手机。实机验证结束后应确认游戏进程已退出：

```bash
adb -s DEVICE shell pidof com.tencent.tmgp.sgame
```

无输出表示游戏进程不存在。

## 注意事项

- 游戏更新、活动弹窗和主题皮肤都可能改变模板匹配结果。
- 不要同时运行多个脚本操作同一设备。
- 无法确认页面状态时，脚本会保存现场并退出本轮，而不是盲目点击。
- `assets/screenshots/` 是后续模板适配的重要源素材，请勿当作运行时缓存删除。
- ROOT 背光节点具有设备差异，使用前应在目标设备上验证。
