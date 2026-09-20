@echo off
REM Resume Studio - build one document, or both.
REM
REM Calls the same CLIs as npm: npm run resume -> node resume.js,
REM and npm run letter -> node letter.js.
REM
REM The two are deliberately separate commands. Building a resume must
REM never touch the cover letter's files, or the other way round, so
REM "Both" below is two builds in a row rather than one combined build.
REM
REM Usage:  build.bat            ask
REM         build.bat resume     just the resume
REM         build.bat letter     just the cover letter
REM         build.bat both       both, resume first

setlocal
cd /d "%~dp0"

if /i "%~1"=="resume" goto resume
if /i "%~1"=="letter" goto letter
if /i "%~1"=="both"   goto both
if not "%~1"=="" goto badarg

:menu
echo.
echo   Resume Studio
echo.
echo     1   Resume         - dist\Your_Name_Resume.pdf
echo     2   Cover letter   - dist\Your_Name_Cover_Letter.pdf
echo     3   Both
echo     Q   Quit
echo.
set "pick="
set /p "pick=Which? "

if /i "%pick%"=="1" goto resume
if /i "%pick%"=="2" goto letter
if /i "%pick%"=="3" goto both
if /i "%pick%"=="q" goto quit
if "%pick%"=="" goto quit
echo   Not one of the choices.
goto menu

:badarg
echo.
echo   Unknown argument: %~1
echo   Expected one of: resume, letter, both - or no argument to be asked.
echo.
goto finish

:resume
echo.
call npm run resume
goto finish

:letter
echo.
call npm run letter
goto finish

:both
echo.
call npm run resume
if errorlevel 1 goto lettersk
echo.
call npm run letter
goto finish

:lettersk
echo.
echo   The resume build failed, so the cover letter was not built.
echo   Fix the error above and run this again.
goto finish

:finish
echo.
pause

:quit
endlocal
