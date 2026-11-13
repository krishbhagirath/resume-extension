@echo off
REM Starts Resume Tailor quietly in the background every time you log in to Windows.
cd /d "%~dp0"
if not exist .venv\Scripts\pythonw.exe (
  echo Run setup.bat first.
  pause
  exit /b 1
)
set "STARTUP=%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup"
set "DIR=%~dp0"
> "%STARTUP%\ResumeTailor.vbs" (
  echo Set sh = CreateObject("WScript.Shell"^)
  echo sh.CurrentDirectory = "%DIR%"
  echo sh.Run """%DIR%.venv\Scripts\pythonw.exe"" ""%DIR%server\app.py""", 0, False
)
start "" wscript "%STARTUP%\ResumeTailor.vbs"
echo.
echo Done. Resume Tailor is starting now and will start by itself whenever you log in.
echo No window will show. Open http://127.0.0.1:8765 to use the web page.
echo To turn this off, run autostart-off.bat.
if /i not "%~1"=="quiet" pause
