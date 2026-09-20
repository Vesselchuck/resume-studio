@echo off
REM Resume Studio - double-click launcher (development build).
REM Runs the same thing as: npm run studio
REM This console window stays open while the app runs; closing it stops the app.

cd /d "%~dp0"

call npm run studio

if errorlevel 1 (
    echo.
    echo Resume Studio exited with an error. The output above says why.
    pause
)
