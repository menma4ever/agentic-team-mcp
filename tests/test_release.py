import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts import check_release


class ReleaseTests(unittest.TestCase):
    def test_export_excludes_runtime_credentials_and_private_documents(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            names = ['README.md', 'main.py', 'core/config.py', 'web/static/app.js',
                     'web/static/logos/LICENSE', 'tests/test_example.py',
                     'settings.json', 'engine.json', 'google_accounts.json',
                     'auth/google/slot/credential.dat', 'projects/private/main.py',
                     'docs/2026-09-21-studio-revision.md', 'tests/.tmp/private.py',
                     'core/__pycache__/private.py', 'engine.log']
            for name in names:
                path = root / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text('synthetic data', encoding='utf-8')
            with patch.object(check_release, 'ROOT', root):
                actual = {p.relative_to(root).as_posix() for p in check_release.candidates()}
            self.assertEqual(actual, set(names[:6]))

    def test_git_ignore_excludes_sensitive_runtime_files(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            (root / '.gitignore').write_bytes((check_release.ROOT / '.gitignore').read_bytes())
            subprocess.run(['git', 'init', '-q', str(root)], check=True, capture_output=True)
            private = ['settings.json', 'engine.json', 'google_accounts.json',
                       'auth/google/slot/credential.dat', 'projects/private/main.py',
                       'team.sqlite3-wal', 'engine-recovery.stdout.log', '.env', 'err.txt']
            result = subprocess.run(['git', '-C', str(root), 'check-ignore', '--stdin', '-z'],
                input=('\0'.join(private) + '\0').encode(), capture_output=True, check=True)
            self.assertEqual(set(result.stdout.decode().strip('\0').split('\0')), set(private))


if __name__ == '__main__': unittest.main()
