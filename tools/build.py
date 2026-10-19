"""Build one variant from repository sources with the current Python runtime."""
from pathlib import Path
import argparse
import hashlib
import subprocess
import sys
import time


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('variant', choices=('original', 'network'))
    args = parser.parse_args()
    if sys.platform != 'win32':
        parser.error('Windows is required to build this application')
    base = Path(__file__).resolve().parents[1] / args.variant
    source = base / 'source'
    output = base / 'build'
    stage = output / ('stage-' + time.strftime('%Y%m%d-%H%M%S'))
    stage.mkdir(parents=True, exist_ok=False)
    name = 'MemoryWidget' if args.variant == 'original' else '内存球网络版'
    command = [sys.executable, '-m', 'PyInstaller', '--onefile', '--noconsole',
               '--noconfirm', '--name', name, '--icon', str(source / 'app.ico'),
               '--paths', str(source), '--distpath', str(output),
               '--workpath', str(stage / 'work'), '--specpath', str(stage / 'spec')]
    for module in sorted(source.glob('*.py')):
        if module.stem != 'memory_widget':
            command.extend(['--hidden-import', module.stem])
    command.append(str(source / 'memory_widget.py'))
    subprocess.run(command, cwd=base, check=True)
    artifact = output / (name + '.exe')
    checksum = hashlib.sha256(artifact.read_bytes()).hexdigest()
    (output / 'release.sha256').write_text(checksum + '  ' + artifact.name + '\n', encoding='utf-8')
    print('BUILD_OK', artifact.name, checksum)


if __name__ == '__main__':
    main()
