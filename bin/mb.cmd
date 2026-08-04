@echo off
rem Windows shim. Paths are relative to this file (%~dp0) -- an absolute engine
rem path here only ever works on one machine (issue #15).
rem Prefer the generated console script, and never `python -m singularity`: `-m`
rem puts the CWD on sys.path, where a `singularity\` directory shadows the
rem installed package (issue #16).
if exist "%~dp0..\.venv\Scripts\mb.exe" (
  "%~dp0..\.venv\Scripts\mb.exe" %*
) else (
  "%~dp0..\.venv\Scripts\python.exe" "%~dp0_launch.py" mb singularity.cli:main %*
)
