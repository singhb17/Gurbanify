<#
  One-command setup for a new machine.

      powershell -ExecutionPolicy Bypass -File tools\setup.ps1

  Installs everything the app needs, checks the things it cannot install, and
  says exactly what is still missing. Safe to run again -- every step checks
  whether it has already been done, so re-running after fixing one thing does
  not redo the rest.

  It NEVER touches shabads.db. Setup and data are separate concerns, and a
  setup script that could damage a library is one you would hesitate to run.
#>

param(
    [switch]$NoVenv,        # install into the system python instead
    [switch]$SkipTorch      # app only -- this machine will not index
)

$ErrorActionPreference = 'Stop'
$Root = Split-Path $PSScriptRoot -Parent
Set-Location $Root

$script:problems = @()
function Ok($m)   { Write-Host "  [ ok ] $m" -ForegroundColor Green }
function Info($m) { Write-Host "  [ .. ] $m" -ForegroundColor Gray }
function Warn($m) { Write-Host "  [warn] $m" -ForegroundColor Yellow }
function Bad($m)  { Write-Host "  [FAIL] $m" -ForegroundColor Red
                    $script:problems += $m }

# winget puts a new tool on PATH for NEW shells only, so this window cannot see
# what it just installed unless we re-read PATH ourselves.
function Sync-Path {
    $env:Path = [Environment]::GetEnvironmentVariable('Path', 'Machine') + ';' +
                [Environment]::GetEnvironmentVariable('Path', 'User')
}

Write-Host ''
Write-Host '  Gurbanify setup' -ForegroundColor Cyan
Write-Host ''

# ---------------------------------------------------------------- python

if (-not (Get-Command python -ErrorAction SilentlyContinue)) {
    Bad 'Python is not installed. Get 3.11+ from python.org and TICK "Add python.exe to PATH", then run this again.'
} else {
    $v = (& python -c "import sys; print('%d.%d' % sys.version_info[:2])")
    if ([version]$v -lt [version]'3.11') {
        Bad "Python $v is too old -- 3.11 or newer is needed."
    } else {
        Ok "Python $v"
    }
}
if ($script:problems) {
    Write-Host ''
    Write-Host '  Fix that first, then run this again.' -ForegroundColor Red
    Write-Host ''
    exit 1
}

# ---------------------------------------------------------------- packages

# A virtual environment keeps this project's packages out of the system python,
# so nothing else on the machine can break it and it cannot break anything else.
# serve.ps1 and watchdog.ps1 find .venv on their own.
$pyExe = 'python'
if (-not $NoVenv) {
    $venvPy = Join-Path $Root '.venv\Scripts\python.exe'
    if (Test-Path $venvPy) {
        Ok 'virtual environment already exists'
    } else {
        Info 'creating a virtual environment in .venv ...'
        & python -m venv (Join-Path $Root '.venv')
        Ok 'created .venv'
    }
    $pyExe = $venvPy
}

Info 'upgrading pip ...'
& $pyExe -m pip install --quiet --upgrade pip setuptools wheel
Ok 'pip up to date'

if ($SkipTorch) {
    Warn 'skipping sentence-transformers -- this machine will NOT be able to index'
    Info 'installing the app packages ...'
    & $pyExe -m pip install --quiet fastapi uvicorn requests numpy scikit-learn pymysql
} else {
    # torch is most of the ~2.5 GB and is the slow part. The default CPU build
    # is the right one: BGE-M3 runs fine on a CPU, and the CUDA build is several
    # GB larger for no benefit at this size.
    Info 'installing packages -- this pulls in torch (~2.5 GB) and takes a while ...'
    & $pyExe -m pip install --quiet -r (Join-Path $Root 'requirements.txt')
}
Ok 'python packages installed'

# ---------------------------------------------------------------- git

# The app runs fine without git. Every LATER change does not: `git pull` is the
# entire update path (MOVING.md), so a missing git is a problem that only shows
# up weeks from now, at the worst moment. Check for it while someone is looking.
if (Get-Command git -ErrorAction SilentlyContinue) {
    Ok 'git is installed'
} elseif (Get-Command winget -ErrorAction SilentlyContinue) {
    Info 'installing git with winget ...'
    try {
        winget install --id Git.Git -e --accept-source-agreements `
                       --accept-package-agreements --silent | Out-Null
    } catch {
        Warn "winget could not install it: $($_.Exception.Message)"
    }
    Sync-Path
    if (Get-Command git -ErrorAction SilentlyContinue) {
        Ok 'git installed'
    } else {
        Warn 'git installed but not on PATH in THIS window -- close it and open a new one'
    }
} else {
    Warn 'git is missing and winget is unavailable. Get it from https://git-scm.com/download/win -- without it, `git pull` cannot fetch later changes.'
}

# A ZIP download is not a repo. It works today and then never updates, and the
# error you get much later ("not a git repository") does not point back here.
if (-not (Test-Path (Join-Path $Root '.git'))) {
    Warn 'this folder is not a git clone -- downloaded as a ZIP? `git pull` will never work here. Clone it instead: git clone https://github.com/singhb17/Gurbanify.git'
}

# ---------------------------------------------------------------- cloudflared

if (Get-Command cloudflared -ErrorAction SilentlyContinue) {
    Ok 'cloudflared is installed'
} elseif (Get-Command winget -ErrorAction SilentlyContinue) {
    Info 'installing cloudflared with winget ...'
    try {
        winget install --id Cloudflare.cloudflared --accept-source-agreements `
                       --accept-package-agreements --silent | Out-Null
    } catch {
        Warn "winget could not install it: $($_.Exception.Message)"
    }
    Sync-Path
    if (Get-Command cloudflared -ErrorAction SilentlyContinue) {
        Ok 'cloudflared installed'
    } else {
        Warn 'cloudflared installed but not on PATH in THIS window -- close it and open a new one'
    }
} else {
    Bad 'cloudflared is missing and winget is unavailable. Download it from https://developers.cloudflare.com/cloudflare-one/connections/connect-networks/downloads/'
}

# ---------------------------------------------------------------- data files

$dbs = @(
    @{ name = 'shabads.db'
       why  = 'your library. Copy it from the old machine -- nothing can recreate it.' },
    @{ name = 'banidb.db'
       why  = 'the Gurbani corpus. Copy it across, or rebuild it with tools/extract_corpus.py (needs Docker).' }
)
foreach ($d in $dbs) {
    $p = Join-Path $Root $d.name
    if (Test-Path $p) {
        Ok ("{0}  ({1:N0} MB)" -f $d.name, ((Get-Item $p).Length / 1MB))
    } else {
        Bad ("{0} is missing -- {1}" -f $d.name, $d.why)
    }
}

if (Test-Path (Join-Path $Root '.env')) {
    Ok '.env'
} else {
    Copy-Item (Join-Path $Root '.env.example') (Join-Path $Root '.env')
    Warn 'no .env -- copied .env.example to .env. Open it and fill in OPENROUTER_API_KEY and NTFY_TOPIC.'
}

# ---------------------------------------------------------------- prove it

Write-Host ''
Info 'checking everything imports ...'

$probe = Join-Path $env:TEMP 'gurbanify-probe.py'
@'
import importlib.util as u
need = ["fastapi", "uvicorn", "requests", "numpy"]
opt = {"sentence_transformers": "indexing and embeddings",
       "sklearn": "the model bench",
       "pymysql": "rebuilding banidb.db"}
print("MISSING:" + ",".join(m for m in need if not u.find_spec(m)))
print("OPTIONAL:" + ",".join(f"{m} ({w})" for m, w in opt.items()
                             if not u.find_spec(m)))
'@ | Set-Content -Path $probe -Encoding UTF8

foreach ($line in (& $pyExe $probe)) {
    if ($line.StartsWith('MISSING:')) {
        $m = $line.Substring(8)
        if ($m) { Bad "these did not install: $m" } else { Ok 'all required packages import' }
    }
    if ($line.StartsWith('OPTIONAL:')) {
        $m = $line.Substring(9)
        if ($m) { Warn "not installed, so these will not work: $m" }
        else    { Ok 'indexing, the bench and the corpus rebuild are all available' }
    }
}
Remove-Item $probe -ErrorAction SilentlyContinue

# ---------------------------------------------------------------- torch loads

# The check above uses find_spec, which proves a package is ON DISK and nothing
# more. torch is a stack of native DLLs that can install perfectly and still
# refuse to load, and the way that surfaces is brutal: the app runs, shabads
# save, summaries get written and PAID FOR, and only then -- minutes into an
# indexing run, in a log file nobody is watching -- does embedding die with
# WinError 1114. This script's whole job is to find that here instead.
#
# The usual cause is an out-of-date Visual C++ runtime. torch needs 14.20+ (the
# first version with vcruntime140_1.dll); a machine can carry a 2017-era 14.11
# indefinitely without anything else noticing, because Python itself only needs
# the part that is already there. The 14.x series is one shared runtime that
# upgrades in place, so installing the current one replaces the old rather than
# sitting beside it, and cannot break anything already working.
if (-not $SkipTorch) {
    $tprobe = Join-Path $env:TEMP 'gurbanify-torch.py'
    @'
import importlib.util as u
if not u.find_spec("torch"):
    print("SKIP")
else:
    try:
        import torch
        print("OK " + torch.__version__)
    except BaseException as e:
        print("FAIL " + type(e).__name__ + ": " + " ".join(str(e).split())[:200])
'@ | Set-Content -Path $tprobe -Encoding UTF8

    Info 'checking torch can actually load ...'
    $r = (& $pyExe $tprobe) -join ' '

    if ($r -like 'FAIL*') {
        Warn "torch is installed but will not load -- $($r.Substring(5))"
        $vcKey = 'HKLM:\SOFTWARE\Microsoft\VisualStudio\14.0\VC\Runtimes\x64'
        $vc = (Get-ItemProperty $vcKey -ErrorAction SilentlyContinue).Version
        Info "Visual C++ runtime: $(if ($vc) { $vc } else { 'not installed' })"
        Info 'installing the current Visual C++ runtime (upgrades in place) ...'
        $vcExe = Join-Path $env:TEMP 'vc_redist.x64.exe'
        try {
            # Deliberately the direct installer and not winget: winget matches on
            # the package being present at all, so an ancient 14.11 makes it
            # decide there is nothing to do and report success.
            $oldPref = $ProgressPreference
            $ProgressPreference = 'SilentlyContinue'
            Invoke-WebRequest 'https://aka.ms/vs/17/release/vc_redist.x64.exe' `
                              -OutFile $vcExe -UseBasicParsing
            $ProgressPreference = $oldPref
            Start-Process $vcExe -ArgumentList '/install','/passive','/norestart' -Wait
            Remove-Item $vcExe -ErrorAction SilentlyContinue
            $now = (Get-ItemProperty $vcKey -ErrorAction SilentlyContinue).Version
            Info "Visual C++ runtime is now $now"
        } catch {
            Warn "could not install it: $($_.Exception.Message)"
        }
        $r = (& $pyExe $tprobe) -join ' '
    }

    if ($r -like 'OK*') {
        Ok "torch loads ($($r.Substring(3)))"
    } elseif ($r -notlike 'SKIP*') {
        # Already reported as a missing optional package if it was SKIP.
        Bad ("torch will not load, so indexing cannot embed anything: $r " +
             '-- install https://aka.ms/vs/17/release/vc_redist.x64.exe by hand, ' +
             'then open a NEW terminal and run this again. If that runtime is ' +
             'already current, the CPU may predate this torch build: ' +
             'pip install --force-reinstall torch==2.2.2')
    }
    Remove-Item $tprobe -ErrorAction SilentlyContinue
}

if (Test-Path (Join-Path $Root 'shabads.db')) {
    $countPy = Join-Path $env:TEMP 'gurbanify-count.py'
    @'
import sqlite3, sys
c = sqlite3.connect(sys.argv[1])
try:
    print(c.execute("SELECT COUNT(*) FROM users").fetchone()[0])
except Exception:
    print("nousers")
'@ | Set-Content -Path $countPy -Encoding UTF8
    $accounts = & $pyExe $countPy (Join-Path $Root 'shabads.db')
    Remove-Item $countPy -ErrorAction SilentlyContinue
    if ($accounts -eq 'nousers') {
        Warn 'the database predates accounts -- run: python tools\migrate_multiuser.py --write'
    } else {
        Ok "$accounts account(s) in the database"
    }
}

# ---------------------------------------------------------------- next

Write-Host ''
if ($script:problems) {
    Write-Host '  NOT READY. Fix these, then run this script again:' -ForegroundColor Red
    foreach ($p in $script:problems) { Write-Host "    - $p" -ForegroundColor Red }
    Write-Host ''
    exit 1
}

Write-Host '  Ready.' -ForegroundColor Green
Write-Host ''
Write-Host '  1. Try it locally first:' -ForegroundColor Cyan
Write-Host '       powershell -ExecutionPolicy Bypass -File tools\serve.ps1 -NoTunnel'
Write-Host '       then open http://localhost:8000 and sign in'
Write-Host ''
Write-Host '  2. Then the real thing:' -ForegroundColor Cyan
Write-Host '       .\restart.bat'
Write-Host ''
Write-Host '  3. Then make it survive reboots, in an ADMIN powershell:' -ForegroundColor Cyan
Write-Host '       powershell -ExecutionPolicy Bypass -File tools\register-task.ps1'
Write-Host '       Start-ScheduledTask -TaskName GurbanifyWatchdog'
Write-Host ''
