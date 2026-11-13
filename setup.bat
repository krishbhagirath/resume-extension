@echo off
REM One-time setup for Resume Tailor on Windows. Safe to run again.
setlocal
cd /d "%~dp0"
echo.
echo === Resume Tailor setup ===
echo.

REM 1) Find Python
set PY=
where py >nul 2>nul && set PY=py -3
if not defined PY python --version >nul 2>nul && set PY=python
if not defined PY if exist "%USERPROFILE%\anaconda3\python.exe" set PY="%USERPROFILE%\anaconda3\python.exe"
if not defined PY if exist "%USERPROFILE%\miniconda3\python.exe" set PY="%USERPROFILE%\miniconda3\python.exe"
if not defined PY (
  echo Python 3.10 or newer is needed. Install it from https://www.python.org/downloads/
  echo During install, tick "Add python.exe to PATH". Then run setup.bat again.
  goto :fail
)

REM 2) Python packages in a private .venv folder
if not exist .venv\Scripts\python.exe (
  echo [1/5] Creating Python environment...
  %PY% -m venv .venv || goto :fail
)
echo [2/5] Installing Python packages...
.venv\Scripts\python -m pip install -q --disable-pip-version-check -r requirements.txt || goto :fail
if not exist .env copy /y .env.example .env >nul
if not exist data\experiences.yaml copy /y data\example-bank.yaml data\experiences.yaml >nul

REM 3) LaTeX compiler
if exist bin\tectonic.exe goto :have_tex
where tectonic >nul 2>nul && goto :have_tex
where pdflatex >nul 2>nul && goto :have_tex
echo [3/5] Downloading the Tectonic LaTeX compiler...
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0setup-tectonic.ps1"
if not exist bin\tectonic.exe (
  echo.
  echo Couldn't download Tectonic automatically. Get the Windows .zip from
  echo   https://github.com/tectonic-typesetting/tectonic/releases
  echo and put tectonic.exe in the "bin" folder here, then run setup.bat again.
  goto :fail
)
:have_tex
echo [3/5] LaTeX compiler ready.

REM 4) Claude Code, signed in with your Claude plan (uses plan usage, no API billing)
set "CLAUDE=%USERPROFILE%\.local\bin\claude.exe"
if exist "%CLAUDE%" goto :have_claude
where claude >nul 2>nul && (set "CLAUDE=claude" & goto :have_claude)
echo [4/5] Installing Claude Code...
powershell -NoProfile -ExecutionPolicy Bypass -Command "irm https://claude.ai/install.ps1 | iex"
set "CLAUDE=%USERPROFILE%\.local\bin\claude.exe"
if not exist "%CLAUDE%" (
  echo Couldn't install Claude Code. Install it from https://code.claude.com/docs/en/setup
  echo then run setup.bat again. Until then, resumes use the free offline matcher.
  goto :after_claude
)
:have_claude
REM Resume Tailor keeps its own login in .claude-account\, separate from your usual
REM "claude" login, so it can use a different Claude account.
set "CLAUDE_CONFIG_DIR=%~dp0.claude-account"
set CLAUDE_CODE_OAUTH_TOKEN=
"%CLAUDE%" auth status >nul 2>nul
if errorlevel 1 (
  echo.
  echo [4/5] Sign in to Claude in the browser window that opens, with the Claude
  echo       Pro or Max account you want Resume Tailor to use. Your usual "claude"
  echo       login in terminals is not changed.
  "%CLAUDE%" auth login
)
"%CLAUDE%" auth status >nul 2>nul
if errorlevel 1 (
  echo Claude Code isn't signed in yet. Resumes use the offline matcher until you run setup.bat again.
) else (
  echo [4/5] Resume Tailor is signed in to Claude. Change it any time with claude-login.bat.
)
if defined ANTHROPIC_API_KEY echo Note: ANTHROPIC_API_KEY is set on this PC. Resume Tailor ignores it, so your plan is used, not the API.
:after_claude

echo [5/5] Making a test resume...
.venv\Scripts\python server\warmup.py || goto :fail

echo Turning on auto-start...
call autostart-on.bat quiet >nul

echo.
echo Setup done. Resume Tailor now runs in the background and starts by itself
echo whenever you log in. You never need to start it by hand.
echo Last step: load the "extension" folder in Chrome (see README.md).
if /i not "%~1"=="quiet" pause
exit /b 0

:fail
echo.
echo Setup stopped. Fix the problem above and run setup.bat again.
if /i not "%~1"=="quiet" pause
exit /b 1
