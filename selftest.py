"""Самопроверка CodeShuttle: полный цикл экспорт -> импорт -> дельта -> импорт.

Запуск:  python selftest.py
Создаёт временные папки, ничего в проекте не трогает.
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
from codeshuttle.util import Log                  # noqa: E402

QUIET = Log(lambda _m: None)
FAILURES: list[str] = []


def check(cond: bool, what: str) -> None:
    print(("  OK   " if cond else "  FAIL ") + what)
    if not cond:
        FAILURES.append(what)


def make_project(root: Path) -> None:
    files = {
        "src/main/java/app/Main.java":
            "package app;\n\npublic class Main {\n"
            "    public static void main(String[] a) {\n"
            "        System.out.println(\"привет, мир\");\n    }\n}\n",
        "src/app/util.py":
            "# -*- coding: utf-8 -*-\ndef add(a, b):\n    return a + b\n",
        "src/app/no_trailing_newline.js":
            "export const x = 1;\nexport const y = 2;",   # без \n в конце
        "src/tricky/markers.md":
            "Текст с маркером формата:\n--8<-- BEGIN нечто\n--8<-- END\n"
            "\\--8<-- END\nи ещё ### CODESHUTTLE 1\nконец\n",
        "src/empty.py": "",
        "docs/README.md": "# Проект\n\nЮникод: 中文, emoji 🚀, tabs\t\tend\n",
        "src/deep/a/b/c/d/deep.py": "VALUE = 'deep'\n",
        "build/ignored.class": "\x00\x01binary",
        "node_modules/lib/index.js": "module.exports = 1;\n",
        "config/app.yaml": "db:\n  password: secret123\n",
    }
    # большой файл (> 45 КБ) -> должен резаться на части
    files["src/big/generated.sql"] = "".join(
        f"INSERT INTO t(id, name) VALUES ({i}, 'строка номер {i}');\n" for i in range(1400))
    # одна гигантская строка без переводов -> hard-split
    files["src/big/oneline.json"] = '{"data":"' + ("x" * 120_000) + '"}\n'
    # файл с кредами -> должен быть очищен
    files["src/app/settings.py"] = (
        "DB_PASSWORD = \"SuperSecret123!\"\n"
        "API_KEY = 'AIzaSyA1B2C3D4E5F6G7H8I9J0K1L2M3N4O5P6Q'\n"
        "AWS = 'AKIAIOSFODNN7EXAMPLE'\n"
        "DB_URL = \"postgres://admin:hunter2@db.internal:5432/app\"\n"
        "password = os.environ[\"DB_PASSWORD\"]\n"
        "timeout = 30\n"
        "token = None\n"
    )
    files[".env"] = "SECRET_KEY=abcdef123456\n"
    files["private.pem"] = "-----BEGIN RSA PRIVATE KEY-----\nMIIabc\n-----END RSA PRIVATE KEY-----\n"
    files["src/app/keyholder.py"] = (
        "KEY = '''-----BEGIN RSA PRIVATE KEY-----\nMIIEowIBAAKC\n-----END RSA PRIVATE KEY-----'''\n")

    for rel, content in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(content, str) and "\x00" in content:
            p.write_bytes(content.encode("latin-1"))
        else:
            p.write_text(content, encoding="utf-8", newline="\n")


def read(root: Path, rel: str) -> str | None:
    p = root / rel
    if not p.exists():
        return None
    return p.read_bytes().decode("utf-8").replace("\r\n", "\n")


def snapshot(root: Path, cfg: dict) -> dict[str, str]:
    from codeshuttle.scanner import scan_project
    c = dict(cfg)
    c["project_root"] = str(root)
    return {f.rel: f.text for f in scan_project(c, QUIET).files}


def move_batches(outbox: Path, inbox: Path) -> int:
    n = 0
    inbox.mkdir(parents=True, exist_ok=True)
    for p in sorted(outbox.glob("*.txt")):
        shutil.move(str(p), str(inbox / p.name))
        n += 1
    return n


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="codeshuttle_test_"))
    print(f"песочница: {tmp}\n")
    src, dst = tmp / "A_source", tmp / "B_target"
    outbox, inbox = tmp / "outbox", tmp / "inbox"
    for d in (src, dst, outbox, inbox):
        d.mkdir(parents=True, exist_ok=True)
    make_project(src)

    base = cfgmod.DEFAULTS
    cfg_a = json.loads(json.dumps(base))
    cfg_a.update({"project_root": str(src), "export_dir": str(outbox),
                  "import_dir": str(inbox), "max_batch_kb": 45,
                  "state_file": str(tmp / "state_a.json")})
    cfg_b = json.loads(json.dumps(base))
    cfg_b.update({"project_root": str(dst), "export_dir": str(tmp / "outbox_b"),
                  "import_dir": str(inbox), "max_batch_kb": 45,
                  "state_file": str(tmp / "state_b.json")})

    # ---------------------------------------------------------- 1. полный экспорт
    print("[1] полный экспорт")
    res = exporter.export(cfg_a, "full", QUIET)
    check(res.ok, "экспорт завершился успешно")
    check(len(res.batch_files) > 1, f"батчей больше одного ({len(res.batch_files)})")
    limit = cfgmod.max_batch_bytes(cfg_a)
    oversize = [p.name for p in res.batch_files if p.stat().st_size > limit]
    check(not oversize, f"все батчи <= 45 КБ (нарушителей: {len(oversize)})")

    # ---------------------------------------------------------- 2. безопасность
    print("\n[2] фильтр кредов")
    body = "\n".join(p.read_text(encoding="utf-8") for p in res.batch_files)
    check("SuperSecret123!" not in body, "пароль не попал в выгрузку")
    check("AKIAIOSFODNN7EXAMPLE" not in body, "AWS-ключ не попал в выгрузку")
    check("AIzaSyA1B2C3D4E5F6G7H8I9J0K1L2M3N4O5P6Q" not in body, "google-ключ вычищен")
    check("hunter2" not in body, "пароль из URL вычищен")
    check("BEGIN RSA PRIVATE KEY" not in body, "приватный ключ не попал в выгрузку")
    check("keyholder.py" not in body, "файл с приватным ключом выкинут целиком")
    check(".env" not in [f for f in res.files_sent], ".env не выгружается")
    check("private.pem" not in res.files_sent, "*.pem не выгружается")
    check("config/app.yaml" not in res.files_sent, "yaml исключён (грузится отдельно)")
    check("node_modules/lib/index.js" not in res.files_sent, "node_modules исключён")
    check("build/ignored.class" not in res.files_sent, "бинарники исключены")
    check("timeout = 30" in body, "обычный код не пострадал")
    check('password = os.environ["DB_PASSWORD"]' in body, "ссылка на переменную не затёрта")

    # ---------------------------------------------------------- 3. импорт на B
    print("\n[3] импорт на второй машине")
    moved = move_batches(outbox, inbox)
    check(moved == len(res.batch_files), f"батчи перенесены в inbox ({moved})")
    imp = importer.apply_import(cfg_b, QUIET)
    check(imp.ok, "импорт завершился без проблем")

    a_files, b_files = snapshot(src, cfg_a), snapshot(dst, cfg_b)
    check(set(a_files) == set(b_files),
          f"набор файлов совпал ({len(a_files)} vs {len(b_files)})")
    mismatch = [k for k in a_files if a_files.get(k) != b_files.get(k)]
    check(not mismatch, f"содержимое всех файлов совпало (расхождений: {len(mismatch)})")
    if mismatch:
        for m in mismatch[:5]:
            print(f"        отличается: {m}")

    check(read(dst, "src/app/no_trailing_newline.js") == "export const x = 1;\nexport const y = 2;",
          "файл без перевода строки в конце восстановлен точно")
    check(read(dst, "src/empty.py") == "", "пустой файл восстановлен")
    check("--8<-- BEGIN нечто" in (read(dst, "src/tricky/markers.md") or ""),
          "строки-маркеры внутри кода восстановлены")
    check(read(dst, "src/big/oneline.json") == read(src, "src/big/oneline.json"),
          "гигантская однострочная строка склеена верно")
    check(read(dst, "docs/README.md") == read(src, "docs/README.md"), "юникод не пострадал")
    check((dst / "src/deep/a/b/c/d/deep.py").exists(), "глубокая вложенность папок создана")
    check((inbox / "_applied").exists(), "применённые батчи убраны в _applied")

    # ---------------------------------------------------------- 4. дельта
    print("\n[4] дельта: правим 3 файла, 1 удаляем, 1 добавляем")
    (src / "src/app/util.py").write_text("def add(a, b):\n    return a + b + 0\n",
                                         encoding="utf-8", newline="\n")
    (src / "docs/README.md").write_text("# Проект v2\n", encoding="utf-8", newline="\n")
    (src / "src/main/java/app/Main.java").write_text(
        "package app;\npublic class Main { public static void main(String[] a) {} }\n",
        encoding="utf-8", newline="\n")
    (src / "src/new_feature.py").write_text("FEATURE = True\n", encoding="utf-8", newline="\n")
    (src / "src/deep/a/b/c/d/deep.py").unlink()

    res2 = exporter.export(cfg_a, "delta", QUIET)
    check(res2.ok, "дельта-экспорт успешен")
    check(len(res2.batch_files) == 1, f"3 изменения уместились в один батч ({len(res2.batch_files)})")
    check(sorted(res2.files_sent) == sorted(
        ["src/app/util.py", "docs/README.md", "src/main/java/app/Main.java", "src/new_feature.py"]),
        f"в дельту попали только изменённые файлы: {res2.files_sent}")
    check(res2.deletes == ["src/deep/a/b/c/d/deep.py"], "удаление зафиксировано")
    delta_size = sum(p.stat().st_size for p in res2.batch_files)
    check(delta_size < 8000, f"дельта компактная ({delta_size} б, вместо полного пака)")

    move_batches(outbox, inbox)
    imp2 = importer.apply_import(cfg_b, QUIET)
    check(imp2.ok, "дельта применена")
    check(read(dst, "src/app/util.py") == "def add(a, b):\n    return a + b + 0\n",
          "изменённый файл обновлён")
    check(read(dst, "docs/README.md") == "# Проект v2\n", "второй изменённый файл обновлён")
    check((dst / "src/new_feature.py").exists(), "новый файл создан")
    check(not (dst / "src/deep/a/b/c/d/deep.py").exists(), "удалённый файл удалён")
    check((dst / ".codeshuttle_backup").exists(), "бэкап перезаписанных файлов создан")

    a_files, b_files = snapshot(src, cfg_a), snapshot(dst, cfg_b)
    check(a_files == b_files, "после дельты деревья снова идентичны")

    # ---------------------------------------------------------- 5. холостой ход
    print("\n[5] повторный экспорт без изменений")
    res3 = exporter.export(cfg_a, "delta", QUIET)
    check(res3.ok and not res3.batch_files, "нечего выгружать -> пустой результат")

    print("\n[6] обратное направление: B -> A")
    (dst / "src/app/util.py").write_text("def add(a, b):\n    return a + b + 42\n",
                                         encoding="utf-8", newline="\n")
    cfg_b_out = dict(cfg_b)
    cfg_b_out["export_dir"] = str(tmp / "outbox_b")
    res4 = exporter.export(cfg_b_out, "delta", QUIET)
    check(res4.ok and res4.files_sent == ["src/app/util.py"],
          f"приёмник сам шлёт дельту обратно: {res4.files_sent}")

    print("\n" + "=" * 60)
    if FAILURES:
        print(f"ПРОВАЛЕНО ПРОВЕРОК: {len(FAILURES)}")
        for f in FAILURES:
            print(f"  - {f}")
        print(f"\nпесочница сохранена для разбора: {tmp}")
        return 1
    print("ВСЕ ПРОВЕРКИ ПРОЙДЕНЫ")
    shutil.rmtree(tmp, ignore_errors=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
