"""Reject sensitive patterns and unexpected files in the Git index.

Reports file names and rule names only, never matched secret values.
"""
from pathlib import Path, PurePosixPath
import re
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
RULES = {
    'personal home path': re.compile(r'(?i)(?:[a-z]:[\\/]+Users[\\/]+|[/]home[/]|[/]Users[/])[^\s\"\'<>]+'),
    'private key': re.compile(r'-----BEGIN (?:RSA |EC |OPENSSH |DSA )?PRIVATE KEY-----'),
    'GitHub credential': re.compile(r'\b(?:gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,})\b'),
    'provider credential': re.compile(r'\b(?:sk-(?:proj-)?[A-Za-z0-9_-]{24,}|AKIA[A-Z0-9]{16}|AIza[A-Za-z0-9_-]{30,})\b'),
    'credential assignment': re.compile(r'(?i)\b(?:api_key|access_token|auth_token|client_secret|password)\s*[:=]\s*[\"\'][^\"\'\n]{12,}[\"\']'),
    'private contact email': re.compile(r'(?i)\b[A-Z0-9._%+-]+@(?!users\.noreply\.github\.com\b|example\.(?:com|org|net)\b)[A-Z0-9.-]+\.[A-Z]{2,}\b'),
    'private IPv4 address': re.compile(r'\b(?:10(?:\.\d{1,3}){3}|192\.168(?:\.\d{1,3}){2}|172\.(?:1[6-9]|2\d|3[01])(?:\.\d{1,3}){2})\b'),
}
SUFFIXES = {'.py', '.md', '.txt', '.yml', '.yaml'}
SPECIAL = {'.gitignore', '.gitattributes'}
BLOCKED_DIRS = {'build', 'dist', 'captures', 'screenshots', 'evidence', 'outputs', '__pycache__', '.venv', 'venv'}
ICONS = {'original/source/app.ico', 'network/source/app.ico'}


def problems(name, data):
    path = PurePosixPath(name)
    findings = []
    if any(part in BLOCKED_DIRS for part in path.parts):
        findings.append('generated/private directory')
    if name in ICONS:
        if not data.startswith(b'\x00\x00\x01\x00'):
            findings.append('invalid icon resource')
        # Raster icon resources have been manually reviewed. New binary types fail closed.
        return findings
    if path.suffix.lower() not in SUFFIXES and path.name not in SPECIAL:
        findings.append('unreviewed file type')
    if 'settings' in path.name.lower() or path.name.startswith('.env'):
        findings.append('local configuration')
    try:
        text = data.decode('utf-8-sig')
    except UnicodeError:
        return findings + ['non-UTF-8 content']
    for rule, pattern in RULES.items():
        if pattern.search(text):
            findings.append(rule)
    return findings


def main():
    names = subprocess.check_output(['git', 'ls-files', '--cached', '-z'], cwd=ROOT).decode().split('\0')
    count, failures = 0, 0
    for name in filter(None, names):
        data = subprocess.check_output(['git', 'show', ':' + name], cwd=ROOT)
        findings = problems(name, data)
        count += 1
        if findings:
            failures += 1
            print(name + ': ' + ', '.join(findings))
    if count == 0:
        print('PRIVACY_FAILED: no staged/tracked files')
        return 1
    print(f'PRIVACY_{"FAILED" if failures else "OK"}: {count} indexed files, {failures} rejected')
    return bool(failures)


if __name__ == '__main__':
    sys.exit(main())
