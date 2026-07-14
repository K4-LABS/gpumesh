"""Shared ANSI color and terminal detection utilities for gpumesh."""

from __future__ import annotations

import sys


_SUPPORTS_COLOR = hasattr(sys.stdout, "isatty") and sys.stdout.isatty()


def esc(code: str) -> str:
    return f"\033[{code}" if _SUPPORTS_COLOR else ""


def bold(text: str) -> str:
    return f"{esc('1m')}{text}{esc('0m')}"


def cyan(text: str) -> str:
    return f"{esc('36m')}{text}{esc('0m')}"


def green(text: str) -> str:
    return f"{esc('32m')}{text}{esc('0m')}"


def yellow(text: str) -> str:
    return f"{esc('33m')}{text}{esc('0m')}"


def red(text: str) -> str:
    return f"{esc('31m')}{text}{esc('0m')}"


def dim(text: str) -> str:
    return f"{esc('2m')}{text}{esc('0m')}"


def erase_line():
    if _SUPPORTS_COLOR:
        sys.stdout.write("\033[2K")


def move_up(n: int):
    if n > 0 and _SUPPORTS_COLOR:
        sys.stdout.write(f"\033[{n}A")


def move_down(n: int):
    if n > 0 and _SUPPORTS_COLOR:
        sys.stdout.write(f"\033[{n}B")


def clear_lines(n: int):
    if n <= 0 or not _SUPPORTS_COLOR:
        return
    sys.stdout.write(f"\033[{n}A")
    for _ in range(n):
        sys.stdout.write("\033[2K\033[1B")
    sys.stdout.write(f"\033[{n}A")


# ---------------------------------------------------------------------------
# Safe printing for Windows cp1252 terminals
# ---------------------------------------------------------------------------

_UNICODE_MAP = {
    "\u2500": "-", "\u2502": "|", "\u2550": "=", "\u2551": "||",
    "\u2713": "[OK]", "\u2717": "[FAIL]", "\u21bb": "[...]", "\u00b7": ".",
    "\u2588": "#", "\u2591": ".", "\u2593": "#",
    "\u2192": "->", "\u2190": "<-", "\u2191": "^", "\u2193": "v",
    "\U0001f680": "*", "\u26a1": "*", "\U0001f5a5\ufe0f": "", "\U0001f310": "", "\U0001f4e1": "",
    "\u2705": "[OK]", "\u274c": "[FAIL]", "\u26a0\ufe0f": "[!]",
}


_ANSI_RE = __import__("re").compile(r"\033\[[0-9;]*[A-Za-z]")


def _safe_str(text: str) -> str:
    """Replace Unicode chars with ASCII on Windows cp1252 terminals."""
    try:
        text.encode(sys.stdout.encoding or "utf-8")
        return text
    except (UnicodeEncodeError, LookupError):
        pass
    parts = _ANSI_RE.split(text)
    ansi_seqs = _ANSI_RE.findall(text)
    out = []
    for i, part in enumerate(parts):
        for uni, ascii_repl in _UNICODE_MAP.items():
            part = part.replace(uni, ascii_repl)
        try:
            part = part.encode(sys.stdout.encoding or "utf-8", errors="replace").decode(
                sys.stdout.encoding or "utf-8", errors="replace"
            )
        except Exception:
            pass
        out.append(part)
        if i < len(ansi_seqs):
            out.append(ansi_seqs[i])
    return "".join(out)


def safe_print(*args, **kwargs):
    """Print that never crashes on Windows cp1252 encoding."""
    sep = kwargs.pop("sep", " ")
    end = kwargs.pop("end", "\n")
    file = kwargs.pop("file", sys.stdout)
    text = sep.join(str(a) for a in args)
    text = _safe_str(text)
    try:
        file.write(text + end)
        file.flush()
    except UnicodeEncodeError:
        safe = text.encode("ascii", errors="replace").decode("ascii")
        file.write(safe + end)
        file.flush()
