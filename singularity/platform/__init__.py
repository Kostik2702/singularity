"""Platform layer — ALL OS-specific code lives in this package.

Core code must never branch on the OS directly; it calls these
abstractions (cross-platform gate, see vault: 05_Cross_Platform_Strategy).
"""
from __future__ import annotations

import sys


def current_os() -> str:
    """Return 'windows' | 'macos' | 'linux'."""
    if sys.platform.startswith("win"):
        return "windows"
    if sys.platform == "darwin":
        return "macos"
    return "linux"
