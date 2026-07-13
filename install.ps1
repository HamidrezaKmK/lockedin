$ErrorActionPreference = 'Stop'
$python = Get-Command python -ErrorAction SilentlyContinue
if (-not $python) { throw 'Python 3.11+ is required.' }
$pythonVersion = & $python.Source -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')"
if ([version]$pythonVersion -lt [version]'3.11') { throw 'Python 3.11+ is required.' }
$root = Join-Path $env:LOCALAPPDATA 'LockedInScientist\client'
$bin = Join-Path $env:LOCALAPPDATA 'LockedInScientist\bin'
New-Item -ItemType Directory -Force -Path $root, $bin | Out-Null
$client = Join-Path $root 'scientist_cli.py'
Invoke-WebRequest 'https://raw.githubusercontent.com/HamidrezaKmK/lockedin/scientist/src/lockedin/scientist_cli.py' -OutFile $client
"@echo off`r`n`"$($python.Source)`" `"$client`" %*`r`n" | Set-Content (Join-Path $bin 'lockedin-scientist.cmd') -NoNewline
Copy-Item (Join-Path $bin 'lockedin-scientist.cmd') (Join-Path $bin 'lockedin_scientist.cmd')
Write-Host "Installed only lockedin-scientist. Add $bin to PATH, then run: lockedin-scientist login --server https://lockedin.codes"
Write-Host "To remove it later: lockedin-scientist uninstall"
