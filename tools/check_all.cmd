@echo off
REM Run every self-check in the repo.  All the real logic lives in
REM tools\check_all.py: this file stays pure ASCII on purpose, because cmd.exe
REM reads a .cmd in the OEM codepage and UTF-8 text in here mangles the parse.
setlocal

set "ROOT=%~dp0.."
set "PY=%~dp0..\.venv\Scripts\python.exe"
if not exist "%PY%" (
    echo.
    echo   no virtualenv yet - run tools\install_deps.cmd first
    echo.
    pause
    exit /b 1
)

set "PYTHONIOENCODING=utf-8"
set "PYTHONPATH=%ROOT%\src"
"%PY%" -u "%~dp0check_all.py" %*
exit /b %errorlevel%
