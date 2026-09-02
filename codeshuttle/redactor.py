"""Санитайзер: вычищает из кода всё, что похоже на креды/секреты.

Идемпотентен: повторный прогон уже очищенного текста ничего не меняет,
поэтому дельта-экспорт не «дёргается» на одних и тех же файлах.

Ключевое различие:
  * `String pwd = "hunter2";`      -> литерал, затираем;
  * `this.password = password;`    -> ссылка на переменную, НЕ трогаем;
  * `db.password=hunter2` в .properties/.ini -> литерал (там всё литералы).
"""
from __future__ import annotations

import math
import re
from dataclasses import dataclass, field

PLACEHOLDER = "__REDACTED__"

# расширения, где значение справа от «=» — всегда литерал, а не переменная
CONFIG_EXTENSIONS = {
    "properties", "ini", "cfg", "conf", "env", "toml", "tfvars", "tf",
    "hcl", "npmrc", "netrc", "editorconfig", "dockercfg",
}
CONFIG_FILENAMES = {"dockerfile", "makefile", ".env", "environment"}


@dataclass
class Finding:
    kind: str
    line: int
    critical: bool = False


@dataclass
class RedactResult:
    text: str
    findings: list[Finding] = field(default_factory=list)
    critical: bool = False
    dropped_reason: str = ""

    @property
    def count(self) -> int:
        return len(self.findings)


# --- критические блоки: файл с таким содержимым не выгружается вообще ---
CRITICAL_PATTERNS: list[tuple[str, re.Pattern]] = [
    ("private-key-block", re.compile(
        r"-----BEGIN[ A-Z0-9]*PRIVATE KEY-----.*?-----END[ A-Z0-9]*PRIVATE KEY-----", re.S)),
    ("pgp-key-block", re.compile(
        r"-----BEGIN PGP PRIVATE KEY BLOCK-----.*?-----END PGP PRIVATE KEY BLOCK-----", re.S)),
    ("cert-with-key", re.compile(
        r"-----BEGIN ENCRYPTED PRIVATE KEY-----.*?-----END ENCRYPTED PRIVATE KEY-----", re.S)),
]

# --- конкретные форматы токенов: затирается всё совпадение ---
TOKEN_PATTERNS: list[tuple[str, re.Pattern]] = [
    ("aws-access-key", re.compile(r"\b(?:A3T[A-Z0-9]|AKIA|ASIA|ABIA|ACCA)[A-Z0-9]{12,20}\b")),
    ("google-api-key", re.compile(r"\bAIza[0-9A-Za-z\-_]{35}\b")),
    ("github-token", re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,})\b")),
    ("gitlab-token", re.compile(r"\bglpat-[A-Za-z0-9\-_]{16,}\b")),
    ("slack-token", re.compile(r"\bxox[abprs]-[A-Za-z0-9\-]{10,}\b")),
    ("slack-webhook", re.compile(r"https://hooks\.slack\.com/services/[A-Za-z0-9/+_-]{20,}")),
    ("discord-webhook", re.compile(r"https://discord(?:app)?\.com/api/webhooks/[0-9]+/[A-Za-z0-9._-]{20,}")),
    ("stripe-key", re.compile(r"\b(?:sk|rk)_(?:live|test)_[A-Za-z0-9]{10,}\b")),
    ("llm-api-key", re.compile(r"\bsk-(?:ant-|proj-|or-)?[A-Za-z0-9_\-]{24,}\b")),
    ("telegram-bot-token", re.compile(r"\b\d{8,12}:AA[A-Za-z0-9_\-]{30,}\b")),
    ("sendgrid-key", re.compile(r"\bSG\.[A-Za-z0-9_\-]{16,}\.[A-Za-z0-9_\-]{16,}\b")),
    ("twilio-sid", re.compile(r"\bAC[0-9a-fA-F]{32}\b")),
    ("npm-token", re.compile(r"\bnpm_[A-Za-z0-9]{30,}\b")),
    ("jwt", re.compile(r"\beyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{6,}")),
    ("crypto-private-key", re.compile(r"\b0x[0-9a-fA-F]{64}\b")),
]

# логин:пароль внутри URL -> user:__REDACTED__@host
# длина схемы ограничена: иначе на очень длинных строках regex уходит в квадрат
URL_CRED_RE = re.compile(r"([a-zA-Z][a-zA-Z0-9+.\-]{0,15}://)([^\s/:@\"']{1,64}):([^\s/@\"']{1,128})@")

# без \b: ключ может быть частью составного имени (DB_PASSWORD, myApiKey)
_KEYWORDS = r"""(?:
    pass(?:word|wd|phrase)?|pwd|secret|token|api[_\-]?key|apikey|access[_\-]?key|
    secret[_\-]?key|client[_\-]?secret|private[_\-]?key|auth[_\-]?token|
    authorization|bearer|credentials?|creds|passwd|dsn|
    conn(?:ection)?[_\-]?str(?:ing)?|sas[_\-]?token|session[_\-]?key|
    encryption[_\-]?key|signing[_\-]?key|master[_\-]?key|refresh[_\-]?token|
    access[_\-]?token|id[_\-]?token|licen[cs]e[_\-]?key|webhook[_\-]?url
)"""

_VALUE = r"""(?P<val>
      "(?:[^"\\\n]|\\.)*"
    | '(?:[^'\\\n]|\\.)*'
    | `[^`\n]*`
    | [A-Za-z_$@][\w$]*(?:\s*(?:\.|::|->)\s*[\w$]+)*
      (?:\s*(?:\([^()\n]{0,200}\)|\[[^\[\]\n]{0,200}\]))*
    | [^\s,;\)\]\}#\[\{\(][^\s,;\)\]\}#]*
)"""

# ключ = значение / ключ: значение
SECRET_KEY_RE = re.compile(
    r"(?ix)" + _KEYWORDS + r"[A-Za-z0-9_\-]{0,20}[\"']?[ \t]*(?P<op>:=|=>|::|[:=])[ \t]*" + _VALUE)

# вызовы вида setProperty("password", "hunter2")
# требуем открывающую скобку перед ключом, иначе под правило попадают
# обычные списки строк вида ["*secret*", "*secrets*"]
CALL_PAIR_RE = re.compile(
    r"(?ix)\([ \t]*(?P<q1>[\"'])[\w\-.]{0,30}" + _KEYWORDS + r"[\w\-.]{0,20}(?P=q1)[ \t]*,[ \t]*"
    r"(?P<val>\"(?:[^\"\\\n]|\\.)*\"|'(?:[^'\\\n]|\\.)*')")

# значения, которые точно не секрет
NOT_A_SECRET_RE = re.compile(
    r"""(?ix)
    ^\s*(?:
        | none|null|nil|true|false|undefined|empty|n/?a
        | -?\d+(?:\.\d+)?
        | \$?\{\{?[^}]*\}?\}
        | <[^>]*>
        | %\(?[A-Za-z_]*\)?[sdv]
        | \{[^}]*\}
        | x{3,}|\*{3,}|\.{3,}|-{3,}|_{3,}
        | (?:your|my|the|some)[_\- ]?\w*
        | change[_\-]?me|placeholder|example|sample|dummy|fake|test|demo|todo|fixme|
          admin|root|user|guest|default|unset|redacted|hidden|masked
        | __redacted__
    )\s*$
    """)

# ссылка на переменную/выражение (в коде — не литерал)
IDENT_EXPR_RE = re.compile(
    r"""^[A-Za-z_$@][\w$]*
        (?:\s*(?:\.|::|->)\s*[\w$]+)*
        (?:\s*(?:\([^()]*\)|\[[^\[\]]*\]))*$""", re.X)

QUOTED_BLOB_TPL = r"""(?P<q>["'])(?P<v>[A-Za-z0-9+/=_\-]{%d,})(?P=q)"""
EMAIL_RE = re.compile(r"\b[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}\b")
IPV4_RE = re.compile(r"\b(?:(?:25[0-5]|2[0-4]\d|1?\d?\d)\.){3}(?:25[0-5]|2[0-4]\d|1?\d?\d)\b")
LOCAL_IP_RE = re.compile(r"^(?:0\.|127\.|10\.|192\.168\.|172\.(?:1[6-9]|2\d|3[01])\.|255\.|224\.)")

MIN_SECRET_LEN = 4


def _shannon(s: str) -> float:
    if not s:
        return 0.0
    freq: dict[str, int] = {}
    for ch in s:
        freq[ch] = freq.get(ch, 0) + 1
    n = len(s)
    return -sum((c / n) * math.log2(c / n) for c in freq.values())


class _Liner:
    """Быстрый перевод смещения в номер строки (позиции идут по возрастанию)."""

    def __init__(self, text: str):
        self.text = text
        self.pos = 0
        self.line = 1

    def at(self, pos: int) -> int:
        if pos < self.pos:
            return self.text.count("\n", 0, pos) + 1
        self.line += self.text.count("\n", self.pos, pos)
        self.pos = pos
        return self.line


def _unquote(val: str) -> tuple[str, str]:
    if len(val) >= 2 and val[0] == val[-1] and val[0] in "\"'`":
        return val[1:-1], val[0]
    return val, ""


def is_config_file(filename: str) -> bool:
    name = (filename or "").rsplit("/", 1)[-1].lower()
    if name in CONFIG_FILENAMES or name.startswith(".env"):
        return True
    ext = name.rsplit(".", 1)[-1] if "." in name else ""
    return ext in CONFIG_EXTENSIONS


def _should_redact(val: str, config_mode: bool) -> bool:
    inner, quote = _unquote(val)
    stripped = inner.strip()
    if PLACEHOLDER in inner:
        return False
    if len(stripped) < MIN_SECRET_LEN:
        return False
    if NOT_A_SECRET_RE.match(stripped):
        return False
    if not quote and not config_mode and IDENT_EXPR_RE.match(stripped):
        return False  # в коде это переменная/вызов, а не литерал
    return True


def redact(text: str, sec_cfg: dict, filename: str = "") -> RedactResult:
    """Возвращает очищенный текст + список находок."""
    if not sec_cfg.get("enabled", True):
        return RedactResult(text=text)

    findings: list[Finding] = []
    critical = False
    out = text
    config_mode = is_config_file(filename)

    def sub(rx: re.Pattern, fn) -> None:
        nonlocal out
        liner = _Liner(out)
        out = rx.sub(lambda m: fn(m, liner), out)

    # 1. критические блоки ключей
    def _crit(m: re.Match, liner: _Liner, name: str) -> str:
        nonlocal critical
        critical = True
        findings.append(Finding(name, liner.at(m.start()), critical=True))
        return f"[{PLACEHOLDER}:{name}]"

    for name, rx in CRITICAL_PATTERNS:
        sub(rx, lambda m, l, _n=name: _crit(m, l, _n))

    if not sec_cfg.get("redact_patterns", True):
        return RedactResult(text=out, findings=findings, critical=critical)

    # 2. известные форматы токенов
    def _tok(m: re.Match, liner: _Liner, name: str) -> str:
        findings.append(Finding(name, liner.at(m.start())))
        return PLACEHOLDER

    for name, rx in TOKEN_PATTERNS:
        sub(rx, lambda m, l, _n=name: _tok(m, l, _n))

    # 3. креды внутри URL
    def _url(m: re.Match, liner: _Liner) -> str:
        if m.group(3) == PLACEHOLDER:
            return m.group(0)
        findings.append(Finding("url-credentials", liner.at(m.start())))
        return f"{m.group(1)}{m.group(2)}:{PLACEHOLDER}@"
    sub(URL_CRED_RE, _url)

    # 4. присваивания «ключ = значение» и пары («ключ», «значение»)
    def _kv(m: re.Match, liner: _Liner) -> str:
        val = m.group("val")
        if not _should_redact(val, config_mode):
            return m.group(0)
        findings.append(Finding("secret-assignment", liner.at(m.start())))
        _, quote = _unquote(val)
        replacement = f"{quote}{PLACEHOLDER}{quote}" if quote else PLACEHOLDER
        head = m.group(0)[: m.start("val") - m.start()]
        return head + replacement
    sub(SECRET_KEY_RE, _kv)
    sub(CALL_PAIR_RE, _kv)

    # 5. длинные высокоэнтропийные строки в кавычках
    if sec_cfg.get("redact_high_entropy", True):
        min_len = max(16, int(sec_cfg.get("high_entropy_min_len", 40)))
        blob_re = re.compile(QUOTED_BLOB_TPL % min_len)

        def _blob(m: re.Match, liner: _Liner) -> str:
            v = m.group("v")
            if PLACEHOLDER in v or _shannon(v) < 3.5:
                return m.group(0)
            findings.append(Finding("high-entropy-blob", liner.at(m.start())))
            q = m.group("q")
            return f"{q}{PLACEHOLDER}{q}"
        sub(blob_re, _blob)

    # 6. пользовательские паттерны из конфига
    for extra in sec_cfg.get("extra_patterns", []) or []:
        try:
            rx = re.compile(extra["regex"])
        except (re.error, KeyError, TypeError):
            continue
        name = extra.get("name", "custom")

        def _extra(m: re.Match, liner: _Liner, _n=name) -> str:
            if PLACEHOLDER in m.group(0):
                return m.group(0)
            findings.append(Finding(_n, liner.at(m.start())))
            return PLACEHOLDER
        sub(rx, _extra)

    # 7. опционально: почты и внешние IP
    if sec_cfg.get("redact_emails", False):
        def _mail(m: re.Match, liner: _Liner) -> str:
            findings.append(Finding("email", liner.at(m.start())))
            return "user@example.com"
        sub(EMAIL_RE, _mail)

    if sec_cfg.get("redact_ips", False):
        def _ip(m: re.Match, liner: _Liner) -> str:
            if LOCAL_IP_RE.match(m.group(0)):
                return m.group(0)
            findings.append(Finding("ip-address", liner.at(m.start())))
            return "0.0.0.0"
        sub(IPV4_RE, _ip)

    result = RedactResult(text=out, findings=findings, critical=critical)

    limit = int(sec_cfg.get("max_redactions_per_file", 40))
    if critical and sec_cfg.get("drop_file_on_critical", True):
        result.dropped_reason = "критический секрет (приватный ключ)"
    elif limit and len(findings) > limit:
        result.dropped_reason = f"слишком много секретов в файле ({len(findings)} > {limit})"
    return result
