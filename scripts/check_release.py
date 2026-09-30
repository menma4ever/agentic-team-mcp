"""Review source candidates; optionally export only this allowlist, never runtime data.

This is a conservative publication helper, not a comprehensive secret scanner.
Findings print a rule, filename and line number, never the matched value.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import zipfile

ROOT = Path(__file__).resolve().parent.parent
TOP = {'.gitignore', 'README.md', 'requirements.txt', 'main.py', 'Setup.ps1',
       'Launch.cmd', 'deepseek_session.py', 'deepseek.cmd', 'test_system.py',
       'test_web_api.py', 'LICENSE', 'NOTICE', 'SOURCE-MANIFEST.json', 'settings.example.json'}
FOLDERS = {'core', 'engine', 'harness', 'mcp_server', 'web', 'tests', 'scripts', '.github', 'assets', 'docs'}
TEXT_EXTENSIONS = {'.py', '.js', '.cjs', '.css', '.html', '.svg', '.yml', '.yaml', '.md', '.json', '.cmd', '.ps1'}
BINARY_EXTENSIONS = {'.png', '.gif', '.ico', '.jpg', '.jpeg', '.webp'}
EXTENSIONS = TEXT_EXTENSIONS | BINARY_EXTENSIONS
DOCS = {'docs/backend-audit.md', 'docs/MCP_SYSTEM_IMPROVEMENTS_COMPREHENSIVE_SUMMARY.md'}
EXCLUDED = {'__pycache__', '.tmp', '.venv', 'node_modules'}
RULES = {
    'key-like literal': re.compile(r'\b(?:sk-[A-Za-z0-9_-]{20,}|AIza[A-Za-z0-9_-]{30,}|gh[pousr]_[A-Za-z0-9]{25,})'),
    'JWT-like literal': re.compile(r'\beyJ[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{15,}'),
    'private key': re.compile(r'-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----'),
    'personal email': re.compile(r'[\w.+-]+@(?:gmail|outlook|hotmail|yahoo)\.com', re.I),
    'personal home path': re.compile(r'C:[/\\]+Users[/\\]+(?!Public(?:[/\\]|$)|<)[^\s\"\n]+', re.I),
}


def candidates():
    result = []
    paths = [ROOT / name for name in TOP | DOCS if (ROOT / name).is_file()]
    for folder in FOLDERS:
        for directory, dirs, names in os.walk(ROOT / folder, followlinks=False):
            dirs[:] = [name for name in dirs if name not in EXCLUDED
                       and not (Path(directory) / name).is_symlink()]
            paths.extend(Path(directory) / name for name in names)
    for path in paths:
        # Only files whose resolved location remains inside source are eligible.
        relative = path.relative_to(ROOT)
        if any(part in EXCLUDED for part in relative.parts): continue
        name = relative.as_posix()
        eligible = (name in TOP or name in DOCS or
            (relative.parts[0] in FOLDERS and
             (path.suffix in EXTENSIONS or name == 'web/static/logos/LICENSE')))
        if eligible and path.is_file():
            if path.is_symlink() or not path.resolve().is_relative_to(ROOT):
                raise ValueError(f'Unexpected linked source file: {name}')
            result.append(path)
    return sorted(result)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--zip', type=Path, help='Write a new source-only ZIP and SHA-256 manifest')
    args = parser.parse_args()
    files = candidates()
    contents = {path: path.read_bytes() for path in files}
    findings = []
    for path in files:
        if path.suffix in BINARY_EXTENSIONS:
            continue
        try:
            text = contents[path].decode('utf-8-sig')
        except UnicodeDecodeError:
            continue
        for number, line in enumerate(text.splitlines(), 1):
            for name, rule in RULES.items():
                if rule.search(line): findings.append(f'{path.relative_to(ROOT).as_posix()}:{number}: {name}')
    # In a Git checkout, also reject tracked files outside the explicit allowlist.
    git = subprocess.run(['git', '-C', str(ROOT), 'ls-files', '-z'], capture_output=True) if shutil.which('git') else None
    if git and git.returncode == 0:
        allowed = {p.relative_to(ROOT).as_posix() for p in files}
        for name in git.stdout.decode('utf-8').split('\0'):
            if name and name not in allowed: findings.append(f'{name}: tracked outside source allowlist')
    if findings:
        print('\n'.join(findings))
        raise SystemExit('Review blocked: potential private content or unreviewed tracked files.')
    print(f'PASS: {len(files)} source files; no configured-pattern matches. Runtime data excluded.')
    if args.zip:
        files = [p for p in files if p.name != 'SOURCE-MANIFEST.json']
        destination = args.zip.resolve()
        destination.parent.mkdir(parents=True, exist_ok=True)
        manifest = {p.relative_to(ROOT).as_posix(): hashlib.sha256(contents[p]).hexdigest() for p in files}
        with zipfile.ZipFile(destination, 'x', zipfile.ZIP_DEFLATED) as archive:
            for path in files: archive.writestr(path.relative_to(ROOT).as_posix(), contents[path])
            archive.writestr('SOURCE-MANIFEST.json', json.dumps(manifest, indent=2) + '\n')
        print(f'Source review archive: {destination}')


if __name__ == '__main__': main()
