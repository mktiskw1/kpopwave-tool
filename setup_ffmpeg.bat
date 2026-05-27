@echo off
chcp 65001 > nul
setlocal

set FFMPEG_DIR=C:\Users\mktis\kpopwave-tool\ffmpeg
set FFMPEG_ZIP=%TEMP%\ffmpeg-release-essentials.zip
set FFMPEG_TMP=%TEMP%\ffmpeg_extract_tmp
set FFMPEG_DL_URL=https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip

echo.
echo ============================================================
echo  ffmpeg setup
echo ============================================================
echo.

:: [1] download
echo [1/4] Downloading ffmpeg...
powershell -NoProfile -Command "Invoke-WebRequest -Uri '%FFMPEG_DL_URL%' -OutFile '%FFMPEG_ZIP%' -UseBasicParsing"
if errorlevel 1 (
    echo.
    echo [ERROR] Download failed. Check internet connection.
    pause
    exit /b 1
)
echo       Done.

:: [2] create destination folder
echo [2/4] Creating destination folder...
if not exist "%FFMPEG_DIR%" mkdir "%FFMPEG_DIR%"
if exist "%FFMPEG_TMP%" rd /s /q "%FFMPEG_TMP%"
mkdir "%FFMPEG_TMP%"

:: [3] extract zip and move contents
echo [3/4] Extracting to %FFMPEG_DIR% ...
powershell -NoProfile -Command "Expand-Archive -Path '%FFMPEG_ZIP%' -DestinationPath '%FFMPEG_TMP%' -Force"
if errorlevel 1 (
    echo.
    echo [ERROR] Extraction failed.
    pause
    exit /b 1
)

:: gyan.dev zip には バージョン付きサブディレクトリが 1 つある（例: ffmpeg-7.1-essentials_build）
:: その中身を ffmpeg\ 直下に移動する
powershell -NoProfile -Command ^
  "$sub = Get-ChildItem '%FFMPEG_TMP%' -Directory | Select-Object -First 1;" ^
  "if ($sub) { Copy-Item (Join-Path $sub.FullName '*') '%FFMPEG_DIR%\' -Recurse -Force }" ^
  "else { Write-Error 'Subdirectory not found in zip'; exit 1 }"
if errorlevel 1 (
    echo.
    echo [ERROR] Failed to move ffmpeg files.
    pause
    exit /b 1
)

:: cleanup
del "%FFMPEG_ZIP%" > nul 2>&1
rd /s /q "%FFMPEG_TMP%" > nul 2>&1
echo       Done.

:: [4] verify
echo [4/4] Verifying installation...
if not exist "%FFMPEG_DIR%\bin\ffmpeg.exe" (
    echo.
    echo [ERROR] ffmpeg.exe not found at %FFMPEG_DIR%\bin\ffmpeg.exe
    echo         Zip structure may have changed. Check %FFMPEG_DIR% manually.
    pause
    exit /b 1
)

"%FFMPEG_DIR%\bin\ffmpeg.exe" -version 2>&1 | findstr "ffmpeg version"

echo.
echo ============================================================
echo  Setup complete!
echo  ffmpeg.exe : %FFMPEG_DIR%\bin\ffmpeg.exe
echo  Next: video_collector.py will use this ffmpeg automatically.
echo ============================================================
echo.
pause
