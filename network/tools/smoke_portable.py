"""Test only our new EXE process tree and verify repository original files are intact."""
from pathlib import Path
import ctypes
import ctypes.wintypes as wt
import hashlib
import json
import os
import subprocess
import sys
import time

from PIL import Image

base = Path(__file__).resolve().parent.parent
workspace = base.parent
sys.path.insert(0, str(base/'tools'))
import screen_capture as sc

k32 = ctypes.WinDLL('kernel32', use_last_error=True)
u32 = ctypes.WinDLL('user32', use_last_error=True)
class PROCESSENTRY32(ctypes.Structure):
    _fields_ = [('dwSize',wt.DWORD),('cntUsage',wt.DWORD),('th32ProcessID',wt.DWORD),
        ('th32DefaultHeapID',ctypes.c_size_t),('th32ModuleID',wt.DWORD),
        ('cntThreads',wt.DWORD),('th32ParentProcessID',wt.DWORD),
        ('pcPriClassBase',wt.LONG),('dwFlags',wt.DWORD),('szExeFile',wt.WCHAR*260)]
k32.CreateToolhelp32Snapshot.argtypes = [wt.DWORD,wt.DWORD]
k32.CreateToolhelp32Snapshot.restype = wt.HANDLE
k32.Process32FirstW.argtypes = [wt.HANDLE,ctypes.POINTER(PROCESSENTRY32)]
k32.Process32NextW.argtypes = [wt.HANDLE,ctypes.POINTER(PROCESSENTRY32)]
k32.CloseHandle.argtypes = [wt.HANDLE]
u32.GetWindowThreadProcessId.argtypes = [wt.HWND,ctypes.POINTER(wt.DWORD)]
u32.GetWindowRect.argtypes = [wt.HWND,ctypes.POINTER(wt.RECT)]
u32.GetClassNameW.argtypes = [wt.HWND,wt.LPWSTR,ctypes.c_int]
u32.IsWindowVisible.argtypes = [wt.HWND]
u32.PostMessageW.argtypes = [wt.HWND,wt.UINT,wt.WPARAM,wt.LPARAM]

def processes():
    handle = k32.CreateToolhelp32Snapshot(2,0)
    if handle == ctypes.c_void_p(-1).value:
        raise ctypes.WinError(ctypes.get_last_error())
    rows = {}
    try:
        row = PROCESSENTRY32()
        row.dwSize = ctypes.sizeof(row)
        okay = k32.Process32FirstW(handle,ctypes.byref(row))
        while okay:
            rows[int(row.th32ProcessID)] = (int(row.th32ParentProcessID),row.szExeFile)
            okay = k32.Process32NextW(handle,ctypes.byref(row))
    finally:
        k32.CloseHandle(handle)
    return rows

def windows(pids):
    found = []
    @ctypes.WINFUNCTYPE(wt.BOOL,wt.HWND,wt.LPARAM)
    def collect(hwnd, _):
        pid = wt.DWORD()
        u32.GetWindowThreadProcessId(hwnd,ctypes.byref(pid))
        if pid.value in pids and u32.IsWindowVisible(hwnd):
            cls = ctypes.create_unicode_buffer(80)
            u32.GetClassNameW(hwnd,cls,80)
            rect = wt.RECT()
            u32.GetWindowRect(hwnd,ctypes.byref(rect))
            if cls.value == 'TkTopLevel':
                found.append((hwnd,pid.value,rect.right-rect.left,rect.bottom-rect.top,
                              rect.left,rect.top))
        return True
    u32.EnumWindows(collect,0)
    return found

protected_paths = [base.parent / 'original/source/memory_widget.py',
                   base.parent / 'original/build/MemoryWidget.exe']

def protected_hashes():
    return {str(p):hashlib.sha256(p.read_bytes()).hexdigest() for p in protected_paths if p.is_file()}

def main():
    exe = Path(sys.argv[1]) if len(sys.argv)>1 else base/'build/内存球网络版.exe'
    assert exe.is_file()
    assert exe.name != 'MemoryWidget.exe'
    report_dir = base/'build'/('smoke-'+time.strftime('%Y%m%d-%H%M%S'))
    report_dir.mkdir(parents=True)
    before = protected_hashes()
    initial = processes()
    original_pids = {pid for pid,(_,name) in initial.items() if name.casefold()=='memorywidget.exe'}
    report = {'exe':str(exe),'protected_before':before,'original_pids':sorted(original_pids)}
    env = {**os.environ,'MEMORYNET_DATA_DIR':str(report_dir/'data'),
           'MEMORY_WIDGET_DEBUG':str(report_dir/'debug.txt')}
    start = time.monotonic()
    proc = subprocess.Popen([str(exe),'--detail'],env=env,cwd=exe.parent)
    owned = {proc.pid}
    try:
        deadline = start+25
        rendered = []
        while time.monotonic()<deadline:
            rows=processes()
            previous=0
            while previous!=len(owned):
                previous=len(owned)
                owned.update(pid for pid,(parent,_) in rows.items() if parent in owned)
            rendered=windows(owned)
            if len(rendered)>=2 and any(w[3]>400 for w in rendered):
                break
            assert proc.poll() is None, 'New EXE exited before rendering'
            time.sleep(.25)
        assert len(rendered)>=2, 'Expected orb and detail panel'
        assert original_pids.issubset(processes()), 'Existing MemoryWidget process disappeared'
        report['startup_seconds']=round(time.monotonic()-start,3)
        time.sleep(7)  # actual network sampling, GPU detection and dynamic reflow
        rendered=windows(owned)
        card=max(rendered,key=lambda w:w[2]*w[3])
        hwnd,pid,width,height,x,y=card
        # The detail panel intentionally supports a compact 0.45 scale now;
        # keep the smoke check focused on finding the card rather than rejecting
        # a user-selected narrow layout.
        assert height>width*1.8 and width>140
        sc.enable_dpi_awareness()
        left,top,right,bottom = -10000,-10000,10000,10000
        # Capture the application's own window, including its antialiased curves.
        image_dir=report_dir/'captures'
        image_dir.mkdir(exist_ok=True)
        bmp=report_dir/'portable-card.bmp'
        sc.capture_window(hwnd,str(bmp),padding=0)
        image=Image.open(bmp).convert('RGB')
        pixels=list(image.getdata())
        orange=sum(1 for r,g,b in pixels if r>170 and 55<g<210 and b<90 and r>g+25)
        assert orange>100, 'Accent/chart/meter pixels absent'
        screenshot=image_dir/'内存球网络版-实机.png'
        image.save(screenshot)
        report.update(card={'width':width,'height':height,'pid':pid},
                      orange_pixels=orange,screenshot=str(screenshot))
        # Exercise the normal native close path; no global process termination.
        assert u32.PostMessageW(hwnd,0x10,0,0)
        assert proc.wait(timeout=12)==0, 'EXE did not exit cleanly'
        time.sleep(.25)
        assert not (owned & set(processes())), 'Owned process tree remains'
        data=report_dir/'data'
        for path in (data/'memorynet-error.log',data/'memorynet-native-error.log'):
            assert not path.exists() or path.stat().st_size==0, path.read_text(encoding='utf-8')
        assert (data/'settings.json').is_file(), 'Independent settings missing'
        after=protected_hashes()
        assert before==after, 'Original files/settings/shortcut changed'
        assert original_pids.issubset(processes()), 'Original app no longer running'
        report.update(protected_after=after,status='PORTABLE_SMOKE_OK',owned_pids=sorted(owned),
                      elapsed_seconds=round(time.monotonic()-start,2))
        print(json.dumps(report,ensure_ascii=False,indent=2),flush=True)
    finally:
        # Exceptional cleanup still targets only this test's launcher/children.
        if proc.poll() is None:
            for window in windows(owned):
                u32.PostMessageW(window[0],0x10,0,0)
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.terminate()
                proc.wait(timeout=5)
        (report_dir/'report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')

if __name__=='__main__':
    main()
