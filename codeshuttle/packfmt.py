"""Формат батча CodeShuttle v1 — plain text, безопасный для копипаста.

Структура файла батча:

    ### CODESHUTTLE 1
    ### PACK 20260902T104500-ab12cd
    ### KIND full|delta
    ### BATCH 2 OF 7
    ### BASE <sha состояния до пака | ->
    ### RESULT <sha состояния после пака>
    ### GENERATED 2026-09-02T10:45:00Z
    ### ---
    --8<-- BEGIN {"path":"src/main.py","part":1,"parts":1,"bytes":812,"sha256":"..","nonl":0}
    <строки файла, экранированные>
    --8<-- END
    --8<-- DELETE {"path":"old/thing.py"}

Экранирование: строка содержимого, начинающаяся с любого числа «\» и затем
с маркера, получает ещё один «\». Обратная операция снимает ровно один.
Поэтому содержимое никогда не ломает разбор.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Iterable

from .util import utf8_len

MARK = "--8<--"
FORMAT_NAME = "CODESHUTTLE"
FORMAT_VERSION = 1
HEADER_PREFIX = "### "
HEADER_END = "### ---"
BEGIN_PREFIX = f"{MARK} BEGIN "
END_LINE = f"{MARK} END"
DELETE_PREFIX = f"{MARK} DELETE "

_ESC_RE = re.compile(r"^(\\*)" + re.escape(MARK))
_UNESC_RE = re.compile(r"^(\\+)" + re.escape(MARK))

# запас на заголовок батча (7 строк) + перевод строки
HEADER_RESERVE = 320
# запас на BEGIN/END одного блока
BLOCK_OVERHEAD = 260


def escape_line(s: str) -> str:
    return "\\" + s if _ESC_RE.match(s) else s


def unescape_line(s: str) -> str:
    return s[1:] if _UNESC_RE.match(s) else s


# ---------------------------------------------------------------- запись

@dataclass
class Block:
    """Готовый к записи блок (кусок файла или команда удаления)."""
    kind: str          # "file" | "delete"
    path: str
    text: str          # полностью отрендеренный блок, заканчивается \n
    size: int
    part: int = 1
    parts: int = 1


def _split_long_line(raw: str, budget: int) -> list[str]:
    """Режет одну длинную строку на куски, влезающие в budget байт."""
    out: list[str] = []
    buf: list[str] = []
    size = 0
    for ch in raw:
        csz = utf8_len(ch)
        if size + csz > budget - 4 and buf:
            out.append("".join(buf))
            buf, size = [], 0
        buf.append(ch)
        size += csz
    out.append("".join(buf))
    return out


def build_file_blocks(path: str, text: str, sha: str, content_budget: int) -> list[Block]:
    """Разбивает содержимое файла на блоки, влезающие в лимит батча."""
    total_bytes = utf8_len(text)

    # units: (экранированный текст без \n, заканчивается ли здесь настоящая строка)
    units: list[tuple[str, bool]] = []
    for line in text.splitlines(keepends=True):
        ends = line.endswith("\n")
        raw = line[:-1] if ends else line
        esc = escape_line(raw)
        if utf8_len(esc) + 1 > content_budget:
            frags = _split_long_line(raw, content_budget)
            for i, frag in enumerate(frags):
                last = i == len(frags) - 1
                units.append((escape_line(frag), ends if last else False))
        else:
            units.append((esc, ends))

    # укладываем units в части
    parts: list[tuple[str, int]] = []   # (body, nonl)
    cur: list[str] = []
    cur_size = 0
    cur_last_ends = True

    def flush() -> None:
        nonlocal cur, cur_size, cur_last_ends
        if not cur:
            return
        parts.append(("".join(u + "\n" for u in cur), 0 if cur_last_ends else 1))
        cur, cur_size, cur_last_ends = [], 0, True

    for utext, ends in units:
        usize = utf8_len(utext) + 1
        if cur and cur_size + usize > content_budget:
            flush()
        cur.append(utext)
        cur_size += usize
        cur_last_ends = ends
        if not ends:
            flush()  # обрыв посреди строки обязан заканчивать часть
    flush()

    if not parts:
        parts = [("", 0)]

    n = len(parts)
    blocks: list[Block] = []
    for i, (body, nonl) in enumerate(parts, start=1):
        head = json.dumps(
            {"path": path, "part": i, "parts": n, "bytes": total_bytes, "sha256": sha, "nonl": nonl},
            ensure_ascii=False, separators=(",", ":"))
        rendered = f"{BEGIN_PREFIX}{head}\n{body}{END_LINE}\n"
        blocks.append(Block("file", path, rendered, utf8_len(rendered), i, n))
    return blocks


def build_delete_block(path: str) -> Block:
    head = json.dumps({"path": path}, ensure_ascii=False, separators=(",", ":"))
    rendered = f"{DELETE_PREFIX}{head}\n"
    return Block("delete", path, rendered, utf8_len(rendered))


def pack_blocks(blocks: Iterable[Block], max_bytes: int) -> list[list[Block]]:
    """Жадная укладка блоков в батчи (порядок сохраняется)."""
    budget = max_bytes - HEADER_RESERVE
    batches: list[list[Block]] = []
    cur: list[Block] = []
    cur_size = 0
    for b in blocks:
        if cur and cur_size + b.size > budget:
            batches.append(cur)
            cur, cur_size = [], 0
        cur.append(b)
        cur_size += b.size
    if cur:
        batches.append(cur)
    return batches or [[]]


def render_batch(blocks: list[Block], *, pack_id: str, kind: str, index: int,
                 total: int, base: str, result: str, generated: str) -> str:
    header = (
        f"{HEADER_PREFIX}{FORMAT_NAME} {FORMAT_VERSION}\n"
        f"{HEADER_PREFIX}PACK {pack_id}\n"
        f"{HEADER_PREFIX}KIND {kind}\n"
        f"{HEADER_PREFIX}BATCH {index} OF {total}\n"
        f"{HEADER_PREFIX}BASE {base or '-'}\n"
        f"{HEADER_PREFIX}RESULT {result or '-'}\n"
        f"{HEADER_PREFIX}GENERATED {generated}\n"
        f"{HEADER_END}\n"
    )
    return header + "".join(b.text for b in blocks)


# ---------------------------------------------------------------- чтение

@dataclass
class ParsedPart:
    path: str
    part: int
    parts: int
    bytes: int
    sha256: str
    nonl: int
    text: str


@dataclass
class ParsedBatch:
    pack_id: str
    kind: str
    index: int
    total: int
    base: str
    result: str
    generated: str
    source: str = ""
    parts: list[ParsedPart] = field(default_factory=list)
    deletes: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


class ParseError(Exception):
    pass


def _decode_body(lines: list[str], nonl: int) -> str:
    body = "".join(unescape_line(ln) + "\n" for ln in lines)
    if nonl and body.endswith("\n"):
        body = body[:-1]
    return body


def parse_text(text: str, source: str = "") -> list[ParsedBatch]:
    """Разбирает текст, который может содержать один или несколько батчей.

    Устойчив к постороннему тексту вокруг (например, обрамление из чата)."""
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    lines = text.split("\n")
    batches: list[ParsedBatch] = []
    cur: ParsedBatch | None = None
    i = 0

    while i < len(lines):
        line = lines[i]

        if line.startswith(f"{HEADER_PREFIX}{FORMAT_NAME} "):
            meta: dict[str, str] = {}
            i += 1
            while i < len(lines) and lines[i].startswith("###"):
                if lines[i].strip() == HEADER_END:
                    i += 1
                    break
                kv = lines[i][len(HEADER_PREFIX):].strip().split(" ", 1)
                if len(kv) == 2:
                    meta[kv[0].upper()] = kv[1].strip()
                i += 1
            batch_meta = meta.get("BATCH", "1 OF 1").split(" OF ")
            try:
                idx, tot = int(batch_meta[0]), int(batch_meta[1])
            except (ValueError, IndexError):
                idx, tot = 1, 1
            cur = ParsedBatch(
                pack_id=meta.get("PACK", "unknown"),
                kind=meta.get("KIND", "full"),
                index=idx, total=tot,
                base=meta.get("BASE", "-"),
                result=meta.get("RESULT", "-"),
                generated=meta.get("GENERATED", ""),
                source=source,
            )
            batches.append(cur)
            continue

        if cur is not None and line.startswith(BEGIN_PREFIX):
            try:
                head = json.loads(line[len(BEGIN_PREFIX):])
            except json.JSONDecodeError as exc:
                cur.errors.append(f"строка {i + 1}: битый заголовок блока ({exc})")
                i += 1
                continue
            body: list[str] = []
            i += 1
            closed = False
            while i < len(lines):
                if lines[i] == END_LINE:
                    closed = True
                    i += 1
                    break
                body.append(lines[i])
                i += 1
            if not closed:
                cur.errors.append(f"блок {head.get('path')} не закрыт (обрезанный батч?)")
                continue
            cur.parts.append(ParsedPart(
                path=head.get("path", ""),
                part=int(head.get("part", 1)),
                parts=int(head.get("parts", 1)),
                bytes=int(head.get("bytes", 0)),
                sha256=head.get("sha256", ""),
                nonl=int(head.get("nonl", 0)),
                text=_decode_body(body, int(head.get("nonl", 0))),
            ))
            continue

        if cur is not None and line.startswith(DELETE_PREFIX):
            try:
                head = json.loads(line[len(DELETE_PREFIX):])
                cur.deletes.append(head["path"])
            except (json.JSONDecodeError, KeyError) as exc:
                cur.errors.append(f"строка {i + 1}: битый DELETE ({exc})")
            i += 1
            continue

        i += 1

    return batches


def looks_like_batch(text: str) -> bool:
    return f"{HEADER_PREFIX}{FORMAT_NAME} " in text[:4096]
