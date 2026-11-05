@echo off
rem Thin wrapper: windows\setup_windows.py with the repo venv Python
rem (fallback: the py launcher, then python on PATH; setup creates .venv).
rem All arguments are passed on.
setlocal
set "ROOT=%~dp0"
set "SCRIPT=%ROOT%windows\setup_windows.py"
if exist "%ROOT%.venv\Scripts\python.exe" goto venv
where py >nul 2>nul
if not errorlevel 1 goto pylauncher
python "%SCRIPT%" %*
exit /b %ERRORLEVEL%
:venv
"%ROOT%.venv\Scripts\python.exe" "%SCRIPT%" %*
exit /b %ERRORLEVEL%
:pylauncher
py -3 "%SCRIPT%" %*
exit /b %ERRORLEVEL%
