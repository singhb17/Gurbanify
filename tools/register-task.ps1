<#
  Register the watchdog to start at boot, so a power cut or a Windows Update
  reboot brings everything back without anyone touching the machine.

      powershell -File tools\register-task.ps1            install
      powershell -File tools\register-task.ps1 -Remove    uninstall

  Task Scheduler rather than NSSM: nothing to install, and it already knows how
  to start at boot, restart on failure, and run whether or not anyone is logged
  in. NSSM is tidier as a true service, but not by enough to justify the extra
  dependency on a machine that only ever runs this one thing.

  Needs an elevated PowerShell -- registering a boot task is a machine-level
  change. It will say so plainly rather than failing halfway.
#>

param([switch]$Remove)

$ErrorActionPreference = 'Stop'
$Root = Split-Path $PSScriptRoot -Parent
$TaskName = 'GurbanifyWatchdog'

$isAdmin = ([Security.Principal.WindowsPrincipal] `
    [Security.Principal.WindowsIdentity]::GetCurrent()
    ).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
if (-not $isAdmin) {
    Write-Host ''
    Write-Host '  This needs an elevated PowerShell.' -ForegroundColor Red
    Write-Host '  Right-click PowerShell -> Run as administrator, then run it again.' -ForegroundColor Red
    Write-Host ''
    exit 1
}

if ($Remove) {
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false -ErrorAction SilentlyContinue
    Write-Host "  removed the '$TaskName' task" -ForegroundColor Yellow
    exit 0
}

$action = New-ScheduledTaskAction -Execute 'powershell.exe' `
    -Argument ('-NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File "{0}"' `
               -f (Join-Path $PSScriptRoot 'watchdog.ps1')) `
    -WorkingDirectory $Root

# AtStartup, not AtLogon: the server should come back after a reboot whether or
# not anybody signs in.
$trigger = New-ScheduledTaskTrigger -AtStartup

# The watchdog is meant to run forever, so every default that stops a
# long-running task has to be turned off explicitly:
#   ExecutionTimeLimit 0  - or Windows kills it after three days
#   RestartCount/Interval - bring it back if it dies for its own reasons
#   no IdleSettings       - do not pause it when the machine looks idle
$settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
    -StartWhenAvailable -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1) `
    -ExecutionTimeLimit (New-TimeSpan -Seconds 0) `
    -MultipleInstances IgnoreNew

# SYSTEM so it needs no stored password and no logged-in user. The library lives
# under the user's profile, so if OneDrive ever makes that unreadable to SYSTEM,
# switch this to the user account with -LogonType S4U.
$principal = New-ScheduledTaskPrincipal -UserId 'SYSTEM' -RunLevel Highest

Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger `
    -Settings $settings -Principal $principal -Force | Out-Null

Write-Host ''
Write-Host "  registered '$TaskName' -- it will start at every boot." -ForegroundColor Green
Write-Host '  start it now with:  Start-ScheduledTask -TaskName ' -NoNewline
Write-Host $TaskName -ForegroundColor Cyan
Write-Host '  watch it with:      Get-Content logs\watchdog.log -Wait -Tail 20'
Write-Host ''
