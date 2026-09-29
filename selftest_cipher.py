"""Проверка кодирования батчей.

Батч при экспорте не должен лежать в исходном виде: заголовки `### CODESHUTTLE`
и содержимое файлов не читаются глазами. Импорт раскладывает такой батч обратно
байт в байт и при этом по-прежнему принимает старые некодированные батчи.

Сценарии:
  1. кодирование обратимо и меняет все символы (нет исходных маркеров)
  2. экспорт пишет кодированные .txt (в файле нет ### CODESHUTTLE и текста кода)
  3. импорт кодированного пака -> файлы совпадают байт в байт
  4. дельта поверх: изменение и удаление доходят
  5. обратная совместимость: старый plain-батч в той же папке применяется
  6. чужой ключ: без правильного ключа пак не читается
  7. размер кодированного .txt не превышает max_batch_kb

Запуск:  python selftest_cipher.py
"""
from __future__ import annotations

import json
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from codeshuttle import cipher, config as cfgmod  # noqa: E402
from codeshuttle import exporter, importer         # noqa: E402
from codeshuttle.util import Log                    # noqa: E402

QUIET = Log(lambda _m: None)
FAILURES: list[str] = []


def check(cond: bool, what: str) -> None:
    print(("  OK   " if cond else "  FAIL ") + what)
    if not cond:
        FAILURES.append(what)


def cfg_for(root, out, inbox, state, key="", enabled=True):
    c = json.loads(json.dumps(cfgmod.DEFAULTS))
    c.update({"project_root": str(root), "export_dir": str(out),
              "import_dir": str(inbox), "state_file": str(state), "max_batch_kb": 25})
    c["cipher"] = {"enabled": enabled, "key": key}
    return c


def write(root: Path, rel: str, text: str) -> None:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8", newline="\n")


def read(root: Path, rel: str):
    p = root / rel
    return p.read_bytes().decode("utf-8") if p.exists() else None


def move(batches, inbox: Path) -> None:
    for p in batches:
        (inbox / p.name).write_bytes(p.read_bytes())


def main() -> int:
    print("[1] обратимость кодирования")
    sample = "### CODESHUTTLE 1\n--8<-- BEGIN {\"path\":\"A.java\"}\nclass A { String s = \"Привет\"; }\n--8<-- END\n"
    enc = cipher.encode(sample, cipher.DEFAULT_KEY)
    check(cipher.decode(enc, [cipher.DEFAULT_KEY]) == sample, "decode(encode(x)) == x")
    check("### CODESHUTTLE" not in enc and "class A" not in enc and "Привет" not in enc,
          "в кодированном тексте нет исходных маркеров и содержимого")
    check(cipher.looks_encoded(enc) and not cipher.looks_encoded(sample),
          "looks_encoded различает кодированный и plain")

    tmp = Path(tempfile.mkdtemp(prefix="codeshuttle_cip_"))
    src, dst = tmp / "src", tmp / "dst"
    out, inbox = tmp / "out", tmp / "in"
    for d in (src, dst, out, inbox):
        d.mkdir(parents=True, exist_ok=True)
    a = cfg_for(src, out, inbox, tmp / "sa.json")
    b = cfg_for(dst, tmp / "outb", inbox, tmp / "sb.json")

    body = "package app;\n\nclass Secret {\n    String token = \"пароль-42\";\n}\n"
    write(src, "app/Secret.java", body)
    write(src, "docs/readme.md", "# Заголовок\n")

    print("\n[2] экспорт пишет кодированные .txt")
    full = exporter.export(a, "full", QUIET)
    raw = full.batch_files[0].read_text(encoding="utf-8")
    check("### CODESHUTTLE" not in raw, "в .txt нет заголовка ### CODESHUTTLE")
    check("Secret" not in raw and "пароль" not in raw, "в .txt нет исходного кода")
    check(cipher.looks_encoded(raw), ".txt распознаётся как кодированный")

    print("\n[3] импорт кодированного пака")
    move(full.batch_files, inbox)
    r1 = importer.apply_import(b, QUIET)
    check(r1.ok, "импорт без ошибок")
    check(read(dst, "app/Secret.java") == body, "содержимое байт в байт")
    check(read(dst, "docs/readme.md") == "# Заголовок\n", "второй файл совпал")

    print("\n[4] дельта: изменение и удаление")
    body2 = body.replace("пароль-42", "пароль-99")
    write(src, "app/Secret.java", body2)
    (src / "docs/readme.md").unlink()
    d1 = exporter.export(a, "delta", QUIET)
    move(d1.batch_files, inbox)
    r2 = importer.apply_import(b, QUIET)
    check(read(dst, "app/Secret.java") == body2, "изменение доехало")
    check(read(dst, "docs/readme.md") is None, "удаление доехало")

    print("\n[5] обратная совместимость: plain-батч читается")
    src2, dst2 = tmp / "src2", tmp / "dst2"
    out2, inbox2 = tmp / "out2", tmp / "in2"
    for d in (src2, dst2, out2, inbox2):
        d.mkdir(parents=True, exist_ok=True)
    a_plain = cfg_for(src2, out2, inbox2, tmp / "sa2.json", enabled=False)
    b_any = cfg_for(dst2, tmp / "outb2", inbox2, tmp / "sb2.json")
    write(src2, "Plain.java", "class Plain { }\n")
    fp = exporter.export(a_plain, "full", QUIET)
    plain_raw = fp.batch_files[0].read_text(encoding="utf-8")
    check("### CODESHUTTLE" in plain_raw, "при enabled=false батч остаётся plain")
    move(fp.batch_files, inbox2)
    r3 = importer.apply_import(b_any, QUIET)
    check(r3.ok and read(dst2, "Plain.java") == "class Plain { }\n", "plain-батч применён приёмником с ключом")

    print("\n[6] чужой ключ пак не читает")
    enc_key = cipher.encode(sample, "alpha")
    check(cipher.decode(enc_key, ["beta"]) is None, "неверный ключ -> None")
    check(cipher.decode(enc_key, ["alpha"]) == sample, "верный ключ -> исходник")

    print("\n[7] размер кодированного .txt в пределах лимита")
    big = "".join(f"line {i} значение={i * 7}\n" for i in range(4000))
    write(src, "app/Big.java", big)
    fb = exporter.export(a, "delta", QUIET)
    over = [p.name for p in fb.batch_files if p.stat().st_size > 25 * 1024]
    check(not over, f"нет батчей больше 25 КБ ({over})")

    shutil.rmtree(tmp, ignore_errors=True)

    print("\n" + "=" * 60)
    if FAILURES:
        print(f"ПРОВАЛЕНО: {len(FAILURES)}")
        for f in FAILURES:
            print(f"  - {f}")
        return 1
    print("ВСЕ ПРОВЕРКИ ПРОЙДЕНЫ")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
