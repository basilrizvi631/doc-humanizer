@echo off
cd /d "%~dp0"
echo Setting up ZeroGPT Document Humanizer...
if not exist .venv (
  py -3.12 -m venv .venv 2>nul || python -m venv .venv
)
call .venv\Scripts\activate.bat
python -m pip install --upgrade pip -q
pip install -r requirements.txt
echo.
echo Setup complete. Double-click run.bat to start the app.
pause
