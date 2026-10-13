# Build the Aves fork (libre flavor, x86_64 debug APK) with CN mirrors.
#
# Every environment variable here is load-bearing:
#   PUB_HOSTED_URL / FLUTTER_STORAGE_BASE_URL  pub.dev and the Flutter engine
#       artifacts are unreachable from here without the flutter-io.cn mirror.
#   JAVA_HOME    Aves needs JDK 21; the machine default shim is not it.
#   ANDROID_HOME / ANDROID_SDK_ROOT
#       Flutter rewrites android/local.properties' sdk.dir from these.  Without
#       them it points at the near-empty default root and AGP tries to fetch a
#       package manifest from dl.google.com (~21 KiB/s) instead of building.
#
# The Maven mirror rewrite itself lives in ~/.gradle/init.d/zz-mirrors.gradle.
#
# -Platform picks the phone architecture: android-x64 runs on the emulator,
# android-arm64 is what a real phone needs.  The APK lands in the same place
# either way (build/app/outputs/flutter-apk/app-libre-debug.apk), so copy it
# aside after an arm64 build or the next x64 build overwrites it.
param(
    [string]$Target = 'lib/main_libre.dart',
    [string]$Log = 'E:\AlbumCategories\.tmp\aves_build.log',
    [string]$Platform = 'android-x64',
    [switch]$NoVerbose
)
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$env:PUB_HOSTED_URL = 'https://pub.flutter-io.cn'
$env:FLUTTER_STORAGE_BASE_URL = 'https://storage.flutter-io.cn'
$env:GIT_TERMINAL_PROMPT = '0'
$env:JAVA_HOME = 'C:\Program Files\Java\jdk-21'
$env:ANDROID_HOME = 'E:\Android\sdk'
$env:ANDROID_SDK_ROOT = 'E:\Android\sdk'
Set-Location E:\AlbumCategories\.tmp\aves-src
$args = @('build', 'apk', '--debug', '--flavor', 'libre', '--target-platform', $Platform, '-t', $Target)
if (-not $NoVerbose) { $args += '--verbose' }
& 'E:\flutter\bin\flutter.bat' @args *>&1 | Out-File -FilePath $Log -Encoding utf8
"EXIT=$LASTEXITCODE  log=$Log  ($((Get-Item $Log).Length) B)"
