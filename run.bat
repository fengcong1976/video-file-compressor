rem Launch the GUI. Pure ASCII on purpose: cmd reads .bat as GBK and
rem non-ASCII paths turn into mojibake.
setlocal
cd /d "%~dp0"

set PY=python
if exist "venv\Scripts\python.exe" set PY=venv\Scripts\python.exe

%PY% main.py
if errorlevel 1 pause
endlocal
