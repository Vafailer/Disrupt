$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot

$cloudPython = Join-Path $PSScriptRoot '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $cloudPython)) {
    throw 'Create .venv and install requirements.txt first. See README.md.'
}

Write-Host ''
Write-Host 'Cloud.ru live mode' -ForegroundColor Green
Write-Host 'The API key is requested with hidden input and is not saved to a file.'
Write-Host 'Each new note sends one paid request. There are no automatic paid retries.'
Write-Host ''

$secureKey = Read-Host 'Cloud.ru API key' -AsSecureString
$keyPointer = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secureKey)
$plainKey = $null

try {
    $plainKey = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($keyPointer)
    if ([string]::IsNullOrWhiteSpace($plainKey)) {
        throw 'API key is empty.'
    }

    $defaultModel = 'ai-sage/GigaChat3-10B-A1.8B'
    $model = Read-Host "Model ID [$defaultModel]"
    if ([string]::IsNullOrWhiteSpace($model)) { $model = $defaultModel }

    Write-Host 'This is a lifetime cap for data/cloudru.db; restarting does not reset the counter.'
    $limitText = Read-Host 'Maximum number of Cloud.ru requests for this database [5]'
    if ([string]::IsNullOrWhiteSpace($limitText)) { $limitText = '5' }
    $requestLimit = 0
    if (-not [int]::TryParse($limitText, [ref]$requestLimit) -or $requestLimit -lt 1 -or $requestLimit -gt 1000) {
        throw 'Request limit must be an integer from 1 to 1000.'
    }

    $env:NOTES_PROVIDER = 'cloudru'
    $env:NOTES_ALLOW_LIVE_REQUESTS = 'true'
    $env:NOTES_CLOUDRU_API_KEY = $plainKey
    $env:NOTES_CLOUDRU_MODEL = $model
    $env:NOTES_LIVE_CALL_LIMIT = [string]$requestLimit
    $env:NOTES_LIVE_USER_CALL_LIMIT = [string]$requestLimit
    $env:NOTES_AUTO_WORKER = 'true'
    $env:NOTES_DATABASE_URL = 'sqlite:///./data/cloudru.db'
    $env:NOTES_PUBLIC_ORIGIN = 'http://127.0.0.1:8000'
    $env:NOTES_SECURE_COOKIES = 'false'
    $env:NOTES_ALLOW_REGISTRATION = 'true'

    # Remove the second plaintext copy before launching the child process.
    $plainKey = $null

    & $cloudPython -m alembic upgrade head
    if ($LASTEXITCODE -ne 0) { throw 'Database migration failed.' }

    Write-Host ''
    Write-Host "Application: http://127.0.0.1:8000" -ForegroundColor Green
    Write-Host "Model: $model"
    Write-Host "Lifetime request cap for data/cloudru.db: $requestLimit"
    Write-Host 'Keep this window open. Stop the server with Ctrl+C.'
    Write-Host ''
    & $cloudPython -m uvicorn app.main:create_app --factory --host 127.0.0.1 --port 8000
}
finally {
    if ($keyPointer -ne [IntPtr]::Zero) {
        [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($keyPointer)
    }
    $plainKey = $null
    $secureKey = $null
    Remove-Item Env:NOTES_CLOUDRU_API_KEY -ErrorAction SilentlyContinue
}
