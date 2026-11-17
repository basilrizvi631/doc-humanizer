@echo off
cd /d "%~dp0"
call .venv\Scripts\activate.bat
set /p DOC="Drag your PDF/DOCX here and press Enter: "
python autopilot.py %DOC%
pause
