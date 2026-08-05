"""Secret redaction at the moment of capture — before anything reaches disk.

The vault is a git repository. A key that lands in a note is not in a file you
can clean up; it is in history, on every machine the vault has ever reached.
That asymmetry is why this runs on the WRITE path and not as a later audit:
`mb curator` finding a secret next week is already too late.

Replace, do not refuse. An insight is worth keeping even when one token in it
has to go, and a capture that fails outright teaches the assistant to stop
capturing. Where a detector knows a non-secret type marker (`sk-`, `ghp_`,
`AKIA`), the marker survives and only the entropy is masked: a reader six months
later should be able to tell that an OpenAI key was here without being able to
use it.

Honest boundary: this catches KNOWN SHAPES. It is protection against accident —
a key pasted into a terminal, a token echoed by a command — not against an
attacker, and not against a credential that looks like prose. It is a filter,
never a guarantee, and anything written here must say so.

stdlib-only, no printing: the caller owns the notice (see `commands/memory.py`).
"""
from __future__ import annotations

import re
from typing import NamedTuple

MASK = "XXXXXXXX"

# Values a human already redacted, or a template placeholder. Masking these
# again would report a "finding" on every doc that talks about secrets — the
# fastest way to get a security check switched off is to make it cry wolf.
_PLACEHOLDER_RE = re.compile(
    r"^(?:[xX*.\-_]{3,}|<[^>]*>|\$\{[^}]*\}|\$[A-Z_]+|%[A-Za-z_]+%|"
    r"(?:your|my|some|the)[-_].*|(?:example|placeholder|dummy|fake|redacted|changeme|"
    r"test|sample|none|null|true|false)$)", re.IGNORECASE)

# A line that is visibly teaching rather than leaking. `mb learn "set
# api_key=<your key> in config"` is documentation, not a credential.
_TEACHING_RE = re.compile(r"(?i)\b(example|placeholder|dummy|fake|e\.g\.|for instance|"
                          r"как\s+пример|например|шаблон)\b")


class Finding(NamedTuple):
    kind: str          # detector name, safe to print
    line: int          # 1-based line in the ORIGINAL text
    hint: str          # short, non-secret context (never the value)


class Redaction(NamedTuple):
    text: str
    findings: list[Finding]

    @property
    def changed(self) -> bool:
        return bool(self.findings)


def _looks_like_credential(value: str) -> bool:
    """Heuristic for the assignment detector only, where the shape is generic.

    Requires mixed character classes: a real token carries entropy, while the
    values that trip this detector in prose (`password: unchanged`,
    `token: rotated`) are plain words. Paths and URLs are excluded outright —
    `api_key: config/keys.json` names a file, it is not one.
    """
    if len(value) < 12:
        return False
    if _PLACEHOLDER_RE.match(value):
        return False
    if "/" in value or "\\" in value or value.startswith("http"):
        return False
    has_digit = any(c.isdigit() for c in value)
    has_alpha = any(c.isalpha() for c in value)
    has_upper = any(c.isupper() for c in value)
    if not (has_digit and has_alpha):
        return False
    # A lowercase-only word with digits ("release2024notes") is prose more often
    # than not; a real token almost always mixes case or uses -_ separators.
    return has_upper or "-" in value or "_" in value or value.isalnum() and has_digit


def _mask_keep_prefix(match: re.Match, prefix_group: int = 1) -> str:
    """Keep the detector's type marker, drop everything that carries entropy."""
    prefix = match.group(prefix_group) or ""
    return f"{prefix}{MASK}"


# Each rule: (kind, compiled pattern, replacement callable).
# Ordered most-specific first so a typed key is never claimed by the generic
# assignment rule, which would lose the type marker in the report.
_RULES: list[tuple[str, re.Pattern, object]] = [
    ("private-key-block",
     re.compile(r"-----BEGIN (?P<t>[A-Z ]*?)PRIVATE KEY-----.*?-----END [A-Z ]*?PRIVATE KEY-----",
                re.DOTALL),
     lambda m: f"-----BEGIN {m.group('t')}PRIVATE KEY-----{MASK}"
               f"-----END {m.group('t')}PRIVATE KEY-----"),

    ("anthropic-key",
     re.compile(r"\b(sk-ant-)[A-Za-z0-9_\-]{16,}"),
     _mask_keep_prefix),

    ("openai-key",
     re.compile(r"\b(sk-(?:proj-)?)[A-Za-z0-9_\-]{20,}"),
     _mask_keep_prefix),

    ("github-token",
     re.compile(r"\b(gh[pousr]_)[A-Za-z0-9]{30,}"),
     _mask_keep_prefix),

    ("slack-token",
     re.compile(r"\b(xox[baprse]-)[A-Za-z0-9\-]{10,}"),
     _mask_keep_prefix),

    ("google-api-key",
     re.compile(r"\b(AIza)[0-9A-Za-z_\-]{35}\b"),
     _mask_keep_prefix),

    ("aws-access-key",
     re.compile(r"\b(A(?:KIA|SIA|ROA|IDA|GPA|NPA|NVA|PKA))[0-9A-Z]{16}\b"),
     _mask_keep_prefix),

    ("url-credentials",
     re.compile(r"\b([a-zA-Z][a-zA-Z0-9+.\-]*://[^\s:/@]+:)[^\s@/]+(@)"),
     lambda m: f"{m.group(1)}{MASK}{m.group(2)}"),

    ("bearer-token",
     re.compile(r"\b(Bearer\s+)[A-Za-z0-9\-._~+/]{20,}=*", re.IGNORECASE),
     _mask_keep_prefix),
]

# Deliberately last and deliberately guarded: the only rule whose shape is
# generic enough to fire on prose.
_ASSIGNMENT_RE = re.compile(
    r"(?i)\b(api[_\-]?key|secret[_\-]?key|access[_\-]?token|auth[_\-]?token|"
    r"client[_\-]?secret|token|secret|password|passwd|pwd)\b"
    r"(\s*[:=]\s*[\"']?)([^\s\"',;]{12,})")


def redact(text: str) -> Redaction:
    """Mask known secret shapes in `text`.

    Returns the rewritten text and one Finding per replacement. Findings never
    carry the value — a report that prints the secret it removed has moved the
    leak, not closed it.

    Detection and replacement are two passes over the SAME original string.
    One pass would be cheaper and would lie: the private-key rule collapses a
    multi-line block, so every line number found after it would be reported
    against text that no longer exists. A security notice pointing at the wrong
    line is worse than none — it sends you looking in the wrong place.
    """
    if not text:
        return Redaction(text, [])

    def line_of(offset: int) -> int:
        return text.count("\n", 0, offset) + 1

    findings: list[Finding] = []
    # Rules overlap by design: `sk-ant-…` matches the Anthropic rule and the
    # broader OpenAI one. Pass 2 applies them in order, so the first rule
    # consumes the span and the second never fires — detection has to model the
    # same thing, or the notice reports a redaction that did not happen.
    claimed: list[tuple[int, int]] = []

    def _free(span: tuple[int, int]) -> bool:
        return not any(span[0] < end and start < span[1] for start, end in claimed)

    # --- pass 1: detect, against the original only -------------------------
    for kind, pattern, _repl in _RULES:
        for m in pattern.finditer(text):
            if not _free(m.span()):
                continue
            claimed.append(m.span())
            findings.append(Finding(kind, line_of(m.start()), _hint_for(m)))

    for m in _ASSIGNMENT_RE.finditer(text):
        if not _looks_like_credential(m.group(3)) or not _free(m.span()):
            continue
        line_no = line_of(m.start())
        if _TEACHING_RE.search(_line_text(text, line_no)):
            continue
        claimed.append(m.span())
        findings.append(Finding("assigned-secret", line_no, f"{m.group(1)}="))

    if not findings:
        return Redaction(text, [])

    # --- pass 2: replace ---------------------------------------------------
    out = text
    for _kind, pattern, repl in _RULES:
        out = pattern.sub(repl, out)

    def _sub_assign(m: re.Match) -> str:
        name, sep, value = m.group(1), m.group(2), m.group(3)
        if not _looks_like_credential(value):
            return m.group(0)
        if _TEACHING_RE.search(_line_text(out, out.count("\n", 0, m.start()) + 1)):
            return m.group(0)
        return f"{name}{sep}{MASK}"

    out = _ASSIGNMENT_RE.sub(_sub_assign, out)

    findings.sort(key=lambda f: (f.line, f.kind))
    return Redaction(out, findings)


def _hint_for(match: re.Match) -> str:
    """Non-secret context for the report: the type marker only, never the body."""
    groups = match.groups()
    prefix = groups[0] if groups else ""
    if not isinstance(prefix, str) or not prefix:
        return ""
    return prefix.strip()[:12]


def _line_text(source: str, line_no: int) -> str:
    lines = source.splitlines()
    return lines[line_no - 1] if 0 < line_no <= len(lines) else ""


def format_notice(findings: list[Finding]) -> list[str]:
    """Lines for the caller to print. Presentation owns the stream, not us."""
    if not findings:
        return []
    kinds: dict[str, int] = {}
    for f in findings:
        kinds[f.kind] = kinds.get(f.kind, 0) + 1
    summary = ", ".join(f"{k}×{n}" if n > 1 else k for k, n in sorted(kinds.items()))
    out = [f"redacted {len(findings)} secret-shaped value(s) before writing: {summary}"]
    for f in findings[:5]:
        where = f" ({f.hint})" if f.hint else ""
        out.append(f"   line {f.line}: {f.kind}{where} -> {MASK}")
    if len(findings) > 5:
        out.append(f"   ... and {len(findings) - 5} more")
    return out
