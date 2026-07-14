"""imgqa-ml — the heavy side of `imgqa cutout` (OD-1 isolation).

Lives in the engine REPO (versioned together with the dispatch code in
singularity/imgqa/external.py) but is installed ONLY into the isolated ML venv
(default D:\\venvs\\imgqa-ml). The engine talks to it via subprocess with a
typed JSON payload; see singularity/imgqa/CONTRACT.md §4 (cutout).
"""
__version__ = "0.1.0"
