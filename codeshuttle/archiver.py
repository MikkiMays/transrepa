"""Дёргает штатный экспорт (exporter.export, по умолчанию kind="delta", батчи .txt)
и упаковывает получившиеся батчи в один .rar через WinRAR. Никакой своей
логики диффа/отбора файлов — только то, что уже делает exporter/scanner/state.

Перед каждым прогоном чистит outbox от батчей прошлого прогона (только *.txt
прямо в export_dir — не трогает _reports/ и _applied/).
"""
from __future__ import annotations

import os
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from . import exporter
from .util import Log, human_size, now_stamp

RAR_CANDIDATES = [
    r"C:\Program Files\WinRAR\Rar.exe",
    r"C:\Program Files (x86)\WinRAR\Rar.exe",
]
UNRAR_CANDIDATES = [
    r"C:\Program Files\WinRAR\UnRAR.exe",
    r"C:\Program Files (x86)\WinRAR\UnRAR.exe",
]


def find_rar_exe() -> str | None:
    for name in ("rar", "Rar", "WinRAR"):
        found = shutil.which(name)
        if found:
            return found
    for p in RAR_CANDIDATES:
        if Path(p).exists():
            return p
    return None


def _extract_commands(rar_path: Path, dest: Path) -> list[list[str]]:
    """Чем можно распаковать: консольные Rar/UnRAR из WinRAR, затем tar из Windows 11."""
    cmds: list[list[str]] = []
    exes = [find_rar_exe(), shutil.which("unrar")] + [p for p in UNRAR_CANDIDATES if Path(p).exists()]
    for exe in exes:
        if exe and Path(exe).stem.lower() != "winrar" and not any(c[0] == exe for c in cmds):
            cmds.append([exe, "x", "-y", "-idq", str(rar_path), str(dest) + os.sep])
    system_tar = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32" / "tar.exe"
    tar = str(system_tar) if system_tar.exists() else shutil.which("tar")
    if tar:
        cmds.append([tar, "-xf", str(rar_path), "-C", str(dest)])
    return cmds


def extract_rar(rar_path: Path, dest: Path, log: Log | None = None) -> bool:
    """Распаковывает присланный архив с батчами в dest (обычно подпапка inbox)."""
    log = log or Log()
    if not rar_path.is_file():
        log(f"[ошибка] архив не найден: {rar_path}")
        return False
    cmds = _extract_commands(rar_path, dest)
    if not cmds:
        log("[ошибка] нечем распаковать .rar — установи WinRAR (или распакуй вручную в inbox)")
        return False
    dest.mkdir(parents=True, exist_ok=True)
    reason = ""
    for cmd in cmds:
        proc = subprocess.run(cmd, capture_output=True, text=True, errors="replace")
        if proc.returncode == 0:
            count = sum(1 for _ in dest.rglob("*.txt"))
            log(f"Распаковано: {rar_path.name} -> {dest}  (файлов .txt: {count})")
            return True
        reason = (proc.stderr or proc.stdout).strip() or f"код {proc.returncode}"
    log(f"[ошибка] не удалось распаковать {rar_path.name}: {reason}")
    return False


def default_archive_dir(cfg: dict) -> Path:
    """Если archive_dir не задан явно — рядом с project_root: <родитель>/.codeshuttle-archives."""
    explicit = (cfg.get("archive_dir") or "").strip()
    if explicit:
        return Path(explicit)
    root = Path(cfg["project_root"]).resolve()
    return root.parent / ".codeshuttle-archives"


def _clear_old_batches(export_dir: Path, log: Log) -> None:
    if not export_dir.is_dir():
        return
    removed = 0
    for f in export_dir.glob("*.txt"):
        f.unlink()
        removed += 1
    if removed:
        log(f"Удалено старых батчей в outbox: {removed}")


@dataclass
class ArchiveResult:
    ok: bool
    rar_path: Path | None = None
    batch_files: list[Path] = field(default_factory=list)
    message: str = ""


def archive_module(cfg: dict, archive_dir: Path | None = None, log: Log | None = None,
                   dry_run: bool = False, kind: str = "delta") -> ArchiveResult:
    """1) чистит outbox от старых .txt  2) exporter.export(cfg, kind)
    3) все получившиеся .txt пакует в один .rar в archive_dir."""
    log = log or Log()
    export_dir = Path(cfg["export_dir"]).resolve()
    archive_dir = archive_dir or default_archive_dir(cfg)

    if not dry_run:
        _clear_old_batches(export_dir, log)

    res = exporter.export(cfg, kind, log, dry_run=dry_run)
    if not res.ok:
        return ArchiveResult(ok=False, message=res.message)
    if not res.batch_files:
        return ArchiveResult(ok=True, message=res.message or "Изменений нет")
    if dry_run:
        return ArchiveResult(ok=True, batch_files=res.batch_files, message=res.message)

    rar_exe = find_rar_exe()
    if not rar_exe:
        log("[ошибка] Rar.exe не найден — установи WinRAR или добавь rar в PATH.")
        return ArchiveResult(ok=False, batch_files=res.batch_files, message="Rar.exe не найден")

    archive_dir.mkdir(parents=True, exist_ok=True)
    project_name = Path(cfg["project_root"]).resolve().name
    rar_path = archive_dir / f"{now_stamp()}_{project_name}_{kind}.rar"

    listfile = export_dir / ".archive_filelist.tmp"
    listfile.write_text("\n".join(p.name for p in res.batch_files), encoding="utf-8")
    try:
        proc = subprocess.run(
            [rar_exe, "a", "-y", "-idq", str(rar_path), f"@{listfile}"],
            cwd=str(export_dir), capture_output=True, text=True,
        )
        if proc.returncode != 0:
            log(f"[ошибка] Rar.exe вернул {proc.returncode}: {(proc.stderr or proc.stdout).strip()}")
            return ArchiveResult(ok=False, batch_files=res.batch_files, message="Rar.exe завершился с ошибкой")
    finally:
        listfile.unlink(missing_ok=True)

    size = rar_path.stat().st_size if rar_path.exists() else 0
    log(f"Архив: {rar_path}  ({human_size(size)}, батчей: {len(res.batch_files)})")
    return ArchiveResult(ok=True, rar_path=rar_path, batch_files=res.batch_files, message=res.message)
