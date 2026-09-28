import json
import re
import shutil
from pathlib import Path
from core.config import DATA_DIR

PROJECTS_DIR = DATA_DIR / 'projects'


def safe_name(name):
    if not name or len(name) > 100 or name != name.strip() or name in ('.', '..'):
        raise ValueError('Use a non-empty name of at most 100 characters')
    if any(c in name for c in '<>:"/\\|?*') or any(ord(c) < 32 for c in name) or name.endswith(('.', ' ')):
        raise ValueError('Name contains a path separator or invalid Windows character')
    if name.split('.')[0].upper() in {'CON', 'PRN', 'AUX', 'NUL', *[f'COM{i}' for i in range(10)], *[f'LPT{i}' for i in range(10)]}:
        raise ValueError('Reserved Windows name')
    return name


def contained(root, relative):
    root = Path(root).resolve()
    value = Path(relative)
    if value.is_absolute() or value.drive or '..' in value.parts:
        raise ValueError('Use a relative path within the allowed folder')
    target = (root / value).resolve()
    if not target.is_relative_to(root):
        raise ValueError('Path leaves the allowed folder')
    return target


class WorkspaceManager:
    def __init__(self, root_dir=PROJECTS_DIR):
        self.root_dir = Path(root_dir).resolve()
        self.root_dir.mkdir(parents=True, exist_ok=True)

    def get_project_dir(self, name):
        return contained(self.root_dir, safe_name(name))

    def create_project(self, name, description=''):
        p = self.get_project_dir(name)
        p.mkdir(exist_ok=False)
        for part in ('ceo', 'manager', 'workers', 'shared', 'artifacts'):
            (p / part).mkdir()
        meta = {'name': name, 'description': description, 'path': str(p)}
        (p / 'project_meta.json').write_text(json.dumps(meta, indent=2), encoding='utf-8')
        return meta

    def list_projects(self):
        result = []
        for p in self.root_dir.glob('*/project_meta.json'):
            try:
                result.append(json.loads(p.read_text(encoding='utf-8')))
            except (ValueError, OSError):
                continue
        return result

    def create_worker_dir(self, project_name, worker_name, **kwargs):
        target = contained(self.get_project_dir(project_name), Path('workers') / safe_name(worker_name))
        target.mkdir(parents=True, exist_ok=True)
        return target

    def read_worker_status_md(self, project_name, worker_name):
        target = contained(self.get_project_dir(project_name), Path('workers') / safe_name(worker_name) / 'status.md')
        return target.read_text(encoding='utf-8') if target.exists() else 'No report yet'

    def delete_worker_dir(self, project_name, worker_name):
        parent = (self.get_project_dir(project_name) / 'workers').resolve()
        target = contained(self.get_project_dir(project_name), Path('workers') / safe_name(worker_name))
        if target.parent != parent or target == parent:
            raise ValueError('Unsafe deletion target')
        if target.exists():
            shutil.rmtree(target)
            return True
        return False

    def delete_project_dir(self, name: str) -> bool:
        target = self.get_project_dir(name)
        if target.parent != self.root_dir.resolve() or target == self.root_dir.resolve():
            raise ValueError('Unsafe project deletion target')
        if target.exists():
            shutil.rmtree(target)
            return True
        return False


workspace_mgr = WorkspaceManager()


