@echo off
setlocal
rem ---------------------------------------------------------------------------
rem Let the phone reach "albumcat serve" over the LAN.
rem
rem Loopback traffic is exempt from Windows Firewall, so http://127.0.0.1:8899
rem answers perfectly even when the phone is being blocked. Worse, firewall
rem rules are matched on the executable *path*, and every Python interpreter on
rem this machine already has its own allow rules -- none of which cover the
rem project's virtualenv. This adds a rule pinned to that one executable and
rem that one port.
rem
rem Needs an Administrator prompt, so this re-launches itself elevated.
rem ---------------------------------------------------------------------------

net session >nul 2>&1
if errorlevel 1 (
    echo Asking for Administrator rights...
    powershell -NoProfile -Command "Start-Process -Verb RunAs -FilePath '%~f0' -ArgumentList '%*'"
    exit /b
)

rem %~dp0 ends with a backslash; "%%~fI" collapses the ".." so netsh is given the
rem real image path. The rule is matched against the process image path, and a
rem stored "tools\..\.venv\..." may never compare equal to it -- which would look
rem exactly like the firewall being off.
for %%I in ("%~dp0..\.venv\Scripts\python.exe") do set "PY=%%~fI"
set "PORT=%~1"
if "%PORT%"=="" set "PORT=8899"

if not exist "%PY%" (
    echo [FAIL] no interpreter at "%PY%"
    echo        run tools\install_deps.cmd first
    pause
    exit /b 1
)

echo Interpreter : %PY%
echo Port        : %PORT%
echo.

netsh advfirewall firewall delete rule name="albumcat serve" >nul 2>&1
netsh advfirewall firewall add rule name="albumcat serve" dir=in action=allow protocol=TCP localport=%PORT% profile=any program="%PY%"
if errorlevel 1 (
    echo [FAIL] could not add the rule
    pause
    exit /b 1
)

rem profile=any on purpose: a machine on a VPN and Wi-Fi at once has two active
rem profiles, and Windows picks the rule profile by the arriving interface.

echo.
echo Done. Check with:
echo     tools\albumcat.cmd doctor
echo Remove it again with:
echo     netsh advfirewall firewall delete rule name="albumcat serve"
echo.
pause
