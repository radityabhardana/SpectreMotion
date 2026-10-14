@echo off
REM ============================================================
REM  SpectreMotion - Hentikan bot yang berjalan
REM ============================================================
cd /d "%~dp0"
echo Menghentikan SpectreMotion Bot...

powershell -NoProfile -Command "Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -like '*app.bot*' -and $_.Name -like 'python*' } | ForEach-Object { Write-Host ('  kill PID ' + $_.ProcessId); Stop-Process -Id $_.ProcessId -Force }"

echo Selesai.
timeout /t 3 >nul
