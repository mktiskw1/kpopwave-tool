@echo off
cd /d "%~dp0"
if exist "venv\Scripts\pythonw.exe" (
    venv\Scripts\pythonw.exe launcher.py
) else (
    pythonw launcher.py
)
