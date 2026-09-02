"""Состояние синхронизации: хэши файлов на момент последнего экспорта/импорта."""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

from .util import now_iso, sha256_str

STATE_VERSION = 1


def state_hash(files: dict[str, str]) -> str:
    """Отпечаток всего дерева: sha256 от отсортированных пар путь:sha."""
    if not files:
        return "empty"
    payload = "\n".join(f"{p}:{files[p]}" for p in sorted(files))
    return sha256_str(payload)[:32]


@dataclass
class State:
    path: Path
    project_root: str = ""
    files: dict[str, str] = field(default_factory=dict)   # rel -> sha256
    last_pack: str = ""
    last_kind: str = ""
    last_action_at: str = ""
    history: list[dict] = field(default_factory=list)

    @property
    def tree_hash(self) -> str:
        return state_hash(self.files)

    @classmethod
    def load(cls, path: Path, project_root: str = "") -> "State":
        if not path.exists():
            return cls(path=path, project_root=project_root)
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return cls(path=path, project_root=project_root)
        return cls(
            path=path,
            project_root=data.get("project_root", project_root),
            files=data.get("files", {}),
            last_pack=data.get("last_pack", ""),
            last_kind=data.get("last_kind", ""),
            last_action_at=data.get("last_action_at", ""),
            history=data.get("history", []),
        )

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "version": STATE_VERSION,
            "project_root": self.project_root,
            "tree_hash": self.tree_hash,
            "last_pack": self.last_pack,
            "last_kind": self.last_kind,
            "last_action_at": self.last_action_at,
            "files": dict(sorted(self.files.items())),
            "history": self.history[-50:],
        }
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
        tmp.replace(self.path)

    def record(self, action: str, pack_id: str, kind: str, files: dict[str, str], note: str = "") -> None:
        self.files = dict(files)
        self.last_pack = pack_id
        self.last_kind = kind
        self.last_action_at = now_iso()
        self.history.append({
            "at": self.last_action_at, "action": action, "pack": pack_id,
            "kind": kind, "files": len(files), "tree_hash": self.tree_hash, "note": note,
        })

    def diff(self, current: dict[str, str]) -> tuple[list[str], list[str], list[str]]:
        """(добавленные, изменённые, удалённые) относительно сохранённого состояния."""
        added = sorted(p for p in current if p not in self.files)
        changed = sorted(p for p in current if p in self.files and self.files[p] != current[p])
        deleted = sorted(p for p in self.files if p not in current)
        return added, changed, deleted
