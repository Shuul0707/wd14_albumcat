@echo off
REM Wrapper so `albumcat` runs from a checkout without being installed.
REM
REM   tools\albumcat.cmd doctor
REM   tools\albumcat.cmd plan samples\
REM   tools\albumcat.cmd tag "D:\Photos" --top 5      (dry run)
REM   tools\albumcat.cmd tag "D:\Photos" --apply      (writes XMP)
REM   tools\albumcat.cmd undo --list
REM   tools\albumcat.cmd run --limit 50               (Immich, dry run)

setlocal
set "PYTHONPATH=%~dp0..\src;%PYTHONPATH%"
set "PYTHONIOENCODING=utf-8"
"%~dp0..\.venv\Scripts\python.exe" -m albumcat %*
exit /b %ERRORLEVEL%
