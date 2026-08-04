"""Finding and choosing a typeface — by measurement, not by name.

The metric that matters for reel copy is **how big a face renders at a FIXED
line width**, because the width is fixed by the safe area and the reader only
experiences the resulting optical size. Measured examples (vault:
`learnings/active/AUTO-REELS.md`):

  club copy, 912 px limit   Bebas Neue 90 · Bahnschrift 85 · Anton 75 · Archivo Black 45
  romantic copy, 900 px     Cormorant 87 · EB Garamond 79 · Playfair 72

Anton *feels* heavier and more powerful than Bebas, and loses 14 px of height
on a long line. Taste is not a reliable guide here; the number is.

Downloads go through `curl --ssl-no-revoke` because this machine sits behind a
TLS-intercepting proxy (vault: `howto/TLS-прокси на этой машине`), and
`Invoke-WebRequest` fails on a prompt rather than on the network.
"""
from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path
from typing import Any, Iterable

GOOGLE_RAW = "https://github.com/google/fonts/raw/main/ofl"

# Curated by register. Every entry is OFL (free for any use, including
# commercial) — the licence question is settled before the aesthetic one.
CATALOG: dict[str, list[dict[str, str]]] = {
    "display-condensed": [                     # club / bold promo / poster
        {"name": "Bebas Neue", "family": "bebasneue", "file": "BebasNeue-Regular.ttf"},
        {"name": "Anton", "family": "anton", "file": "Anton-Regular.ttf"},
        {"name": "Oswald", "family": "oswald", "file": "Oswald%5Bwght%5D.ttf"},
        {"name": "Archivo Black", "family": "archivoblack", "file": "ArchivoBlack-Regular.ttf"},
        {"name": "Teko", "family": "teko", "file": "Teko%5Bwght%5D.ttf"},
    ],
    "serif-editorial": [                       # love story / cinematic titles
        {"name": "Cormorant Garamond", "family": "cormorantgaramond",
         "file": "CormorantGaramond%5Bwght%5D.ttf",
         "italic": "CormorantGaramond-Italic%5Bwght%5D.ttf"},
        {"name": "Playfair Display", "family": "playfairdisplay",
         "file": "PlayfairDisplay%5Bwght%5D.ttf",
         "italic": "PlayfairDisplay-Italic%5Bwght%5D.ttf"},
        {"name": "EB Garamond", "family": "ebgaramond",
         "file": "EBGaramond%5Bwght%5D.ttf",
         "italic": "EBGaramond-Italic%5Bwght%5D.ttf"},
        {"name": "Libre Baskerville", "family": "librebaskerville",
         "file": "LibreBaskerville-Regular.ttf",
         "italic": "LibreBaskerville-Italic.ttf"},
    ],
    "blackletter-gothic": [                    # gothic / Victorian / occult
        {"name": "UnifrakturMaguntia", "family": "unifrakturmaguntia",
         "file": "UnifrakturMaguntia-Regular.ttf"},
        {"name": "Pirata One", "family": "pirataone", "file": "PirataOne-Regular.ttf"},
        {"name": "IM Fell English", "family": "imfellenglish",
         "file": "IMFeENrm28P.ttf", "italic": "IMFeENit28P.ttf"},
        {"name": "IM Fell English SC", "family": "imfellenglishsc",
         "file": "IMFeENsc28P.ttf"},
        {"name": "Cinzel", "family": "cinzel", "file": "Cinzel%5Bwght%5D.ttf"},
        {"name": "Cinzel Decorative", "family": "cinzeldecorative",
         "file": "CinzelDecorative-Regular.ttf"},
    ],
    "sans-neutral": [                          # captions, lower thirds, UI-ish
        {"name": "Inter", "family": "inter", "file": "Inter%5Bopsz,wght%5D.ttf"},
        {"name": "Manrope", "family": "manrope", "file": "Manrope%5Bwght%5D.ttf"},
    ],
}

SYSTEM_FALLBACK = {                             # always present on Windows
    "display-condensed": r"C:\Windows\Fonts\bahnschrift.ttf",
    "sans-neutral": r"C:\Windows\Fonts\segoeui.ttf",
    "serif-editorial": r"C:\Windows\Fonts\georgia.ttf",
}

EMOJI_FONT = r"C:\Windows\Fonts\seguiemj.ttf"


def fetch(entry: dict[str, str], dest: str | Path, *, italic: bool = False) -> Path:
    """Download one catalog entry. Returns the local path (idempotent)."""
    dest = Path(dest)
    dest.mkdir(parents=True, exist_ok=True)
    remote = entry.get("italic") if italic else entry["file"]
    if not remote:
        raise ValueError(f"{entry['name']}: no italic in catalog")
    local = dest / remote.replace("%5B", "[").replace("%5D", "]").replace("%2C", ",")
    if local.exists() and local.stat().st_size > 20_000:
        return local
    url = f"{GOOGLE_RAW}/{entry['family']}/{remote}"
    curl = shutil.which("curl") or shutil.which("curl.exe")
    if not curl:
        raise RuntimeError("curl not available for font download")
    subprocess.run([curl, "--ssl-no-revoke", "-L", "-s", "-o", str(local), url],
                   check=True)
    if not local.exists() or local.stat().st_size < 20_000:
        raise RuntimeError(f"download failed or truncated: {url}")
    return local


def fetch_register(register: str, dest: str | Path) -> list[dict[str, Any]]:
    out = []
    for entry in CATALOG.get(register, []):
        rec: dict[str, Any] = {"name": entry["name"]}
        try:
            rec["roman"] = str(fetch(entry, dest))
            if entry.get("italic"):
                rec["italic"] = str(fetch(entry, dest, italic=True))
        except Exception as exc:
            rec["error"] = str(exc)
        out.append(rec)
    return out


def _load(path: str, size: int, weight: int | None):
    from PIL import ImageFont
    f = ImageFont.truetype(path, size)
    if weight is not None:
        try:
            f.set_variation_by_axes([weight])
        except Exception:
            pass
    return f


def line_width(text: str, font, track: float = 0.0) -> float:
    if not text:
        return 0.0
    return sum(font.getlength(c) + track for c in text) - track


def fit_size(path: str, lines: Iterable[str], max_w: int, *,
             weight: int | None = None, track: float = 0.0,
             hi: int = 160, lo: int = 20) -> int:
    """Largest point size at which EVERY line fits `max_w`.

    One size for the whole reel — fitting each line separately breaks the
    typographic system and reads as a mistake (vault: `Субтитры для
    вертикального видео`)."""
    lines = [ln for ln in lines if ln]
    size = hi
    while size > lo:
        f = _load(path, size, weight)
        if all(line_width(ln, f, track) <= max_w for ln in lines):
            return size
        size -= 1
    return lo


def measure(candidates: list[dict[str, Any]], lines: list[str], max_w: int, *,
            weight: int | None = None, track: float = 0.0) -> list[dict[str, Any]]:
    """Rank faces by the size each achieves at the same line width."""
    rows = []
    for c in candidates:
        path = c.get("roman") or c.get("path")
        if not path or not Path(path).exists():
            rows.append({"name": c.get("name", path), "error": c.get("error", "missing")})
            continue
        try:
            size = fit_size(path, lines, max_w, weight=weight, track=track)
            f = _load(path, size, weight)
            rows.append({"name": c.get("name", Path(path).stem), "path": path,
                         "size": size,
                         "widest": round(max(line_width(ln, f, track) for ln in lines if ln))})
        except Exception as exc:
            rows.append({"name": c.get("name", path), "error": str(exc)})
    rows.sort(key=lambda r: (-(r.get("size") or 0), r["name"]))
    return rows


def system_fonts() -> list[str]:
    root = Path(os.environ.get("WINDIR", r"C:\Windows")) / "Fonts"
    if not root.is_dir():
        return []
    return sorted(str(p) for p in root.glob("*.ttf"))
