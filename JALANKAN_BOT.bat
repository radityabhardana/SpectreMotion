@echo off
REM ============================================================
REM  SpectreMotion - Jalankan bot Telegram (klik 2x file ini)
REM  Bot nyala sampai jendela ini ditutup / tekan Ctrl+C.
REM ============================================================
cd /d "%~dp0"

REM Tambahkan ffmpeg ke PATH (winget install lokasi umum)
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
  echo [!] File .env tidak ada. Copy dari .env.example dan isi token bot.
  pause
  exit /b 1
)

REM Model whisper lokal -> jangan kontak jaringan (hindari hang).
set HF_HUB_OFFLINE=1

echo ==========================================
echo   SpectreMotion Bot
echo   Tutup jendela ini atau Ctrl+C untuk stop.
echo ==========================================
echo.
REM --seconds 0 = jalan TANPA BATAS (bot nyala sampai ditutup / Ctrl+C).
".venv\Scripts\python.exe" -u -m app.bot --seconds 0

echo.
echo Bot berhenti.
pause
