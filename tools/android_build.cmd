@echo off
rem  Build the Android app with the Gradle distribution already in the local
rem  cache (there is no gradle on PATH and no wrapper jar in the project).
rem
rem    tools\android_build.cmd assembleDebug
rem    tools\android_build.cmd installDebug
rem
setlocal
set "JAVA_HOME=C:\Program Files\Java\jdk-17"
set "ANDROID_SDK_ROOT=E:\Android\sdk"
set "ANDROID_HOME=E:\Android\sdk"
set "GRADLE=%USERPROFILE%\.gradle\wrapper\dists\gradle-8.10.2-bin\a04bxjujx95o3nb99gddekhwo\gradle-8.10.2\bin\gradle.bat"
if not exist "%GRADLE%" (
  echo Gradle 8.10.2 not found in the wrapper cache. Run tools\setup_gradle.py first.
  exit /b 1
)
call "%GRADLE%" -p "%~dp0..\android" %*
exit /b %ERRORLEVEL%
