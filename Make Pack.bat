@echo off
cd /d "%~dp0"
call :findpython
if not defined PYEXE goto nopython
set "GITHUB=sjames1066/fcac-packs"
set "AIRLINE=BAW"
set /p "AIRLINE=Airline ICAO code to package [BAW]: "
set "ANAME=British Airways"
set /p "ANAME=Airline name shown in the tray menu [British Airways]: "
set "VER=1.0.0"
set /p "VER=Version [1.0.0]: "
echo.
"%PYEXE%" make_pack.py --icao %AIRLINE% --name "%ANAME%" --version %VER% --airline-dir "%~dp0Airlines\%AIRLINE%" --out "%~dp0dist" --github %GITHUB%
if errorlevel 1 goto failed
echo.
echo Opening the dist folder and the GitHub new-release page...
start "" "%~dp0dist"
start "" "https://github.com/%GITHUB%/releases/new"
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
