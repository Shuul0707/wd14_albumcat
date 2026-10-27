@echo off
REM Install albumcat's Python dependencies.
REM
REM Two environment quirks on this machine, both handled here:
REM   * the DSH sandbox redirects TEMP at a path the venv bootstrap cannot use,
REM     so we pin TEMP/TMP inside the project;
REM   * a direct PyPI connection stalls, so we install from a Chinese mirror.
REM
REM Usage:  tools\install_deps.cmd [--cpu] [--no-ocr]

setlocal
cd /d "%~dp0.."

set "TEMP=%~dp0..\.tmp"
set "TMP=%~dp0..\.tmp"
if not exist "%TEMP%" mkdir "%TEMP%"

set "PY=.venv\Scripts\python.exe"
if not exist "%PY%" (
    echo venv missing -- creating it without pip, then bootstrapping pip from
    echo the bundled wheel ^(ensurepip cannot write to the sandbox TEMP^).
    python -m venv --without-pip .venv || exit /b 1
    for /f "delims=" %%w in ('dir /b /s "%LOCALAPPDATA%\Programs\Python\Python313\Lib\ensurepip\_bundled\pip-*.whl"') do set "PIPWHL=%%w"
    if not defined PIPWHL (
        echo could not find the bundled pip wheel; run: python -m ensurepip
        exit /b 1
    )
    "%PY%" -c "import zipfile,sys;zipfile.ZipFile(sys.argv[1]).extractall(sys.argv[2])" "%PIPWHL%" "%~dp0..\.venv\Lib\site-packages" || exit /b 1
)

set "INDEX=https://pypi.tuna.tsinghua.edu.cn/simple"
set "ORT=onnxruntime-directml"
if /i "%~1"=="--cpu" set "ORT=onnxruntime"

"%PY%" -m pip install --cache-dir "%~dp0..\.pipcache" --index-url "%INDEX%" ^
    "%ORT%" opencv-python-headless pandas pillow numpy requests || exit /b 1

REM OCR (reading the text printed on a photo, see docs/OCR.md) is optional but
REM the app asks for it on every new photo, so install it by default; pass
REM --no-ocr to skip. --no-deps is deliberate: rapidocr declares opencv-python,
REM and pulling that would overwrite the headless cv2 installed above (and fail
REM while `serve` holds cv2.pyd open). Everything it imports is already here.
if /i not "%~1"=="--no-ocr" (
    "%PY%" -m pip install --no-deps --cache-dir "%~dp0..\.pipcache" --index-url "%INDEX%" ^
        rapidocr-onnxruntime || exit /b 1
)

"%PY%" -c "import onnxruntime as o, cv2, pandas, PIL, numpy, requests; print('ORT', o.__version__); print('providers', o.get_available_providers()); print('cv2', cv2.__version__); print('numpy', numpy.__version__)" || exit /b 1
"%PY%" -c "import rapidocr_onnxruntime; print('OCR engine ok')" 2>nul || echo OCR engine missing -- run: .venv\Scripts\pip install --no-deps rapidocr-onnxruntime

echo.
echo deps OK
endlocal
