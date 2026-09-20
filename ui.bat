@echo off
REM Opens Resume Studio in your browser without needing the Rust
REM toolchain. The desktop build (studio.bat) shows the same app in
REM its own window.

cd /d "%~dp0"

call npm run ui
pause
