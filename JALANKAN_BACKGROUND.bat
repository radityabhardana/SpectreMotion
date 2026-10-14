@echo off
REM ============================================================
REM  SpectreMotion - Jalankan bot di background (tanpa jendela)
REM  Log ditulis ke data\bot_live.log
REM  Untuk stop: jalankan STOP_BOT.bat
REM ============================================================
cd /d "%~dp0"

for /f "delims=" %%i in ('where ffmpeg 2^>nul') do set "HAVE_FFMPEG=1"
if not defined HAVE_FFMPEG if exist "%LOCALAPPDATA%\Microsoft\WinGet\Packages\Gyan.FFmpeg_Microsoft.Winget.Source_8wekyb3d8bbwe\ffmpeg-9.0.2-full_build\bin\ffmpeg.exe" (
  set "PATH=%LOCALAPPDATA%\Microsoft\WinGet\Packages\Gyan.FFmpeg_Microsoft.Winget.Source_8wekyb3d8bbwe\ffmpeg-9.0.2-full_build\bin;%PATH%"
)

set HF_HUB_OFFLINE=1

if not exist "data" mkdir data
echo Menjalankan bot di background...
start "SpectreMotion Bot" /min cmd /c "".venv\Scripts\python.exe" -u -m app.bot > "data\bot_live.log" 2>&1"
echo Bot berjalan. Log: data\bot_live.log
echo. 
echo Tunggu 5 detik lalu cek Telegram (@spectremotionbot).
timeout /t 5 >nul
