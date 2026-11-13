@echo off
REM Stops Resume Tailor from starting at login, and stops the background copy that's running now.
set "STARTUP=%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup"
if exist "%STARTUP%\ResumeTailor.vbs" del "%STARTUP%\ResumeTailor.vbs"
powershell -NoProfile -Command "Get-CimInstance Win32_Process | Where-Object { $_.Name -eq 'pythonw.exe' -and $_.CommandLine -like '*resume-tailor\server\app.py*' } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force }"
echo Auto-start is off, and the background server has been stopped.
pause
