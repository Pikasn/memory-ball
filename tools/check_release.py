"""Inspect locally built PyInstaller executables before publishing.

Checks archive contents and nested Python code filenames without running them.
Use the same Python minor version that was used for the build.
"""
from pathlib import Path
import argparse
import io
import marshal
import re
import types
import zipfile

import pefile
from PyInstaller.archive.readers import CArchiveReader

HOME_PATH = re.compile(rb'(?i)[A-Z]:[\\/]+Users[\\/]+[A-Z0-9_. -]+[\\/]')
PRIVATE_NAMES = {'settings.json', '.env', 'credentials.json'}


def inspect_executable(path, forbidden):
    archive = CArchiveReader(str(path))
    count = 0
    failures = set()
    with pefile.PE(str(path), fast_load=True) as executable:
        if executable.FILE_HEADER.Machine != 0x8664:
            failures.add('executable is not Windows x64')
        if executable.OPTIONAL_HEADER.Subsystem != 2:
            failures.add('executable is not a windowed application')

    def scan(label, data):
        if HOME_PATH.search(data):
            failures.add(label + ': embedded user directory')
        for marker in forbidden:
            if marker in data or marker.decode().encode('utf-16le') in data:
                failures.add(label + ': forbidden build text')

    def code(label, obj):
        nonlocal count
        if not isinstance(obj, types.CodeType):
            return
        count += 1
        filename = obj.co_filename
        if re.match(r'(?i)^[A-Z]:[\\/]|^[/]', filename):
            failures.add(label + ': absolute Python source filename')
        for item in obj.co_consts:
            if isinstance(item, types.CodeType):
                code(label, item)

    scan(path.name, path.read_bytes())
    for name, entry in archive.toc.items():
        normalized = name.replace('\\', '/')
        if Path(normalized).name.lower() in PRIVATE_NAMES or normalized.lower().endswith(('.log', '.dmp')):
            failures.add(name + ': private/generated file')
        if entry[-1] == 'z':
            pyz = archive.open_embedded_archive(name)
            for module_name in pyz.toc:
                raw = pyz.extract(module_name, raw=True)
                if raw is not None:
                    scan(module_name, raw)
                    code(module_name, pyz.extract(module_name))
            continue
        data = archive.extract(name)
        scan(name, data)
        if entry[-1] in ('s', 'm', 'M'):
            code(name, marshal.loads(data))
        elif name.endswith('.pyc'):
            code(name, marshal.loads(data[16:]))
        elif name.endswith('.zip'):
            with zipfile.ZipFile(io.BytesIO(data)) as bundle:
                for member in bundle.namelist():
                    content = bundle.read(member)
                    scan(member, content)
                    if member.endswith('.pyc'):
                        code(member, marshal.loads(content[16:]))
    for failure in sorted(failures):
        print(path.name + ': ' + failure)
    print(f'RELEASE_{"FAILED" if failures else "OK"}: {path.name}, '
          f'{len(archive.toc)} archive entries, {count} code objects')
    return bool(failures)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('artifacts', nargs='+', type=Path)
    parser.add_argument('--forbid-text', action='append', default=[],
                        help='Additional local identifiers to reject; values are never printed')
    args = parser.parse_args()
    forbidden = [value.encode() for value in args.forbid_text]
    return any([inspect_executable(path, forbidden) for path in args.artifacts])


if __name__ == '__main__':
    raise SystemExit(main())
