@echo off
cd /d "%~dp0"
call :findpython
if not defined PYEXE goto nopython
echo Installing build tools (one time)...
"%PYEXE%" -m pip install -r requirements.txt pyinstaller
if errorlevel 1 goto failed
echo.
echo Building FCAC (a folder of files, not one self-extracting file: antivirus programs trust that more) ...
"%PYEXE%" -m PyInstaller --noconfirm --clean --onedir --noconsole --name FCAC --icon fcac.ico --hidden-import pystray._win32 --add-data "airport_names.json;." --collect-all SimConnect fcac.py
if errorlevel 1 goto failed
for /f "tokens=3 delims= " %%V in ('findstr /b /c:"VERSION = " fcac.py') do set "FCACVER=%%~V"
echo FCAC version: %FCACVER%
set "ISCC="
for %%P in ("%LOCALAPPDATA%\Programs\Inno Setup 6" "%ProgramFiles(x86)%\Inno Setup 6" "%ProgramFiles%\Inno Setup 6" "%ProgramFiles(x86)%\Inno Setup 7" "%ProgramFiles%\Inno Setup 7") do if not defined ISCC if exist "%%~P\ISCC.exe" set "ISCC=%%~P\ISCC.exe"
if not defined ISCC for /f "delims=" %%I in ('where ISCC.exe 2^>nul') do if not defined ISCC set "ISCC=%%I"
if not defined ISCC for %%K in ("HKCU\Software\Microsoft\Windows\CurrentVersion\Uninstall\Inno Setup 6_is1" "HKLM\Software\Microsoft\Windows\CurrentVersion\Uninstall\Inno Setup 6_is1" "HKLM\Software\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall\Inno Setup 6_is1") do if not defined ISCC for /f "tokens=2,*" %%A in ('reg query %%K /v InstallLocation 2^>nul ^| findstr InstallLocation') do if exist "%%B\ISCC.exe" set "ISCC=%%B\ISCC.exe"
if defined ISCC echo Inno Setup compiler: %ISCC%
if not defined ISCC goto noinno
echo.
echo Building the installer ...
"%ISCC%" /DMyVersion=%FCACVER% installer.iss
if errorlevel 1 goto failed
echo.
echo Done. Your installer is  %~dp0dist\FCAC-Setup-%FCACVER%.exe
echo.
echo.
"%PYEXE%" update_catalog.py %FCACVER% "%~dp0dist\FCAC-Setup-%FCACVER%.exe"
echo.
echo To publish: 1) create the GitHub release v%FCACVER% and attach the installer above,
echo             2) then upload the updated catalog.json in this folder to the fcac repository on GitHub.
start "" "%~dp0dist"
pause
exit /b 0

:noinno
echo.
echo Done. The program folder is  %~dp0dist\FCAC   (run FCAC.exe inside it)
echo To also make an installer, install Inno Setup 6 from https://jrsoftware.org/isdl.php and run this again.
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
