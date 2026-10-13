@echo off
REM Wrapper so `albumcat` runs from a checkout without being installed.
REM
REM   tools\albumcat.cmd doctor
REM   tools\albumcat.cmd plugins
REM   tools\albumcat.cmd plan samples\
REM   tools\albumcat.cmd serve --port 8899            (the phone talks to this)

setlocal
set "PYTHONPATH=%~dp0..\src;%PYTHONPATH%"
set "PYTHONIOENCODING=utf-8"
"%~dp0..\.venv\Scripts\python.exe" -m albumcat %*
exit /b %ERRORLEVEL%
