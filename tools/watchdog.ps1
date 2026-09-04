<#
  Keep the Shabad Library and its tunnel alive indefinitely.

      restart.bat                                  starts this automatically
      powershell -File tools\watchdog.ps1          run it by hand
      powershell -File tools\register-task.ps1     run it at every boot

  THE IMPORTANT THING IT KNOWS: a dead app does not need a new link.

  Measured -- cloudflared holds its connection to Cloudflare when the origin
  dies. The url returns 502 while the app is down and goes straight back to 401
  when it returns, same address, no new tunnel. So the common failure is fixed
  by restarting the app alone, and the link in your phone keeps working.

  Only a dead CLOUDFLARED forces a new address, and that is the only thing worth
  a notification. What it watches, and why each is separate:

    the app      can exit, or wedge with the port still open. So the check is an
                 HTTP GET of /health, which touches the database -- a process
                 stuck on a locked database passes a port check and fails every
                 real request.

    the tunnel   can exit. Then the address changes and you need telling.

    the machine  can reboot, which this cannot survive by itself. That is what
                 register-task.ps1 is for.

  Notifications are for things you must act on: a new address, or a failure it
  could not fix. There is no "still alive" ping -- a heartbeat every few hours
  trains you to ignore the channel that also carries the alerts.
#>

param(
    [int]$Port = 8000,
    [int]$CheckSeconds = 30,
    # Set when serve.ps1 hands off: everything is already up, so do not restart
    # anything just because this happens to be starting.
    [switch]$NoInitialStart
)

$ErrorActionPreference = 'Continue'
$Root = Split-Path $PSScriptRoot -Parent
$LogDir = Join-Path $Root 'logs'
if (-not (Test-Path $LogDir)) { New-Item -ItemType Directory $LogDir | Out-Null }
$Log = Join-Path $LogDir 'watchdog.log'

# One watchdog per machine. restart.bat starts one, and the boot task starts
# another; two of them would each restart the app the other just started and
# fight over the port forever. A named mutex is the cheapest way to make the
# second one simply leave.
$mutex = New-Object System.Threading.Mutex($false, 'Global\GurbanifyWatchdog')
if (-not $mutex.WaitOne(0)) {
    Write-Host '  another watchdog is already running; exiting.'
    exit 0
}

function Write-Log($msg) {
    $line = "{0}  {1}" -f (Get-Date -Format 'yyyy-MM-dd HH:mm:ss'), $msg
    Write-Host $line
    try { Add-Content -Path $Log -Value $line -Encoding UTF8 } catch {}
}

function Get-DotEnv {
    $e = @{}
    $path = Join-Path $Root '.env'
    if (-not (Test-Path $path)) { return $e }
    foreach ($line in Get-Content $path -Encoding UTF8) {
        $t = $line.Trim()
        if ($t -and -not $t.StartsWith('#') -and $t.Contains('=')) {
            $k, $v = $t.Split('=', 2)
            $e[$k.Trim()] = $v.Trim().Trim('"').Trim("'")
        }
    }
    return $e
}

function Send-Ntfy($title, $body, $tags, $priority = 'default') {
    $topic = (Get-DotEnv)['NTFY_TOPIC']
    if (-not $topic) { Write-Log 'no NTFY_TOPIC in .env; cannot notify'; return }
    try {
        Invoke-RestMethod -Uri "https://ntfy.sh/$topic" -Method Post `
            -Body ([Text.Encoding]::UTF8.GetBytes($body)) `
            -Headers @{ Title = $title; Tags = $tags; Priority = $priority } `
            -TimeoutSec 20 | Out-Null
        Write-Log "notified: $title"
    } catch { Write-Log "ntfy FAILED: $($_.Exception.Message)" }
}

function Test-Health {
    try {
        $r = Invoke-WebRequest "http://127.0.0.1:$Port/health" -TimeoutSec 8 -UseBasicParsing
        return ($r.StatusCode -eq 200)
    } catch { return $false }
}

function Test-TunnelAlive {
    return [bool](Get-Process cloudflared -ErrorAction SilentlyContinue)
}

function Get-TunnelUrl {
    $f = Join-Path $LogDir 'current-url.txt'
    if (Test-Path $f) {
        # Trim the BOM as well as whitespace -- .NET does not count U+FEFF as
        # whitespace, so an unchanged url would otherwise compare as different.
        return (Get-Content $f -Raw).Trim([char]0xFEFF, ' ', "`r", "`n", "`t")
    }
    return $null
}

function Invoke-Serve($serveArgs, $why) {
    <#
      Start-Process -Wait with a redirect file, NOT `& powershell | ForEach`.

      The pipeline version ran serve.ps1 correctly -- the app came back every
      time -- but nothing after the pipeline was ever logged, so a successful
      restart looked identical to a watchdog that had hung. Reading a file after
      the process has exited has no such ambiguity, and -PassThru gives a real
      exit code rather than depending on $LASTEXITCODE surviving a pipeline.
    #>
    Write-Log "RESTARTING ($why): serve.ps1 $($serveArgs -join ' ')"
    $tmp = Join-Path $LogDir 'serve.out.log'
    $args = @('-NoProfile', '-ExecutionPolicy', 'Bypass', '-File',
              (Join-Path $PSScriptRoot 'serve.ps1'),
              '-Port', $Port, '-Unattended') + $serveArgs
    try {
        $p = Start-Process -FilePath 'powershell' -ArgumentList $args `
             -WorkingDirectory $Root -NoNewWindow -Wait -PassThru `
             -RedirectStandardOutput $tmp
    } catch {
        Write-Log "could not run serve.ps1: $($_.Exception.Message)"
        return $false
    }
    foreach ($line in (Get-Content $tmp -ErrorAction SilentlyContinue)) {
        if ("$line".Trim()) { Write-Log "  $line" }
    }
    Write-Log "serve.ps1 exited with $($p.ExitCode)"
    return ($p.ExitCode -eq 0)
}

# ---------------------------------------------------------------- loop

Write-Log '=== watchdog started ==='
$fails = 0

if (-not $NoInitialStart) {
    if (-not (Test-Health)) {
        if (-not (Invoke-Serve @() 'nothing running at startup')) {
            Send-Ntfy 'Shabad Library is DOWN' `
                'Could not start it at all. Check logs on the server.' `
                'rotating_light' 'high'
        }
    }
}

try {
    while ($true) {
        Start-Sleep -Seconds $CheckSeconds

        $healthy = Test-Health
        $tunnel = Test-TunnelAlive

        # Tunnel gone: the address is lost whatever else is true, so this is
        # checked first and always means a full restart plus a notification.
        if (-not $tunnel) {
            Write-Log 'cloudflared is gone -- a new address is needed'
            $before = Get-TunnelUrl
            if (Invoke-Serve @() 'tunnel process disappeared') {
                $after = Get-TunnelUrl
                # serve.ps1 sends the new-link push itself, so nothing is sent
                # here. Saying it twice would be worse than not saying it.
                Write-Log "url now: $after (was $before)"
            } else {
                Send-Ntfy 'Shabad Library is DOWN' `
                    'The tunnel died and could not be replaced.' `
                    'rotating_light' 'high'
            }
            $fails = 0
            continue
        }

        if (-not $healthy) {
            $fails++
            Write-Log "health check failed ($fails)"
            # Two strikes, not one. A single failed check during a slow moment
            # is not a dead server, and restarting on it would make the app
            # LESS available than leaving it alone.
            if ($fails -ge 2) {
                # -AppOnly: the tunnel is alive, so the address survives and
                # there is nothing to tell anyone about.
                if (Invoke-Serve @('-AppOnly') 'health check failed twice') {
                    Write-Log "recovered on the same url: $(Get-TunnelUrl)"
                } else {
                    Send-Ntfy 'Shabad Library is DOWN' `
                        'The app failed twice and would not restart.' `
                        'rotating_light' 'high'
                }
                $fails = 0
            }
            continue
        }

        $fails = 0
    }
} finally {
    $mutex.ReleaseMutex()
    $mutex.Dispose()
}
