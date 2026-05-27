@echo off
chcp 65001 > nul
setlocal enabledelayedexpansion

set TOOL_DIR=C:\Users\mktis\kpopwave-tool
set NGROK_EXE=%TOOL_DIR%\ngrok\ngrok.exe
set PYTHON_EXE=%TOOL_DIR%\venv\Scripts\python.exe
set DB_PATH=%TOOL_DIR%\instance\rock_metal.db
set TMP_PY=%TEMP%\kpopwave_update_url.py

echo.
echo ============================================================
echo  ngrok tunnel start
echo ============================================================
echo.

:: ngrok.exe check
if not exist "%NGROK_EXE%" (
    echo [ERROR] ngrok.exe not found.
    echo         Please run setup_ngrok.bat first.
    echo         Expected: %NGROK_EXE%
    pause
    exit /b 1
)

:: stop existing ngrok process
taskkill /f /im ngrok.exe > nul 2>&1

:: [1] start ngrok in a new window
echo [1/3] Starting ngrok http 5000 ...
start "ngrok" "%NGROK_EXE%" http 5000

:: wait for ngrok API to become available (max 20 seconds)
echo       Waiting for ngrok to start...
set WAIT=0
:WAIT_LOOP
timeout /t 2 /nobreak > nul
set /a WAIT+=2
powershell -NoProfile -Command "try { Invoke-WebRequest -Uri 'http://localhost:4040/api/tunnels' -UseBasicParsing -TimeoutSec 2 | Out-Null; exit 0 } catch { exit 1 }" > nul 2>&1
if not errorlevel 1 goto GOT_API
if %WAIT% geq 20 (
    echo.
    echo [ERROR] ngrok did not start within 20 seconds.
    echo         Check the ngrok window for error messages.
    pause
    exit /b 1
)
goto WAIT_LOOP

:GOT_API
:: [2] get HTTPS tunnel URL
echo [2/3] Getting ngrok URL...
for /f "usebackq delims=" %%U in (`powershell -NoProfile -Command "(Invoke-WebRequest -Uri 'http://localhost:4040/api/tunnels' -UseBasicParsing | ConvertFrom-Json).tunnels | Where-Object {$_.proto -eq 'https'} | Select-Object -First 1 -ExpandProperty public_url"`) do set NGROK_URL=%%U

if "!NGROK_URL!"=="" (
    echo.
    echo [ERROR] Could not get HTTPS URL.
    echo         Check http://localhost:4040 for details.
    pause
    exit /b 1
)
echo       URL: !NGROK_URL!

:: [3] update DB app_base_url via Python temp script
echo [3/3] Updating DB app_base_url...

> "%TMP_PY%" echo import sqlite3, sys
>> "%TMP_PY%" echo db = sqlite3.connect(sys.argv[1])
>> "%TMP_PY%" echo c = db.cursor()
>> "%TMP_PY%" echo c.execute("UPDATE settings SET value=? WHERE key='app_base_url'", (sys.argv[2],))
>> "%TMP_PY%" echo if c.rowcount == 0:
>> "%TMP_PY%" echo     c.execute("INSERT INTO settings (key, value) VALUES ('app_base_url', ?)", (sys.argv[2],))
>> "%TMP_PY%" echo db.commit()
>> "%TMP_PY%" echo db.close()
>> "%TMP_PY%" echo print("DB updated: app_base_url =", sys.argv[2])

"%PYTHON_EXE%" "%TMP_PY%" "%DB_PATH%" "!NGROK_URL!"
if errorlevel 1 (
    echo [WARN] DB update failed. Please update app_base_url manually in settings.
) else (
    del "%TMP_PY%" > nul 2>&1
)

echo.
echo ============================================================
echo  ngrok URL  : !NGROK_URL!
echo  app_base_url set to !NGROK_URL!
echo.
echo  Open the app  : http://localhost:5000
echo  ngrok dashboard: http://localhost:4040
echo ============================================================
echo.
pause
