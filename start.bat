@echo off
rem KALYPSO: запуск на синтетике (Windows). Первый запуск ставит окружение — 3-10 минут.
chcp 65001 >nul
setlocal
cd /d "%~dp0"

if exist ".venv\.kalypso-installed" goto run

set "PY="
where py >nul 2>nul && set "PY=py -3"
if not defined PY where python >nul 2>nul && set "PY=python"
if not defined PY goto nopython
%PY% -c "import sys; sys.exit(0 if sys.version_info >= (3, 12) else 1)" || goto oldpython
rem Встроенный PostgreSQL содержит глубокие пути: без «длинных путей» Windows папка проекта — до ~135 символов
%PY% -c "import os,sys,ctypes;f=ctypes.windll.ntdll.RtlAreLongPathsEnabled;f.restype=ctypes.c_ubyte;lp=f();sys.exit(1 if len(os.path.realpath(os.getcwd()))>135 and not lp else 0)" || goto longpath

echo [KALYPSO] Создаю окружение Python (.venv) и ставлю зависимости...
if not exist ".venv\Scripts\python.exe" %PY% -m venv .venv || goto fail
".venv\Scripts\python.exe" -m pip install --upgrade pip || goto fail
".venv\Scripts\python.exe" -m pip install -r requirements-dev.txt || goto fail
echo ok> ".venv\.kalypso-installed"

:run
if not defined PORT set "PORT=8765"
echo.
echo [KALYPSO] Запуск. Когда появится "Uvicorn running", откройте http://127.0.0.1:%PORT%
echo [KALYPSO] Первый запуск создаёт синтетическую когорту - несколько минут. Остановить - Ctrl+C.
echo.
".venv\Scripts\python.exe" -X utf8 -m kalypso.dev
goto end

:nopython
echo [KALYPSO] Не найден Python. Установите Python 3.12+ с python.org (отметьте "Add python.exe to PATH").
goto end
:oldpython
echo [KALYPSO] Нужен Python 3.12 или новее.
goto end
:longpath
echo [KALYPSO] Слишком длинный путь к папке проекта для Windows:
%PY% -c "import os;print('  '+os.path.realpath(os.getcwd()))"
echo [KALYPSO] Переложите папку ближе к корню диска, например C:\kalypso, и запустите start.bat снова.
goto end
:fail
echo [KALYPSO] Не удалось установить зависимости. Удалите папку .venv и запустите снова (нужен интернет).
:end
pause
