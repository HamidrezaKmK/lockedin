$ErrorActionPreference = 'Stop'
if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
  throw 'uv is required. Install it from https://docs.astral.sh/uv/getting-started/installation/ and rerun this command.'
}
uv tool install --force --refresh 'git+https://github.com/HamidrezaKmK/lockedin.git@scientist'
Write-Host 'Installed lockedin-scientist from the scientist branch. Run: lockedin-scientist login --server https://your-lockedin.example'
