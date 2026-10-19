"""EXE fault scenarios, duplicate launch and steady-state resource monitoring.

Every process/window touched belongs to this script's launches. Configurations
are in the workspace; the installed original is checked read-only.
"""
from pathlib import Path
import ctypes
import ctypes.wintypes as wt
import json
import os
import subprocess
import sys
import time

import smoke_portable as smoke

k32, u32 = smoke.k32, smoke.u32
class PMC(ctypes.Structure):
    _fields_=[('cb',wt.DWORD),('PageFaultCount',wt.DWORD),
        ('PeakWorkingSetSize',ctypes.c_size_t),('WorkingSetSize',ctypes.c_size_t),
        ('QuotaPeakPagedPoolUsage',ctypes.c_size_t),('QuotaPagedPoolUsage',ctypes.c_size_t),
        ('QuotaPeakNonPagedPoolUsage',ctypes.c_size_t),('QuotaNonPagedPoolUsage',ctypes.c_size_t),
        ('PagefileUsage',ctypes.c_size_t),('PeakPagefileUsage',ctypes.c_size_t),
        ('PrivateUsage',ctypes.c_size_t)]
k32.OpenProcess.argtypes=[wt.DWORD,wt.BOOL,wt.DWORD]
k32.OpenProcess.restype=wt.HANDLE
k32.GetProcessHandleCount.argtypes=[wt.HANDLE,ctypes.POINTER(wt.DWORD)]
u32.GetGuiResources.argtypes=[wt.HANDLE,wt.DWORD]
u32.GetGuiResources.restype=wt.DWORD
psapi=ctypes.WinDLL('psapi')
psapi.GetProcessMemoryInfo.argtypes=[wt.HANDLE,ctypes.POINTER(PMC),wt.DWORD]
psapi.GetProcessMemoryInfo.restype=wt.BOOL

def resource(pid):
    handle=k32.OpenProcess(0x410,False,pid)
    assert handle
    try:
        value=PMC()
        value.cb=ctypes.sizeof(value)
        assert psapi.GetProcessMemoryInfo(handle,ctypes.byref(value),value.cb)
        handles=wt.DWORD()
        assert k32.GetProcessHandleCount(handle,ctypes.byref(handles))
        return dict(private_bytes=value.PrivateUsage,working_bytes=value.WorkingSetSize,
                    handles=handles.value,gdi=u32.GetGuiResources(handle,0),
                    user=u32.GetGuiResources(handle,1))
    finally:
        k32.CloseHandle(handle)

def children(proc, owned):
    rows=smoke.processes()
    previous=-1
    while previous!=len(owned):
        previous=len(owned)
        owned.update(pid for pid,(parent,_) in rows.items() if parent in owned)
    return owned

def all_visible(pids):
    found=[]
    @ctypes.WINFUNCTYPE(wt.BOOL,wt.HWND,wt.LPARAM)
    def collect(hwnd,_):
        pid=wt.DWORD()
        u32.GetWindowThreadProcessId(hwnd,ctypes.byref(pid))
        if pid.value in pids and u32.IsWindowVisible(hwnd):
            cls=ctypes.create_unicode_buffer(80)
            u32.GetClassNameW(hwnd,cls,80)
            found.append((hwnd,pid.value,cls.value))
        return True
    u32.EnumWindows(collect,0)
    return found

def launch(exe, data, label, reports):
    environment={**os.environ,'MEMORYNET_DATA_DIR':str(data),
        'MEMORY_WIDGET_DEBUG':str(reports/(label+'-debug.txt'))}
    return subprocess.Popen([str(exe),'--detail'],cwd=exe.parent,env=environment)

def wait_card(proc,owned):
    for _ in range(80):
        children(proc,owned)
        cards=[w for w in smoke.windows(owned) if w[3]>w[2]*1.8 and w[2]>150]
        if cards:
            return max(cards,key=lambda x:x[2]*x[3])
        assert proc.poll() is None, 'Early exit'
        time.sleep(.25)
    raise AssertionError('No visible detail card')

def close(proc,owned):
    children(proc,owned)
    for window in all_visible(owned):
        u32.PostMessageW(window[0],0x10,0,0)
    assert proc.wait(timeout=15)==0, 'Unexpected exit status'
    time.sleep(.2)
    assert not owned.intersection(smoke.processes()),'Owned processes leaked'

def no_errors(data):
    for name in ('memorynet-error.log','memorynet-native-error.log'):
        p=data/name
        assert not p.exists() or p.stat().st_size==0, p.read_text(encoding='utf-8')

def main():
    exe=Path(sys.argv[1]) if len(sys.argv)>1 else smoke.base/'build/内存球网络版.exe'
    out=smoke.base/'build'/('fault-exe-'+time.strftime('%Y%m%d-%H%M%S'))
    out.mkdir(parents=True)
    protected=smoke.protected_hashes()
    rows=smoke.processes()
    existing={pid for pid,(_,name) in rows.items() if name.casefold()=='memorywidget.exe'}
    assert not any(name==exe.name for _,name in rows.values()), 'A user-owned network EXE is already running'
    report={'exe':str(exe),'protected_before':protected,'original_pids':sorted(existing),'cases':[]}
    current=[]
    try:
        for label,content in (
            ('broken-json','{"ring_size":'),
            ('invalid-values',json.dumps(dict(warn_yellow=float('inf'),warn_red=float('-inf'),
                detail_scale=float('nan'),autostart='false',pin_top='false',
                pos=[10**40,-10**40],panel_offset=[0,10**40],ring_size={},poll_interval_ms=True))),
            ('recursive-json','['*1500+']'*1500)):
            data=out/label
            data.mkdir()
            (data/'settings.json').write_text(content,encoding='utf-8')
            proc=launch(exe,data,label,out)
            owned={proc.pid}
            current.append((proc,owned))
            card=wait_card(proc,owned)
            time.sleep(1.1)
            close(proc,owned)
            no_errors(data)
            settings=json.loads((data/'settings.json').read_text(encoding='utf-8'))
            assert settings['autostart'] is False and settings['pin_top'] is False
            assert settings['warn_yellow']==70 and settings['warn_red']==85
            report['cases'].append({'case':label,'status':'PASS','card_size':card[2:4]})
            print(label+' PASS',flush=True)
        # A profile path blocked by a file: run without writable settings.
        data=out/'readonly-profile'
        data.write_text('intentional directory collision',encoding='utf-8')
        proc=launch(exe,data,'readonly-profile',out)
        owned={proc.pid}
        current.append((proc,owned))
        wait_card(proc,owned)
        close(proc,owned)
        assert data.read_text(encoding='utf-8')=='intentional directory collision'
        report['cases'].append({'case':'readonly-profile','status':'PASS'})
        print('readonly-profile PASS',flush=True)

        data=out/'soak-data'
        proc=launch(exe,data,'soak',out)
        owned={proc.pid}
        current.append((proc,owned))
        card=wait_card(proc,owned)
        time.sleep(7)
        # The second launch gets a notice, while the first remains healthy.
        second=launch(exe,data,'duplicate',out)
        second_owned={second.pid}
        current.append((second,second_owned))
        dialogs=[]
        for _ in range(80):
            children(second,second_owned)
            dialogs=[w for w in all_visible(second_owned) if w[2]=='#32770']
            if dialogs:
                break
            assert second.poll() is None
            time.sleep(.25)
        assert dialogs,'Expected duplicate-launch notice'
        assert not smoke.windows(second_owned),'Duplicate launch produced second Tk UI'
        close(second,second_owned)
        assert proc.poll() is None
        report['cases'].append({'case':'duplicate-launch','status':'PASS'})
        print('duplicate-launch PASS',flush=True)
        samples=[]
        started=time.monotonic()
        for index in range(16):
            assert proc.poll() is None,'EXE stopped during steady-state monitoring'
            card=wait_card(proc,owned)
            snapshot=resource(card[1])
            snapshot['elapsed']=round(time.monotonic()-started,2)
            samples.append(snapshot)
            if index in (0,7,15):
                print('soak '+str(snapshot),flush=True)
            if index<15:
                time.sleep(5)
        warm,last=samples[1],samples[-1]
        assert last['private_bytes']-warm['private_bytes']<24*1024*1024,'Private memory grew excessively'
        assert last['handles']-warm['handles']<25,'Handle count grew excessively'
        assert last['gdi']-warm['gdi']<20,'GDI objects grew excessively'
        assert last['user']-warm['user']<20,'USER objects grew excessively'
        close(proc,owned)
        no_errors(data)
        report['cases'].append({'case':'75-second-steady-state','status':'PASS','resources':samples})
        assert smoke.protected_hashes()==protected,'Original protected files changed'
        assert existing.issubset(smoke.processes()),'Original process disappeared'
        report['status']='PACKAGED_FAULT_CHECK_OK'
        report['protected_after']=smoke.protected_hashes()
        print('PACKAGED_FAULT_CHECK_OK report='+str(out/'report.json'),flush=True)
    finally:
        for proc,owned in reversed(current):
            if proc.poll() is None:
                try:
                    close(proc,owned)
                except Exception:
                    proc.terminate()
        (out/'report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')

if __name__=='__main__':
    main()
