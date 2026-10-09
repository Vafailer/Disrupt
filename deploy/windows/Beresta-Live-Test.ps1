$ErrorActionPreference = 'Stop'
Write-Host 'OWNER ONLY. Starting the worker can spend the real Cloud.ru key.' -ForegroundColor Yellow
Write-Host '1 - Enable processing of queued notes (total and user call limits must be 1)'
Write-Host '2 - Stop the worker and disable live processing'
Write-Host '0 - Exit'
$choice = Read-Host 'Choose'
$remoteCommand = switch ($choice) {
    '1' {
@'
set -eu
cd /opt/beresta/current
grep -qx 'NOTES_LIVE_CALL_LIMIT=1' .env.production
grep -qx 'NOTES_LIVE_USER_CALL_LIMIT=1' .env.production
sed -i 's/^NOTES_ALLOW_LIVE_REQUESTS=.*/NOTES_ALLOW_LIVE_REQUESTS=true/' .env.production
grep -qx 'NOTES_ALLOW_LIVE_REQUESTS=true' .env.production
beresta-core --profile owner-live up -d --no-deps --no-build worker
printf 'Owner started the live worker. Existing queued notes may be processed. Do not submit the same note again.\n'
'@
    }
    '2' {
@'
set -eu
cd /opt/beresta/current
beresta-core stop worker
sed -i 's/^NOTES_ALLOW_LIVE_REQUESTS=.*/NOTES_ALLOW_LIVE_REQUESTS=false/' .env.production
grep -qx 'NOTES_ALLOW_LIVE_REQUESTS=false' .env.production
printf 'Worker stopped and live processing disabled. An in-flight request may already have been charged.\n'
'@
    }
    default { $null }
}
try {
    if ($remoteCommand) {
        $remoteCommand = $remoteCommand -replace "`r`n", "`n"
        & "$env:WINDIR\System32\OpenSSH\ssh.exe" -t -o BatchMode=yes -o StrictHostKeyChecking=yes `
            -o HostKeyAlias=178.217.98.101 -o ConnectTimeout=15 -o ForwardAgent=no `
            -J root@185.106.94.251 root@10.77.0.1 $remoteCommand
        if ($LASTEXITCODE -ne 0) { throw 'Operation failed. Do not retry a note with an unknown outcome. Check server state.' }
    }
} catch {
    Write-Host $_.Exception.Message -ForegroundColor Red
}
Read-Host 'Press Enter to close'
