"""Конфигурация CodeShuttle: значения по умолчанию, загрузка, слияние, сохранение."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

TOOL_DIR = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG_PATH = TOOL_DIR / "config.json"

DEFAULTS: dict[str, Any] = {
    # --- пути (заполняются пользователем) ---
    "project_root": "",   # корень проекта: источник для экспорта И цель для импорта
    "export_dir": "",     # куда складывать батчи (outbox)
    "import_dir": "",     # откуда забирать батчи (inbox)
    "archive_dir": "",    # куда класть .rar с батчами; пусто -> <родитель project_root>/.codeshuttle-archives
    "state_file": "",     # пусто -> <tool>/.state/<hash>.json

    # --- размер батча ---
    "max_batch_kb": 45,

    # --- экспорт одной кнопкой (git -> батчи -> .rar) ---
    "ship_kind": "full",  # full — весь проект, delta — только изменения
    "gui_tab": "export",  # какую вкладку окна открыть при запуске

    "git": {
        "fetch_timeout_sec": 180,  # сколько ждать fetch одной папки
        "confirm_switch": True,    # окно: спрашивать перед переключением веток
    },

    "scan": {
        "mode": "whitelist",          # whitelist | all
        "extensions": [
            "py", "pyi", "pyx", "js", "mjs", "cjs", "jsx", "ts", "tsx", "vue", "svelte",
            "java", "kt", "kts", "scala", "groovy", "go", "rs", "rb", "php", "cs", "fs",
            "c", "h", "cc", "cpp", "cxx", "hpp", "hh", "m", "mm", "swift", "dart",
            "sh", "bash", "zsh", "ps1", "psm1", "bat", "cmd",
            "sql", "prisma", "graphql", "gql", "proto", "thrift",
            "html", "htm", "css", "scss", "sass", "less", "styl",
            "json", "jsonc", "yaml", "yml", "toml", "ini", "cfg", "conf", "properties",
            "xml", "xsd", "gradle", "sbt", "cmake", "mk", "make",
            "md", "mdx", "rst", "txt", "tf", "tfvars", "hcl", "lua", "pl", "pm",
            "r", "jl", "ex", "exs", "erl", "clj", "cljs", "elm", "nim", "zig", "asm",
            "tpl", "hbs", "ejs", "pug", "twig", "razor", "cshtml", "jinja", "j2",
            "jsp", "jspx", "tag", "tld", "ftl", "mustache", "bzl", "bazel", "aidl",
        ],
        "filenames": [
            "Dockerfile", "Makefile", "Rakefile", "Gemfile", "Procfile", "Justfile",
            "CMakeLists.txt", ".gitignore", ".dockerignore", ".editorconfig",
            ".eslintrc", ".prettierrc", ".babelrc", "LICENSE", "README",
            "mvnw", "gradlew", "BUILD", "WORKSPACE",
        ],
        "exclude_dirs": [
            ".git", ".svn", ".hg", ".idea", ".vscode", ".vs",
            "node_modules", "bower_components", "vendor", "jspm_packages",
            "__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache", ".tox",
            "venv", ".venv", "env", ".env.d", "virtualenv", "site-packages",
            "dist", "build", "out", "target", "bin", "obj", ".next", ".nuxt", ".svelte-kit",
            "coverage", "htmlcov", ".gradle", ".terraform", ".serverless",
            ".cache", ".parcel-cache", ".turbo", "logs", "tmp", "temp",
            ".codeshuttle", ".codeshuttle_backup",
        ],
        "exclude_globs": [
            "*.min.js", "*.min.css", "*.map", "*.lock", "*-lock.json", "*.log",
            "*.pyc", "*.pyo", "*.class", "*.o", "*.so", "*.dll", "*.exe", "*.bin",
            "*.zip", "*.gz", "*.tar", "*.7z", "*.rar", "*.jar", "*.war",
            "*.png", "*.jpg", "*.jpeg", "*.gif", "*.ico", "*.svg", "*.webp", "*.pdf",
            "*.woff", "*.woff2", "*.ttf", "*.eot", "*.mp4", "*.mp3", "*.wav",
            "*.db", "*.sqlite", "*.sqlite3", "*.mdb",
        ],
        # сильнее exclude_globs и белого списка расширений
        "keep_globs": [
            "pom.xml", "*-pom.xml", "build.xml", "ivy.xml",
            "package.json", "tsconfig*.json", "jsconfig*.json", "composer.json",
            "pyproject.toml", "Cargo.toml", "setup.cfg", "tox.ini", "MANIFEST.in",
        ],
        "max_file_kb": 512,
        "follow_symlinks": False,
    },

    "cipher": {
        "enabled": True,   # кодировать батчи при экспорте (импорт читает и кодированные, и старые plain)
        "key": "",         # пусто -> встроенный ключ; свой ключ задаётся на ОБЕИХ машинах
    },

    "import": {
        "apply_deletes": True,   # применять DELETE-блоки из дельты
        "move_applied": True,    # переносить применённые батчи в import_dir/_applied/<pack>
        "prune_extra_on_full": False,  # при full-паке удалять локальные файлы, которых нет в паке
        "require_all_batches": False,  # true = не применять пак, если донесли не все батчи
    },
}


def _deep_merge(base: dict, override: dict) -> dict:
    out = dict(base)
    for k, v in (override or {}).items():
        if k.startswith("_"):
            continue
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def ensure_config(path: str | Path | None = None) -> tuple[Path, bool]:
    """Создаёт конфиг при первом запуске (из config.example.json или из DEFAULTS).

    Возвращает (путь, был_ли_создан)."""
    p = Path(path) if path else DEFAULT_CONFIG_PATH
    if p.exists():
        return p, False
    example = p.parent / "config.example.json"
    if example.exists():
        p.write_text(example.read_text(encoding="utf-8"), encoding="utf-8")
    else:
        write_default_config(p)
    return p, True


def load_config(path: str | Path | None = None) -> dict:
    p, _created = ensure_config(path)
    if not p.exists():
        raise FileNotFoundError(f"Не найден конфиг: {p}")
    raw = json.loads(p.read_text(encoding="utf-8"))
    cfg = _deep_merge(DEFAULTS, raw)
    cfg["_config_path"] = str(p)
    return cfg


def save_config(cfg: dict, path: str | Path | None = None) -> Path:
    p = Path(path) if path else Path(cfg.get("_config_path") or DEFAULT_CONFIG_PATH)
    clean = {k: v for k, v in cfg.items() if not k.startswith("_")}
    p.write_text(json.dumps(clean, ensure_ascii=False, indent=2), encoding="utf-8")
    return p


def write_default_config(path: str | Path | None = None) -> Path:
    p = Path(path) if path else DEFAULT_CONFIG_PATH
    p.write_text(json.dumps(DEFAULTS, ensure_ascii=False, indent=2), encoding="utf-8")
    return p


def state_file_for(cfg: dict) -> Path:
    """Файл состояния (хэши файлов) — по умолчанию рядом с инструментом."""
    explicit = (cfg.get("state_file") or "").strip()
    if explicit:
        return Path(explicit)
    root = (cfg.get("project_root") or "").strip()
    slug = hashlib.sha256(str(Path(root).resolve() if root else "none").lower().encode("utf-8")).hexdigest()[:12]
    return TOOL_DIR / ".state" / f"state_{slug}.json"


def max_batch_bytes(cfg: dict) -> int:
    return int(float(cfg.get("max_batch_kb", 45)) * 1024)


def validate(cfg: dict, need_import: bool = False, need_export: bool = False) -> list[str]:
    errors: list[str] = []
    root = (cfg.get("project_root") or "").strip()
    if not root:
        errors.append("project_root не задан в конфиге")
    elif not Path(root).is_dir():
        errors.append(f"project_root не существует или не папка: {root}")
    if need_export:
        d = (cfg.get("export_dir") or "").strip()
        if not d:
            errors.append("export_dir не задан в конфиге")
    if need_import:
        d = (cfg.get("import_dir") or "").strip()
        if not d:
            errors.append("import_dir не задан в конфиге")
        elif not Path(d).is_dir():
            errors.append(f"import_dir не существует: {d}")
    if max_batch_bytes(cfg) < 4096:
        errors.append("max_batch_kb слишком мал (минимум 4)")
    return errors
