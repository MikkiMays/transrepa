"""Мелкие утилиты: хэши, нормализация текста, логгер."""
from __future__ import annotations

import hashlib
import time
from pathlib import Path
from typing import Callable, Optional

UTF8_BOM = "\ufeff"


def utf8_len(s: str) -> int:
    return len(s.encode("utf-8"))


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def sha256_str(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()


def decode_source(raw: bytes) -> Optional[str]:
    """Декодирует байты исходника в текст с LF-переводами строк.

    Возвращает None, если файл бинарный (не UTF-8 или содержит NUL)."""
    if b"\x00" in raw:
        return None
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        return None
    if text.startswith(UTF8_BOM):
        text = text[len(UTF8_BOM):]
    return text.replace("\r\n", "\n").replace("\r", "\n")


def now_stamp() -> str:
    """Метка времени с миллисекундами: два пака в одну секунду должны различаться."""
    t = time.time()
    return time.strftime("%Y%m%dT%H%M%S", time.gmtime(t)) + f"{int((t % 1) * 1000):03d}"


def now_iso() -> str:
    t = time.time()
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(t)) + f".{int((t % 1) * 1000):03d}Z"


def human_size(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024.0
    return f"{n} B"


def rel_posix(root: Path, path: Path) -> str:
    return path.relative_to(root).as_posix()


def is_safe_relpath(rel: str) -> bool:
    """Защита от path traversal при импорте."""
    if not rel or rel.startswith("/") or rel.startswith("\\"):
        return False
    if ":" in rel.split("/")[0]:  # C:/...
        return False
    parts = rel.replace("\\", "/").split("/")
    if any(p in ("..", "") for p in parts):
        return False
    if any(p.strip() != p for p in parts):
        return False
    return True


class Log:
    """Логгер с подключаемым приёмником (консоль или GUI)."""

    def __init__(self, sink: Optional[Callable[[str], None]] = None):
        self.sink = sink or (lambda line: print(line, flush=True))
        self.lines: list[str] = []

    def __call__(self, msg: str = "") -> None:
        self.lines.append(msg)
        try:
            self.sink(msg)
        except Exception:
            pass

    def section(self, title: str) -> None:
        self(f"\n=== {title} ===")
