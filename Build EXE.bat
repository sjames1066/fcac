@echo off
cd /d "%~dp0"
call :findpython
if not defined PYEXE goto nopython
echo Installing build tools (one time)...
"%PYEXE%" -m pip install -r requirements.txt pyinstaller
if errorlevel 1 goto failed
echo.
echo Building FCAC.exe ...
"%PYEXE%" -m PyInstaller --noconfirm --clean --onefile --noconsole --name FCAC --icon fcac.ico --hidden-import pystray._win32 --add-data "airport_names.json;." --collect-all SimConnect fcac.py
if errorlevel 1 goto failed
echo.
echo Done. Your program is  %~dp0dist\FCAC.exe
start "" "%~dp0dist"
pause
exit /b 0

:failed
echo.
echo Something went wrong - see the message above.
pause
exit /b 1

:nopython
echo Could not find Python on this PC. Run "Setup FCAC.bat" first.
pause
exit /b 1

:findpython
set "PYEXE="
for /f "delims=" %%D in ('dir /b /ad /o-n "%LOCALAPPDATA%\Python\pythoncore-*" 2^>nul') do if not defined PYEXE if exist "%LOCALAPPDATA%\Python\%%D\python.exe" set "PYEXE=%LOCALAPPDATA%\Python\%%D\python.exe"
if not defined PYEXE for /f "delims=" %%D in ('dir /b /ad /o-n "%LOCALAPPDATA%\Programs\Python\Python3*" 2^>nul') do if not defined PYEXE if exist "%LOCALAPPDATA%\Programs\Python\%%D\python.exe" set "PYEXE=%LOCALAPPDATA%\Programs\Python\%%D\python.exe"
if not defined PYEXE py -c "import sys" >nul 2>nul && set "PYEXE=py"
if not defined PYEXE python -c "import sys" >nul 2>nul && set "PYEXE=python"
exit /b 0
