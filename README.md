# 内存球 · Memory Ball

[![checks](https://github.com/Pikasn/memory-ball/actions/workflows/checks.yml/badge.svg)](https://github.com/Pikasn/memory-ball/actions/workflows/checks.yml)
[![release](https://img.shields.io/github/v/release/Pikasn/memory-ball?display_name=tag)](https://github.com/Pikasn/memory-ball/releases)
[![platform](https://img.shields.io/badge/platform-Windows%2010%2F11-0078D4)](https://github.com/Pikasn/memory-ball/releases)
[![architecture](https://img.shields.io/badge/architecture-x64-555555)](https://github.com/Pikasn/memory-ball/releases)

**适用于 Windows 的桌面内存与网络监控工具。**

内存球以悬浮圆环显示内存占用，点击即可展开详情卡片，查看历史趋势、占用较高的进程和显卡状态。网络版同时提供实时上传、下载速度与趋势曲线，适合在办公、开发和游戏时持续观察电脑状态。

**[下载网络版](https://github.com/Pikasn/memory-ball/releases/latest/download/MemoryBall-Network.exe)** · **[下载原版](https://github.com/Pikasn/memory-ball/releases/latest/download/MemoryBall.exe)** · [全部版本](https://github.com/Pikasn/memory-ball/releases) · [问题反馈](https://github.com/Pikasn/memory-ball/issues)

Windows 10 / 11（64 位）· 单文件 EXE · 免安装 · 无需 Python 环境

## 目录

- [项目特点](#项目特点)
- [功能一览](#功能一览)
- [版本选择](#版本选择)
- [下载与运行](#下载与运行)
- [界面与操作](#界面与操作)
- [数据说明与兼容性](#数据说明与兼容性)
- [从源码运行](#从源码运行)
- [构建与测试](#构建与测试)
- [项目结构](#项目结构)
- [更新日志](#更新日志)
- [反馈与贡献](#反馈与贡献)
- [第三方组件](#第三方组件)
- [English summary](#english-summary)

## 项目特点

- **桌面常驻**：通过悬浮球查看内存占用，点击展开详情，减少反复打开任务管理器的操作。
- **趋势与明细结合**：既能观察内存变化，也能查看同名进程合并后的占用与实例数量。
- **网络状态可视化**：网络版将上传和下载速度放在同一张图中，便于观察文件传输与后台网络活动。
- **可调整的桌面布局**：支持深浅主题、悬浮球尺寸切换，以及详情卡片的移动和缩放。
- **独立运行**：发布版本已包含运行所需的组件，下载后双击运行。

## 功能一览

| 功能 | 说明 |
| --- | --- |
| **内存监控** | 显示物理内存占用率、已用内存与总内存；圆环与状态颜色随占用变化。 |
| **内存趋势** | 原版提供 5 分钟、30 分钟、2 小时视图；网络版提供 60 秒、5 分钟、10 分钟视图。 |
| **进程 Top 5** | 同名进程合并显示工作集与实例数量；操作菜单支持复制进程名和结束进程。 |
| **显卡信息** | 显示显卡型号；在设备与驱动支持时提供温度和利用率。 |
| **网络监控** | 网络版显示实时上传、下载速度，两条曲线共享纵轴，支持 60 秒、5 分钟、10 分钟视图。 |
| **外观设置** | 浅色、深色、跟随系统三种主题；悬浮球提供 64 / 72 / 96 三档设计尺寸。 |
| **窗口交互** | 悬浮球拖动、详情卡片移动与缩放、置顶和悬停展开选项。 |
| **常驻管理** | 系统托盘入口；支持可选开机启动，默认关闭。 |
| **刷新频率** | 内存刷新间隔可选 1 / 2 / 4 秒。 |

## 版本选择

| | 原版 | 网络版 |
| --- | --- | --- |
| 内存占用与趋势 | 支持 | 支持 |
| 进程列表与操作 | 支持 | 支持 |
| 显卡信息 | 支持 | 支持，增强设备识别 |
| 上传 / 下载监控 | — | 支持 |
| 默认详情内容 | 内存趋势与进程列表按需开启 | 默认开启趋势与进程列表 |
| 下载文件 | [MemoryBall.exe](https://github.com/Pikasn/memory-ball/releases/latest/download/MemoryBall.exe) | [MemoryBall-Network.exe](https://github.com/Pikasn/memory-ball/releases/latest/download/MemoryBall-Network.exe) |

需要同时观察内存与网络状态时，选择网络版；主要关注内存占用时，可选择原版。

## 下载与运行

1. 打开 [Releases](https://github.com/Pikasn/memory-ball/releases/latest)，选择需要的 EXE。
2. 将文件保存到一个具有写入权限的固定文件夹。
3. 双击运行。网络版首次启动会展开详情卡片；原版可点击悬浮球查看详情。
4. 右键悬浮球进入设置，调整主题、尺寸、刷新间隔和其他选项。

发布文件均为 **Windows x64 单文件程序**，无需安装 Python。发布页提供 `SHA256SUMS.txt` 用于核对下载文件的完整性。

## 界面与操作

| 操作 | 功能 |
| --- | --- |
| 点击悬浮球 | 展开或收起详情卡片。 |
| 拖动悬浮球 | 调整桌面位置。 |
| 拖动详情卡片空白区域 | 移动详情卡片。 |
| 拖动详情卡片右下角 | 调整卡片尺寸。 |
| 点击趋势图上方的时间选项 | 切换观察时段；网络版的内存与网络视图可分别选择。 |
| 点击进程列表中的一行 | 打开进程操作菜单。 |
| 右键悬浮球 | 打开设置或退出。 |

关闭详情卡片后，程序继续保留悬浮球和托盘入口。完全退出时，在右键菜单中选择“退出”。

设置会在本机保存：原版位于 EXE 同目录的 `settings.json`，网络版位于 `%LOCALAPPDATA%/MemoryNetWidget`。更换或升级程序前，请先退出正在运行的旧实例。

## 数据说明与兼容性

- **内存口径**：总占用来自 Windows 物理内存状态；进程列表使用工作集统计，同名进程合并求和。两者统计口径不同，进程列表的数值不能直接相加作为系统总占用。
- **网络口径**：速度来自本机网卡收发计数，包含局域网流量；KB/s、MB/s 使用十进制字节单位。它反映当前传输活动，宽带测速结果用于衡量链路传输能力。
- **趋势记录**：历史曲线在程序运行期间积累；新启动时尚未采集的时段保留为空白。
- **显卡支持**：型号识别和传感器能力取决于设备与驱动。无法读取的温度或利用率显示为未知，网络读数暂不可用时显示“—”。
- **权限边界**：程序可在普通用户权限下运行；结束权限更高或受系统保护的进程可能被拒绝，界面会报告操作结果。
- **系统范围**：当前发布包面向 Windows 10 / 11 x64。

## 从源码运行

需要 Windows、带 Tk 的 64 位 Python 3.11。

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\pythonw.exe network/source/memory_widget.py
# 原版：将上面的入口改为 original/source/memory_widget.py
```

## 构建与测试

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
.\.venv\Scripts\python.exe tools/build.py original
.\.venv\Scripts\python.exe tools/build.py network
```

构建输出分别位于 `original/build/` 和 `network/build/`。两版使用独立源码、配置与构建目录。

自动检查涵盖网络采样、GPU 设备识别与异常处理、配置校验、DPI 变化和趋势渲染；桌面环境下可进一步运行界面与打包检查。完整命令见 [开发指南](docs/development.md)。

## 项目结构

```text
.
├─ original/
│  ├─ source/               # 原版：内存监控、界面、GPU 与托盘
│  └─ tools/                # 布局、渲染、DPI 与托盘检查
├─ network/
│  ├─ source/               # 网络版：增加网卡采样、网络曲线与 GPU 识别
│  └─ tools/                # 采样、异常处理、界面和打包检查
├─ tools/
│  ├─ build.py              # 单文件 EXE 构建入口
│  ├─ check_release.py      # 发布包内容与代码路径检查
│  ├─ check_privacy.py      # 暂存文件检查
│  └─ check_syntax.py       # Python 语法检查
├─ docs/development.md     # 开发、配置与验证说明
├─ .github/workflows/      # GitHub Actions 检查
└─ requirements*.txt       # 运行与构建依赖
```

## 更新日志

当前公开发布版本为 **v1.0.0**，包含原版与网络版的 Windows x64 单文件程序。版本说明和下载文件见 [Releases](https://github.com/Pikasn/memory-ball/releases)。

## 反馈与贡献

项目由 [Pikasn](https://github.com/Pikasn) 维护。欢迎通过 [Issues](https://github.com/Pikasn/memory-ball/issues) 提交问题或功能建议，也欢迎提交 Pull Request。

反馈问题时，请说明使用的版本、Windows 版本、复现步骤、预期结果与实际结果。开发和提交约定见 [CONTRIBUTING.md](CONTRIBUTING.md)。

## 第三方组件

运行与打包使用 Python、Tcl/Tk、Pillow 和 PyInstaller。组件用途及许可信息见 [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)；发布页附有第三方许可文本。

## English summary

Memory Ball is a Windows desktop monitor with a floating memory-usage ring and an expandable detail panel. It provides memory history, grouped process working sets, GPU information, themes, resizing and system-tray access. The network edition adds live upload/download rates and traffic charts.

Download `MemoryBall.exe` or `MemoryBall-Network.exe` from [Releases](https://github.com/Pikasn/memory-ball/releases). Both are standalone Windows 10/11 x64 executables; Python is only required for source development.
