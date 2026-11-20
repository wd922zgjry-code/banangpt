@echo off
rem BananGPT: локальный запуск на Windows. Двойной клик или из cmd. Остановить: Ctrl+C.
chcp 65001 >nul
setlocal
cd /d "%~dp0"
set PYTHONUTF8=1
set PYTHONUNBUFFERED=1
set USE_POLLING=1

if not exist ".env" (
    echo [!] Нет файла .env. Скопируй .env.example в .env и заполни токен и куки.
    if not defined NOPAUSE pause
    exit /b 2
)

rem --- первый запуск: venv + зависимости (повторно только если изменился requirements.txt) ---
if not exist "venv\Scripts\python.exe" (
    echo Создаю venv...
    py -3 -m venv venv 2>nul || python -m venv venv
    if errorlevel 1 (
        echo [!] Не нашёл Python 3.10+. Установи с python.org и поставь галку "Add python to PATH".
        if not defined NOPAUSE pause
        exit /b 2
    )
)
set "STAMP=venv\.req_stamp"
fc /b requirements.txt "%STAMP%" >nul 2>&1
if errorlevel 1 (
    echo Ставлю зависимости...
    "venv\Scripts\python.exe" -m pip install --upgrade pip >nul
    "venv\Scripts\python.exe" -m pip install -r requirements.txt
    if errorlevel 1 (
        echo [!] Не удалось установить зависимости. Проверь интернет и запусти снова.
        if not defined NOPAUSE pause
        exit /b 2
    )
    copy /y requirements.txt "%STAMP%" >nul
)

rem --- запуск с перезапуском при падении (.env и бэкап базы читает/делает сам бот) ---
:loop
echo [%date% %time%] Запуск BananGPT...
"venv\Scripts\python.exe" bot.py
set "CODE=%errorlevel%"
if "%CODE%"=="0" (
    echo Бот остановлен.
    goto :end
)
if "%CODE%"=="2" (
    echo [!] Ошибка настройки - см. сообщение выше и файл bot.log. Перезапуск не нужен.
    goto :end
)
if "%CODE%"=="3" (
    echo [!] Бот уже запущен на этом ПК. Закрой другое окно и запусти снова.
    goto :end
)
echo [%date% %time%] Бот упал с кодом %CODE%. Перезапуск через 10 секунд (Ctrl+C - отмена)...
timeout /t 10 /nobreak >nul
goto :loop

:end
if not defined NOPAUSE pause
endlocal
