@echo off
REM Browser semantic search demo - no phone, no Aves, no APK build needed.
REM
REM   tools\serve_semantic_demo.cmd              (uses samples\)
REM   tools\serve_semantic_demo.cmd D:\pics      (any folder)
REM
REM The server listens on http://127.0.0.1:8801 and this script opens it
REM once the model has loaded. Close the window to stop.

setlocal
set "ROOT=%~dp0.."
set "PY=%ROOT%\.venv\Scripts\python.exe"

if not exist "%PY%" (
  echo [x] Python venv not found: %PY%
  echo     Run tools\install_deps.cmd first.
  pause
  exit /b 1
)

set "DIR=%~1"
if "%DIR%"=="" set "DIR=%ROOT%\samples"

echo.
echo   Albumcat semantic search demo
echo   -----------------------------
echo   Images : %DIR%
echo   Address: http://127.0.0.1:8801
echo.
echo   Put a few of your own pictures in that folder if it is empty.
echo   Close this window to stop the server.
echo.

start "" cmd /c "timeout /t 8 >nul & start http://127.0.0.1:8801"
"%PY%" "%ROOT%\tools\serve_semantic_demo.py" --dir "%DIR%"

echo.
echo Server stopped.
pause
