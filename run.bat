@echo off
setlocal
cd /d "%~dp0"

if exist ".venv\Scripts\pythonw.exe" goto run

rem --- find a real Python (skip the Microsoft Store stub) ---
set "PY="
for /d %%D in ("%LOCALAPPDATA%\Programs\Python\Python3*") do if exist "%%D\python.exe" set "PY=%%D\python.exe"
if not defined PY for /d %%D in ("%ProgramFiles%\Python3*") do if exist "%%D\python.exe" set "PY=%%D\python.exe"
if not defined PY (
  py -3 -c "import sys" >nul 2>&1 && for /f "delims=" %%P in ('py -3 -c "import sys;print(sys.executable)"') do set "PY=%%P"
)
if not defined PY (
  echo Could not find Python 3. Install it from https://www.python.org/downloads/
  echo and tick "Add python.exe to PATH" plus "tcl/tk and IDLE".
  pause
  exit /b 1
)

echo Using %PY%
echo Creating virtual environment...
"%PY%" -m venv .venv || (echo venv creation failed & pause & exit /b 1)
".venv\Scripts\python.exe" -m pip install -q --upgrade pip
".venv\Scripts\python.exe" -m pip install -q -r requirements.txt || (echo pip install failed & pause & exit /b 1)

:run
".venv\Scripts\python.exe" adhoc_share.py
if errorlevel 1 pause
