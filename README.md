# 内存球 · Memory Ball

Windows 桌面悬浮监控工具，包含原版与网络版。两版保留独立源码、配置和构建输出，方便分别维护。

| 版本 | 目录 | 功能 |
| --- | --- | --- |
| 原版 | `original/` | 内存占用、趋势、进程列表、GPU 信息、托盘与主题设置 |
| 网络版 | `network/` | 在内存监控基础上增加本机网卡上传、下载速度与趋势；增强 GPU 识别 |

支持 Windows 10 / 11，建议使用带 Tk 的 64 位 Python 3.11。GPU 传感器是否可用取决于设备与驱动，不可用时显示未知状态。网络速度来自本地网卡计数，包含局域网流量。

## 从源码运行

在仓库根目录执行：

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\pythonw.exe original/source/memory_widget.py
# 或运行网络版
.\.venv\Scripts\pythonw.exe network/source/memory_widget.py
```

点击悬浮球打开详情，拖动调整位置；右键打开设置或退出。程序支持查看进程占用及用户主动结束进程。

## 构建单文件 EXE

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
.\.venv\Scripts\python.exe tools/build.py original
.\.venv\Scripts\python.exe tools/build.py network
```

产物分别为 `original/build/MemoryWidget.exe` 和 `network/build/内存球网络版.exe`。构建目录、日志和 EXE 不进入版本控制；发布二进制前应从本仓库源码重新构建并单独检查。

## 检查

```powershell
python tools/check_privacy.py
python tools/check_syntax.py
python -m unittest discover -s network/tools -p "test_*.py"
python network/tools/check_portable_faults.py
python original/tools/check_dpi_change.py
python original/tools/check_chart_startup.py
```

图形界面检查需交互式 Windows 桌面：

```powershell
python original/tools/check_memory_widget_ui.py
python network/tools/check_network_ui.py --no-capture
```

其他保留的检查脚本在各版本 `tools/` 目录。部分脚本可在本地生成界面图片、诊断报告或读取实时硬件状态；这些输出仅供本地检查，不提交仓库。

## 隐私与维护

公开仓库只包含经过筛选的代码、应用图标、合成测试和维护文档。个人配置、真实运行记录、截图、旧 EXE 和归档均未导入；仓库从新的首次提交开始。

原版配置保存在源码或 EXE 同目录的 `settings.json`；网络版保存在 `%LOCALAPPDATA%/MemoryNetWidget`，可用 `MEMORYNET_DATA_DIR` 指定本地目录。均由运行程序在使用者电脑上生成，默认忽略。

公开版使用通用单实例标识。它与既有本地版本的标识可能不同，升级时请先退出旧实例。

后续可让 Codex 基于本仓库修改、验证并提交更新。维护规则见 [AGENTS.md](AGENTS.md)，脱敏范围见 [PRIVACY.md](PRIVACY.md)。
