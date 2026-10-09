$ErrorActionPreference = 'Stop'
Write-Host '1 - Save Telegram token on Helsinki VPS'
Write-Host '2 - Save Cloud.ru key on core VPS (worker remains disabled)'
Write-Host '0 - Exit'
$choice = Read-Host 'Choose'
switch ($choice) {
    '1' {
        & "$env:WINDIR\System32\OpenSSH\ssh.exe" -t -o BatchMode=yes -o StrictHostKeyChecking=yes -o ConnectTimeout=15 root@185.106.94.251 'cd /opt/beresta/current && python3 deploy/host/save-secret.py telegram_token'
    }
    '2' {
        & "$env:WINDIR\System32\OpenSSH\ssh.exe" -t -o BatchMode=yes -o StrictHostKeyChecking=yes -o HostKeyAlias=178.217.98.101 -o ConnectTimeout=15 -o ForwardAgent=no -J root@185.106.94.251 root@10.77.0.1 'cd /opt/beresta/current && python3 deploy/host/save-secret.py cloudru_api_key'
    }
}
Read-Host 'No service was started. Press Enter to close'
