@echo off
rem Runs the EQOA server and restarts it when the admin panel asks (exit code 75),
rem e.g. after "Update & restart". Pass your usual options, for example:
rem   .\start_server.bat --host 0.0.0.0 --public-host 192.168.1.51   (PowerShell)
rem   start_server.bat --host 0.0.0.0 --public-host 192.168.1.51     (cmd.exe)
rem Set PYTHON to use a specific interpreter (default: python).

rem A batch file is read from disk while it runs, so run from a temp copy in case
rem an update changes this file.
if not "%EQOA_LAUNCHER_COPY%"=="1" (
    set "EQOA_LAUNCHER_COPY=1"
    set "EQOA_REPO=%~dp0"
    copy /y "%~f0" "%TEMP%\eqoa_start_server.bat" >nul
    "%TEMP%\eqoa_start_server.bat" %*
)

rem Running from the temp copy now; clear the marker so the next run copies again.
set "EQOA_LAUNCHER_COPY="
setlocal
cd /d "%EQOA_REPO%"
set "EQOA_LAUNCHER=1"
if "%PYTHON%"=="" set "PYTHON=python"

:run
"%PYTHON%" -m eqoa_server %*
if "%ERRORLEVEL%"=="75" (
    echo Restarting the server...
    goto run
)
endlocal
