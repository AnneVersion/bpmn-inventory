@echo off
REM Launch the BPMN Inventory webapp by double-click in Windows Explorer
cd /d "%~dp0"
python serve.py
if errorlevel 1 (
    echo.
    echo Er ging iets mis. Check de foutmelding hierboven.
    pause
)
