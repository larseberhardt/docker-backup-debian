"""Small input helpers for the interactive ``create`` wizard."""

from __future__ import annotations

import getpass
from typing import Optional


def prompt(text: str, default: Optional[str] = None) -> str:
    suffix = " [%s]" % default if default else ""
    try:
        val = input("%s%s: " % (text, suffix)).strip()
    except EOFError:
        val = ""
    return val or (default or "")


def prompt_secret(text: str) -> str:
    try:
        return getpass.getpass("%s: " % text)
    except EOFError:
        return ""


def confirm(text: str, default: bool = True) -> bool:
    hint = "Y/n" if default else "y/N"
    try:
        val = input("%s [%s]: " % (text, hint)).strip().lower()
    except EOFError:
        return default
    if not val:
        return default
    return val in ("y", "yes")
