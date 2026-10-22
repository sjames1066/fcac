@echo off
cd /d "%~dp0"
call :findpython
if not defined PYEXE goto nopython
if /i "%PYEXE%"=="py" goto usepy
if /i "%PYEXE%"=="python" goto usepython
set "PYW=%PYEXE:python.exe=pythonw.exe%"
start "" "%PYW%" "%~dp0fcac.py"
exit /b 0

:usepy
start "" pyw "%~dp0fcac.py"
exit /b 0

:usepython
start "" pythonw "%~dp0fcac.py"
exit /b 0

:nopython
echo Could not find Python on this PC.
echo Open the Python window, type:  import sys; print(sys.executable)
echo and send Claude the path it prints.
pause
exit /b 1

:findpython
set "PYEXE="
for /f "delims=" %%D in ('dir /b /ad /o-n "%LOCALAPPDATA%\Python\pythoncore-*" 2^>nul') do if not defined PYEXE if exist "%LOCALAPPDATA%\Python\%%D\python.exe" set "PYEXE=%LOCALAPPDATA%\Python\%%D\python.exe"
if not defined PYEXE for /f "delims=" %%D in ('dir /b /ad /o-n "%LOCALAPPDATA%\Programs\Python\Python3*" 2^>nul') do if not defined PYEXE if exist "%LOCALAPPDATA%\Programs\Python\%%D\python.exe" set "PYEXE=%LOCALAPPDATA%\Programs\Python\%%D\python.exe"
if not defined PYEXE py -c "import sys" >nul 2>nul && set "PYEXE=py"
if not defined PYEXE python -c "import sys" >nul 2>nul && set "PYEXE=python"
exit /b 0
