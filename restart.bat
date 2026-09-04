@echo off
REM Double-click this to restart everything: app + tunnel, from scratch.
REM
REM It exists only to launch the PowerShell script, because the work needs
REM cmdlets (Get-NetTCPConnection, Invoke-WebRequest) that plain batch has no
REM equivalent for. -ExecutionPolicy Bypass applies to this one process only
REM and changes nothing about the machine's policy.

REM -Watch starts the watchdog in the background once everything is up, so this
REM one double-click is the whole of "run the app and keep it running".

cd /d "%~dp0"
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0tools\serve.ps1" -Watch %*
echo.
pause
