@echo off
chcp 65001 > nul
setlocal enabledelayedexpansion

set TOOL_DIR=C:\Users\mktis\kpopwave-tool
set PYTHON_EXE=%TOOL_DIR%\venv\Scripts\python.exe
set DB_PATH=%TOOL_DIR%\instance\rock_metal.db
set TMP_PY=%TEMP%\kpopwave_cf.py
set CF_EXE=cloudflared

echo.
echo ============================================================
echo  Cloudflare Tunnel start
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

taskkill /f /im cloudflared.exe > nul 2>&1

echo [2/3] Writing runner script...

> "%TMP_PY%" echo import subprocess, re, sqlite3, sys, time
>> "%TMP_PY%" echo DB = sys.argv[1]
>> "%TMP_PY%" echo EXE = sys.argv[2]
>> "%TMP_PY%" echo proc = subprocess.Popen(
>> "%TMP_PY%" echo     [EXE, 'tunnel', '--url', 'http://localhost:5000'],
>> "%TMP_PY%" echo     stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
>> "%TMP_PY%" echo     creationflags=0x08000000,
>> "%TMP_PY%" echo )
>> "%TMP_PY%" echo print('[cloudflared] starting tunnel...')
>> "%TMP_PY%" echo url = None
>> "%TMP_PY%" echo pat = re.compile(r'https://[a-z0-9-]+\.trycloudflare\.com')
>> "%TMP_PY%" echo t0 = time.time()
>> "%TMP_PY%" echo for line in proc.stdout:
>> "%TMP_PY%" echo     sys.stdout.write(line)
>> "%TMP_PY%" echo     sys.stdout.flush()
>> "%TMP_PY%" echo     m = pat.search(line)
>> "%TMP_PY%" echo     if m:
>> "%TMP_PY%" echo         url = m.group(0)
>> "%TMP_PY%" echo         break
>> "%TMP_PY%" echo     if time.time() - t0 ^> 30:
>> "%TMP_PY%" echo         break
>> "%TMP_PY%" echo if not url:
>> "%TMP_PY%" echo     print('[ERROR] URL not found. Timeout or cloudflared error.')
>> "%TMP_PY%" echo     sys.exit(1)
>> "%TMP_PY%" echo db = sqlite3.connect(DB)
>> "%TMP_PY%" echo c = db.cursor()
>> "%TMP_PY%" echo c.execute('UPDATE settings SET value=? WHERE key=?', (url, 'app_base_url'))
>> "%TMP_PY%" echo if c.rowcount == 0:
>> "%TMP_PY%" echo     c.execute('INSERT INTO settings (key, value) VALUES (?, ?)', ('app_base_url', url))
>> "%TMP_PY%" echo db.commit()
>> "%TMP_PY%" echo db.close()
>> "%TMP_PY%" echo print()
>> "%TMP_PY%" echo print('============================================================')
>> "%TMP_PY%" echo print('[OK] app_base_url =', url)
>> "%TMP_PY%" echo print('     Open admin  : http://localhost:5000')
>> "%TMP_PY%" echo print('     Close this window to stop the tunnel.')
>> "%TMP_PY%" echo print('============================================================')
>> "%TMP_PY%" echo for line in proc.stdout:
>> "%TMP_PY%" echo     sys.stdout.write(line)
>> "%TMP_PY%" echo     sys.stdout.flush()

echo [3/3] Launching Cloudflare Tunnel...
echo.
"%PYTHON_EXE%" "%TMP_PY%" "%DB_PATH%" "!CF_EXE!"
pause
