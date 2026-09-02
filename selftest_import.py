"""Проверка импорта на коллизии: «свалил все батчи в папку и применил».

Сценарии:
  1. те же батчи применены дважды подряд      -> ничего не меняется
  2. дубликаты файлов батчей в одной папке    -> применяется один раз
  3. full и delta лежат вперемешку            -> итог = состояние после delta
  4. батчи в произвольном порядке/переименованы -> порядок не важен
  5. одного батча не хватает                  -> остальные файлы применяются,
                                                 удаления откладываются
  6. файл на диске уже совпадает              -> не трогается (mtime не меняется)

Запуск:  python selftest_import.py
"""
from __future__ import annotations

import json
import shutil
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from codeshuttle import config as cfgmod          # noqa: E402
from codeshuttle import exporter, importer        # noqa: E402
from codeshuttle.util import Log                  # noqa: E402

QUIET = Log(lambda _m: None)
FAILURES: list[str] = []


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
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8", newline="\n")


def read(root: Path, rel: str) -> str | None:
    p = root / rel
    return p.read_bytes().decode("utf-8").replace("\r\n", "\n") if p.exists() else None


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="codeshuttle_imp_"))
    print(f"песочница: {tmp}\n")
    src, dst = tmp / "src", tmp / "dst"
    out, inbox = tmp / "out", tmp / "in"
    for d in (src, dst, out, inbox):
        d.mkdir(parents=True, exist_ok=True)

    cfg_a = cfg_for(src, out, inbox, tmp / "sa.json")
    cfg_b = cfg_for(dst, tmp / "out_b", inbox, tmp / "sb.json")

    write(src, "app/Main.java", "class Main { int v = 1; }\n")
    write(src, "app/Util.java", "class Util { }\n")
    write(src, "docs/readme.md", "# v1\n")

    # --- полный пак, кладём его в inbox дважды под разными именами -------
    print("[1] дубликаты батчей в папке")
    full = exporter.export(cfg_a, "full", QUIET)
    for p in full.batch_files:
        shutil.copy2(p, inbox / p.name)
        shutil.copy2(p, inbox / ("copy_of_" + p.name))          # дубликат
        shutil.copy2(p, inbox / (p.stem + "_renamed.txt"))      # ещё и переименованный
    r1 = importer.apply_import(cfg_b, QUIET)
    check(r1.ok, "импорт с дубликатами прошёл без ошибок")
    check(len(r1.created) == 3, f"создано ровно 3 файла, без задвоений ({len(r1.created)})")
    check(not r1.updated, "ничего не перезаписывалось повторно")
    check(read(dst, "app/Main.java") == "class Main { int v = 1; }\n", "содержимое верное")

    # --- повторное применение тех же батчей ------------------------------
    print("\n[2] тот же пак применён второй раз")
    for p in full.batch_files:
        shutil.copy2(p, inbox / p.name)
    mtime_before = (dst / "app/Main.java").stat().st_mtime_ns
    time.sleep(0.01)
    r2 = importer.apply_import(cfg_b, QUIET)
    check(r2.ok, "повторный импорт прошёл без ошибок")
    check(not r2.created and not r2.updated, "ни одного изменения (идемпотентно)")
    check(len(r2.unchanged) == 3, f"все 3 файла помечены «уже совпадали» ({len(r2.unchanged)})")
    check((dst / "app/Main.java").stat().st_mtime_ns == mtime_before,
          "совпадающий файл physически не переписан (mtime тот же)")

    # --- full и delta вперемешку -----------------------------------------
    print("\n[3] full и delta свалены в одну папку")
    write(src, "app/Main.java", "class Main { int v = 2; }\n")   # изменили
    write(src, "app/New.java", "class New { }\n")                # добавили
    (src / "app/Util.java").unlink()                             # удалили
    delta = exporter.export(cfg_a, "delta", QUIET)
    for p in full.batch_files + delta.batch_files:               # оба пака сразу
        shutil.copy2(p, inbox / p.name)
    r3 = importer.apply_import(cfg_b, QUIET)
    check(r3.ok, "смешанный импорт прошёл без ошибок")
    check(read(dst, "app/Main.java") == "class Main { int v = 2; }\n",
          "победила более свежая версия из дельты")
    check((dst / "app/New.java").exists(), "новый файл добавлен")
    check(not (dst / "app/Util.java").exists(), "удаление применено")
    check("app/Main.java" in r3.updated, "изменённый файл в списке обновлённых")

    # --- неполный пак ------------------------------------------------------
    print("\n[4] один батч не донесли")
    shutil.rmtree(inbox, ignore_errors=True)
    inbox.mkdir(parents=True, exist_ok=True)
    big = "\n".join(f"// строка {i} автогенерации" for i in range(3000)) + "\n"
    write(src, "app/Big.java", big)
    write(src, "app/Small.java", "class Small { }\n")
    (src / "docs/readme.md").unlink()
    d2 = exporter.export(cfg_a, "delta", QUIET)
    check(len(d2.batch_files) >= 2, f"пак из нескольких батчей ({len(d2.batch_files)})")
    for p in d2.batch_files[:-1]:              # последний батч «потерян»
        shutil.copy2(p, inbox / p.name)
    r4 = importer.apply_import(cfg_b, QUIET)
    check(not r4.ok, "неполный пак помечен как проблемный")
    check(d2.pack_id in r4.incomplete_packs, "пак назван неполным в отчёте")
    check((dst / "docs/readme.md").exists(), "удаления отложены до полного пака")
    check(all("нет частей" in why for _, why in r4.failed),
          f"недобравшийся файл назван «нет частей», а не порчей: {r4.failed}")
    check(not (dst / "app/Big.java").exists(),
          "файл с недостающей частью не записан наполовину")

    print("\n[5] донесли недостающий батч")
    shutil.copy2(d2.batch_files[-1], inbox / d2.batch_files[-1].name)
    r5 = importer.apply_import(cfg_b, QUIET)
    check(r5.ok, "теперь пак полный и применился")
    check(read(dst, "app/Big.java") == big, "большой файл собран из частей верно")
    check(not (dst / "docs/readme.md").exists(), "отложенное удаление применилось")

    # --- порченый батч ------------------------------------------------------
    print("\n[6] повреждённый батч")
    shutil.rmtree(inbox, ignore_errors=True)
    inbox.mkdir(parents=True, exist_ok=True)
    write(src, "app/Small.java", "class Small { int x = 9; }\n")
    d3 = exporter.export(cfg_a, "delta", QUIET)
    broken = d3.batch_files[0].read_text(encoding="utf-8").replace("int x = 9", "int x = 8")
    (inbox / "broken.txt").write_text(broken, encoding="utf-8", newline="\n")
    before = read(dst, "app/Small.java")
    r6 = importer.apply_import(cfg_b, QUIET)
    check(not r6.ok, "испорченный батч отбракован")
    check(any("Small" in f[0] for f in r6.failed), "назван конкретный битый файл")
    check(read(dst, "app/Small.java") == before, "битые данные НЕ записаны в проект")

    print("\n" + "=" * 60)
    if FAILURES:
        print(f"ПРОВАЛЕНО: {len(FAILURES)}")
        for f in FAILURES:
            print(f"  - {f}")
        print(f"песочница: {tmp}")
        return 1
    print("ВСЕ ПРОВЕРКИ ПРОЙДЕНЫ")
    shutil.rmtree(tmp, ignore_errors=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
