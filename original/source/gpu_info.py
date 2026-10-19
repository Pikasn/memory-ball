"""读取 NVIDIA 显卡的温度、占用率与显存。纯标准库 + 系统 DLL，不联网。

**两条取数路径，优先用第一条：**

1. **直调 `nvml.dll`**（首选）。这个 DLL 随驱动装在 `System32\\` 下。
   初始化一次之后，每次读数就是**一次普通的函数调用** ——
   实测单次 **2.6 微秒**（含温度、占用率、显存、型号），初始化约 20–30 ms。

   代价是**内存**：它带进来两份映像共约 **22 MB**，而且加载后不会卸掉
   （见 `_NvmlDevice.close()` 里那段实测记录）。所以只显示小球时**完全不加载**
   （仍是约 37 MB），打开过详情面板之后进程常驻约 63 MB。

2. **退回 `nvidia-smi.exe`**（`System32\\` 下，也随驱动装）。
   只在 `nvml.dll` 不在预期位置、或初始化/首次读取失败时用。
   这条路径**每次都要起一个子进程**，实测 **CPU 141 ms / 墙上 189 ms**，
   而且**只查 1 个字段也是 141 ms** —— 成本全在 NVML 初始化与驱动握手上，
   跟查几个字段无关。所以走这条路时必须把间隔拉长（见 `INTERVAL_SMI`）。

第 1 条路径不是"优化"，是**实测逼出来的**：3 秒一次 × 141 ms 等于持续
4.7% 单核，而 GPU 温度是慢变量，这个开销纯属浪费。换成直接调用后，
面板打开时的 CPU 从 **7.77% 单核降到 0.47%（16 倍）**，
顺带也不再需要子进程、临时文件与句柄管理 —— 连"取数线程上 GC 掉
`subprocess` 对象撞上 Tk"那一整类隐患都一起消失（那段崩溃诊断仍保留在
`_run_capture` 的注释里，因为降级路径还在用它）。

**这个交换是 22 MB 内存换 16 倍 CPU**，写在明处，别当成白捡的。

**设计约束（别改坏）：**

* **只在需要时取。** 详情面板没开就完全不取 —— 实测只显示小球时
  `nvidia-smi` 的启动次数是 **0**，采样线程根本不存在。
* **后台取、主线程读。** 调用方永远不会阻塞在取数上，读到的永远是最近一次
  的结果。取数线程是 daemon，且只在面板打开期间存活。
* **失败就先闭嘴，冷却后可再探。** 两条路都拿不到（没 N 卡 / 驱动太老 /
  被安全软件拦），就把 `available` 置假并停手；但瞬态故障不该被固化，
  所以距上次失败满 `RETRY_AFTER_S`（300 秒）后 `start()` 会再给一次
  探测机会（见 `start()`）。

对外只有这几个东西：`available()` / `start()` / `stop()` / `reading()` / `name()`，
外加模块级 `last_error`（取数线程意外死亡时的异常摘要，供宿主记录/显示）。
"""

import ctypes
import ctypes.wintypes as wt
import os
import sys
import tempfile
import threading
import time

# 直调 nvml.dll 时的采样间隔。单次 0.33 µs，几乎免费，可以刷得勤一些 ——
# 打开面板看占用率时，2 秒一跳比 3 秒跟手。
INTERVAL_NVML = 2.0

# 退回 nvidia-smi 时的采样间隔。每次 141 ms CPU，必须拉长 ——
# 温度是慢变量，10 秒对"看一眼"完全够用，而 CPU 开销降到 1/3 以下。
INTERVAL_SMI = 10.0

# 单次 `nvidia-smi` 调用的超时。正常几百毫秒就返回；驱动出问题时它会挂住，
# 这里不让它拖住取数线程。
TIMEOUT = 3.0

# `nvidia-smi` 那条路径每次探测要 141 ms CPU，失败后允许再探的冷却期。
# 面板每次打开都调 start()，这个值决定"失败探测"最勤的重复频率。
RETRY_AFTER_S = 300.0

# 当前生效的采样间隔。`_worker` 选定路径后写进来，供调试/文档引用。
# 真正的权威是上面两个常量。
INTERVAL = INTERVAL_NVML

# 当前走的是哪条路："nvml" / "smi" / None（还没探测过）。只作诊断用。
backend = None

# `CREATE_NO_WINDOW`：不加这个，每次调用都会在屏幕上闪一个黑框。
# 对常驻小工具来说那是不可接受的观感问题。
_CREATE_NO_WINDOW = 0x08000000

_lock = threading.Lock()
_reading = None                 # (temperature_c, utilization_pct, used_mb, total_mb)
_name = ""                      # 显卡型号，例如 "NVIDIA Example GPU"
_available = None               # None = 还没探测过；False = 本机读不到，别再试
_thread = None
_stop = threading.Event()

# 上次探测**失败**（把 `_available` 置成 False）的时间戳（time.monotonic），
# 锁内读写。`start()` 靠它实现"失败后 300 秒允许再探一次"。
# 问题背景：原来是"失败就永久闭嘴" —— 但实测驱动升级、独显热插拔、
# 安全软件策略变更都会让"当时读不到"变成"现在能读"，永久闭嘴意味着
# 用户装完驱动后必须重启程序才能看到 GPU 行。方案：冷却 300 秒后再给
# 一次机会（面板每次打开都会调 start()，300 秒冷却不会被刷成敲打）。
_last_probe_failed_at = None

# 最近一次取数线程的**意外**异常摘要（"模块.异常: 消息"），供宿主读取后
# 显示或记日志。探测性失败（没 N 卡）不会写这里 —— 那是正常结果不是错误。
last_error = None


# ---------------------------------------------------------------------------
# 路径 1：直调 nvml.dll
# ---------------------------------------------------------------------------

_NVML_SUCCESS = 0
_NVML_TEMPERATURE_GPU = 0

# 温度合法区间。读出来离谱就当没读到 —— 驱动刚重启或设备正在重置时会有脏值。
_TEMP_MIN, _TEMP_MAX = 0, 150


class _NvmlUtilization(ctypes.Structure):
    """`nvmlUtilization_t`：两个 `unsigned int`。"""

    _fields_ = [("gpu", ctypes.c_uint), ("memory", ctypes.c_uint)]


class _NvmlMemory(ctypes.Structure):
    """`nvmlMemory_t`：三个 `unsigned long long`（total / free / used）。

    注意它**随 NVML 版本变过**：早期只有 total + free 两个字段。
    多读一个字段在这台机器的 NVML 13.616.92 上验证无问题；万一某个老版本
    因为尺寸不符而写坏内存也不会崩在这里 —— `nvmlDeviceGetMemoryInfo`
    会返回错误码，我们按"读不到显存"处理（显存不是这一行的主内容，
    温度才是）。
    """

    _fields_ = [("total", ctypes.c_ulonglong), ("free", ctypes.c_ulonglong),
                ("used", ctypes.c_ulonglong)]


def _system32_dir():
    """`System32` 的真实路径，处理 32 位解释器下的 WoW64 重定向。

    问题：32 位 Python 访问 `C:\\Windows\\System32` 会被文件系统重定向到
    `SysWOW64`，而 nvml.dll / nvidia-smi.exe 只装在**真** System32 下 ——
    32 位解释器（或 32 位冻结体）会把整个 GPU 取数判成"本机读不到"。

    实测/方案：WoW64 下 `C:\\Windows\\Sysnative` 这个别名**绕过**重定向、
    直达真 System32（只有 32 位进程能用它；64 位进程访问它会报路径不存在）。
    所以只在 `sys.maxsize <= 2**32` 时换名，64 位进程保持原路径不动。
    """
    root = os.environ.get("SystemRoot", r"C:\Windows")
    if sys.maxsize <= 2**32:
        return os.path.join(root, "Sysnative")
    return os.path.join(root, "System32")


def _nvml_paths():
    """`nvml.dll` 的两个标准位置，按可能性排序。

    只查这两个**固定**位置，不去 `PATH` 上遍历 —— 后者是安全软件的敏感行为，
    `nvidia-smi` 那条路径也是同样的纪律（见 `_query`）。

    1. `System32\\` —— 现代驱动的安装位置
    2. `Program Files\\NVIDIA Corporation\\NVSMI\\` —— 老驱动的安装位置
    """
    yield os.path.join(_system32_dir(), "nvml.dll")
    program_files = os.environ.get("ProgramFiles", r"C:\Program Files")
    yield os.path.join(program_files, "NVIDIA Corporation", "NVSMI", "nvml.dll")


class _NvmlDevice:
    """`nvml.dll` 的最小绑定：开一个设备、反复读它。

    所有调用都在**取数线程**上发生（`open()` / `read()` / `close()` 都由
    `_worker` 调用），句柄不跨线程，省掉一整类同步问题。
    """

    def __init__(self, dll):
        self.dll = dll
        self.device = ctypes.c_void_p()
        self.name = ""

    @classmethod
    def open(cls):
        """加载 + 初始化 + 取设备 + 读一次型号。任何一步失败都返回 None。"""
        for path in _nvml_paths():
            if not os.path.isfile(path):
                continue
            try:
                dll = ctypes.WinDLL(path)
            except OSError:
                continue
            if cls._init(dll) is None:
                # 问题：`WinDLL(path)` 已经给这个 DLL 加了一次引用计数，
                # 初始化失败就 continue 的话计数白白泄漏，随开关次数累积。
                # 方案：还回去再走下一条路径。失败也无所谓 —— 最坏就是
                # 引用计数少减一次，跟 `close()` 里的容错口径一致。
                try:
                    ctypes.WinDLL("kernel32").FreeLibrary(
                        ctypes.c_void_p(dll._handle))
                except (AttributeError, OSError):
                    pass
                continue
            handle = ctypes.c_void_p()
            try:
                rc = dll.nvmlDeviceGetHandleByIndex_v2(0, ctypes.byref(handle))
            except AttributeError:
                try:
                    rc = dll.nvmlDeviceGetHandleByIndex(0, ctypes.byref(handle))
                except AttributeError:
                    continue
            if rc != _NVML_SUCCESS or not handle.value:
                continue
            device = cls(dll)
            device.device = handle
            device.name = device._read_name()
            return device
        return None

    @staticmethod
    def _init(dll):
        """初始化 NVML；返回 0 表示成功。老驱动只有 `nvmlInit`。"""
        for symbol in ("nvmlInit_v2", "nvmlInit"):
            try:
                rc = getattr(dll, symbol)()
            except AttributeError:
                continue
            except OSError:
                return None
            if rc == _NVML_SUCCESS:
                return rc
        return None

    def _read_name(self):
        """型号字符串。读不到就返回空串（面板会退化成不显示型号）。"""
        buffer = ctypes.create_string_buffer(96)
        try:
            rc = self.dll.nvmlDeviceGetName(self.device, buffer, 96)
        except (AttributeError, OSError):
            return ""
        if rc != _NVML_SUCCESS:
            return ""
        return buffer.value.decode("utf-8", "replace").strip()

    def read(self):
        """读一次，返回五元组；读不到关键项返回 None。

        温度是这一行的主内容，读不到就整条作废；占用率和显存读不到按 0 处理，
        跟 `_parse` 对 `[N/A]` 的容错口径一致。
        """
        temperature = ctypes.c_uint()
        try:
            rc = self.dll.nvmlDeviceGetTemperature(
                self.device, _NVML_TEMPERATURE_GPU, ctypes.byref(temperature))
        except OSError:
            return None
        if rc != _NVML_SUCCESS:
            return None
        if not _TEMP_MIN <= temperature.value <= _TEMP_MAX:
            return None

        utilization = 0
        rates = _NvmlUtilization()
        try:
            if self.dll.nvmlDeviceGetUtilizationRates(
                    self.device, ctypes.byref(rates)) == _NVML_SUCCESS:
                utilization = max(0, min(100, int(rates.gpu)))
        except (AttributeError, OSError):
            pass

        used_mb = total_mb = 0
        memory = _NvmlMemory()
        try:
            if self.dll.nvmlDeviceGetMemoryInfo(
                    self.device, ctypes.byref(memory)) == _NVML_SUCCESS:
                used_mb = int(memory.used // 1048576)
                total_mb = int(memory.total // 1048576)
        except (AttributeError, OSError):
            pass

        return self.name, temperature.value, utilization, used_mb, total_mb

    def close(self):
        """关掉 NVML 会话，并把这一侧的引用计数还回去。已经 join 过取数线程才调。

        ★ **`nvmlShutdown()` 不会卸载 DLL，`FreeLibrary` 也卸不掉它 —— 这是实测结论。**

        `nvml.dll` 进来是**两份**映像（实测路径）：

            C:\\WINDOWS\\System32\\nvml.dll                                    1.4 MB
            C:\\WINDOWS\\system32\\DriverStore\\FileRepository\\nvti.inf_...\\nvml.dll   22.3 MB

        System32 那个只是个壳，真正的实现在驱动目录里，由它带进来。合计约
        **22 MB**，而且**加载之后就一直留着**：

        * `nvmlShutdown()` 只关会话，不动映像；
        * 对着自己 `LoadLibrary` 拿到的句柄调 `FreeLibrary`，系统**返回成功**
          （实测 `rc=1, GetLastError=0`），但模块列表里两份映像都还在。

        所以这里调 `FreeLibrary` 的目的**不是**把 22 MB 要回来（要不回来），
        而是**给每次 `LoadLibrary` 配平引用计数** —— `open()` 每次面板打开都会
        `WinDLL(path)` 一次，不配平的话引用计数会随开关次数一路涨上去。
        （内存实测不涨：连开连关 3 轮工作集稳定在 44 MB，所以这只是洁癖，
        不是缺陷。）

        **别为了省这 22 MB 去折腾**：实测把它塞进"可回收的映像页"这个说法也
        不成立 —— 加载后**已提交内存同样 +19.5 MB**（工作集 +21.9 MB），
        是真的占住了。想避免它就只能不用 NVML，那就得回到每次 141 ms 的
        `nvidia-smi` 子进程。**这个交换是划算的**：CPU 从 7.77% 单核降到
        0.47%（16 倍），代价是打开过详情面板之后进程常驻内存从约 37 MB
        变成约 63 MB。只显示小球时**完全不加载 NVML**，仍是 37 MB。
        """
        try:
            self.dll.nvmlShutdown()
        except (AttributeError, OSError):
            pass
        handle = getattr(self.dll, "_handle", None)
        if not handle:
            return
        try:
            ctypes.WinDLL("kernel32").FreeLibrary(ctypes.c_void_p(handle))
        except (AttributeError, OSError):
            # 配平失败也不是错误：最坏情况就是引用计数少减一次。
            pass


# ---------------------------------------------------------------------------
# 路径 2：调 nvidia-smi.exe（降级）
# ---------------------------------------------------------------------------


def _parse(text):
    """把 `nvidia-smi` 的一行 CSV 转成五元组，读不出来就返回 None。

    期望格式（`nounits` 去了单位）：型号在最前，之后是四个数字
        NVIDIA Example GPU, 50, 10, 1024, 8192 (synthetic example)
    有的驱动版本对不支持的字段会回 `[N/A]`，所以逐项容错 —— 温度读不到就
    整条作废（温度是这个功能的主内容），占用率读不到按 0 算。
    """
    fields = [part.strip() for part in text.strip().split(",")]
    if len(fields) < 5:
        return None
    name = fields[0]
    try:
        temperature = int(fields[1])
    except ValueError:
        return None
    try:
        utilization = int(fields[2])
    except ValueError:
        utilization = 0
    try:
        used_mb = int(fields[3])
        total_mb = int(fields[4])
    except ValueError:
        used_mb = total_mb = 0
    if not _TEMP_MIN <= temperature <= _TEMP_MAX:
        return None
    return name, temperature, max(0, min(100, utilization)), used_mb, total_mb


def _run_capture(exe, args, out_path, timeout):
    """跑一个控制台程序，把 stdout 写进 `out_path`；成功返回 True。

    ★ **这里刻意不用 `subprocess`，而是直接调 `CreateProcessW`。**

    这是被实测逼出来的，不是洁癖。`subprocess` 在 `_execute_child` 里**一定要**
    走 `_close_pipe_fds`，而那条路径会构造一个 `contextlib.ExitStack` 并注册
    close 回调对象。这些回调对象一旦被 GC 回收，析构就发生在**调用它的那个
    线程**上；只要那个线程不是主线程、而主线程此刻正在调 Tk，Windows 上就会
    打出

        Tcl_AsyncDelete: async handler deleted by the wrong thread

    并立刻中止进程（退出码 0x80000003）。实测崩溃栈固定是：

        Garbage-collecting
          contextlib._create_cb_wrapper / callback
          subprocess._close_pipe_fds
          subprocess._execute_child
          gpu_info._query   <- 取数线程上
        同时主线程在 tkinter.create_text / _configure

    把 `capture_output` 换成裸整数句柄**并不能绕开**（`_close_pipe_fds` 照样跑），
    所以这里彻底不碰 `subprocess`：进程、句柄全部自己拿 Win32 管理，
    没有任何 Python 侧的清理回调，也就没有任何东西能在取数线程上被 GC。

    句柄全部继承给子进程，父进程侧用完立刻关掉，不留任何悬空引用。

    注：这条路径现在是**降级**用的（首选 `nvml.dll`，见 `_NvmlDevice`）。
    留着它是为了兜住"`nvml.dll` 不在预期位置"的机器，所以上面这段纪律仍然有效。
    """
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

    # 问题：ctypes 默认把函数返回值当 c_int（32 位）处理。HANDLE 在 64 位
    # 进程里是 64 位指针，返回值会被**截断成 32 位** —— 现在能用只是因为
    # 本机句柄值恰好都落在低 32 位（实测 CreateFileW 返回的句柄 < 2^24）。
    # 一旦某个句柄带高位，校验 `h == INVALID` 和 CloseHandle 就会拿截断值
    # 操作一个不存在的句柄。方案：给每个 Win32 调用显式声明 restype/argtypes。
    kernel32.CreateFileW.restype = wt.HANDLE
    kernel32.CreateFileW.argtypes = [
        wt.LPCWSTR, wt.DWORD, wt.DWORD, ctypes.c_void_p, wt.DWORD,
        wt.DWORD, wt.HANDLE]
    kernel32.SetHandleInformation.restype = wt.BOOL
    kernel32.SetHandleInformation.argtypes = [
        wt.HANDLE, wt.DWORD, wt.DWORD]
    kernel32.CreateProcessW.restype = wt.BOOL
    kernel32.CreateProcessW.argtypes = [
        wt.LPCWSTR, wt.LPWSTR, ctypes.c_void_p, ctypes.c_void_p,
        wt.BOOL, wt.DWORD, ctypes.c_void_p, wt.LPCWSTR,
        ctypes.c_void_p, ctypes.c_void_p]
    kernel32.WaitForSingleObject.restype = wt.DWORD
    kernel32.WaitForSingleObject.argtypes = [wt.HANDLE, wt.DWORD]
    kernel32.TerminateProcess.restype = wt.BOOL
    kernel32.TerminateProcess.argtypes = [wt.HANDLE, wt.UINT]
    kernel32.GetExitCodeProcess.restype = wt.BOOL
    kernel32.GetExitCodeProcess.argtypes = [wt.HANDLE, ctypes.c_void_p]
    kernel32.CloseHandle.restype = wt.BOOL
    kernel32.CloseHandle.argtypes = [wt.HANDLE]

    CREATE_NO_WINDOW = _CREATE_NO_WINDOW
    STARTF_USESTDHANDLES = 0x00000100
    STARTF_USESHOWWINDOW = 0x00000001
    SW_HIDE = 0
    WAIT_TIMEOUT = 0x00000102
    HANDLE_FLAG_INHERIT = 0x00000001
    GENERIC_WRITE = 0x40000000

    class STARTUPINFO(ctypes.Structure):
        _fields_ = [
            ("cb", wt.DWORD), ("lpReserved", wt.LPWSTR),
            ("lpDesktop", wt.LPWSTR), ("lpTitle", wt.LPWSTR),
            ("dwX", wt.DWORD), ("dwY", wt.DWORD),
            ("dwXSize", wt.DWORD), ("dwYSize", wt.DWORD),
            ("dwXCountChars", wt.DWORD), ("dwYCountChars", wt.DWORD),
            ("dwFillAttribute", wt.DWORD), ("dwFlags", wt.DWORD),
            ("wShowWindow", wt.WORD), ("cbReserved2", wt.WORD),
            ("lpReserved2", ctypes.POINTER(ctypes.c_byte)),
            ("hStdInput", wt.HANDLE), ("hStdOutput", wt.HANDLE),
            ("hStdError", wt.HANDLE),
        ]

    class PROCESS_INFORMATION(ctypes.Structure):
        _fields_ = [("hProcess", wt.HANDLE), ("hThread", wt.HANDLE),
                    ("dwProcessId", wt.DWORD), ("dwThreadId", wt.DWORD)]

    INVALID = ctypes.c_void_p(-1).value

    def _mkfile(path, access):
        sa = ctypes.c_void_p(0)
        return kernel32.CreateFileW(path, access, 0x00000001, sa,
                                    2, 0x00000080, None)

    infile = outfile = errfile = None
    hproc = hthread = None
    try:
        devnull = os.path.join(_system32_dir(), "NUL")
        infile = _mkfile(devnull, 0x80000000)             # GENERIC_READ
        outfile = _mkfile(out_path, GENERIC_WRITE)
        errfile = _mkfile(devnull, GENERIC_WRITE)
        for h in (infile, outfile, errfile):
            if not h or h == INVALID:
                return None
            kernel32.SetHandleInformation(h, HANDLE_FLAG_INHERIT,
                                          HANDLE_FLAG_INHERIT)

        si = STARTUPINFO()
        si.cb = ctypes.sizeof(STARTUPINFO)
        si.dwFlags = STARTF_USESTDHANDLES | STARTF_USESHOWWINDOW
        si.wShowWindow = SW_HIDE
        si.hStdInput = infile
        si.hStdOutput = outfile
        si.hStdError = errfile
        pi = PROCESS_INFORMATION()

        cmdline = ctypes.create_unicode_buffer(
            '"%s" %s' % (exe, " ".join(args)))
        ok = kernel32.CreateProcessW(
            None, cmdline, None, None, True, CREATE_NO_WINDOW,
            None, None, ctypes.byref(si), ctypes.byref(pi))
        if not ok:
            return None
        hproc, hthread = pi.hProcess, pi.hThread
        wait = kernel32.WaitForSingleObject(hproc, int(timeout * 1000))
        if wait == WAIT_TIMEOUT:
            kernel32.TerminateProcess(hproc, 1)
            # 有界等待：TerminateProcess 是异步的，进程退出还要时间。
            # 问题：这里原来等 INFINITE —— 驱动僵死时进程可能永远不退出，
            # 取数线程就被永久钉死，面板关掉也收不掉。实测正常退出是
            # 毫秒级，给 2 秒足够；超时也不再等，直接走清理（句柄关闭后
            # 孤儿进程由系统回收，反正它已经不被允许继续写临时文件了）。
            kernel32.WaitForSingleObject(hproc, 2000)
            return None
        code = wt.DWORD()
        kernel32.GetExitCodeProcess(hproc, ctypes.byref(code))
        if code.value != 0:
            return None
        return code.value == 0
    except (OSError, ValueError):
        return None
    finally:
        for h in (infile, outfile, errfile):
            if h and h != INVALID:
                kernel32.CloseHandle(h)
        for h in (hproc, hthread):
            if h and h != INVALID:
                kernel32.CloseHandle(h)


def _query():
    """真正调一次 `nvidia-smi`，返回五元组或 None（降级路径）。

    走 `_run_capture`（自己拿 Win32 管进程，不用 `subprocess`），
    输出落到一个临时文件。原因见 `_run_capture` 的注释 —— 一句话：
    `subprocess` 的清理回调会在取数线程上被 GC，撞上主线程的 Tk 调用
    就会以 0x80000003 中止整个进程。
    """
    exe = os.path.join(_system32_dir(), "nvidia-smi.exe")
    if not os.path.isfile(exe):
        # 不在默认位置就直接放弃 —— 不去 PATH 上乱找，那是安全软件敏感行为。
        return None
    handle = None
    path = None
    try:
        handle, path = tempfile.mkstemp(prefix="memwidget-gpu-", suffix=".csv")
        os.close(handle)
        handle = None
        if not _run_capture(exe, [
                "--query-gpu=name,temperature.gpu,utilization.gpu,"
                "memory.used,memory.total",
                "--format=csv,noheader,nounits"], path, TIMEOUT):
            return None
        with open(path, "rb") as fh:
            raw = fh.read()
    except OSError:
        return None
    finally:
        if handle is not None:
            try:
                os.close(handle)
            except OSError:
                pass
        if path:
            try:
                os.unlink(path)
            except OSError:
                pass
    lines = raw.decode("utf-8", "replace").splitlines()
    return _parse(lines[0]) if lines else None


# ---------------------------------------------------------------------------
# 取数线程
# ---------------------------------------------------------------------------


def _loop(read, interval):
    """按 `interval` 反复调 `read`，把结果写进模块变量。`read` 返回五元组或 None。

    第一轮拿不到就直接收工并把 `available` 置假 —— 这台机器读不到，
    没必要继续起进程/调函数。中途读不到（驱动忙、临时错误）**保留上一次的值**，
    不清空，免得数字在面板上闪烁。
    """
    global _reading, _available, _name, _last_probe_failed_at
    first = True
    while not _stop.is_set():
        value = read()
        with _lock:
            if value is None:
                if first:
                    _available = False
                    # 记下冷却起点：`start()` 据此在 300 秒后允许再探一次。
                    _last_probe_failed_at = time.monotonic()
                    return
            else:
                name, temperature, utilization, used_mb, total_mb = value
                _available = True
                _name = name
                _reading = (temperature, utilization, used_mb, total_mb)
        first = False
        _stop.wait(interval)


def _worker():
    """取数线程：先试 nvml.dll，不行再退回 nvidia-smi。

    两条路的**探测成本差三个数量级**（0.33 µs vs 141 ms），所以不共用间隔：
    选定路径后把 `INTERVAL` 一起定下来，`_loop` 用它。
    """
    global backend, INTERVAL, last_error
    global _thread, _available, _last_probe_failed_at

    try:
        device = _NvmlDevice.open()
        if device is not None:
            # 初始化成功不等于这台机器真能读出数（可能是被虚拟化的、
            # 或设备正在重置），所以再真读一次才算数。
            if device.read() is None:
                device.close()
                device = None

        if device is not None:
            backend = "nvml"
            INTERVAL = INTERVAL_NVML
            try:
                _loop(device.read, INTERVAL_NVML)
            finally:
                # 线程退出时才关。`stop()` 已经 join 过，不会有人在用了。
                device.close()
            return

        backend = "smi"
        INTERVAL = INTERVAL_SMI
        _loop(_query, INTERVAL_SMI)
    except Exception as exc:
        # 问题：线程函数没有任何兜底时，一次意外异常（DLL 内部访问违例、
        # OSError 变种……）只会让线程**静默死亡** —— `_thread` 停在原地
        # 非 None，`start()` 的幂等检查从此永远返回，模块变成"看着在跑、
        # 实际死透"，宿主连为什么都不知道。实测这类故障只出现在驱动
        # 出问题的机器上，但一旦出现就是无声的全灭。
        # 方案：把状态归位成"可以重来" —— 记录异常摘要到 `last_error`
        # 供宿主显示/记日志，置不可用；线程槽位只在 finally
        # 收尾时清空，避免新取数线程在旧线程退出前启动。
        last_error = "%s.%s: %s" % (
            type(exc).__module__, type(exc).__name__, exc)
        with _lock:
            _available = False
            _last_probe_failed_at = time.monotonic()
    finally:
        # 正常退出（探测失败收工 / stop() 叫停）也要把 `_thread` 归位，
        # 否则死线程对象会一直占着这个位置，`start()` 的"非 None 就不起"
        # 检查会让重试探测永远发不出去。只在还指向自己时清；
        # stop() 等待超时也不能提前释放这个槽位。
        with _lock:
            if _thread is threading.current_thread():
                _thread = None


def start():
    """开始取数（幂等）。详情面板打开时调用。

    ★ **幂等靠的是锁，不是"线程还活着"这个判断。**

    以前的写法是 `if _thread is not None and _thread.is_alive(): return`，
    但 `_thread` 只在 `stop()` 里被清空。两个 `MemoryWidget` 实例在同一进程里
    先后建起来（UI 回归检查、抓屏工具都会这样）时：第一个实例的
    `stop()` 可能因为面板已被直接 `destroy()` 而没走到，`_thread` 就留在原地；
    第二个实例再 `start()`，若那一刻旧线程恰好正在收尾（`is_alive()` 返回
    False），就会**另外再起一个** —— 于是进程里同时跑着两个取数线程。

    在 `nvidia-smi` 那条路径上这很致命：两个线程会各自在自己的线程上 GC 掉
    `subprocess` 对象，而 `subprocess` 的析构路径会碰 Tcl 的 async handler。
    撞在一起就打出

        Tcl_AsyncDelete: async handler deleted by the wrong thread

    并把进程**在打印结论之前**中止（退出码 0x80000003）。现象是
    "50 项组合全过、没有一条 FAIL、结论行整行不见"，完全随机。
    实测崩溃线程的栈就是 `gpu_info._worker -> subprocess.run -> _close_pipe_fds
    -> callback -> Garbage-collecting`。
    （现在首选路径已经不碰 `subprocess` 了，但降级路径还在，而且多一个
    采样线程本身就是浪费 —— 所以这条仍然必须守住。）

    用锁把"检查 + 起线程"做成原子的，`_thread` 就是唯一权威：只要它不是
    None，就说明已经有采样线程在跑，直接返回。

    ★ **`_available is False` 不是终态，是冷却态。**

    以前 `_available` 一旦置 False 就永久拒绝再试（"失败就永久闭嘴"）。
    问题：这会把**瞬态**故障（驱动正在重启、安全软件刚拦截又放行、
    独显切换中）固化成永久缺失 —— 用户装好驱动后得重启程序才能看到
    GPU 行。方案：距上次失败探测满 `RETRY_AFTER_S`（300 秒）后允许
    `start()` 重试一次探测。面板每次打开都调 `start()`，但 300 秒冷却
    意味着最勤也是 5 分钟一探，不会形成敲打；探测本身要么 0.33 µs
    （nvml 路径）要么 141 ms（smi 路径，且只在 nvml 缺失时），成本可控。
    """
    global _thread, _available
    with _lock:
        if _available is False:
            # 已确认读不到。冷却未满就维持原判；满了就清掉状态再来一次。
            if (_last_probe_failed_at is not None
                    and time.monotonic() - _last_probe_failed_at < RETRY_AFTER_S):
                return
            _available = None                 # 重新进入"探测中"
        if _thread is not None:
            return                          # 已经有一个采样线程了，绝不重复起
        _stop.clear()
        thread = threading.Thread(target=_worker, name="gpu-temp", daemon=True)
        _thread = thread
        # 发布槽位和启动必须在同一次锁内完成，否则 stop()
        # 会看到一个尚未启动的线程并误以为它已经退出。
        # Thread.start() 只等 bootstrap 设置启动标志，不等 _worker
        # 获取本锁，因此不会和 _worker 的收尾互锁。
        try:
            thread.start()
        except Exception:
            _thread = None
            _stop.set()
            raise


def stop(wait=True):
    """停止取数。面板关掉后不该继续取。

    `wait=True` 会有界地等线程真的退出来（最多一个 `TIMEOUT` 加一点余量）。

    为什么要等：如果调用方紧接着就把它读过的 Tk 对象丢掉（`PhotoImage`
    之类），而取数线程还在跑，那个对象的 `__del__` 就可能落在**这个线程**
    上执行，去调 Tk 就会抛 `RuntimeError: main thread is not in main loop`。
    等它退干净，析构就只会在主线程发生。有界是因为这是个 daemon 线程：
    万一驱动卡住，宁可留下一个不碰 Tk 的空转线程，也不能把界面冻住。
    """
    with _lock:
        _stop.set()
        # 超时时旧线程仍可能卡在驱动调用内，必须留住槽位。
        # 否则 start() 会清除共用停止信号，同时复活旧线程并起新线程。
        thread = _thread
    if wait and thread is not None and thread.is_alive():
        thread.join(timeout=TIMEOUT + 0.5)


def reading():
    """返回最近一次读数 `(温度, 占用率, 已用 MB, 总 MB)`；没有就返回 None。

    由 `_worker` 在后台刷新，这里只读一个变量、绝不阻塞。
    """
    with _lock:
        return _reading


def name():
    """显卡型号字符串；还没读到就返回空串。"""
    with _lock:
        return _name


def available():
    """本机能不能读到 GPU 数据。

    `None` 表示还在探测，调用方按"先不显示"处理即可 —— 探测完成后
    详情面板下一次重绘就会带上那一行。

    持锁读：`_available` / `_reading` 都由取数线程在锁内写，这里不持锁
    的话理论上能读到写了一半的状态（GIL 只保证引用原子，不保证这组
    判断的前后一致）。
    """
    with _lock:
        if _available is not None:
            return bool(_available)
        # 还没探测出结果时，先看线程有没有已经跑出个说法：
        # 探到值就等于可用，探不到会在 `_worker` 里被置 False。
        return _reading is not None
