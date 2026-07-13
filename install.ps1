$ErrorActionPreference = 'Stop'
$repo = 'HamidrezaKmK/lockedin'
$version = if ($env:LOCKEDIN_SCIENTIST_VERSION) { $env:LOCKEDIN_SCIENTIST_VERSION } else { 'latest' }
$dest = Join-Path $env:LOCALAPPDATA 'LockedInScientist\bin'
New-Item -ItemType Directory -Force -Path $dest | Out-Null
$asset = 'lockedin-scientist-windows-x64.exe'
$base = "https://github.com/$repo/releases/$version/download"
$file = Join-Path $dest 'lockedin-scientist.exe'
Invoke-WebRequest "$base/$asset" -OutFile $file
$sumFile = "$file.sha256"
Invoke-WebRequest "$base/$asset.sha256" -OutFile $sumFile
$expected = (Get-Content $sumFile).Split(' ', [System.StringSplitOptions]::RemoveEmptyEntries)[0]
$actual = (Get-FileHash $file -Algorithm SHA256).Hash.ToLower()
Remove-Item $sumFile
if ($expected.ToLower() -ne $actual) { Remove-Item $file; throw 'Checksum verification failed.' }
Write-Host "Installed $file. Add $dest to PATH, then run: lockedin-scientist login --server https://your-lockedin.example"
