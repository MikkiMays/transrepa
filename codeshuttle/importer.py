"""Импорт батчей: сборка файлов обратно в дерево проекта.

Импорт не зависит от файла состояния: сравнивается то, что приехало в батче,
с тем, что реально лежит на диске.

  файла нет            -> создать
  есть, sha разошлись  -> обновить (старую версию в бэкап)
  есть, sha совпали    -> не трогать вообще
  DELETE в дельте      -> удалить (в бэкап)

Повторное применение тех же батчей ничего не меняет — операция идемпотентна.
"""
from __future__ import annotations

import shutil
from dataclasses import dataclass, field
from pathlib import Path

from . import config as cfgmod
from . import packfmt
from .scanner import scan_project
from .state import State, state_hash
from .util import Log, human_size, is_safe_relpath, now_stamp, sha256_text

READABLE_SUFFIXES = (".txt", ".md", ".batch", ".log", ".text", ".dat", "")


@dataclass
class PackPlan:
    pack_id: str
    kind: str
    base: str
    result: str
    generated: str
    batches: dict[int, packfmt.ParsedBatch] = field(default_factory=dict)
    total: int = 0
    files: dict[str, str] = field(default_factory=dict)   # rel -> проверенный текст
    shas: dict[str, str] = field(default_factory=dict)    # rel -> sha256
    deletes: list[str] = field(default_factory=list)
    missing_batches: list[int] = field(default_factory=list)
    broken: list[tuple[str, str]] = field(default_factory=list)  # (файл, причина)
    notes: list[str] = field(default_factory=list)
    sources: list[Path] = field(default_factory=list)
    duplicates: int = 0

    @property
    def complete(self) -> bool:
        return not self.missing_batches and not self.broken


@dataclass
class ImportResult:
    ok: bool
    created: list[str] = field(default_factory=list)
    updated: list[str] = field(default_factory=list)
    unchanged: list[str] = field(default_factory=list)
    deleted: list[str] = field(default_factory=list)
    failed: list[tuple[str, str]] = field(default_factory=list)
    packs: list[str] = field(default_factory=list)
    incomplete_packs: list[str] = field(default_factory=list)
    message: str = ""
    backup_dir: Path | None = None

    @property
    def written(self) -> list[str]:
        return self.created + self.updated


def collect_batches(import_dir: Path, log: Log) -> list[packfmt.ParsedBatch]:
    """Читает все файлы папки и вытаскивает из них батчи (имя файла не важно)."""
    found: list[packfmt.ParsedBatch] = []
    seen_files = 0
    for path in sorted(import_dir.rglob("*")):
        if not path.is_file() or "_applied" in path.parts:
            continue
        if path.suffix.lower() not in READABLE_SUFFIXES:
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        seen_files += 1
        if not packfmt.looks_like_batch(text):
            continue
        parsed = packfmt.parse_text(text, source=str(path))
        if parsed:
            log(f"  {path.name}: батчей {len(parsed)}")
            found.extend(parsed)
    if not found:
        log(f"  просмотрено файлов: {seen_files}, батчей не найдено")
    return found


def build_plans(batches: list[packfmt.ParsedBatch]) -> list[PackPlan]:
    """Группирует батчи по пакам и собирает из частей целые файлы."""
    plans: dict[str, PackPlan] = {}
    for b in batches:
        plan = plans.get(b.pack_id)
        if plan is None:
            plan = PackPlan(pack_id=b.pack_id, kind=b.kind, base=b.base,
                            result=b.result, generated=b.generated, total=b.total)
            plans[b.pack_id] = plan
        if b.index in plan.batches:
            plan.duplicates += 1   # тот же батч принесли дважды — игнорируем копию
            continue
        plan.batches[b.index] = b
        plan.total = max(plan.total, b.total)
        if b.source and Path(b.source) not in plan.sources:
            plan.sources.append(Path(b.source))
        for err in b.errors:
            plan.broken.append(("<батч>", err))

    for plan in plans.values():
        plan.missing_batches = [i for i in range(1, plan.total + 1) if i not in plan.batches]

        chunks: dict[str, dict[int, packfmt.ParsedPart]] = {}
        for idx in sorted(plan.batches):
            b = plan.batches[idx]
            for part in b.parts:
                chunks.setdefault(part.path, {})[part.part] = part
            for d in b.deletes:
                if d not in plan.deletes:
                    plan.deletes.append(d)

        for rel, parts in sorted(chunks.items()):
            expected = max(p.parts for p in parts.values())
            gaps = [i for i in range(1, expected + 1) if i not in parts]
            if gaps:
                plan.broken.append((rel, f"нет частей {gaps} из {expected}"))
                continue
            text = "".join(parts[i].text for i in range(1, expected + 1))
            declared = parts[1].sha256
            actual = sha256_text(text)
            if declared and actual != declared:
                plan.broken.append((rel, "не сошлась контрольная сумма (батч повреждён)"))
                continue
            if not is_safe_relpath(rel):
                plan.broken.append((rel, "небезопасный путь — отклонён"))
                continue
            plan.files[rel] = text
            plan.shas[rel] = declared or actual

        plan.deletes = [d for d in plan.deletes if is_safe_relpath(d)]

    return order_plans(list(plans.values()))


def order_plans(plans: list[PackPlan]) -> list[PackPlan]:
    """Порядок применения паков.

    За основу берётся время создания, но оно может врать (часы разъехались,
    два пака в одну миллисекунду). Поэтому сверху накладывается цепочка
    состояний: если BASE пака совпал с RESULT другого, он обязан идти после
    него — дельта не может примениться раньше своей основы."""
    ordered = sorted(plans, key=lambda p: (p.generated or "", p.pack_id))

    by_result: dict[str, PackPlan] = {}
    for p in ordered:
        if p.result not in ("-", ""):
            by_result.setdefault(p.result, p)

    deps: dict[str, set[str]] = {p.pack_id: set() for p in ordered}
    for p in ordered:
        parent = by_result.get(p.base)
        if parent is not None and parent.pack_id != p.pack_id:
            deps[p.pack_id].add(parent.pack_id)

    out: list[PackPlan] = []
    done: set[str] = set()
    pool = list(ordered)
    while pool:
        for p in pool:
            if deps[p.pack_id] <= done:
                out.append(p)
                done.add(p.pack_id)
                pool.remove(p)
                break
        else:                      # кольцо зависимостей — оставляем как есть
            out.extend(pool)
            break
    return out


def _read_normalized(path: Path) -> str | None:
    try:
        return path.read_bytes().decode("utf-8").replace("\r\n", "\n").replace("\r", "\n")
    except (OSError, UnicodeDecodeError):
        return None


def apply_import(cfg: dict, log: Log | None = None, dry_run: bool = False,
                 import_dir: str | Path | None = None) -> ImportResult:
    """Применяет все батчи из папки. import_dir перекрывает значение из конфига."""
    log = log or Log()

    root_str = (cfg.get("project_root") or "").strip()
    if root_str and not Path(root_str).exists() and not dry_run:
        Path(root_str).mkdir(parents=True, exist_ok=True)
        log(f"Создана папка проекта: {root_str}")

    cfg_view = dict(cfg)
    if import_dir:
        cfg_view["import_dir"] = str(import_dir)
    errors = cfgmod.validate(cfg_view, need_import=True)
    if errors:
        for e in errors:
            log(f"[ошибка] {e}")
        return ImportResult(ok=False, message="; ".join(errors))

    root = Path(cfg_view["project_root"]).resolve()
    inbox = Path(cfg_view["import_dir"]).resolve()
    imp_cfg = cfg_view["import"]
    require_all = bool(imp_cfg.get("require_all_batches", False))

    log.section("Импорт" + (" (проверка, без записи)" if dry_run else ""))
    log(f"Источник батчей: {inbox}")
    log(f"Цель           : {root}")

    batches = collect_batches(inbox, log)
    if not batches:
        log("Батчей не найдено. Положи файлы выгрузки в эту папку.")
        return ImportResult(ok=False, message="Батчи не найдены")

    plans = build_plans(batches)
    log(f"Паков к применению: {len(plans)}")

    st = State.load(cfgmod.state_file_for(cfg), str(root))
    result = ImportResult(ok=True)
    backup_root: Path | None = None
    if imp_cfg.get("backup", True) and not dry_run:
        backup_root = root / ".codeshuttle_backup" / now_stamp()

    written_by: dict[str, str] = {}   # файл -> пак, который его уже записал в этом прогоне

    def backup(target: Path, rel: str) -> None:
        if backup_root and target.exists():
            dst = backup_root / rel
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(target, dst)

    for plan in plans:
        log(f"\n-- пак {plan.pack_id} [{plan.kind}]: батчей {len(plan.batches)}/{plan.total}, "
            f"файлов {len(plan.files)}, удалений {len(plan.deletes)}")
        if plan.duplicates:
            log(f"   дубликаты батчей пропущены: {plan.duplicates}")

        if plan.missing_batches:
            result.incomplete_packs.append(plan.pack_id)
            log(f"   [ВНИМАНИЕ] не хватает батчей {plan.missing_batches} из {plan.total}")
            if require_all:
                log("   пак пропущен целиком (require_all_batches = true)")
                result.ok = False
                continue
            log("   применяю файлы, которые собрались и сошлись по хэшу; остальные донеси")
        for rel, why in plan.broken:
            log(f"   [ПОВРЕЖДЕНО] {rel}: {why}")
            result.failed.append((rel, why))
            result.ok = False

        if plan.kind == "delta" and plan.base not in ("-", "", st.tree_hash):
            log(f"   [заметка] дельта считалась от состояния {plan.base}, "
                f"здесь сейчас {st.tree_hash}; применяю по факту содержимого")

        for rel in sorted(plan.files):
            target = root / rel
            new_text = plan.files[rel]

            if rel in written_by and written_by[rel] != plan.pack_id:
                log(f"   [поверх] {rel} — более свежая версия из пака {plan.pack_id}")

            if target.exists():
                old = _read_normalized(target)
                if old is not None and sha256_text(old) == plan.shas[rel]:
                    result.unchanged.append(rel)
                    written_by[rel] = plan.pack_id
                    continue
                backup(target, rel)
                if not dry_run:
                    target.write_text(new_text, encoding="utf-8", newline="\n")
                else:
                    log(f"   [обновил бы] {rel} ({human_size(len(new_text.encode()))})")
                result.updated.append(rel)
            else:
                if not dry_run:
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_text(new_text, encoding="utf-8", newline="\n")
                else:
                    log(f"   [создал бы]  {rel} ({human_size(len(new_text.encode()))})")
                result.created.append(rel)
            written_by[rel] = plan.pack_id

        if imp_cfg.get("apply_deletes", True):
            if plan.missing_batches:
                log(f"   удаления ({len(plan.deletes)}) пропущены: пак неполный")
            else:
                for rel in plan.deletes:
                    target = root / rel
                    if not target.exists():
                        continue
                    backup(target, rel)
                    if not dry_run:
                        target.unlink()
                    else:
                        log(f"   [удалил бы]  {rel}")
                    result.deleted.append(rel)

        if plan.kind == "full" and imp_cfg.get("prune_extra_on_full", False) and plan.complete:
            known = set(plan.files)
            for f in scan_project(cfg_view, Log(lambda _m: None)).files:
                if f.rel in known:
                    continue
                target = root / f.rel
                backup(target, f.rel)
                if not dry_run and target.exists():
                    target.unlink()
                result.deleted.append(f.rel)

        result.packs.append(plan.pack_id)

        if not dry_run and imp_cfg.get("move_applied", True) and plan.complete:
            dest = inbox / "_applied" / plan.pack_id
            dest.mkdir(parents=True, exist_ok=True)
            for src in plan.sources:
                if not src.exists():
                    continue
                try:
                    shutil.move(str(src), str(dest / src.name))
                except (OSError, shutil.Error) as exc:
                    log(f"   [заметка] не удалось перенести {src.name}: {exc}")

    log("")
    log(f"Создано новых файлов : {len(result.created)}")
    log(f"Обновлено файлов     : {len(result.updated)}")
    log(f"Уже совпадали        : {len(result.unchanged)}")
    log(f"Удалено файлов       : {len(result.deleted)}")
    if result.failed:
        log(f"Не применено (брак)  : {len(result.failed)}")
    if result.incomplete_packs:
        log(f"Неполные паки        : {', '.join(result.incomplete_packs)}")

    if dry_run:
        result.message = "проверка завершена, ничего не записано"
        return result

    if backup_root and backup_root.exists():
        log(f"Бэкап старых версий  : {backup_root}")
        result.backup_dir = backup_root

    # пересобираем состояние: приёмник теперь сам может слать дельты обратно
    scan = scan_project(cfg_view, Log(lambda _m: None))
    current = scan.as_sha_map()
    st.record("import", ",".join(result.packs) or "-", "import", current,
              note=f"+{len(result.created)} ~{len(result.updated)} -{len(result.deleted)}")
    st.save()

    local_hash = state_hash(current)
    log(f"Отпечаток проекта    : {local_hash}")
    for plan in plans:
        if plan.pack_id in result.packs and plan.result not in ("-", "") and plan.complete:
            if plan.result == local_hash:
                log(f"Пак {plan.pack_id}: отпечаток совпал с отправителем — деревья идентичны.")
            else:
                log(f"Пак {plan.pack_id}: отпечаток отправителя {plan.result} != локального "
                    f"{local_hash} (нормально, если здесь есть свои файлы вне выгрузки)")

    result.message = (f"создано {len(result.created)}, обновлено {len(result.updated)}, "
                      f"без изменений {len(result.unchanged)}, удалено {len(result.deleted)}")
    return result
