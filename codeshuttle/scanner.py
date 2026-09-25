"""Обход проекта: отбор и чтение файлов без изменения содержимого."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from fnmatch import fnmatch
from pathlib import Path

from .util import Log, decode_source, sha256_text, utf8_len

BATCH_SIGNATURE = "### CODESHUTTLE"
BRANCH_MARKER = ".codeshuttle-branch.json"   # файл ветки (см. gitsync) — нужен только отправителю


@dataclass
class CodeFile:
    rel: str
    text: str
    sha: str
    size: int


@dataclass
class ScanResult:
    files: list[CodeFile] = field(default_factory=list)
    skipped: list[tuple[str, str]] = field(default_factory=list)   # (rel, причина)

    @property
    def total_bytes(self) -> int:
        return sum(f.size for f in self.files)

    def as_sha_map(self) -> dict[str, str]:
        return {f.rel: f.sha for f in self.files}


def _match_any(rel: str, name: str, patterns: list[str]) -> bool:
    low_rel, low_name = rel.lower(), name.lower()
    for p in patterns:
        pl = p.lower()
        if fnmatch(low_name, pl) or fnmatch(low_rel, pl) or fnmatch("/" + low_rel, pl):
            return True
    return False


def _is_code_name(name: str, scan_cfg: dict) -> bool:
    if scan_cfg.get("mode", "whitelist") == "all":
        return True
    ext = name.rsplit(".", 1)[-1].lower() if "." in name else ""
    if ext and ext in {e.lower().lstrip(".") for e in scan_cfg.get("extensions", [])}:
        return True
    lname = name.lower()
    for fn in scan_cfg.get("filenames", []):
        if lname == fn.lower() or lname.startswith(fn.lower() + "."):
            return True
    return False


def _reserved_dirs(cfg: dict) -> list[Path]:
    out = []
    for key in ("export_dir", "import_dir"):
        val = (cfg.get(key) or "").strip()
        if val:
            try:
                out.append(Path(val).resolve())
            except Exception:
                pass
    return out


def scan_project(cfg: dict, log: Log | None = None) -> ScanResult:
    log = log or Log(lambda _m: None)
    root = Path(cfg["project_root"]).resolve()
    scan_cfg = cfg["scan"]
    max_file_bytes = int(float(scan_cfg.get("max_file_kb", 512)) * 1024)
    exclude_dirs = [d.lower() for d in scan_cfg.get("exclude_dirs", [])]
    reserved = _reserved_dirs(cfg)
    result = ScanResult()

    for dirpath, dirnames, filenames in os.walk(root, followlinks=bool(scan_cfg.get("follow_symlinks"))):
        here = Path(dirpath)
        # обрезаем ветки целиком
        keep = []
        for d in dirnames:
            dl = d.lower()
            if any(fnmatch(dl, pat) for pat in exclude_dirs):
                continue
            sub = (here / d).resolve()
            if any(sub == r or r in sub.parents for r in reserved):
                continue  # это outbox/inbox самого CodeShuttle
            keep.append(d)
        dirnames[:] = sorted(keep)

        for name in sorted(filenames):
            path = here / name
            try:
                rel = path.relative_to(root).as_posix()
            except ValueError:
                continue

            if name == BRANCH_MARKER:
                result.skipped.append((rel, "файл ветки CodeShuttle"))
                continue
            # keep_globs сильнее exclude_globs и белого списка расширений
            forced = _match_any(rel, name, scan_cfg.get("keep_globs", []))
            if not forced:
                if _match_any(rel, name, scan_cfg.get("exclude_globs", [])):
                    result.skipped.append((rel, "исключено по маске"))
                    continue
                if not _is_code_name(name, scan_cfg):
                    result.skipped.append((rel, "не код (расширение)"))
                    continue
            try:
                st = path.stat()
            except OSError as exc:
                result.skipped.append((rel, f"нет доступа: {exc.strerror}"))
                continue
            if st.st_size > max_file_bytes:
                result.skipped.append((rel, f"слишком большой ({st.st_size} б)"))
                continue
            try:
                raw = path.read_bytes()
            except OSError as exc:
                result.skipped.append((rel, f"ошибка чтения: {exc.strerror}"))
                continue

            text = decode_source(raw)
            if text is None:
                result.skipped.append((rel, "бинарный файл"))
                continue
            if text.lstrip().startswith(BATCH_SIGNATURE):
                result.skipped.append((rel, "это батч CodeShuttle"))
                continue

            result.files.append(CodeFile(
                rel=rel,
                text=text,
                sha=sha256_text(text),
                size=utf8_len(text),
            ))

    result.files.sort(key=lambda f: f.rel)
    result.skipped.sort()
    return result
