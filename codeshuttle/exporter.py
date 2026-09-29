"""Экспорт проекта в батчи по N килобайт."""
from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from pathlib import Path

from . import cipher
from . import config as cfgmod
from . import packfmt
from .scanner import ScanResult, scan_project
from .state import State, state_hash
from .util import Log, fs_path, human_size, now_iso, now_stamp


@dataclass
class ExportResult:
    ok: bool
    pack_id: str = ""
    kind: str = ""
    batch_files: list[Path] = field(default_factory=list)
    files_sent: list[str] = field(default_factory=list)
    deletes: list[str] = field(default_factory=list)
    total_bytes: int = 0
    message: str = ""
    report_path: Path | None = None


def _pack_id(kind: str) -> str:
    salt = hashlib.sha256(f"{now_iso()}{kind}".encode()).hexdigest()[:6]
    return f"{now_stamp()}-{salt}"


def _write_report(export_dir: Path, pack_id: str, kind: str, scan: ScanResult,
                  sent: list[str], deletes: list[str], batches: list[Path]) -> Path:
    lines = [
        f"# CodeShuttle — отчёт пака {pack_id}",
        "",
        f"- Тип: **{kind}**",
        f"- Сформировано: {now_iso()}",
        f"- Батчей: {len(batches)}",
        f"- Файлов в паке: {len(sent)}",
        f"- Удалений в паке: {len(deletes)}",
        f"- Всего файлов для выгрузки: {human_size(scan.total_bytes)} в {len(scan.files)} файлах",
        "",
        "## Батчи",
    ]
    lines += [f"- `{p.name}` — {human_size(p.stat().st_size)}" for p in batches]

    if deletes:
        lines += ["", "## Удаляемые файлы"] + [f"- `{p}`" for p in deletes]

    if scan.skipped:
        lines += ["", f"## Пропущенные файлы ({len(scan.skipped)})", "", "<details><summary>показать</summary>", ""]
        lines += [f"- `{rel}` — {why}" for rel, why in scan.skipped[:2000]]
        lines += ["", "</details>"]

    # отчёт кладём в подпапку: в самом outbox должны лежать ТОЛЬКО батчи,
    # чтобы «выделить всё и загрузить» не утащило лишнего
    reports = export_dir / "_reports"
    reports.mkdir(parents=True, exist_ok=True)
    path = reports / f"report_{pack_id}.md"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def export(cfg: dict, kind: str = "delta", log: Log | None = None,
           dry_run: bool = False) -> ExportResult:
    """kind: 'full' — весь проект, 'delta' — только изменения с прошлого раза."""
    log = log or Log()
    errors = cfgmod.validate(cfg, need_export=True)
    if errors:
        for e in errors:
            log(f"[ошибка] {e}")
        return ExportResult(ok=False, message="; ".join(errors))

    root = Path(cfg["project_root"]).resolve()
    export_dir = Path(cfg["export_dir"]).resolve()
    max_bytes = cfgmod.max_batch_bytes(cfg)
    st = State.load(cfgmod.state_file_for(cfg), str(root))

    log.section(f"Экспорт ({'полный' if kind == 'full' else 'изменения'})")
    log(f"Проект : {root}")
    log(f"Выгрузка: {export_dir}")
    log(f"Лимит батча: {human_size(max_bytes)}")

    scan = scan_project(cfg, log)
    current = scan.as_sha_map()
    log(f"Найдено файлов кода: {len(scan.files)} ({human_size(scan.total_bytes)})")
    log(f"Пропущено по техническим фильтрам: {len(scan.skipped)}")

    if kind == "full":
        send = list(scan.files)
        deletes: list[str] = []
        base = "-"
    else:
        added, changed, deleted = st.diff(current)
        # файл исчез из скана, но лежит на диске -> его отфильтровали (например,
        # конфиг), а не удалили. Удалением такое не шлём: приёмник потеряет копию
        filtered = [p for p in deleted if fs_path(root / p).exists()]
        deleted = [p for p in deleted if p not in set(filtered)]
        wanted = set(added) | set(changed)
        send = [f for f in scan.files if f.rel in wanted]
        deletes = deleted
        base = st.tree_hash
        log(f"Новых: {len(added)}, изменённых: {len(changed)}, удалённых: {len(deleted)}")
        if filtered:
            log(f"Выпало из синхронизации по фильтрам (на приёмнике не трогаем): {len(filtered)}")
            for p in filtered[:20]:
                log(f"  = {p}")
            if len(filtered) > 20:
                log(f"  ... ещё {len(filtered) - 20}")
        if not send and not deletes:
            log("Изменений нет — выгружать нечего.")
            return ExportResult(ok=True, kind=kind, message="Изменений нет")

    result_hash = state_hash(current)
    pack_id = _pack_id(kind)

    encode = cipher.is_enabled(cfg)
    enc_key = cipher.encode_key(cfg)
    # base64 раздувает батч на ~1/3 — пакуем plain на 0.72 лимита, чтобы
    # закодированный .txt остался в пределах max_batch_kb
    pack_bytes = int(max_bytes * 0.72) if encode else max_bytes
    if encode:
        log("Кодирование батчей: включено")

    content_budget = pack_bytes - packfmt.HEADER_RESERVE - packfmt.BLOCK_OVERHEAD
    blocks: list[packfmt.Block] = []
    for f in send:
        blocks.extend(packfmt.build_file_blocks(f.rel, f.text, f.sha, content_budget))
    blocks.extend(packfmt.build_delete_block(p) for p in deletes)

    batched = packfmt.pack_blocks(blocks, pack_bytes)
    total = len(batched)
    generated = now_iso()

    if dry_run:
        log(f"[dry-run] Будет создано батчей: {total}, файлов: {len(send)}, удалений: {len(deletes)}")
        for f in send[:50]:
            log(f"  + {f.rel} ({human_size(f.size)})")
        if len(send) > 50:
            log(f"  ... ещё {len(send) - 50}")
        for p in deletes[:50]:
            log(f"  - {p}")
        return ExportResult(ok=True, kind=kind, pack_id=pack_id,
                            files_sent=[f.rel for f in send], deletes=deletes,
                            message=f"dry-run: {total} батчей")

    export_dir.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    width = max(3, len(str(total)))
    for i, group in enumerate(batched, start=1):
        text = packfmt.render_batch(
            group, pack_id=pack_id, kind=kind, index=i, total=total,
            base=base, result=result_hash, generated=generated)
        if encode:
            text = cipher.encode(text, enc_key)
        name = f"{pack_id}_{kind}_batch_{i:0{width}d}_of_{total:0{width}d}.txt"
        path = export_dir / name
        path.write_text(text, encoding="utf-8", newline="\n")
        size = path.stat().st_size
        if size > max_bytes:
            log(f"[внимание] {name} = {human_size(size)} превысил лимит")
        written.append(path)
        log(f"  {name}  ({human_size(size)}, блоков: {len(group)})")

    report = _write_report(export_dir, pack_id, kind, scan, [f.rel for f in send], deletes, written)

    st.record("export", pack_id, kind, current,
              note=f"{len(send)} файлов, {len(deletes)} удалений, {total} батчей")
    st.save()

    total_size = sum(p.stat().st_size for p in written)
    log(f"\nГотово: {total} батч(ей), {human_size(total_size)}")
    log(f"Отпечаток состояния: {result_hash}")
    log(f"В папке {export_dir.name} лежат только батчи — можно выделять все и грузить.")
    log(f"Отчёт (не для загрузки): _reports/{report.name}")
    return ExportResult(
        ok=True, pack_id=pack_id, kind=kind, batch_files=written,
        files_sent=[f.rel for f in send], deletes=deletes,
        total_bytes=total_size, report_path=report,
        message=f"{total} батч(ей), {len(send)} файлов",
    )


def status(cfg: dict, log: Log | None = None) -> dict:
    """Что изменилось с прошлой синхронизации."""
    log = log or Log()
    errors = cfgmod.validate(cfg)
    if errors:
        for e in errors:
            log(f"[ошибка] {e}")
        return {"ok": False}

    st = State.load(cfgmod.state_file_for(cfg), cfg["project_root"])
    scan = scan_project(cfg, log)
    current = scan.as_sha_map()
    added, changed, deleted = st.diff(current)
    root = Path(cfg["project_root"]).resolve()
    filtered = [p for p in deleted if fs_path(root / p).exists()]
    deleted = [p for p in deleted if p not in set(filtered)]

    log.section("Статус")
    log(f"Файлов кода сейчас : {len(scan.files)} ({human_size(scan.total_bytes)})")
    log(f"Состояние сохранено: {st.last_action_at or 'никогда'} "
        f"({st.last_kind or '-'}, пак {st.last_pack or '-'})")
    log(f"Отпечаток: сохранён {st.tree_hash} -> сейчас {state_hash(current)}")
    log(f"Новых: {len(added)} | Изменённых: {len(changed)} | Удалённых: {len(deleted)}")
    for p in added[:40]:
        log(f"  + {p}")
    for p in changed[:40]:
        log(f"  ~ {p}")
    for p in deleted[:40]:
        log(f"  - {p}")
    if len(added) + len(changed) + len(deleted) > 120:
        log("  ...")
    if filtered:
        log(f"Выпало из синхронизации по фильтрам: {len(filtered)} (файлы на месте, просто больше не возятся)")
    return {"ok": True, "added": added, "changed": changed, "deleted": deleted,
            "filtered": filtered, "files": len(scan.files), "state": st}


def reset_state(cfg: dict, log: Log | None = None) -> bool:
    """Пометить текущее состояние проекта как «синхронизированное» без выгрузки."""
    log = log or Log()
    scan = scan_project(cfg, log)
    st = State.load(cfgmod.state_file_for(cfg), cfg["project_root"])
    st.record("baseline", f"baseline-{now_stamp()}", "baseline", scan.as_sha_map())
    st.save()
    log(f"Базовая линия записана: {len(scan.files)} файлов, отпечаток {st.tree_hash}")
    return True
