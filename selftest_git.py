"""Проверка экспорта одной кнопкой: git fetch + ветка из файла ветки -> батчи -> .rar.

Строит во временной папке настоящие git-репозитории (bare-remote + клоны):

  ws/alpha           файл ветки: develop (auto), сейчас на main -> переключить
  ws/beta            файл ветки: release (manual), сейчас на release, remote ушёл вперёд -> обновить
  ws/nomarker        git-репозиторий без файла ветки -> как есть, git не трогаем
  ws/gamma           просто папка -> как есть

Сценарии: нет ветки на remote, незакоммиченные правки, конфликт при переключении,
неотслеживаемый файл под перезапись, незапушенные коммиты, разошедшиеся ветки,
manual-режим, незавершённый merge, «одна проблема блокирует всё», отмена,
изменение репозитория между проверкой и переключением, полный прогон.

Запуск:  python selftest_git.py
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from codeshuttle import archiver, gitsync, importer, ship   # noqa: E402
from codeshuttle import config as cfgmod          # noqa: E402
from codeshuttle.util import Log                  # noqa: E402

QUIET = Log(lambda _m: None)
FAILURES: list[str] = []

os.environ.update({
    "GIT_AUTHOR_NAME": "selftest", "GIT_AUTHOR_EMAIL": "selftest@example.com",
    "GIT_COMMITTER_NAME": "selftest", "GIT_COMMITTER_EMAIL": "selftest@example.com",
})


def check(cond: bool, what: str) -> None:
    print(("  OK   " if cond else "  FAIL ") + what)
    if not cond:
        FAILURES.append(what)


def g(cwd: Path, *args: str) -> str:
    r = subprocess.run(["git", "-c", "core.autocrlf=false", "-c", "init.defaultBranch=main", *args],
                       cwd=str(cwd), capture_output=True, text=True, encoding="utf-8")
    if r.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} в {cwd}: {r.stderr.strip()}")
    return r.stdout.strip()


def write(root: Path, rel: str, text: str) -> None:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8", newline="\n")


def commit(repo: Path, files: dict[str, str], msg: str) -> None:
    for rel, text in files.items():
        write(repo, rel, text)
    g(repo, "add", "-A")
    g(repo, "commit", "-q", "-m", msg)


def clone(remote: Path, dest: Path, branch: str = "main") -> Path:
    g(dest.parent, "clone", "-q", "-b", branch, str(remote), dest.name)
    g(dest, "config", "core.autocrlf", "false")
    return dest


def has_errors(plan, fragment: str) -> bool:
    return any(fragment in e for e in plan.errors)


def repo_of(sync, folder_name: str):
    return next(r for r in sync.repos if r.spec.folder.name == folder_name)


def main() -> int:
    if not gitsync.git_available():
        print("git не найден — тест пропущен")
        return 0
    tmp = Path(tempfile.mkdtemp(prefix="codeshuttle_git_"))
    print(f"песочница: {tmp}\n")
    remotes, seeds, ws = tmp / "remotes", tmp / "seeds", tmp / "ws"
    for d in (remotes, seeds, ws):
        d.mkdir(parents=True)

    # --- remote alpha: main + develop ---------------------------------------
    alpha_remote = remotes / "alpha.git"
    g(remotes, "init", "-q", "--bare", "alpha.git")
    alpha_seed = seeds / "alpha"
    alpha_seed.mkdir()
    g(alpha_seed, "init", "-q")
    commit(alpha_seed, {"src/App.java": "class App { String v = \"main\"; }\n",
                        "README.md": "# alpha\n"}, "main")
    g(alpha_seed, "remote", "add", "origin", str(alpha_remote))
    g(alpha_seed, "push", "-q", "origin", "main")
    g(alpha_seed, "checkout", "-q", "-b", "develop")
    commit(alpha_seed, {"src/App.java": "class App { String v = \"develop\"; }\n",
                        "src/Feature.java": "class Feature { }\n"}, "develop")
    g(alpha_seed, "push", "-q", "origin", "develop")

    # --- remote beta: main + release, release потом уйдёт вперёд ---------------
    beta_remote = remotes / "beta.git"
    g(remotes, "init", "-q", "--bare", "beta.git")
    beta_seed = seeds / "beta"
    beta_seed.mkdir()
    g(beta_seed, "init", "-q")
    commit(beta_seed, {"lib/core.py": "X = 1\n"}, "main")
    g(beta_seed, "remote", "add", "origin", str(beta_remote))
    g(beta_seed, "push", "-q", "origin", "main")
    g(beta_seed, "checkout", "-q", "-b", "release")
    commit(beta_seed, {"lib/core.py": "X = 2\n"}, "release 2")
    g(beta_seed, "push", "-q", "origin", "release")

    alpha = clone(alpha_remote, ws / "alpha")
    beta = clone(beta_remote, ws / "beta", "release")
    nomarker = clone(alpha_remote, ws / "nomarker")
    write(ws, "gamma/tool.py", "print('gamma')\n")

    commit(beta_seed, {"lib/core.py": "X = 3\n"}, "release 3")
    g(beta_seed, "push", "-q", "origin", "release")

    gitsync.write_marker(alpha, "develop")
    gitsync.write_marker(beta, "release", checkout="manual")

    cfg = json.loads(json.dumps(cfgmod.DEFAULTS))
    cfg.update({"project_root": str(ws), "export_dir": str(tmp / "out"),
                "archive_dir": str(tmp / "arch"), "state_file": str(tmp / "state.json")})

    # ------------------------------------------------------------------ 1
    print("[1] поиск папок с файлом ветки")
    found = gitsync.discover(cfg)
    check(sorted(s.folder.name for s in found.specs) == ["alpha", "beta"],
          f"найдены alpha и beta: {[s.folder.name for s in found.specs]}")
    check(sorted(p.name for p in found.plain) == ["gamma", "nomarker"],
          f"без файла ветки — gamma и nomarker: {[p.name for p in found.plain]}")
    check(g(alpha, "status", "--porcelain") == "", "файл ветки не пачкает git status (info/exclude)")

    # ------------------------------------------------------------------ 2
    print("\n[2] чистая проверка: план без изменений в рабочих копиях")
    sync = gitsync.preflight(cfg, QUIET)
    a, b = repo_of(sync, "alpha"), repo_of(sync, "beta")
    check(sync.ok, f"проверка прошла: {sync.problems()}")
    check(a.action == "switch" and a.current == "main", "alpha: переключить main -> develop")
    check(b.action == "ff" and b.behind == 1, f"beta: обновить на 1 коммит (behind={b.behind})")
    check(g(alpha, "branch", "--show-current") == "main", "проверка сама ничего не переключила")
    check((beta / "lib/core.py").read_text() == "X = 2\n", "проверка не обновила рабочую копию")

    # ------------------------------------------------------------------ 3
    print("\n[3] ветки нет на remote")
    gitsync.write_marker(alpha, "no-such-branch")
    sync = gitsync.preflight(cfg, QUIET)
    check(has_errors(repo_of(sync, "alpha"), "нет на origin"), "ошибка «ветки нет на origin»")
    check(not sync.ok, "план не готов")
    gitsync.write_marker(alpha, "develop")

    # ------------------------------------------------------------------ 4
    print("\n[4] незакоммиченная правка в файле, который отличается в ветке")
    write(alpha, "src/App.java", "class App { String v = \"my local edit\"; }\n")
    sync = gitsync.preflight(cfg, QUIET)
    a = repo_of(sync, "alpha")
    check(has_errors(a, "незакоммиченные изменения"), "ошибка про незакоммиченные изменения")
    check(has_errors(a, "конфликт при переключении") and has_errors(a, "src/App.java"),
          "конфликт при переключении назван поимённо")

    print("\n[5] одна проблемная папка блокирует всё: ship ничего не трогает")
    res = ship.ship(cfg, QUIET, kind="full", confirm=lambda _s: True)
    check(not res.ok and res.stage == "git", f"ship остановился на git ({res.stage})")
    check(g(beta, "rev-parse", "HEAD") != g(beta, "rev-parse", "origin/release"),
          "beta не обновлена, хотя у неё всё было в порядке")
    check(not (tmp / "out").exists() or not list((tmp / "out").glob("*.txt")), "батчи не создавались")
    g(alpha, "checkout", "-q", "--", "src/App.java")

    # ------------------------------------------------------------------ 6
    print("\n[6] allow_dirty: правка без конфликта едет вместе с переключением")
    gitsync.write_marker(alpha, "develop", allow_dirty=True)
    write(alpha, "README.md", "# alpha (локальная правка)\n")
    sync = gitsync.preflight(cfg, QUIET)
    a = repo_of(sync, "alpha")
    check(a.ok and a.action == "switch", f"переключение разрешено: {a.errors}")
    check(any("незакоммиченные" in w for w in a.warnings), "предупреждение, что правки уедут в выгрузку")
    write(alpha, "src/App.java", "class App { String v = \"conflict\"; }\n")
    a = repo_of(gitsync.preflight(cfg, QUIET), "alpha")
    check(has_errors(a, "конфликт при переключении"), "конфликт блокирует даже при allow_dirty")
    g(alpha, "checkout", "-q", "--", "src/App.java", "README.md")
    gitsync.write_marker(alpha, "develop")

    # ------------------------------------------------------------------ 7
    print("\n[7] неотслеживаемый файл, который ветка перезаписала бы")
    write(alpha, "src/Feature.java", "class Feature { int mine; }\n")
    a = repo_of(gitsync.preflight(cfg, QUIET), "alpha")
    check(has_errors(a, "неотслеживаемые файлы затёрлись бы") and has_errors(a, "src/Feature.java"),
          "перезапись неотслеживаемого файла поймана")
    (alpha / "src/Feature.java").unlink()
    write(alpha, "notes.md", "мои заметки\n")
    a = repo_of(gitsync.preflight(cfg, QUIET), "alpha")
    check(a.ok and any("неотслеживаемые" in w for w in a.warnings),
          "безобидный неотслеживаемый файл — только предупреждение")
    (alpha / "notes.md").unlink()

    # ------------------------------------------------------------------ 8
    print("\n[8] manual: не на той ветке -> просим переключить самому")
    g(beta, "checkout", "-q", "main")
    b = repo_of(gitsync.preflight(cfg, QUIET), "beta")
    check(has_errors(b, "переключи сам"), f"manual-режим просит переключить вручную: {b.errors}")
    g(beta, "checkout", "-q", "release")

    # ------------------------------------------------------------------ 9
    print("\n[9] незапушенные и разошедшиеся коммиты")
    g(alpha, "branch", "-q", "develop", "origin/develop")
    g(alpha, "checkout", "-q", "develop")
    commit(alpha, {"local.txt": "local\n"}, "local only")
    a = repo_of(gitsync.preflight(cfg, QUIET), "alpha")
    check(has_errors(a, "незапушенных"), "локальный коммит не на remote -> ошибка")
    commit(alpha_seed, {"src/Other.java": "class Other { }\n"}, "remote moved")
    g(alpha_seed, "push", "-q", "origin", "develop")
    a = repo_of(gitsync.preflight(cfg, QUIET), "alpha")
    check(has_errors(a, "разошлась"), "разошедшиеся ветки -> ошибка")
    g(alpha, "checkout", "-q", "main")
    g(alpha, "branch", "-q", "-D", "develop")

    # ------------------------------------------------------------------ 10
    print("\n[10] незавершённый merge")
    git_dir = Path(g(alpha, "rev-parse", "--absolute-git-dir"))
    (git_dir / "MERGE_HEAD").write_text(g(alpha, "rev-parse", "HEAD") + "\n")
    a = repo_of(gitsync.preflight(cfg, QUIET), "alpha")
    check(has_errors(a, "незавершённый merge"), "незавершённый merge пойман")
    (git_dir / "MERGE_HEAD").unlink()

    # ------------------------------------------------------------------ 11
    print("\n[11] файл ветки: одна строка вместо JSON, битый JSON")
    (alpha / gitsync.MARKER_NAME).write_text("develop\n", encoding="utf-8")
    spec = gitsync.read_spec(alpha / gitsync.MARKER_NAME)
    check(spec.branch == "develop" and not spec.errors, "одна строка = имя ветки")
    (alpha / gitsync.MARKER_NAME).write_text("{ branch: develop", encoding="utf-8")
    check(bool(gitsync.read_spec(alpha / gitsync.MARKER_NAME).errors), "битый JSON -> ошибка")
    (alpha / gitsync.MARKER_NAME).write_text('{"branch": "--upload-pack=evil"}', encoding="utf-8")
    check(bool(gitsync.read_spec(alpha / gitsync.MARKER_NAME).errors), "ветка вида «--опция» отклонена")
    gitsync.write_marker(alpha, "develop")

    # ------------------------------------------------------------------ 12
    print("\n[12] отказ в подтверждении -> ничего не переключено")
    res = ship.ship(cfg, QUIET, kind="full", confirm=lambda _s: False)
    check(res.stage == "cancel" and g(alpha, "branch", "--show-current") == "main",
          f"отмена: alpha осталась на main ({res.stage})")

    # ------------------------------------------------------------------ 13
    print("\n[13] репозиторий изменился между проверкой и переключением")
    sync = gitsync.preflight(cfg, QUIET)
    commit(alpha, {"src/Late.java": "class Late { }\n"}, "late commit")
    check(not gitsync.apply(sync, QUIET), "apply отказался работать по устаревшему плану")
    check(g(alpha, "branch", "--show-current") == "main", "alpha не переключена")
    g(alpha, "reset", "-q", "--hard", "origin/main")

    # ------------------------------------------------------------------ 14
    print("\n[14] полный прогон: git -> батчи -> .rar")
    asked: list[int] = []
    res = ship.ship(cfg, QUIET, kind="full", confirm=lambda s: asked.append(len(s.switches)) or True)
    rar_ok = archiver.find_rar_exe() is not None
    check(asked == [1], f"подтверждение спрошено один раз, про одно переключение: {asked}")
    check(g(alpha, "branch", "--show-current") == "develop", "alpha переключена на develop")
    check(g(alpha, "rev-parse", "HEAD") == g(alpha, "rev-parse", "origin/develop"), "alpha = origin/develop")
    check((beta / "lib/core.py").read_text() == "X = 3\n", "beta подтянута до origin/release")
    check(g(nomarker, "branch", "--show-current") == "main", "репозиторий без файла ветки не тронут")
    batches = sorted((tmp / "out").glob("*.txt"))
    body = "\n".join(p.read_text(encoding="utf-8") for p in batches)
    check('"path":"alpha/src/Feature.java"' in body and 'v = \\"develop\\"' not in body
          and 'String v = "develop"' in body, "в батчах код alpha из develop")
    check('"path":"beta/lib/core.py"' in body and "X = 3" in body, "в батчах свежая beta")
    check('"path":"nomarker/src/App.java"' in body and 'String v = "main"' in body,
          "папка без файла ветки выгружена как есть")
    check('"path":"gamma/tool.py"' in body, "обычная папка выгружена")
    check(gitsync.MARKER_NAME not in body, "файлы веток в выгрузку не попали")
    if rar_ok:
        check(res.ok and res.archive and res.archive.rar_path and res.archive.rar_path.exists(),
              f"архив создан: {res.archive.rar_path if res.archive else None}")
        check(res.archive.rar_path.name.endswith("_ws_full.rar"), f"в имени архива тип выгрузки: {res.archive.rar_path.name}")
    else:
        print("  --   Rar.exe не найден — проверка архива пропущена")

    if rar_ok and res.archive and res.archive.rar_path:
        print("\n[14б] приёмник: распаковать .rar в inbox и применить")
        target, inbox = tmp / "target", tmp / "inbox"
        cfg_b = json.loads(json.dumps(cfgmod.DEFAULTS))
        cfg_b.update({"project_root": str(target), "import_dir": str(inbox),
                      "state_file": str(tmp / "state_b.json")})
        target.mkdir()
        rar = res.archive.rar_path
        check(archiver.extract_rar(rar, inbox / rar.stem, QUIET), "архив распакован в inbox/<имя архива>")
        imp = importer.apply_import(cfg_b, QUIET)
        check(imp.ok and (target / "alpha/src/Feature.java").exists()
              and (target / "beta/lib/core.py").read_text() == "X = 3\n",
              "батчи из архива применены на приёмнике")

    print("\n[15] повторный прогон: ветки актуальны, вопроса нет")
    asked.clear()
    res = ship.ship(cfg, QUIET, kind="delta", confirm=lambda s: asked.append(1) or True)
    check(res.stage in ("done", "export") and not asked, f"без вопросов, stage={res.stage}")
    check(all(r.action == "none" for r in res.sync.repos), "обе ветки уже актуальны")

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
