# 开发指南

使用带 Tk 的 Windows 64 位 Python 3.11，在仓库根目录执行以下命令。

## 从源码运行

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\pythonw.exe original/source/memory_widget.py
# 或运行网络版
.\.venv\Scripts\pythonw.exe network/source/memory_widget.py
```

原版配置位于源码或 EXE 同目录的 `settings.json`；网络版配置位于 `%LOCALAPPDATA%/MemoryNetWidget`，可用 `MEMORYNET_DATA_DIR` 指定其他本地目录。升级或更换版本前请先退出已有实例。

## 构建单文件 EXE

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
.\.venv\Scripts\python.exe tools/build.py original
.\.venv\Scripts\python.exe tools/build.py network
```

输出分别为 `original/build/MemoryWidget.exe` 和 `network/build/内存球网络版.exe`。GitHub Releases 中的下载文件分别命名为 `MemoryBall.exe` 和 `MemoryBall-Network.exe`。

建议在独立的虚拟环境中构建，避免收集无关的本地依赖。发布前检查打包内容、在隔离配置下验证 EXE，并生成 SHA-256 校验值。

检查包内资源和 Python 代码文件名：

```powershell
python tools/check_release.py original/build/MemoryWidget.exe network/build/内存球网络版.exe
```

## 检查

```powershell
python tools/check_privacy.py
python tools/check_syntax.py
python -m unittest discover -s network/tools -p "test_*.py"
python network/tools/check_portable_faults.py
python original/tools/check_dpi_change.py
python original/tools/check_chart_startup.py
```

界面检查需要交互式 Windows 桌面：

```powershell
python original/tools/check_memory_widget_ui.py
python network/tools/check_network_ui.py --no-capture
```

打包后检查网络版：

```powershell
python network/tools/smoke_portable.py
python network/tools/check_packaged_faults.py
```

各版本 `tools/` 目录还保留了渲染、布局与生命周期检查。生成的配置、图片和诊断输出仅用于本地验证。提交前检查暂存区内容；维护规则见 [AGENTS.md](../AGENTS.md)，数据边界见 [PRIVACY.md](../PRIVACY.md)。
