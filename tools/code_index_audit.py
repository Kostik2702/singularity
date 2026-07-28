"""Privacy + composition audit of a built code index (C06).

Decision D5 has three layers. The first two — the extension allowlist and the
skip patterns — are HYPOTHESES about where secrets live. This is the third and
only one that checks a fact: it greps the index that actually got built.

Composition is reported alongside, because the C06 run found that the number
which matters in practice is not "did a secret leak" (none did) but "does one
repository dominate the corpus" (one did, at 83%, because it contained a
vendored engine SDK — invisible on fixtures, obvious here).

    python tools/code_index_audit.py [--json]

Exit code 1 if any secret marker is found, so it can gate a release.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from singularity.config import load_config          # noqa: E402
from singularity.search import indexer              # noqa: E402

# Deliberately broad and deliberately noisy-if-wrong: a false positive costs
# one look, a false negative costs a leaked credential in every backup.
MARKERS = {
    "aws_access_key": r"AKIA[0-9A-Z]{16}",
    "pem_private_key": r"-----BEGIN [A-Z ]*PRIVATE KEY-----",
    "password_literal": r"password\s*[=:]\s*['\"][^'\"]{3,}",
    "api_key_literal": r"api[_-]?key\s*[=:]\s*['\"][^'\"]{8,}",
    "secret_literal": r"secret\s*[=:]\s*['\"][^'\"]{8,}",
    "token_literal": r"token\s*[=:]\s*['\"][^'\"]{20,}",
    "bearer_token": r"Bearer\s+[A-Za-z0-9\-\._~\+/]{20,}",
    "openai_key": r"\bsk-[A-Za-z0-9]{20,}",
    "github_token": r"\bgh[pousr]_[A-Za-z0-9]{20,}",
}

# Extensions that must never appear: the allowlist excludes them by
# construction, so a hit here means the allowlist itself regressed.
FORBIDDEN_SUFFIXES = {".env", ".pem", ".key", ".p12", ".pfx", ".sql", ".dump",
                      ".pkcs12", ".jks", ".crt"}


def audit(cfg) -> dict:
    files = indexer.index_files(cfg.code_index_dir)
    if files is None:
        return {"error": "no code index — run `mb index rebuild --code` first"}
    meta_path = files[1]
    raw = meta_path.read_text(encoding="utf-8")
    meta = json.loads(raw)
    chunks = meta.get("chunks", [])

    findings = {}
    for name, pattern in MARKERS.items():
        hits = [m.group(0)[:24] for m in re.finditer(pattern, raw, re.IGNORECASE)]
        if hits:
            findings[name] = {"count": len(hits), "samples": hits[:5]}

    repos = Counter(str(c.get("repo") or "-") for c in chunks)
    total = max(len(chunks), 1)
    top_repo, top_n = repos.most_common(1)[0] if repos else ("-", 0)

    return {
        "chunks": len(chunks),
        "files": len(meta.get("file_meta") or {}),
        "metadata_mb": round(len(raw) / 1e6, 1),
        "chunk_schema": meta.get("chunk_schema"),
        "model": meta.get("model"),
        "secret_markers": findings,
        "secret_marker_total": sum(f["count"] for f in findings.values()),
        "forbidden_extension_files": sorted(
            {c["file"] for c in chunks
             if Path(c["file"]).suffix.lower() in FORBIDDEN_SUFFIXES}),
        "paths_naming_secrets": sorted(
            {c["file"] for c in chunks
             if re.search(r"secret|credential", c["file"], re.IGNORECASE)}),
        "by_extension": dict(Counter(Path(c["file"]).suffix
                                     for c in chunks).most_common()),
        "by_kind": dict(Counter(c.get("kind", "?") for c in chunks).most_common()),
        "repos": len(repos),
        "chunks_per_repo": dict(repos.most_common()),
        # the composition signal: one repository holding most of the corpus is
        # almost always a vendored dependency that slipped past SKIP_DIRS
        "largest_repo_share": round(top_n / total, 3),
        "largest_repo": top_repo,
        "fallback_share": round(
            sum(1 for c in chunks if c.get("kind") == "lines") / total, 3),
    }


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--json", action="store_true")
    args = p.parse_args(argv)

    report = audit(load_config())
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=1))
        return 1 if report.get("secret_marker_total") else 0
    if "error" in report:
        print(f"[audit] {report['error']}", file=sys.stderr)
        return 2

    print(f"chunks {report['chunks']} · files {report['files']} · "
          f"repos {report['repos']} · metadata {report['metadata_mb']} MB · "
          f"schema {report['chunk_schema']}")
    print(f"fallback share : {report['fallback_share']:.1%} "
          f"(low = the chunker found real symbol boundaries)")
    print(f"largest repo   : {report['largest_repo']} "
          f"{report['largest_repo_share']:.1%}"
          + ("   <- check for a vendored dependency"
             if report["largest_repo_share"] > 0.5 else ""))
    print("\nsecret markers:")
    for name in MARKERS:
        found = report["secret_markers"].get(name)
        print(f"  {name:<20} {found['count'] if found else 0}")
        for sample in (found or {}).get("samples", []):
            print(f"      {sample}…")
    print(f"  {'TOTAL':<20} {report['secret_marker_total']}")
    print(f"\nforbidden-extension files : {len(report['forbidden_extension_files'])}")
    print(f"paths naming secrets      : {len(report['paths_naming_secrets'])}")
    return 1 if report["secret_marker_total"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
