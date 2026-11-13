@echo off
REM Signs Resume Tailor into the Claude account you want it to use.
REM This login is stored in .claude-account\ inside this folder, so typing "claude"
REM in a terminal keeps using your usual account. Run this again any time to switch.
setlocal
cd /d "%~dp0"
set "CLAUDE_CONFIG_DIR=%~dp0.claude-account"
set ANTHROPIC_API_KEY=
set CLAUDE_CODE_OAUTH_TOKEN=

set "CLAUDE=%USERPROFILE%\.local\bin\claude.exe"
if not exist "%CLAUDE%" set "CLAUDE=claude"
"%CLAUDE%" --version >nul 2>nul || (
  echo Claude Code isn't installed. Run setup.bat first.
  pause
  exit /b 1
)

echo.
echo === Sign Resume Tailor into Claude ===
echo.
"%CLAUDE%" auth status >nul 2>nul || goto :login
echo Resume Tailor is currently signed in as:
"%CLAUDE%" auth status --text
echo.
choice /c YN /m "Switch Resume Tailor to a different Claude account"
if errorlevel 2 goto :done
"%CLAUDE%" auth logout >nul 2>nul

:login
echo.
echo A browser window will open. Sign in with the Claude Pro or Max account
echo you want Resume Tailor to use. If it shows your other account, sign out
echo on that page first, or copy the link into a private window.
echo.
"%CLAUDE%" auth login
echo.
echo Resume Tailor is now signed in as:
"%CLAUDE%" auth status --text

:done
echo.
echo Your normal "claude" in terminals is unchanged.
echo Resume Tailor picks this up within a minute. No restart needed.
pause
