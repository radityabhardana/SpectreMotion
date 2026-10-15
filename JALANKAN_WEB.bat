@echo off
REM ============================================================
REM  SpectreMotion - Jalankan Web Dashboard (Klik 2x file ini)
REM  Otomatis menjalankan web server + worker + membuka browser.
REM ============================================================
cd /d "%~dp0"

REM Tambahkan ffmpeg ke PATH jika belum ada
for /f "delims=" %%i in ('where ffmpeg 2^>nul') do set "HAVE_FFMPEG=1"
if not defined HAVE_FFMPEG (
  if exist "%LOCALAPPDATA%\Microsoft\WinGet\Packages\Gyan.FFmpeg_Microsoft.Winget.Source_8wekyb3d8bbwe\ffmpeg-9.0.2-full_build\bin\ffmpeg.exe" (
    set "PATH=%LOCALAPPDATA%\Microsoft\WinGet\Packages\Gyan.FFmpeg_Microsoft.Winget.Source_8wekyb3d8bbwe\ffmpeg-9.0.2-full_build\bin;%PATH%"
  )
)
for /f "delims=" %%i in ('where ffmpeg 2^>nul') do set "HAVE_FFMPEG=1"
if not defined HAVE_FFMPEG (
  echo [!] ffmpeg tidak ditemukan di PATH. Install dulu: winget install --id Gyan.FFmpeg -e
  pause
  exit /b 1
)

if not exist ".venv\Scripts\python.exe" (
  echo [!] virtualenv .venv tidak ditemukan. Jalankan dulu: python -m venv .venv
  pause
  exit /b 1
)

if not exist ".env" (
  echo [!] File .env tidak ada. Copy dari .env.example
  pause
  exit /b 1
)

REM Model whisper lokal -> offline
set HF_HUB_OFFLINE=1
REM Aktifkan background worker otomatis
set EMBEDDED_WORKER=1

REM ---- Bersihkan server lama yang masih pegang port 8080 ----
echo Membersihkan server lama (jika ada)...
powershell -NoProfile -Command "Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -like '*uvicorn*app.web*' -or $_.CommandLine -like '*app.bot*' } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }" >nul 2>&1
timeout /t 2 >nul

REM ---- Pilih port yang bebas (8080, kalau dipakai -> 8081, lalu 8082) ----
set "PORT=8080"
powershell -NoProfile -Command "if (Get-NetTCPConnection -LocalPort 8080 -State Listen -ErrorAction SilentlyContinue) { exit 1 } else { exit 0 }"
if errorlevel 1 (
  set "PORT=8081"
  powershell -NoProfile -Command "if (Get-NetTCPConnection -LocalPort 8081 -State Listen -ErrorAction SilentlyContinue) { exit 1 } else { exit 0 }"
  if errorlevel 1 set "PORT=8082"
)
echo Menggunakan port: %PORT%

echo ============================================================
echo   SpectreMotion Web Dashboard
echo   Buka http://127.0.0.1:%PORT% di browser
echo   Tekan Ctrl+C untuk menghentikan server.
echo ============================================================
echo.

REM Buka browser otomatis di background tanpa memunculkan jendela powershell baru (2 detik)
start "" /B powershell -WindowStyle Hidden -NoProfile -Command "Start-Sleep -Seconds 2; Start-Process 'http://127.0.0.1:%PORT%'"

REM Jalankan FastAPI Uvicorn Server
".venv\Scripts\python.exe" -m uvicorn app.web:app --host 127.0.0.1 --port %PORT% --reload

echo.
echo Server web berhenti.
pause
