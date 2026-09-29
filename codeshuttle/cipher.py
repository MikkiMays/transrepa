"""Обфускация батчей: батч не уезжает в исходном виде.

Это НЕ криптостойкое шифрование, а обратимое кодирование, чтобы .txt-батч не
читался как исходник и раскладывался обратно только этой утилитой. Схема
открытая (обычный Python), так что кодирование защищает от случайного взгляда и
индексаторов, но не от того, у кого есть CodeShuttle.

Как устроено: байты UTF-8 XOR-ятся потоком, растянутым из ключа через sha256
(чтобы это был не однобайтовый Цезарь), затем base64 — результат остаётся ASCII
и безопасен для копипаста и упаковки в .rar. Ключ по умолчанию встроенный, чтобы
работало между машинами без настройки; свой ключ в config.json надо задать на
обеих машинах.

Приёмник декодирует сам: не наш файл (старый plain-батч или мусор) не
проходит base64/UTF-8 или не выглядит батчем — тогда читается как есть. Значит
старые некодированные батчи и новые кодированные лежат в одной папке вперемешку.
"""
from __future__ import annotations

import base64
import binascii
import hashlib

DEFAULT_KEY = "codeshuttle"
LINE_WIDTH = 76
_STREAM_LEN = 512


def _keystream(key: str) -> bytes:
    seed = (key or DEFAULT_KEY).encode("utf-8")
    out = bytearray()
    block = hashlib.sha256(seed).digest()
    while len(out) < _STREAM_LEN:
        out.extend(block)
        block = hashlib.sha256(block + seed).digest()
    return bytes(out[:_STREAM_LEN])


def _xor(data: bytes, key: str) -> bytes:
    ks = _keystream(key)
    n = len(ks)
    return bytes(b ^ ks[i % n] for i, b in enumerate(data))


def resolve_keys(cfg: dict) -> list[str]:
    """Ключи для попыток декодирования: заданный в конфиге, затем встроенный."""
    key = (cfg.get("cipher", {}).get("key") or "").strip()
    keys = [key] if key else []
    if DEFAULT_KEY not in keys:
        keys.append(DEFAULT_KEY)
    return keys


def encode_key(cfg: dict) -> str:
    return (cfg.get("cipher", {}).get("key") or "").strip() or DEFAULT_KEY


def is_enabled(cfg: dict) -> bool:
    return bool(cfg.get("cipher", {}).get("enabled", True))


def encode(text: str, key: str = DEFAULT_KEY) -> str:
    raw = _xor(text.encode("utf-8"), key)
    b64 = base64.b64encode(raw).decode("ascii")
    return "\n".join(b64[i:i + LINE_WIDTH] for i in range(0, len(b64), LINE_WIDTH)) + "\n"


def looks_encoded(text: str) -> bool:
    """Похоже ли на наш кодированный батч: только base64-символы и это не plain-батч."""
    compact = "".join(text.split())
    if len(compact) < 24 or "### CODESHUTTLE " in text[:4096]:
        return False
    try:
        base64.b64decode(compact, validate=True)
    except (binascii.Error, ValueError):
        return False
    return True


def decode(text: str, keys: list[str]) -> str | None:
    """Раскодировать батч. None — если это не наш кодированный файл."""
    compact = "".join(text.split())
    if not compact:
        return None
    try:
        raw = base64.b64decode(compact, validate=True)
    except (binascii.Error, ValueError):
        return None
    for key in keys:
        try:
            return _xor(raw, key).decode("utf-8")
        except UnicodeDecodeError:
            continue
    return None
