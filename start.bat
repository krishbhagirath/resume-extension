@echo off
REM Runs Resume Tailor at http://127.0.0.1:8765 and opens it. Keep this window open while you apply.
cd /d "%~dp0"
if not exist .venv\Scripts\python.exe (
  call setup.bat quiet || (pause & exit /b 1)
)
.venv\Scripts\python server\app.py --open
pause
