$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot
$demoPython = Join-Path $PSScriptRoot '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $demoPython)) {
    throw 'Create .venv and install requirements.txt first. See README.md.'
}
# This launcher is always offline with respect to AI, even if credentials exist elsewhere.
$env:NOTES_PROVIDER = 'mock'
$env:NOTES_ALLOW_LIVE_REQUESTS = 'false'
$env:NOTES_AUTO_WORKER = 'true'
$env:NOTES_DATABASE_URL = 'sqlite:///./data/notes.db'
$env:NOTES_PUBLIC_ORIGIN = 'http://127.0.0.1:8000'
$env:NOTES_SECURE_COOKIES = 'false'
$env:NOTES_ALLOW_REGISTRATION = 'true'
& $demoPython -m alembic upgrade head
if ($LASTEXITCODE -ne 0) { throw 'Database migration failed.' }
Write-Host 'Demo: http://127.0.0.1:8000 (mock mode, no AI API calls)'
& $demoPython -m uvicorn app.main:create_app --factory --host 127.0.0.1 --port 8000
