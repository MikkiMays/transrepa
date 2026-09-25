"""Git-подготовка перед выгрузкой: fetch и переключение на ветку из файла ветки.

В папке-репозитории лежит `.codeshuttle-branch.json` с именем ветки на remote.
Перед выгрузкой для каждой такой папки:

  1. git fetch <remote> <ветка>; ветки на remote нет -> ошибка;
  2. проверки без каких-либо изменений: незавершённый merge/rebase, неразрешённые
     конфликты, незакоммиченные правки, незапушенные коммиты, файлы, которые
     переключение или обновление перезаписало бы;
  3. только если чисто ВО ВСЕХ папках — переключение на ветку и fast-forward
     до состояния remote.

Папки без файла ветки git не трогает: они выгружаются как есть.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from fnmatch import fnmatch
from pathlib import Path
from typing import Callable

from .scanner import BRANCH_MARKER as MARKER_NAME
from .util import Log

EMPTY_TREE = "4b825dc642cb6eb9a060e54bf8d69288fbee4904"
CHECKOUT_MODES = ("auto", "manual")
KNOWN_KEYS = {"branch", "remote", "checkout", "allow_dirty", "enabled"}
UNMERGED = {"DD", "AU", "UD", "UA", "DU", "AA", "UU"}
BAD_NAME_CHARS = set(" ~^:?*[\\\t\n\r")
LIST_LIMIT = 8

# признаки операции, которую начали и не закончили (файлы внутри .git)
IN_PROGRESS = {
    "MERGE_HEAD": "незавершённый merge — заверши или отмени (git merge --abort)",
    "rebase-merge": "незавершённый rebase — заверши или отмени (git rebase --abort)",
    "rebase-apply": "незавершённый rebase/am — заверши или отмени (git rebase --abort)",
    "CHERRY_PICK_HEAD": "незавершённый cherry-pick — заверши или отмени (git cherry-pick --abort)",
    "REVERT_HEAD": "незавершённый revert — заверши или отмени (git revert --abort)",
    "BISECT_LOG": "идёт bisect — заверши его (git bisect reset)",
    "index.lock": "есть .git/index.lock: другой git-процесс работает с репозиторием "
                  "(или упал и оставил замок — тогда удали файл)",
}

MARKER_HELP = ("branch — ветка на remote; checkout: auto — CodeShuttle переключит сам, "
               "manual — только проверит, что ты уже на ней; allow_dirty: true — разрешить "
               "незакоммиченные правки, если они не конфликтуют; enabled: false — выгружать как есть")


# ------------------------------------------------------------------ запуск git
def git_available() -> bool:
    return shutil.which("git") is not None


def run_git(cwd: Path, *args: str, timeout: float | None = 60) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    env["GIT_TERMINAL_PROMPT"] = "0"   # не висеть на вопросе о пароле: пусть лучше упадёт
    env["GIT_OPTIONAL_LOCKS"] = "0"    # status не должен брать index.lock
    extra = {}
    if sys.platform == "win32":
        extra["creationflags"] = subprocess.CREATE_NO_WINDOW
    cmd = ["git", "-c", "core.quotepath=off", *args]
    try:
        return subprocess.run(cmd, cwd=str(cwd), capture_output=True, text=True,
                              encoding="utf-8", errors="replace", stdin=subprocess.DEVNULL,
                              env=env, timeout=timeout, **extra)
    except subprocess.TimeoutExpired:
        return subprocess.CompletedProcess(cmd, 124, "", f"нет ответа за {timeout:.0f} с")
    except OSError as exc:
        return subprocess.CompletedProcess(cmd, 127, "", f"git не запускается: {exc}")


def _rev(cwd: Path, ref: str) -> str | None:
    r = run_git(cwd, "rev-parse", "--verify", "--quiet", ref + "^{commit}")
    return r.stdout.strip() or None if r.returncode == 0 else None


def _names(cwd: Path, *args: str) -> list[str]:
    r = run_git(cwd, *args)
    return [p for p in r.stdout.split("\0") if p] if r.returncode == 0 else []


def _tail(text: str, lines: int = 3) -> str:
    rows = [ln.strip() for ln in (text or "").strip().splitlines() if ln.strip()]
    return " | ".join(rows[-lines:]) or "без подробностей"


def _fmt(paths: list[str]) -> str:
    shown = ", ".join(paths[:LIST_LIMIT])
    return shown + (f" … и ещё {len(paths) - LIST_LIMIT}" if len(paths) > LIST_LIMIT else "")


def current_branch(cwd: Path) -> str | None:
    """Текущая ветка; None — detached HEAD или не репозиторий."""
    r = run_git(cwd, "symbolic-ref", "--quiet", "--short", "HEAD")
    return r.stdout.strip() or None if r.returncode == 0 else None


def list_remotes(cwd: Path) -> list[str]:
    r = run_git(cwd, "remote")
    return r.stdout.split() if r.returncode == 0 else []


def remote_branches(cwd: Path, remote: str) -> list[str]:
    """Ветки remote, известные локально (по последнему fetch)."""
    r = run_git(cwd, "for-each-ref", "--format=%(refname:strip=3)", f"refs/remotes/{remote}")
    return sorted(b for b in r.stdout.split() if b and b != "HEAD")


def repo_toplevel(cwd: Path) -> Path | None:
    r = run_git(cwd, "rev-parse", "--show-toplevel")
    return Path(r.stdout.strip()) if r.returncode == 0 and r.stdout.strip() else None


def bad_name(name: str) -> str | None:
    """Почему имя ветки/remote не годится; None — годится."""
    if not name:
        return "пустое"
    if name.startswith("-"):
        return "начинается с «-»"
    if ".." in name or name.endswith((".", "/", ".lock")) or name.startswith("/"):
        return "недопустимое для git имя"
    if any(ch in BAD_NAME_CHARS for ch in name):
        return "содержит пробел или символ из ~^:?*[\\"
    return None


# ------------------------------------------------------------------ файл ветки
@dataclass
class BranchSpec:
    folder: Path
    marker: Path
    branch: str = ""
    remote: str = "origin"
    checkout: str = "auto"      # auto | manual
    allow_dirty: bool = False
    enabled: bool = True
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def summary(self) -> str:
        if not self.branch:
            return "?"
        text = f"{self.branch} @ {self.remote}"
        if self.checkout == "manual":
            text += ", вручную"
        if self.allow_dirty:
            text += ", allow_dirty"
        return text


def read_spec(marker: Path) -> BranchSpec:
    """Читает файл ветки. Кроме JSON понимает файл из одной строки — имени ветки."""
    spec = BranchSpec(folder=marker.parent, marker=marker)
    try:
        text = marker.read_text(encoding="utf-8-sig").strip()
    except OSError as exc:
        spec.errors.append(f"{MARKER_NAME} не читается: {exc.strerror or exc}")
        return spec
    if not text:
        spec.errors.append(f"{MARKER_NAME} пустой — впиши ветку")
        return spec
    if text.startswith("{"):
        try:
            data = json.loads(text)
        except json.JSONDecodeError as exc:
            spec.errors.append(f"{MARKER_NAME}: это не JSON (строка {exc.lineno}: {exc.msg})")
            return spec
    else:
        data = {"branch": text.splitlines()[0].strip()}

    spec.branch = str(data.get("branch") or "").strip()
    spec.remote = str(data.get("remote") or "origin").strip()
    spec.checkout = str(data.get("checkout") or "auto").strip().lower()
    spec.allow_dirty = data.get("allow_dirty", False) is True
    spec.enabled = data.get("enabled", True) is not False

    why = bad_name(spec.branch)
    if why:
        spec.errors.append(f"ветка в {MARKER_NAME}: {why}" if spec.branch
                           else f"в {MARKER_NAME} не указана ветка (\"branch\")")
    why = bad_name(spec.remote)
    if why:
        spec.errors.append(f"remote в {MARKER_NAME}: {why}")
    if spec.checkout not in CHECKOUT_MODES:
        spec.errors.append(f"checkout в {MARKER_NAME} должен быть auto или manual, а не «{spec.checkout}»")
    unknown = sorted(k for k in data if k not in KNOWN_KEYS and not k.startswith("_"))
    if unknown:
        spec.warnings.append(f"неизвестные ключи в {MARKER_NAME}: {', '.join(unknown)}")
    return spec


def write_marker(folder: Path, branch: str, remote: str = "origin", checkout: str = "auto",
                 allow_dirty: bool = False, enabled: bool = True) -> Path:
    """Создаёт (перезаписывает) файл ветки и прячет его от `git status` через info/exclude."""
    for what, name in (("ветка", branch), ("remote", remote)):
        why = bad_name(name)
        if why:
            raise ValueError(f"{what}: {why}")
    if checkout not in CHECKOUT_MODES:
        raise ValueError(f"checkout: auto или manual, а не {checkout}")
    payload = {"branch": branch, "remote": remote, "checkout": checkout,
               "allow_dirty": allow_dirty, "enabled": enabled, "_help": MARKER_HELP}
    path = folder / MARKER_NAME
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    _hide_from_git(folder)
    return path


def _hide_from_git(folder: Path) -> None:
    r = run_git(folder, "rev-parse", "--git-path", "info/exclude")
    if r.returncode != 0 or not r.stdout.strip():
        return
    exclude = Path(r.stdout.strip())
    if not exclude.is_absolute():
        exclude = folder / exclude
    try:
        lines = exclude.read_text(encoding="utf-8").splitlines() if exclude.exists() else []
        if MARKER_NAME not in (ln.strip() for ln in lines):
            exclude.parent.mkdir(parents=True, exist_ok=True)
            with exclude.open("a", encoding="utf-8") as fh:
                if lines and lines[-1].strip():
                    fh.write("\n")
                fh.write(f"# CodeShuttle: файл ветки для выгрузки\n{MARKER_NAME}\n")
    except OSError:
        pass  # не критично: status всё равно игнорирует файл ветки


# ------------------------------------------------------------------ поиск папок
@dataclass
class Discovery:
    root: Path
    specs: list[BranchSpec] = field(default_factory=list)
    plain: list[Path] = field(default_factory=list)   # папки первого уровня без файла ветки

    @property
    def active(self) -> list[BranchSpec]:
        return [s for s in self.specs if s.enabled]

    def rel(self, folder: Path) -> str:
        try:
            rel = folder.resolve().relative_to(self.root).as_posix()
        except ValueError:
            return str(folder)
        return rel if rel != "." else "(корень проекта)"


def _excluded(name: str, patterns: list[str]) -> bool:
    low = name.lower()
    return any(fnmatch(low, p) for p in patterns)


def discover(cfg: dict) -> Discovery:
    """Находит папки с файлом ветки. Внутрь такой папки дальше не спускается:
    репозиторий — единое целое."""
    root = Path(cfg["project_root"]).resolve()
    exclude = [d.lower() for d in cfg.get("scan", {}).get("exclude_dirs", [])]
    reserved = set()
    for key in ("export_dir", "import_dir", "archive_dir"):
        val = (cfg.get(key) or "").strip()
        if val:
            reserved.add(Path(val).resolve())

    found = Discovery(root=root)
    for dirpath, dirnames, filenames in os.walk(root):
        if MARKER_NAME in filenames:
            found.specs.append(read_spec(Path(dirpath) / MARKER_NAME))
            dirnames[:] = []
            continue
        here = Path(dirpath)
        dirnames[:] = sorted(d for d in dirnames
                             if not _excluded(d, exclude) and (here / d).resolve() not in reserved)

    marked_top = set()
    for s in found.specs:
        if not s.enabled:
            continue
        try:
            parts = s.folder.resolve().relative_to(root).parts
        except ValueError:  # ссылка/junction, уводящая за пределы корня
            parts = s.folder.relative_to(root).parts
        marked_top.add(parts[0] if parts else "")
    if "" not in marked_top:
        try:
            children = sorted(p for p in root.iterdir() if p.is_dir())
        except OSError:
            children = []
        found.plain = [p for p in children
                       if p.name not in marked_top and not _excluded(p.name, exclude)
                       and p.resolve() not in reserved]
    return found


# ------------------------------------------------------------------ проверка репозитория
@dataclass
class RepoPlan:
    spec: BranchSpec
    toplevel: Path | None = None
    current: str | None = None      # текущая ветка; None — detached HEAD
    head: str | None = None
    target: str | None = None       # коммит <remote>/<ветка> после fetch
    behind: int = 0                 # сколько коммитов подтянет обновление
    action: str = ""                # none | ff | switch
    dirty: list[str] = field(default_factory=list)
    untracked: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors

    @property
    def remote_ref(self) -> str:
        return f"refs/remotes/{self.spec.remote}/{self.spec.branch}"

    @property
    def current_label(self) -> str:
        if self.current:
            return self.current
        return f"(detached {self.head[:9]})" if self.head else "—"

    def describe(self) -> str:
        """Одна строка для таблицы."""
        if self.errors:
            return "✖ " + self.errors[0]
        prefix = "⚠ " if self.warnings else ""
        if self.action == "none":
            return prefix + "✔ актуально, выгрузится как есть"
        if self.action == "ff":
            return prefix + f"↓ обновить до {self.spec.remote}/{self.spec.branch} (+{self.behind})"
        if self.action == "switch":
            return prefix + f"↔ переключить {self.current_label} → {self.spec.branch}"
        return "?"


def _status(top: Path, marker_rel: str) -> tuple[list[str], list[str], list[str], str | None]:
    """(изменённые отслеживаемые, неотслеживаемые, с конфликтом, ошибка)."""
    r = run_git(top, "status", "--porcelain=v1", "-z", "--untracked-files=all")
    if r.returncode != 0:
        return [], [], [], f"git status не удался: {_tail(r.stderr)}"
    tracked: list[str] = []
    untracked: list[str] = []
    unmerged: list[str] = []
    entries = r.stdout.split("\0")
    i = 0
    while i < len(entries):
        entry = entries[i]
        i += 1
        if len(entry) < 4:
            continue
        xy, paths = entry[:2], [entry[3:]]
        if xy[0] in "RC" and i < len(entries):   # у переименования следом идёт исходный путь
            paths.append(entries[i])
            i += 1
        paths = [p for p in paths if p and p != marker_rel]
        if not paths or xy == "!!":
            continue
        if xy == "??":
            untracked += paths
        elif xy in UNMERGED:
            unmerged += paths
        else:
            tracked += paths
    return sorted(set(tracked)), sorted(set(untracked)), sorted(set(unmerged)), None


def inspect(spec: BranchSpec, fetch: bool = True, fetch_timeout: float = 180) -> RepoPlan:
    """Всё выясняет и ничего не меняет (кроме refs/remotes после fetch)."""
    plan = RepoPlan(spec=spec, errors=list(spec.errors), warnings=list(spec.warnings))
    if plan.errors:
        return plan
    branch, remote = spec.branch, spec.remote

    top = repo_toplevel(spec.folder)
    if top is None:
        plan.errors.append("папка не git-репозиторий (нет .git)")
        return plan
    plan.toplevel = top
    plan.head = _rev(top, "HEAD")
    plan.current = current_branch(top)

    gitdir = run_git(top, "rev-parse", "--absolute-git-dir").stdout.strip()
    if gitdir:
        plan.errors += [what for name, what in IN_PROGRESS.items() if (Path(gitdir) / name).exists()]

    remotes = list_remotes(top)
    if remote not in remotes:
        plan.errors.append(f"remote «{remote}» не настроен (есть: {', '.join(remotes) or 'ни одного'})")
        return plan

    if fetch:
        r = run_git(top, "fetch", remote, f"+refs/heads/{branch}:{plan.remote_ref}", timeout=fetch_timeout)
        if r.returncode != 0:
            ls = run_git(top, "ls-remote", "--exit-code", "--heads", remote, f"refs/heads/{branch}",
                         timeout=fetch_timeout)
            if ls.returncode == 2:
                plan.errors.append(f"ветки «{branch}» нет на {remote}")
            else:
                plan.errors.append(f"fetch {remote} {branch} не удался: {_tail(r.stderr)}")
            return plan

    plan.target = _rev(top, plan.remote_ref)
    if not plan.target:
        plan.errors.append(f"нет {remote}/{branch} локально — нужен fetch")
        return plan

    local = _rev(top, f"refs/heads/{branch}")
    if local:
        r = run_git(top, "rev-list", "--left-right", "--count", f"refs/heads/{branch}...{plan.remote_ref}")
        ahead, behind = (int(x) for x in r.stdout.split()) if r.returncode == 0 else (0, 0)
        plan.behind = behind
        if ahead and behind:
            plan.errors.append(f"локальная «{branch}» разошлась с {remote}/{branch}: "
                               f"своих коммитов {ahead}, чужих {behind} — сделай pull/rebase сам")
        elif ahead:
            plan.errors.append(f"в локальной «{branch}» {ahead} незапушенных коммит(ов) — "
                               f"запушь их или сбрось ветку, иначе выгрузка не совпадёт с {remote}")

    try:
        marker_rel = spec.marker.resolve().relative_to(top.resolve()).as_posix()
    except ValueError:
        marker_rel = MARKER_NAME
    tracked, untracked, unmerged, status_error = _status(top, marker_rel)
    if status_error:
        plan.errors.append(status_error)
        return plan
    plan.dirty, plan.untracked = tracked, untracked
    if unmerged:
        plan.errors.append(f"неразрешённые конфликты: {_fmt(unmerged)}")

    if plan.current == branch:
        plan.action = "none" if plan.head == plan.target else "ff"
    else:
        plan.action = "switch"
        if spec.checkout == "manual":
            plan.errors.append(f"сейчас {plan.current_label}, а нужна «{branch}» — "
                               f"переключи сам (git checkout {branch}) и запусти снова")

    if plan.action != "none":
        base = plan.head or EMPTY_TREE
        changed = _names(top, "diff", "--name-only", "-z", "--no-renames", base, plan.target)
        added = _names(top, "diff", "--name-only", "-z", "--no-renames", "--diff-filter=A", base, plan.target)
        verb = "переключении" if plan.action == "switch" else "обновлении"
        overlap = sorted(set(tracked) & set(changed))
        if overlap:
            plan.errors.append(f"конфликт при {verb}: эти файлы изменены у тебя и отличаются "
                               f"в {remote}/{branch} — {_fmt(overlap)}")
        clobber = sorted(set(untracked) & set(added))
        if clobber:
            plan.errors.append(f"при {verb} неотслеживаемые файлы затёрлись бы файлами "
                               f"из {remote}/{branch} — {_fmt(clobber)}")

    if tracked and not spec.allow_dirty:
        plan.errors.append(f"незакоммиченные изменения ({len(tracked)}): {_fmt(tracked)} — "
                           f"закоммить, спрячь (git stash) или поставь \"allow_dirty\": true")
    elif tracked:
        plan.warnings.append(f"незакоммиченные изменения уедут в выгрузку ({len(tracked)}): {_fmt(tracked)}")
    if untracked:
        plan.warnings.append(f"неотслеживаемые файлы уедут в выгрузку как есть ({len(untracked)}): "
                             f"{_fmt(untracked)}")
    return plan


# ------------------------------------------------------------------ план по всем папкам
@dataclass
class SyncPlan:
    discovery: Discovery
    repos: list[RepoPlan] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)   # общие: нет git, два файла на репозиторий

    @property
    def ok(self) -> bool:
        return not self.errors and all(r.ok for r in self.repos)

    @property
    def switches(self) -> list[RepoPlan]:
        return [r for r in self.repos if r.ok and r.action == "switch"]

    @property
    def updates(self) -> list[RepoPlan]:
        return [r for r in self.repos if r.ok and r.action == "ff"]

    def name(self, repo: RepoPlan) -> str:
        return self.discovery.rel(repo.spec.folder)

    def problems(self) -> list[str]:
        out = list(self.errors)
        for r in self.repos:
            out += [f"{self.name(r)}: {e}" for e in r.errors]
        return out


def _log_repo(p: RepoPlan, log: Log) -> None:
    if p.toplevel:
        where = f" | {p.spec.remote}/{p.spec.branch} = {p.target[:9]}" if p.target else ""
        log(f"  сейчас: {p.current_label}{' @ ' + p.head[:9] if p.head else ''}{where}")
    if p.ok:
        log(f"  план: {p.describe()}")
    for w in p.warnings:
        log(f"  ⚠ {w}")
    for e in p.errors:
        log(f"  ✖ {e}")


def preflight(cfg: dict, log: Log | None = None, fetch: bool = True,
              discovery: Discovery | None = None,
              on_repo: Callable[[SyncPlan], None] | None = None) -> SyncPlan:
    """fetch + все проверки во всех папках с файлом ветки. Рабочие копии не меняет."""
    log = log or Log()
    found = discovery or discover(cfg)
    sync = SyncPlan(discovery=found)
    log.section("Git: проверка веток" + (" (fetch)" if fetch else ""))
    for s in found.specs:
        if not s.enabled:
            log(f"[{found.rel(s.folder)}] файл ветки выключен (enabled: false) — выгружается как есть")
    if found.plain:
        log(f"Без файла ветки, выгружаются как есть: {', '.join(p.name for p in found.plain)}")
    active = found.active
    if not active:
        log(f"Папок с файлом {MARKER_NAME} нет — git не трогаем.")
        return sync
    if not git_available():
        sync.errors.append("git не найден в PATH — установи Git или убери файлы веток")
        log(f"✖ {sync.errors[-1]}")
        return sync

    timeout = float(cfg.get("git", {}).get("fetch_timeout_sec", 180))
    for s in active:
        log(f"\n[{found.rel(s.folder)}] ветка {s.summary}")
        plan = inspect(s, fetch=fetch, fetch_timeout=timeout)
        sync.repos.append(plan)
        _log_repo(plan, log)
        if on_repo:
            on_repo(sync)

    by_repo: dict[str, list[RepoPlan]] = {}
    for p in sync.repos:
        if p.toplevel:
            by_repo.setdefault(str(p.toplevel.resolve()).lower(), []).append(p)
    for group in by_repo.values():
        if len(group) > 1:
            sync.errors.append(f"в репозитории {group[0].toplevel} несколько файлов ветки "
                               f"({', '.join(sync.name(p) for p in group)}) — оставь один, в корне репозитория")

    ready = sum(1 for p in sync.repos if p.ok)
    log(f"\nGit: папок с файлом ветки {len(sync.repos)} | готовы {ready} | "
        f"с проблемами {len(sync.repos) - ready} | переключить {len(sync.switches)} | "
        f"обновить {len(sync.updates)} | без git {len(found.plain)}")
    for e in sync.errors:
        log(f"✖ {e}")
    return sync


def apply(sync: SyncPlan, log: Log | None = None) -> bool:
    """Переключает и подтягивает ветки по уже проверенному плану."""
    log = log or Log()
    todo = [p for p in sync.repos if p.action in ("ff", "switch")]
    if not sync.ok:
        log("✖ План с ошибками — ветки не трогаю.")
        return False
    if not todo:
        if sync.repos:
            log("Git: все ветки уже актуальны.")
        return True

    log.section("Git: переключение и обновление")
    for p in todo:
        name, branch = sync.name(p), p.spec.branch
        fresh = inspect(p.spec, fetch=False)
        if not fresh.ok or fresh.head != p.head or fresh.target != p.target:
            log(f"[{name}] ✖ репозиторий изменился после проверки — запусти заново")
            for e in fresh.errors:
                log(f"  ✖ {e}")
            return False
        top = p.toplevel or p.spec.folder
        if p.action == "switch":
            r = run_git(top, "checkout", "-B", branch, p.remote_ref)
        else:
            r = run_git(top, "merge", "--ff-only", p.remote_ref)
        if r.returncode != 0:
            log(f"[{name}] ✖ git {'checkout' if p.action == 'switch' else 'merge --ff-only'} "
                f"не удался: {_tail(r.stderr, 5)}")
            return False
        now, cur = _rev(top, "HEAD"), current_branch(top)
        if now != p.target or cur != branch:
            log(f"[{name}] ✖ после {'переключения' if p.action == 'switch' else 'обновления'} "
                f"HEAD = {cur or 'detached'} @ {(now or '?')[:9]}, ожидалось {branch} @ {p.target[:9]}")
            return False
        verb = f"переключено {p.current_label} → {branch}" if p.action == "switch" \
            else f"обновлено +{p.behind}"
        log(f"[{name}] ✔ {verb} @ {now[:9]}")
    return True


def switch_question(sync: SyncPlan) -> str:
    lines = ["Будут переключены ветки:"]
    lines += [f"  • {sync.name(p)}: {p.current_label} → {p.spec.branch}" for p in sync.switches]
    if sync.updates:
        lines += ["", "Обновятся до remote (fast-forward):"]
        lines += [f"  • {sync.name(p)}: {p.spec.branch} +{p.behind}" for p in sync.updates]
    lines += ["", "Проверки пройдены: конфликтов при переключении нет.",
              "Переключить и выгрузить?  «Нет» — ничего не трогать, переключишь сам."]
    return "\n".join(lines)
