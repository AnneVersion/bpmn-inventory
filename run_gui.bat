@echo off
REM Launch the BPMN Data-Inventarisatie GUI by double-click in Windows Explorer
REM Place this file at the project root: E:\scripts\webscraper\bpmn\run_gui.bat

cd /d "%~dp0"
python src\gui.py
if errorlevel 1 (
    echo.
    echo Er ging iets mis. Check de foutmelding hierboven.
    pause
)
