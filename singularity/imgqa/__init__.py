"""imgqa — deterministic image-QA toolkit (WEBSITE-FACTORY WSF-01).

Design contract (frozen in WSF-01b before subcommands are implemented):
- every subcommand supports --json output carrying `schema_version`;
- exit codes: 0 = successful execution (transform/informational commands, or
  evaluative commands whose verdict lives in the JSON payload), 1 = reserved
  for evaluative failure where the contract says so, 2 = tool error. Exit 2
  must NEVER be interpreted as "no defects";
- file-writing subcommands take an explicit --out root, write atomically and
  never in-place over the source tree.

Heavy ML (rembg/onnxruntime) is deliberately NOT a dependency of this package:
`cutout` dispatches to a separate venv (owner decision OD-1, 2026-07-14) so the
engine venv that hosts `mb` can never be broken by ML dependency resolution.
"""

__all__ = ["__version__"]

# Versioned independently of the engine release cadence; bumped when the CLI
# contract (schema_version / exit semantics / subcommand set) changes.
__version__ = "0.1.0.dev0"
