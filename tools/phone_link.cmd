@echo off
rem ---------------------------------------------------------------------------
rem phone_link.cmd - make the phone reach this computer through the USB cable.
rem
rem Why: the AI tagging page talks to the albumcat server over HTTP. On a LAN it
rem uses the address you typed; with no Wi-Fi to share, `adb reverse` punches a
rem hole so the phone's own 127.0.0.1:8899 lands on this computer instead. The
rem app falls back to that address by itself, so nobody has to retype anything.
rem
rem Prerequisites (one time): USB debugging enabled on the phone
rem (Settings > About > tap Build number 7x > Developer options > USB debugging),
rem the phone plugged in with a data cable, and this computer authorized on it.
rem
rem Usage:
rem   phone_link.cmd                 set the tunnel up (default port 8899)
rem   phone_link.cmd -r              remove it
rem   phone_link.cmd -s SERIAL       pick a device when several are attached
rem   phone_link.cmd 9999            use another port
rem
rem The tunnel dies when the cable is unplugged or adb restarts - just run this
rem again. `adb devices` must list the phone as "device" (not "unauthorized").
rem ---------------------------------------------------------------------------
setlocal enabledelayedexpansion
set PORT=8899
set REMOVE=
set DEV=

:parse
if "%~1"=="" goto :run
if /i "%~1"=="-s" (
  set DEV=-s %~2
  shift
  shift
  goto :parse
)
if /i "%~1"=="-r" (
  set REMOVE=1
  shift
  goto :parse
)
if /i "%~1"=="--remove" (
  set REMOVE=1
  shift
  goto :parse
)
set PORT=%~1
shift
goto :parse

:run
set ADB=
where adb >nul 2>nul && set ADB=adb
if not defined ADB if defined ANDROID_HOME if exist "%ANDROID_HOME%\platform-tools\adb.exe" set ADB=%ANDROID_HOME%\platform-tools\adb.exe
if not defined ADB if defined ANDROID_SDK_ROOT if exist "%ANDROID_SDK_ROOT%\platform-tools\adb.exe" set ADB=%ANDROID_SDK_ROOT%\platform-tools\adb.exe
if not defined ADB if exist "E:\Android\sdk\platform-tools\adb.exe" set ADB=E:\Android\sdk\platform-tools\adb.exe
if not defined ADB (
  echo [phone_link] adb not found. Install Android platform-tools, or set ANDROID_HOME.
  exit /b 1
)

if not "%REMOVE%"=="" (
  "%ADB%" %DEV% reverse --remove tcp:%PORT%
  echo [phone_link] tunnel on port %PORT% removed.
  goto :end
)

"%ADB%" %DEV% reverse tcp:%PORT% tcp:%PORT%
if errorlevel 1 (
  echo.
  echo [phone_link] could not set the tunnel up. Usual causes:
  echo   * several devices attached -^> unplug the others, or pass -s SERIAL
  echo   * USB debugging is off, or the phone has not authorized this computer
  echo     ^(accept the "Allow USB debugging?" prompt on the phone^)
  echo   * the cable is charge-only ^(no data lines^)
  goto :end
)

echo.
echo [phone_link] tunnel up: phone 127.0.0.1:%PORT%  --^>  this PC port %PORT%
echo [phone_link] Start the server here if it is not running yet:
echo               tools\albumcat.cmd serve
echo [phone_link] Then tag from the phone as usual - the app tries the address you
echo               typed first and falls back to 127.0.0.1:%PORT% on its own.
echo [phone_link] Unplugging the cable kills the tunnel; run this again after.
echo.
"%ADB%" %DEV% reverse --list
"%ADB%" %DEV% shell "command -v curl >/dev/null 2>&1 && curl -s -m 5 http://127.0.0.1:%PORT%/health || true"
echo.

:end
endlocal
