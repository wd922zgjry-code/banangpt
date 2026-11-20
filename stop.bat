@echo off
rem BananGPT: stop the bot started by run.bat / start_hidden.vbs (also the hidden one).
chcp 65001 >nul
powershell -NoProfile -Command "Get-CimInstance Win32_Process | Where-Object { $_.ProcessId -ne $PID -and ($_.CommandLine -match 'banangpt' -or $_.ExecutablePath -match 'banangpt') -and ($_.CommandLine -match 'run[.]bat' -or $_.CommandLine -match 'bot[.]py') } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force }"
echo Бот остановлен (если был запущен).
if not defined NOPAUSE pause
