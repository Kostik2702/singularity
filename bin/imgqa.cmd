@echo off
rem Windows shim -- see bin\mb.cmd for why this is relative (#15) and why it
rem never uses `python -m` (#16).
if exist "%~dp0..\.venv\Scripts\imgqa.exe" (
  "%~dp0..\.venv\Scripts\imgqa.exe" %*
) else (
  "%~dp0..\.venv\Scripts\python.exe" "%~dp0_launch.py" imgqa singularity.imgqa.cli:main %*
)
