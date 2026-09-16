"""Командная строка CodeShuttle."""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from . import archiver, config as cfgmod
from . import exporter, importer
from .util import Log


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="codeshuttle",
        description="Перенос кода между машинами батчами по 45 КБ (без кредов).")
    p.add_argument("-c", "--config", default=None, help="путь к config.json")
    sub = p.add_subparsers(dest="cmd")

    e = sub.add_parser("export", help="выгрузить проект в батчи")
    e.add_argument("--full", action="store_true", help="весь проект (иначе только изменения)")
    e.add_argument("--dry-run", action="store_true", help="показать план без записи")

    i = sub.add_parser("import", help="применить батчи к проекту")
    i.add_argument("--dry-run", action="store_true", help="проверить без записи")
    i.add_argument("--from", dest="from_dir", default=None,
                   help="папка с батчами (перекрывает import_dir из конфига)")

    a = sub.add_parser("archive", help="выгрузить изменения батчами и упаковать батчи в .rar")
    a.add_argument("--dry-run", action="store_true", help="показать план без записи")

    sub.add_parser("status", help="что изменилось с прошлой синхронизации")
    sub.add_parser("baseline", help="считать текущее состояние синхронизированным")
    sub.add_parser("gui", help="запустить окно с кнопками")
    sub.add_parser("init", help="создать config.json со значениями по умолчанию")
    return p


def main(argv: list[str] | None = None) -> int:
    try:  # не падать на кириллице при перенаправлении вывода в файл
        sys.stdout.reconfigure(errors="replace")
    except (AttributeError, ValueError):
        pass
    args = build_parser().parse_args(argv if argv is not None else sys.argv[1:])
    log = Log()

    if args.cmd == "init":
        path = cfgmod.write_default_config(args.config)
        log(f"Создан конфиг: {path}\nЗаполни project_root / export_dir / import_dir.")
        return 0

    if args.cmd in (None, "gui"):
        from .gui import run_gui
        run_gui(args.config)
        return 0

    path, created = cfgmod.ensure_config(args.config)
    if created:
        log(f"Создан конфиг: {path}\nЗаполни project_root / export_dir / import_dir и запусти снова.")
        return 2
    try:
        cfg = cfgmod.load_config(path)
    except (FileNotFoundError, ValueError) as exc:
        log(f"[ошибка] конфиг не читается: {exc}")
        return 2

    if args.cmd == "export":
        res = exporter.export(cfg, "full" if args.full else "delta", log, dry_run=args.dry_run)
        return 0 if res.ok else 1

    if args.cmd == "import":
        res = importer.apply_import(cfg, log, dry_run=args.dry_run, import_dir=args.from_dir)
        return 0 if res.ok else 1

    if args.cmd == "archive":
        res = archiver.archive_module(cfg, log=log, dry_run=args.dry_run)
        return 0 if res.ok else 1

    if args.cmd == "status":
        return 0 if exporter.status(cfg, log).get("ok") else 1

    if args.cmd == "baseline":
        exporter.reset_state(cfg, log)
        return 0

    return 2


if __name__ == "__main__":
    raise SystemExit(main())
