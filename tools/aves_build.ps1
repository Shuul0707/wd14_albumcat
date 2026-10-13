# Build the Aves fork (libre flavor) into an installable debug APK.
#
#   tools\aves_build.ps1 -Platform android-arm64     # a real phone
#   tools\aves_build.ps1 -Platform android-x64       # an emulator
#
# The source tree is aves\ in this repository (an Aves fork with the AI tagging
# page; see aves\FORK.md).  -Source points somewhere else if you keep your own
# checkout.
#
# Result: <source>\build\app\outputs\flutter-apk\app-libre-debug.apk, which both
# platforms overwrite -- copy it aside after an arm64 build before building x64.
#
# Environment variables, and why they are here:
#   PUB_HOSTED_URL / FLUTTER_STORAGE_BASE_URL
#       pub.dev and the Flutter engine artifacts, via the flutter-io.cn mirror.
#       Only needed on a Chinese network; they are left alone if you already
#       have them set in your environment.
#   JAVA_HOME    Aves needs JDK 21 (Gradle toolchain 21).  Set it here only if
#       the script cannot find a JDK 21 of its own.
#   ANDROID_HOME / ANDROID_SDK_ROOT
#       Flutter rewrites android/local.properties' sdk.dir from these.
#
# A debug APK is signed with the local debug keystore, so it installs with a
# plain `adb install` on any device -- that is why releases ship debug APKs.
# For a release build you need your own keystore: copy android\key_template.properties
# to android\key.properties and fill in the store/key values.
param(
    [string]$Target = 'lib/main_libre.dart',
    [string]$Platform = 'android-x64',
    [string]$Source = '',
    [string]$Log = '',
    [switch]$NoVerbose
)

[Console]::OutputEncoding = [System.Text.Encoding]::UTF8

$root = Split-Path -Parent $PSScriptRoot
if (-not $Source) { $Source = Join-Path $root 'aves' }
if (-not $Log) { $Log = Join-Path $root '.tmp\aves_build.log' }

if (-not (Test-Path $Source)) { throw "no source tree at $Source" }
if (-not $env:PUB_HOSTED_URL) { $env:PUB_HOSTED_URL = 'https://pub.flutter-io.cn' }
if (-not $env:FLUTTER_STORAGE_BASE_URL) { $env:FLUTTER_STORAGE_BASE_URL = 'https://storage.flutter-io.cn' }
if (-not $env:JAVA_HOME -and (Test-Path 'C:\Program Files\Java\jdk-21')) {
    $env:JAVA_HOME = 'C:\Program Files\Java\jdk-21'
}
if (-not $env:ANDROID_HOME -and (Test-Path 'E:\Android\sdk')) {
    $env:ANDROID_HOME = 'E:\Android\sdk'
    $env:ANDROID_SDK_ROOT = 'E:\Android\sdk'
}
$env:GIT_TERMINAL_PROMPT = '0'

$flutter = (Get-Command flutter.bat -ErrorAction SilentlyContinue).Source
if (-not $flutter -and (Test-Path 'E:\flutter\bin\flutter.bat')) { $flutter = 'E:\flutter\bin\flutter.bat' }
if (-not $flutter) { throw "flutter.bat not on PATH and not at E:\flutter\bin" }

New-Item -ItemType Directory -Force -Path (Split-Path -Parent $Log) | Out-Null
Set-Location $Source
$cmdArgs = @('build', 'apk', '--debug', '--flavor', 'libre', '--target-platform', $Platform, '-t', $Target)
if (-not $NoVerbose) { $cmdArgs += '--verbose' }
& $flutter @cmdArgs *>&1 | Out-File -FilePath $Log -Encoding utf8
$apk = Join-Path $Source 'build\app\outputs\flutter-apk\app-libre-debug.apk'
"EXIT=$LASTEXITCODE  log=$Log ($((Get-Item $Log).Length) B)"
if (Test-Path $apk) { "APK=$apk  $((Get-Item $apk).Length) B" } else { "APK missing" }
