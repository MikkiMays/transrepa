@echo off
rem CodeShuttle launcher. ASCII only: cmd.exe breaks on non-ASCII inside blocks.
title CodeShuttle
cd /d "%~dp0"

set "PY="
where py >nul 2>nul
if not errorlevel 1 set "PY=py -3"
if not defined PY (
    where python >nul 2>nul
    if not errorlevel 1 set "PY=python"
)

if not defined PY goto nopython

%PY% -m codeshuttle %*
if errorlevel 1 goto failed
exit /b 0

:nopython
echo.
echo  [!] Python not found.
echo      Install Python 3.9+ from https://python.org
echo      and check "Add Python to PATH" during setup.
echo.
pause
exit /b 1

:failed
echo.
echo  [!] Finished with an error - see the message above.
echo.
pause
exit /b 1
