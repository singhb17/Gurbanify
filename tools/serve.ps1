<#
  Start the Shabad Library and its Cloudflare tunnel, from scratch, in one go.

      .\restart.bat                  (from the project root -- just double-click it)
      powershell -File tools\serve.ps1 -NoTunnel      local only, no public link

  This replaces the eight manual steps that used to be needed: kill whatever is
  on the port, start uvicorn in one window, start cloudflared in another, read
  the link out of its output, click it to see whether it actually works, and if
  it doesn't, ctrl-C and do the last part again.

  That last step is the one worth automating. A Quick Tunnel regularly comes up
  with a hostname that is not yet routable, and the only way to know is to
  fetch it. So this fetches it, and retries the tunnel if it fails, rather than
  handing over a link that doesn't load.

  Nothing here is destructive to data. It kills processes and writes logs.
#>

param(
    [int]$Port = 8000,
    [switch]$NoTunnel,      # local only, no public link
    [int]$Tries = 3,
    # Restart ONLY the app and leave any running tunnel alone.
    #
    # Measured: cloudflared keeps its connection to Cloudflare when the origin
    # dies -- the url returns 502 while the app is down and goes straight back
    # to 401 when it comes back, with no new tunnel and no new address. So the
    # common failure by far does not need a new link, and the watchdog uses this.
    [switch]$AppOnly,
    # Never prompt. For the watchdog, where a Read-Host would block forever.
    # Notifications are NOT suppressed by this -- telling you the address
    # changed is the entire point of running it unattended.
    [switch]$Unattended,
    [switch]$Watch          # hand off to the watchdog once everything is up
)

$ErrorActionPreference = 'Stop'
$Root = Split-Path $PSScriptRoot -Parent
$LogDir = Join-Path $Root 'logs'
if (-not (Test-Path $LogDir)) { New-Item -ItemType Directory $LogDir | Out-Null }

function Say($msg, $colour = 'Gray') { Write-Host "  $msg" -ForegroundColor $colour }

# ---------------------------------------------------------------- .env

function Get-DotEnv {
    $env = @{}
    $path = Join-Path $Root '.env'
    if (-not (Test-Path $path)) { return $env }
    foreach ($line in Get-Content $path -Encoding UTF8) {
        $t = $line.Trim()
        if ($t -and -not $t.StartsWith('#') -and $t.Contains('=')) {
            $k, $v = $t.Split('=', 2)
            $env[$k.Trim()] = $v.Trim().Trim('"').Trim("'")
        }
    }
    return $env
}

$DotEnv = Get-DotEnv

# ---------------------------------------------------------------- notify

function Send-Ntfy($title, $body, $tags) {
    $topic = $DotEnv['NTFY_TOPIC']
    if (-not $topic) { return }          # not configured; silence is correct
    try {
        Invoke-RestMethod -Uri "https://ntfy.sh/$topic" -Method Post `
            -Body ([Text.Encoding]::UTF8.GetBytes($body)) `
            -Headers @{ Title = $title; Tags = $tags } -TimeoutSec 20 | Out-Null
    } catch {
        Say "could not send notification: $($_.Exception.Message)" 'DarkYellow'
    }
}

# ---------------------------------------------------------------- stop

function Stop-Everything {
    $killed = 0
    try {
        $pids = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction Stop |
                Select-Object -ExpandProperty OwningProcess -Unique
        foreach ($processId in $pids) {
            try { Stop-Process -Id $processId -Force -ErrorAction Stop; $killed++ } catch {}
        }
    } catch {}                            # nothing listening: the normal case

    # Under -AppOnly the tunnel is deliberately left running: it survives the
    # origin dying, so killing it would throw away a perfectly good address and
    # force everyone onto a new link for no reason.
    if (-not $AppOnly) {
        foreach ($p in (Get-Process cloudflared -ErrorAction SilentlyContinue)) {
            try { Stop-Process -Id $p.Id -Force -ErrorAction Stop; $killed++ } catch {}
        }
    }
    if ($killed) { Say "stopped $killed process(es)" }
    Start-Sleep -Milliseconds 700         # let the port actually free up
}

# ---------------------------------------------------------------- app

function Start-App {
    $out = Join-Path $LogDir 'server.log'
    $err = Join-Path $LogDir 'server.err.log'
    $p = Start-Process -FilePath 'python' `
        -ArgumentList '-m', 'uvicorn', 'api:app', '--host', '127.0.0.1', '--port', $Port `
        -WorkingDirectory $Root -WindowStyle Hidden -PassThru `
        -RedirectStandardOutput $out -RedirectStandardError $err
    return $p
}

function Wait-Health($seconds = 40) {
    # /health is deliberately unauthenticated and touches the database, so this
    # proves the app can actually serve rather than merely that the port is open.
    $deadline = (Get-Date).AddSeconds($seconds)
    while ((Get-Date) -lt $deadline) {
        try {
            $r = Invoke-WebRequest "http://127.0.0.1:$Port/health" -TimeoutSec 3 -UseBasicParsing
            if ($r.StatusCode -eq 200) { return $true }
        } catch {}
        Start-Sleep -Milliseconds 600
    }
    return $false
}

# ---------------------------------------------------------------- tunnel

function Start-Tunnel {
    $log = Join-Path $LogDir 'tunnel.log'
    $err = Join-Path $LogDir 'tunnel.err.log'
    Remove-Item $log, $err -ErrorAction SilentlyContinue

    $p = Start-Process -FilePath 'cloudflared' `
        -ArgumentList 'tunnel', '--url', "http://localhost:$Port" `
        -WorkingDirectory $Root -WindowStyle Hidden -PassThru `
        -RedirectStandardOutput $log -RedirectStandardError $err

    # cloudflared announces the hostname on stderr a second or two after start.
    # [regex]::Match, not -match/$Matches. When the left operand of -match is an
    # array rather than a string, PowerShell filters it instead of testing it:
    # the result is truthy but $Matches is never set, and reading $Matches[0]
    # then dies with "Cannot index into a null array". Get-Content can hand back
    # an array while the file is mid-write, which is exactly when this runs.
    $pattern = 'https://[a-z0-9-]+\.trycloudflare\.com'
    $deadline = (Get-Date).AddSeconds(40)
    while ((Get-Date) -lt $deadline) {
        foreach ($f in @($err, $log)) {
            if (Test-Path $f) {
                $text = (Get-Content $f -Raw -ErrorAction SilentlyContinue) -join "`n"
                $m = [regex]::Match($text, $pattern)
                if ($m.Success) { return @{ Proc = $p; Url = $m.Value } }
            }
        }
        if ($p.HasExited) { return @{ Proc = $null; Url = $null } }
        Start-Sleep -Milliseconds 500
    }
    return @{ Proc = $p; Url = $null }
}

function Wait-PublicUrl($url, $seconds = 60, $settle = 10) {
    <#
      Two things had to be got right here, and the second one is not obvious.

      1. POLL, do not check once. cloudflared prints the hostname about three
         seconds in, but Cloudflare publishes the DNS record a moment later.

      2. DO NOT LOOK TOO EARLY, AND FLUSH BETWEEN TRIES. A lookup that fails
         because the name does not exist yet is cached by Windows as a NEGATIVE
         entry. Every later attempt then reads that cached "does not exist"
         rather than asking again -- so polling for another 75 seconds changes
         nothing, and the browser is poisoned too.

         That is what made the first version look like a tunnel problem when it
         was not: measured, the hostname resolved fine the moment the cache was
         cleared, and answered 401 immediately. cloudflared had been right all
         along and the script was reading its own stale failure.

         So: wait `$settle` seconds before looking at all, and clear the resolver
         cache between attempts.

      Any HTTP status counts as success. 401 is the expected one -- it proves
      the request reached our app and the password middleware answered, which
      exercises the entire path. A dead tunnel returns no response at all.
    #>
    Start-Sleep -Seconds $settle
    $deadline = (Get-Date).AddSeconds($seconds)
    $lastMsg = 'no attempt made'
    while ((Get-Date) -lt $deadline) {
        try {
            $r = Invoke-WebRequest $url -TimeoutSec 10 -UseBasicParsing
            return @{ Ok = $true; Code = $r.StatusCode }
        } catch {
            if ($_.Exception.Response) {
                return @{ Ok = $true; Code = [int]$_.Exception.Response.StatusCode }
            }
            $lastMsg = $_.Exception.Message      # dns/connect: not ready yet
        }
        # Drop the negative entry this attempt just created, or the next one
        # reads it back instead of asking Cloudflare again.
        try { Clear-DnsClientCache } catch {}
        Start-Sleep -Seconds 4
    }
    return @{ Ok = $false; Code = $null; Message = $lastMsg }
}

# ---------------------------------------------------------------- run

Write-Host ''
Write-Host '  Shabad Library' -ForegroundColor Cyan
Write-Host ''

# Does the database have any accounts?
#
# This used to check APP_PASSWORD in .env, which is now wrong: since accounts
# moved into the database, .env's password only ever SEEDS the first admin at
# migration. Change your password in the app and .env goes stale -- so a check
# against it would either block a perfectly secure setup or, worse, pass on a
# database with no accounts at all.
if (-not $NoTunnel) {
    $probe = python -c "import sqlite3,sys; d=sqlite3.connect(r'$Root\shabads.db'); print(d.execute('SELECT COUNT(*) FROM users').fetchone()[0])" 2>$null
    if ($LASTEXITCODE -ne 0 -or [int]$probe -lt 1) {
        Write-Host '  This database has no accounts.' -ForegroundColor Red
        Write-Host '  A tunnel without one publishes your library to anyone who' -ForegroundColor Red
        Write-Host '  finds the link. Run:' -ForegroundColor Red
        Write-Host '      python tools\migrate_multiuser.py --write' -ForegroundColor Red
        Write-Host '  or pass -NoTunnel to run locally.' -ForegroundColor Red
        Write-Host ''
        exit 1
    }
    Say "$probe account(s) registered"
}

Stop-Everything
Say 'starting the app...'
$app = Start-App
if (-not (Wait-Health)) {
    Say 'the app did not come up. Last lines of logs\server.err.log:' 'Red'
    Get-Content (Join-Path $LogDir 'server.err.log') -Tail 15 -ErrorAction SilentlyContinue
    Send-Ntfy 'Shabad Library FAILED' 'The app did not start. Check logs\server.err.log' 'rotating_light'
    exit 1
}
Say "app is up on http://localhost:$Port  (pid $($app.Id))" 'Green'

if ($NoTunnel) {
    Write-Host ''
    Say "open http://localhost:$Port" 'Cyan'
    Write-Host ''
    exit 0
}

if ($AppOnly) {
    # The tunnel was never touched, so the address is unchanged and there is
    # nothing to announce. Silence here is the feature: a restart that keeps
    # the same link should not put a notification on your phone.
    $existing = $null
    $f = Join-Path $LogDir 'current-url.txt'
    if (Test-Path $f) { $existing = (Get-Content $f -Raw).Trim() }
    Say "tunnel left running; url unchanged: $existing" 'Green'
    exit 0
}

$url = $null
for ($i = 1; $i -le $Tries; $i++) {
    Say "starting the tunnel (attempt $i of $Tries)..."
    $t = Start-Tunnel
    if ($t.Url) {
        Say "got $($t.Url)"
        Say 'waiting for it to go live (dns takes a few seconds)...'
        $check = Wait-PublicUrl $t.Url
        if ($check.Ok) {
            Say "it answers (HTTP $($check.Code))" 'Green'
            $url = $t.Url
            break
        }
        Say "still unreachable: $($check.Message)" 'DarkYellow'

        # Last resort before throwing away a tunnel: ask. Flush first, or the
        # browser reads back the same negative entry this script just created
        # and says no to a hostname that is actually live. A phone on mobile
        # data also resolves through a different server than this machine.
        if (-not $Unattended) {
            try { Clear-DnsClientCache } catch {}
            Write-Host ''
            Write-Host "  Try it yourself:  $($t.Url)" -ForegroundColor Cyan
            $answer = Read-Host '  Does it load (a password prompt counts)? [y/N]'
            if ($answer -match '^(y|yes)$') { $url = $t.Url; break }
        }
        Say 'discarding it and asking for a fresh one' 'DarkYellow'
    } else {
        Say 'the tunnel did not report a url' 'DarkYellow'
    }
    if ($t.Proc) { try { Stop-Process -Id $t.Proc.Id -Force } catch {} }
    Start-Sleep -Seconds 2
}

if (-not $url) {
    Say 'could not get a working tunnel.' 'Red'
    Get-Content (Join-Path $LogDir 'tunnel.err.log') -Tail 15 -ErrorAction SilentlyContinue
    Send-Ntfy 'Shabad Library FAILED' 'The app is running but no tunnel could be established.' 'rotating_light'
    exit 1
}

try { Set-Clipboard -Value $url } catch {}
# ASCII, no BOM. Set-Content -Encoding UTF8 writes a byte-order mark in
# PowerShell 5.1, which then comes back on the front of the string and makes an
# unchanged url compare as different.
Set-Content (Join-Path $LogDir 'current-url.txt') $url -Encoding ASCII

Write-Host ''
Write-Host "  $url" -ForegroundColor Green
Write-Host ''
Say "sign in as '$($DotEnv['APP_USER'])' if it asks." 'DarkGray'
Write-Host ''

Send-Ntfy 'New Shabad Library link' $url 'link'

if ($Watch) {
    # Hand off to the watchdog, detached, so this window can be closed and the
    # thing keeps running. Its own single-instance guard means starting it twice
    # is harmless.
    Start-Process -FilePath 'powershell' -WindowStyle Hidden `
        -ArgumentList '-NoProfile', '-ExecutionPolicy', 'Bypass', '-File',
                      (Join-Path $PSScriptRoot 'watchdog.ps1'),
                      '-Port', $Port, '-NoInitialStart' | Out-Null
    Say 'watchdog started in the background -- it will keep this running.' 'Cyan'
    Say "log: logs\watchdog.log" 'DarkGray'
    Write-Host ''
}
