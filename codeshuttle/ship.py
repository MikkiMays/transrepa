"""Экспорт одной кнопкой: git (fetch + ветка из файла ветки) -> батчи -> .rar.

  1. во всех папках с файлом ветки — fetch и проверки, рабочие копии не меняются;
  2. хоть одна проблема -> стоп: ни одна ветка не переключена, ничего не выгружено;
  3. переключение/обновление веток (если нужно переключать — сначала спросить);
  4. штатная выгрузка батчей и упаковка их в .rar (archiver.archive_module).

Папки без файла ветки git не трогает — они уходят в выгрузку как есть.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from . import archiver, config as cfgmod
from . import gitsync
from .archiver import ArchiveResult
from .gitsync import SyncPlan
from .util import Log


@dataclass
class ShipResult:
    ok: bool
    stage: str                      # config | git | dry-run | cancel | switch | export | done
    sync: SyncPlan | None = None
    archive: ArchiveResult | None = None
    message: str = ""


def ship(cfg: dict, log: Log | None = None, kind: str | None = None, dry_run: bool = False,
         confirm: Callable[[SyncPlan], bool] | None = None,
         on_repo: Callable[[SyncPlan], None] | None = None) -> ShipResult:
    """confirm(sync) спрашивают перед переключением веток (если git.confirm_switch);
    on_repo(sync) дёргается после проверки каждой папки — для живой таблицы в окне."""
    log = log or Log()
    kind = kind or cfg.get("ship_kind") or "full"
    errors = cfgmod.validate(cfg, need_export=True)
    if errors:
        for e in errors:
            log(f"[ошибка] {e}")
        return ShipResult(False, "config", message="; ".join(errors))

    log.section(f"Экспорт одной кнопкой: git → батчи ({'весь проект' if kind == 'full' else 'изменения'}) → .rar")
    sync = gitsync.preflight(cfg, log, on_repo=on_repo)
    if not sync.ok:
        log("\n✖ Выгрузка остановлена до каких-либо изменений: ни одна ветка не переключалась, "
            "батчи не создавались. Исправь проблемы выше и запусти снова.")
        return ShipResult(False, "git", sync, message="проблемы в git")

    if dry_run:
        log("\n[dry-run] Git-проверки пройдены. Ветки не переключались, выгрузка не выполнялась.")
        return ShipResult(True, "dry-run", sync)

    ask = cfg.get("git", {}).get("confirm_switch", True)
    if sync.switches and confirm is not None and ask and not confirm(sync):
        log("\nОтменено: ветки не переключались, выгрузка не выполнялась. "
            "Переключи ветки сам и запусти снова.")
        return ShipResult(False, "cancel", sync, message="отменено")

    if not gitsync.apply(sync, log):
        log("\n✖ Выгрузка не выполнялась: git не смог переключить/обновить ветку (см. выше).")
        return ShipResult(False, "switch", sync, message="git не переключил ветку")

    ar = archiver.archive_module(cfg, log=log, kind=kind)
    return ShipResult(ar.ok, "done" if ar.ok else "export", sync, ar, ar.message)
