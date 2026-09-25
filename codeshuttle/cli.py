"""Командная строка CodeShuttle."""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from . import archiver, config as cfgmod
from . import exporter, gitsync, importer, ship
from .util import Log


def _confirm_switch(yes: bool):
    """Вопрос перед переключением веток в консоли; без терминала — только с --yes."""
    def confirm(sync: gitsync.SyncPlan) -> bool:
        if yes:
            return True
        if not (sys.stdin and sys.stdin.isatty()):
            print("Нужно переключить ветки, а спросить некого — запусти с --yes.")
            return False
        try:  # на Windows stdin из NUL тоже «терминал», но читать из него нечего
            answer = input("\n" + gitsync.switch_question(sync) + " [y/N]: ")
        except (EOFError, KeyboardInterrupt):
            print("\nОтвета нет — запусти с --yes, если переключать можно без вопроса.")
            return False
        return answer.strip().lower() in ("y", "yes", "д", "да")
    return confirm


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="codeshuttle",
        description="Перенос кода между машинами текстовыми батчами.")
    p.add_argument("-c", "--config", default=None, help="путь к config.json")
    sub = p.add_subparsers(dest="cmd")

    e = sub.add_parser("export", help="выгрузить проект в батчи")
    e.add_argument("--full", action="store_true", help="весь проект (иначе только изменения)")
    e.add_argument("--dry-run", action="store_true", help="показать план без записи")

    i = sub.add_parser("import", help="применить батчи к проекту")
    i.add_argument("--dry-run", action="store_true", help="проверить без записи")
    i.add_argument("--from", dest="from_dir", default=None,
                   help="папка с батчами (перекрывает import_dir из конфига)")
    i.add_argument("--rar", default=None,
                   help="сначала распаковать этот .rar в <inbox>/<имя архива>/, затем применить")

    a = sub.add_parser("archive", help="выгрузить изменения батчами и упаковать батчи в .rar (без git)")
    a.add_argument("--full", action="store_true", help="весь проект (иначе только изменения)")
    a.add_argument("--dry-run", action="store_true", help="показать план без записи")

    s = sub.add_parser("ship", help="экспорт одной кнопкой: git fetch + ветки из файлов веток -> батчи -> .rar")
    kind = s.add_mutually_exclusive_group()
    kind.add_argument("--full", dest="kind", action="store_const", const="full", help="весь проект")
    kind.add_argument("--delta", dest="kind", action="store_const", const="delta", help="только изменения")
    s.add_argument("--dry-run", action="store_true", help="только fetch и проверки, без переключения и выгрузки")
    s.add_argument("--yes", action="store_true", help="переключать ветки без вопроса")

    sub.add_parser("git-check", help="fetch и проверки папок с файлом ветки, ничего не меняя")

    m = sub.add_parser("git-mark", help=f"создать {gitsync.MARKER_NAME} в папке-репозитории")
    m.add_argument("folder", help="папка репозитория")
    m.add_argument("branch", help="ветка на remote")
    m.add_argument("--remote", default="origin")
    m.add_argument("--manual", action="store_true", help="не переключать самому, только проверять")
    m.add_argument("--allow-dirty", action="store_true", help="разрешить незакоммиченные правки без конфликтов")

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

    if args.cmd == "git-mark":
        try:
            path = gitsync.write_marker(Path(args.folder).resolve(), args.branch, args.remote,
                                        "manual" if args.manual else "auto", args.allow_dirty)
        except (ValueError, OSError) as exc:
            log(f"[ошибка] {exc}")
            return 2
        log(f"Создан {path}")
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
        if args.rar:
            inbox = (args.from_dir or cfg.get("import_dir") or "").strip()
            if not inbox:
                log("[ошибка] для --rar нужна папка inbox: import_dir в конфиге или --from")
                return 2
            rar = Path(args.rar)
            if not archiver.extract_rar(rar, Path(inbox) / rar.stem, log):
                return 1
            args.from_dir = inbox
        res = importer.apply_import(cfg, log, dry_run=args.dry_run, import_dir=args.from_dir)
        return 0 if res.ok else 1

    if args.cmd == "archive":
        res = archiver.archive_module(cfg, log=log, dry_run=args.dry_run,
                                      kind="full" if args.full else "delta")
        return 0 if res.ok else 1

    if args.cmd == "ship":
        res = ship.ship(cfg, log, kind=args.kind, dry_run=args.dry_run,
                        confirm=_confirm_switch(args.yes))
        return 0 if res.ok else 1

    if args.cmd == "git-check":
        errors = cfgmod.validate(cfg)
        for e in errors:
            log(f"[ошибка] {e}")
        return 0 if not errors and gitsync.preflight(cfg, log).ok else 1

    if args.cmd == "status":
        return 0 if exporter.status(cfg, log).get("ok") else 1

    if args.cmd == "baseline":
        exporter.reset_state(cfg, log)
        return 0

    return 2


if __name__ == "__main__":
    raise SystemExit(main())
