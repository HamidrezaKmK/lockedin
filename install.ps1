$ErrorActionPreference = 'Stop'
$python = $null
$pythonArgs = @()
if ($env:PYTHON) { $python = Get-Command $env:PYTHON -ErrorAction SilentlyContinue }
if (-not $python) { $python = Get-Command python -ErrorAction SilentlyContinue }
if (-not $python) {
  # The CPython Windows installer commonly provides the ``py`` launcher even when ``python``
  # has not been added to PATH. It lets us support a normal Windows installation without asking
  # the user to change execution aliases first.
  $python = Get-Command py -ErrorAction SilentlyContinue
  if ($python) { $pythonArgs = @('-3') }
}
if (-not $python) { throw 'Python 3.11+ is required. Install Python from python.org, then rerun this command.' }
$pythonVersion = [string](& $python.Source @pythonArgs -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')")
$pythonVersion = $pythonVersion.Trim()
if ($LASTEXITCODE -ne 0 -or -not $pythonVersion -or [version]$pythonVersion -lt [version]'3.11') {
  throw 'Python 3.11+ is required. Set the PYTHON environment variable to its executable if needed.'
}
# Pin the launcher to the interpreter version just verified; a later Python installation cannot
# silently make the installed command choose an older interpreter.
if ($pythonArgs.Count) { $pythonArgs = @("-$pythonVersion") }
if (-not $env:LOCALAPPDATA) { throw 'LOCALAPPDATA is unavailable; cannot choose a per-user install location.' }
$root = Join-Path $env:LOCALAPPDATA 'LockedInScientist\client'
$bin = Join-Path $env:LOCALAPPDATA 'LockedInScientist\bin'
New-Item -ItemType Directory -Force -Path $root, $bin | Out-Null
$client = Join-Path $root 'scientist_cli.py'
$branch = 'main'
$commit = (Invoke-RestMethod "https://api.github.com/repos/HamidrezaKmK/lockedin/commits/$branch").sha
if (-not $commit) { throw 'Could not resolve the current LockedIn Scientist release from main.' }
# Fetch an immutable commit URL rather than a branch URL: raw GitHub branch responses can be
# served from an older CDN cache immediately after a release.
$clientTemp = Join-Path $root ("scientist_cli." + [guid]::NewGuid().ToString('N') + '.tmp')
Invoke-WebRequest "https://raw.githubusercontent.com/HamidrezaKmK/lockedin/$commit/src/lockedin/scientist_cli.py" -OutFile $clientTemp
Move-Item -Force -Path $clientTemp -Destination $client
$pythonArgText = $pythonArgs -join ' '
"@echo off`r`n`"$($python.Source)`" $pythonArgText `"$client`" %*`r`n" | Set-Content (Join-Path $bin 'lockedin-scientist.cmd') -NoNewline
Copy-Item (Join-Path $bin 'lockedin-scientist.cmd') (Join-Path $bin 'lockedin_scientist.cmd')

# Persist the command directory for future terminals and update this one immediately. Avoid
# setx: it can truncate a long PATH and does not affect the current PowerShell process.
$userPath = [Environment]::GetEnvironmentVariable('Path', 'User')
$userEntries = @($userPath -split ';' | Where-Object { $_ })
$hasUserEntry = $userEntries | Where-Object { $_.TrimEnd('\') -ieq $bin.TrimEnd('\') }
if (-not $hasUserEntry) {
  try {
    [Environment]::SetEnvironmentVariable('Path', (($userEntries + $bin) -join ';'), 'User')
    Write-Host "Added $bin to your user PATH."
  } catch {
    Write-Warning "Could not update your user PATH. Run this command directly: $bin\lockedin-scientist.cmd"
  }
}
$sessionEntries = @($env:Path -split ';' | Where-Object { $_ })
$hasSessionEntry = $sessionEntries | Where-Object { $_.TrimEnd('\') -ieq $bin.TrimEnd('\') }
if (-not $hasSessionEntry) { $env:Path = "$bin;$env:Path" }

Write-Host "Installed only lockedin-scientist. Run: lockedin-scientist login --server https://lockedin.codes"
Write-Host "Updated command: $bin\lockedin-scientist.cmd"
Write-Host "Updated source:  $client"
Write-Host "Source commit:   $commit"
