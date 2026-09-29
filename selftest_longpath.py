"""Проверка длинных путей (Windows MAX_PATH = 260).

На Windows без включённого LongPathsEnabled файлы, у которых полный путь длиннее
259 символов, раньше молча пропускались при экспорте («нет доступа») и не могли
быть записаны при импорте. Теперь к файлам проекта обращаемся через \\\\?\\-путь.

Сценарии:
  1. преобразование путей в \\\\?\\-форму (C:\\..., UNC, уже с префиксом)
  2. экспорт видит файл с полным путём > 300 символов
  3. импорт создаёт такой файл, содержимое совпадает байт в байт
  4. дельта с изменением длинного файла применяется
  5. дельта с удалением длинного файла применяется
  6. длинные файлы не попадают в пропущенные

На Linux/macOS длина пути не ограничена, сценарии 2–6 проходят и без правки;
доказательство правки — прогон на Windows.

Запуск:  python selftest_longpath.py
"""
from __future__ import annotations

import json
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from codeshuttle import config as cfgmod          # noqa: E402
from codeshuttle import exporter, importer        # noqa: E402
from codeshuttle.scanner import scan_project      # noqa: E402
from codeshuttle.util import Log, fs_path, nt_long_path  # noqa: E402

QUIET = Log(lambda _m: None)
FAILURES: list[str] = []

DEEP_DIR = "/".join([
    "motivation-report-processor", "motivation-report-processor-app", "src", "main", "java",
    "ru", "sber", "tort", "java", "motivationreportprocessor", "motivationreportprocessorapi",
    "service", "report", "portfolio", "rest", "strategy", "impl", "dataAggregator",
    "very", "deep", "package", "for", "long", "path", "check",
])
DEEP_FILE = DEEP_DIR + "/IndicatorDetailsPortfolioDataAggregatorStrategyImplWithAVeryLongName.java"


def check(cond: bool, what: str) -> None:
    print(("  OK   " if cond else "  FAIL ") + what)
    if not cond:
        FAILURES.append(what)


def cfg_for(root: Path, out: Path, inbox: Path, state: Path) -> dict:
    c = json.loads(json.dumps(cfgmod.DEFAULTS))
    c.update({"project_root": str(root), "export_dir": str(out),
              "import_dir": str(inbox), "state_file": str(state),
              "max_batch_kb": 45})
    return c


def write(root: Path, rel: str, text: str) -> None:
    p = fs_path(root / rel)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8", newline="\n")


def read(root: Path, rel: str) -> str | None:
    p = fs_path(root / rel)
    return p.read_bytes().decode("utf-8") if p.exists() else None


def move_to_inbox(batches: list[Path], inbox: Path) -> None:
    for p in batches:
        shutil.copy2(p, inbox / p.name)


def main() -> int:
    print("[1] преобразование путей")
    check(nt_long_path("C:\\r\\a\\B.java") == "\\\\?\\C:\\r\\a\\B.java", "C:\\... -> \\\\?\\C:\\...")
    check(nt_long_path("C:\\r/a/B.java") == "\\\\?\\C:\\r\\a\\B.java", "прямые слэши нормализуются")
    check(nt_long_path("\\\\?\\C:\\r\\B.java") == "\\\\?\\C:\\r\\B.java", "путь с префиксом не меняется")
    check(nt_long_path("\\\\srv\\share\\B.java") == "\\\\?\\UNC\\srv\\share\\B.java", "UNC -> \\\\?\\UNC\\...")

    tmp = Path(tempfile.mkdtemp(prefix="codeshuttle_long_"))
    print(f"\nпесочница: {tmp}")
    src, dst = tmp / "src", tmp / "dst"
    out, inbox = tmp / "out", tmp / "in"
    for d in (src, dst, out, inbox):
        d.mkdir(parents=True, exist_ok=True)
    cfg_a = cfg_for(src, out, inbox, tmp / "sa.json")
    cfg_b = cfg_for(dst, tmp / "out_b", inbox, tmp / "sb.json")

    body_v1 = "package deep;\n\nclass Deep {\n    String s = \"Привет\";\n}\n"
    write(src, DEEP_FILE, body_v1)
    write(src, "short/Short.java", "class Short { }\n")
    full_len = len(str(src.resolve() / DEEP_FILE))
    print(f"длина полного пути длинного файла: {full_len}")
    check(full_len > 300, "полный путь длиннее 300 символов")

    print("\n[2] экспорт видит длинный файл")
    scan = scan_project(cfg_a, QUIET)
    rels = {f.rel for f in scan.files}
    check(DEEP_FILE in rels, "длинный файл в выгрузке")
    check("short/Short.java" in rels, "короткий файл в выгрузке")
    check(not any(rel == DEEP_FILE for rel, _why in scan.skipped), "длинный файл не в пропущенных")

    print("\n[3] импорт создаёт длинный файл")
    full = exporter.export(cfg_a, "full", QUIET)
    move_to_inbox(full.batch_files, inbox)
    r1 = importer.apply_import(cfg_b, QUIET)
    check(r1.ok, "импорт полного пака без ошибок")
    check(read(dst, DEEP_FILE) == body_v1, "содержимое совпадает байт в байт")

    print("\n[4] дельта с изменением")
    body_v2 = body_v1.replace("Привет", "Пока")
    write(src, DEEP_FILE, body_v2)
    delta = exporter.export(cfg_a, "delta", QUIET)
    check(DEEP_FILE in delta.files_sent, "изменённый длинный файл в дельте")
    move_to_inbox(delta.batch_files, inbox)
    r2 = importer.apply_import(cfg_b, QUIET)
    check(r2.ok and DEEP_FILE in r2.updated, "длинный файл обновлён")
    check(read(dst, DEEP_FILE) == body_v2, "новое содержимое на месте")

    print("\n[5] дельта с удалением")
    fs_path(src / DEEP_FILE).unlink()
    delta2 = exporter.export(cfg_a, "delta", QUIET)
    check(DEEP_FILE in delta2.deletes, "удаление длинного файла в дельте")
    move_to_inbox(delta2.batch_files, inbox)
    r3 = importer.apply_import(cfg_b, QUIET)
    check(r3.ok and DEEP_FILE in r3.deleted, "длинный файл удалён у приёмника")
    check(read(dst, DEEP_FILE) is None, "файла больше нет на диске")

    print("\n[6] пропущенных по доступу нет")
    scan_b = scan_project(cfg_b, QUIET)
    check(not [rel for rel, why in scan_b.skipped if why.startswith("нет доступа")], "нет «нет доступа» у приёмника")

    print("\n" + "=" * 60)
    if FAILURES:
        print(f"ПРОВАЛЕНО: {len(FAILURES)}")
        for f in FAILURES:
            print(f"  - {f}")
        print(f"песочница: {tmp}")
        return 1
    print("ВСЕ ПРОВЕРКИ ПРОЙДЕНЫ")
    shutil.rmtree(fs_path(tmp), ignore_errors=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
