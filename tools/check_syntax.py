"""Check source syntax without importing Windows APIs or generating bytecode."""
from pathlib import Path
import subprocess

ROOT = Path(__file__).resolve().parents[1]
names = subprocess.check_output(['git', 'ls-files', '-z', '--', '*.py'], cwd=ROOT).decode().split('\0')
count = 0
for name in filter(None, names):
    compile((ROOT / name).read_bytes(), name, 'exec')
    count += 1
print(f'SYNTAX_OK: {count} Python files')
