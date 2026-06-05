@echo off
setlocal

cd /d "%~dp0"

python -m pip install --upgrade pip
python -m pip install -r requirements.txt
python -m pip install pyinstaller

set PLAYWRIGHT_BROWSERS_PATH=0
python -m playwright install chromium

pyinstaller --clean --noconfirm KappalRateCapture.spec

echo.
echo Build complete.
echo EXE: dist\Kappal Rate Capture.exe
echo.
pause
