@echo off
REM Updates Resume Tailor after new files are copied in: stops the background server,
REM installs any new Python packages, makes a test resume, and starts the server again.
cd /d "%~dp0"
echo Stopping the background server...
powershell -NoProfile -Command "Get-CimInstance Win32_Process | Where-Object { $_.Name -match '^pythonw?\.exe$' -and $_.CommandLine -like '*resume-tailor\server\app.py*' } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force }"
call setup.bat quiet || (pause & exit /b 1)
echo.
echo Updated. Last step: in Chrome, open chrome://extensions and click the reload
echo arrow on Resume Tailor, then reload any job tabs you already had open.
pause
