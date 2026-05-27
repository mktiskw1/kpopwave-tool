@echo off
chcp 65001 > nul
setlocal

set NGROK_DIR=C:\Users\mktis\kpopwave-tool\ngrok
set NGROK_ZIP=%TEMP%\ngrok-windows-amd64.zip
set NGROK_DL_URL=https://bin.equinox.io/c/bNyj1mQVY4c/ngrok-v3-stable-windows-amd64.zip
set AUTHTOKEN=3EJ8sfbJT6le8n91TkmVSxg5Rt8_2qVo33ZJJhnK835WGU52J

echo.
echo ============================================================
echo  ngrok setup
echo ============================================================
echo.

:: [1] download
echo [1/4] Downloading ngrok...
powershell -NoProfile -Command "Invoke-WebRequest -Uri '%NGROK_DL_URL%' -OutFile '%NGROK_ZIP%' -UseBasicParsing"
if errorlevel 1 (
    echo.
    echo [ERROR] Download failed. Check internet connection.
    pause
    exit /b 1
)
echo       Done.

:: [2] create destination folder
echo [2/4] Creating destination folder...
if not exist "%NGROK_DIR%" mkdir "%NGROK_DIR%"

:: [3] extract zip
echo [3/4] Extracting to %NGROK_DIR% ...
powershell -NoProfile -Command "Expand-Archive -Path '%NGROK_ZIP%' -DestinationPath '%NGROK_DIR%' -Force"
if errorlevel 1 (
    echo.
    echo [ERROR] Extraction failed.
    pause
    exit /b 1
)
del "%NGROK_ZIP%" > nul 2>&1
echo       Done.

:: [4] configure authtoken
echo [4/4] Configuring authtoken...
"%NGROK_DIR%\ngrok.exe" config add-authtoken %AUTHTOKEN%
if errorlevel 1 (
    echo.
    echo [ERROR] authtoken configuration failed.
    pause
    exit /b 1
)

echo.
echo ============================================================
echo  Setup complete!
echo  ngrok.exe : %NGROK_DIR%\ngrok.exe
echo  Next: run start_ngrok.bat to start the tunnel.
echo ============================================================
echo.
pause
