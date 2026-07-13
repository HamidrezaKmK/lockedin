$ErrorActionPreference = 'Stop'
$python = Get-Command python -ErrorAction SilentlyContinue
if (-not $python) { throw 'Python 3.11+ is required.' }
$pythonVersion = & $python.Source -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')"
if ([version]$pythonVersion -lt [version]'3.11') { throw 'Python 3.11+ is required.' }
$root = Join-Path $env:LOCALAPPDATA 'LockedInScientist\client'
$bin = Join-Path $env:LOCALAPPDATA 'LockedInScientist\bin'
New-Item -ItemType Directory -Force -Path $root, $bin | Out-Null
$client = Join-Path $root 'scientist_cli.py'
$branch = if ($env:LOCKEDIN_SCIENTIST_BRANCH) { $env:LOCKEDIN_SCIENTIST_BRANCH } else { 'scientist' }
$commit = (Invoke-RestMethod "https://api.github.com/repos/HamidrezaKmK/lockedin/commits/$branch").sha
if (-not $commit) { throw "Could not resolve the current LockedIn Scientist commit for branch $branch." }
# Fetch an immutable commit URL rather than a branch URL: raw GitHub branch responses can be
# served from an older CDN cache immediately after a release.
Invoke-WebRequest "https://raw.githubusercontent.com/HamidrezaKmK/lockedin/$commit/src/lockedin/scientist_cli.py" -OutFile $client
"@echo off`r`n`"$($python.Source)`" `"$client`" %*`r`n" | Set-Content (Join-Path $bin 'lockedin-scientist.cmd') -NoNewline
Copy-Item (Join-Path $bin 'lockedin-scientist.cmd') (Join-Path $bin 'lockedin_scientist.cmd')
Write-Host "Installed only lockedin-scientist. Add $bin to PATH, then run: lockedin-scientist login --server https://lockedin.codes"
Write-Host "Updated command: $bin\lockedin-scientist.cmd"
Write-Host "Updated source:  $client"
Write-Host "Source commit:   $commit"
Write-Host "To remove it later: lockedin-scientist uninstall"
