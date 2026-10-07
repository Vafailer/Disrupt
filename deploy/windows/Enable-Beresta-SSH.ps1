$ErrorActionPreference = 'Stop'
$principal = New-Object Security.Principal.WindowsPrincipal([Security.Principal.WindowsIdentity]::GetCurrent())
if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    $launchArguments = '-NoProfile -ExecutionPolicy Bypass -File "' + $PSCommandPath + '"'
    Start-Process -FilePath powershell.exe -Verb RunAs -ArgumentList $launchArguments
    exit
}
try {
    Set-Service ssh-agent -StartupType Automatic
    Start-Service ssh-agent
    Write-Host 'Loading Beresta SSH key. Enter the passphrase here if requested.'
    $deployKeyPath = Join-Path $env:USERPROFILE '.ssh\beresta_deploy'
    & "$env:WINDIR\System32\OpenSSH\ssh-add.exe" $deployKeyPath
    if ($LASTEXITCODE -ne 0) { throw 'ssh-add did not load the key.' }
    Write-Host 'Beresta SSH agent is ready. You can return to Codex.' -ForegroundColor Green
} catch {
    Write-Host $_.Exception.Message -ForegroundColor Red
}
Read-Host 'Press Enter to close'
