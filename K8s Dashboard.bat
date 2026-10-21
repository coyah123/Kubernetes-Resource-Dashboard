@echo off
REM Launch the K8s Resource Dashboard desktop app.
REM This .bat runs the app from the folder it lives in, so it works no matter
REM where the project is checked out. You can also copy it to your Desktop and
REM point the shortcut's "Start in" at the project folder.

set "APPDIR=%~dp0"
cd /d "%APPDIR%"

REM Prefer the Windows "py" launcher; fall back to python if it's missing.
where py >nul 2>nul
if %ERRORLEVEL%==0 (
    py -3 "%APPDIR%k8s_dashboard_gui.py"
) else (
    python "%APPDIR%k8s_dashboard_gui.py"
)

if %ERRORLEVEL% NEQ 0 (
    echo.
    echo The dashboard exited with an error ^(code %ERRORLEVEL%^).
    pause
)
