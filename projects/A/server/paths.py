"""Explicit paths; importing or inspecting never initializes runtime storage."""
from dataclasses import dataclass
from pathlib import Path
from .common import AppError

@dataclass(frozen=True)
class Paths:
    root: Path
    @classmethod
    def from_root(cls, root):
        path = Path(root)
        if not path.is_absolute():
            raise AppError(400, 'absolute_path_required', '运行目录必须为绝对路径')
        path = path.resolve()
        source = Path(__file__).resolve().parents[1]
        if path == source or source.is_relative_to(path) or path.is_relative_to(source):
            raise AppError(400, 'unsafe_runtime_root', '运行目录须独立于程序目录')
        return cls(path)
    @property
    def db(self): return self.root / 'runtime' / 'facts.sqlite3'
    @property
    def auth(self): return self.root / 'keys' / 'enrollment.dpapi'
    @property
    def settings(self): return self.root / 'config.json'
    @property
    def backup(self): return self.root / 'backups'
    def initialize_directories(self):
        for name in ('runtime','keys','staging','sync/incoming','sync/outgoing','backups','journal'):
            (self.root / name).mkdir(parents=True, exist_ok=True)
