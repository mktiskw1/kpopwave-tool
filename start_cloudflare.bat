@echo off
chcp 65001 > nul
setlocal enabledelayedexpansion

set TOOL_DIR=C:\Users\mktis\kpopwave-tool
set PYTHON_EXE=%TOOL_DIR%\venv\Scripts\python.exe
set DB_PATH=%TOOL_DIR%\instance\rock_metal.db
set TMP_PY=%TEMP%\kpopwave_cf.py
set CF_EXE=cloudflared
set TUNNEL_NAME=contentwave
set CONFIG_PATH=%USERPROFILE%\.cloudflared\config.yml
set FIXED_URL=https://mktiskw.com

echo.
echo ============================================================
echo  Cloudflare Tunnel start (named tunnel: %TUNNEL_NAME%)
echo ============================================================
echo.

:: Check cloudflared availability, try WinGet path as fallback
cloudflared --version > nul 2>&1
if errorlevel 1 (
    set CF_EXE=%LOCALAPPDATA%\Microsoft\WinGet\Links\cloudflared.exe
    if not exist "!CF_EXE!" (
        echo [ERROR] cloudflared not found.
        echo         Run: winget install --id Cloudflare.cloudflared
        pause
        exit /b 1
    )
)
echo [1/3] cloudflared: !CF_EXE!

if not exist "%CONFIG_PATH%" (
    echo [ERROR] config.yml not found: %CONFIG_PATH%
    echo         Run: cloudflared tunnel login / tunnel create / tunnel route dns first.
    pause
    exit /b 1
)

tasklist /fi "imagename eq cloudflared.exe" 2>nul | find /i "cloudflared.exe" >nul
if not errorlevel 1 (
    echo [INFO] cloudflared.exe is already running - the tunnel is likely already active.
    echo        Public URL: %FIXED_URL%
    echo        Close the existing tunnel window first if you want to restart it.
    echo.
    pause
    exit /b 0
)

echo [2/3] Setting app_base_url = %FIXED_URL% ...

> "%TMP_PY%" echo import sqlite3, sys
>> "%TMP_PY%" echo DB = sys.argv[1]
>> "%TMP_PY%" echo URL = sys.argv[2]
>> "%TMP_PY%" echo db = sqlite3.connect(DB)
>> "%TMP_PY%" echo c = db.cursor()
>> "%TMP_PY%" echo c.execute('UPDATE settings SET value=? WHERE key=?', (URL, 'app_base_url'))
>> "%TMP_PY%" echo if c.rowcount == 0:
>> "%TMP_PY%" echo     c.execute('INSERT INTO settings (key, value) VALUES (?, ?)', ('app_base_url', URL))
>> "%TMP_PY%" echo db.commit()
>> "%TMP_PY%" echo db.close()
>> "%TMP_PY%" echo print('[OK] app_base_url =', URL)

"%PYTHON_EXE%" "%TMP_PY%" "%DB_PATH%" "%FIXED_URL%"

echo.
echo ============================================================
echo [3/3] Launching Cloudflare Tunnel (named: %TUNNEL_NAME%)...
echo      Public URL  : %FIXED_URL%
echo      Open admin  : http://localhost:5000
echo      Close this window to stop the tunnel.
echo ============================================================
echo.
"!CF_EXE!" tunnel --config "%CONFIG_PATH%" run %TUNNEL_NAME%

echo.
echo ============================================================
echo [STOPPED] Cloudflare Tunnel process has exited (exit code %ERRORLEVEL%).
echo           This window normally stays open forever while the tunnel
echo           is active - if you did not close it yourself, check the
echo           log above for the reason (crash, or killed by a second
echo           "start_cloudflare.bat" run).
echo ============================================================
pause
