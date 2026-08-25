# 王者荣耀农场自动化工具

通过 ADB、OpenCV 模板匹配和 RapidOCR，自动完成王者荣耀农场的启动、务农、成熟时间识别、定时等待与下一轮执行。

当前支持：

- 安卓实体手机与雷电模拟器
- Windows、Linux
- 1280×720、2400×1080，并可按逻辑坐标适配其他分辨率
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

作物周期可能为 5、60、480、960、1920 分钟。完整周期包含四次浇水：种植时、周期的 `1/3`、`2/3` 和 `11/15`。每轮以本次一键务农的真实点击时刻重新计算下一次执行时间。

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
│   ├── crop_cycle.json          # 当前作物周期
│   ├── screenshots/             # 模板源截图
│   └── templates/
│       ├── *.png                # 1280×720 默认模板
│       └── 2400x1080/*.png      # 2400×1080 专用模板
├── scripts/
│   ├── check_requirements.py    # 依赖版本检查
│   ├── run_with_log.py          # 跨平台终端与文件双路日志
│   └── stats_server.py          # 本地统计面板（HTTP）
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

Python 依赖：

```text
opencv-python
numpy
rapidocr-onnxruntime
```

## Windows 图形助手（推荐）

不想在任务栏挂一个终端窗口时，使用图形助手：

1. 首次运行双击 `start_gui.bat`（自动创建 venv 并安装依赖，之后拉起界面）。
2. 日常使用双击 `启动农场助手.vbs`，全程无控制台窗口。

界面提供：

- 启动 / 停止挂机按钮，实时滚动日志（同时写入日志文件）
- 亮度模式下拉选择（代替终端的 Y/R/1/N 交互）
- 累计统计与下一轮启动倒计时
- 深色 / 浅色模式切换（首次启动跟随系统设置，含标题栏）
- 选项记忆：打开助手后自动开始、启动直接进托盘、脚本异常退出自动重启、深色模式

点击窗口关闭按钮或"缩到托盘"后，助手隐藏到系统托盘（不占任务栏）；
双击托盘图标恢复窗口，托盘右键菜单可直接启动/停止挂机或退出。
托盘图标绿色表示挂机运行中，灰色表示未运行。

停止挂机时助手会通知脚本优雅退出：先退出游戏、恢复手机亮度，再结束进程；
即使助手进程被强制结束，挂机脚本也会因管道断开而自行退出，不会残留后台进程。

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

如果作物周期记录错误，可删除：

```text
assets/crop_cycle.json
```

脚本会在下次确认新种植时重新计算并保存周期。

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
