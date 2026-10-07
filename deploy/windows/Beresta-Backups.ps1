param([string]$Destination = (Join-Path $PSScriptRoot 'Beresta-recovery\backups\pilot'))
$ErrorActionPreference = 'Stop'
try {
    $sshOptions = @('-T', '-o', 'BatchMode=yes', '-o', 'StrictHostKeyChecking=yes',
        '-o', 'HostKeyAlias=178.217.98.101', '-o', 'ConnectTimeout=15', '-o', 'ForwardAgent=no',
        '-J', 'root@185.106.94.251')
    $metadataText = & ssh @sshOptions root@10.77.0.1 'cat /var/backups/beresta/latest.json'
    if ($LASTEXITCODE -ne 0) { throw 'Cannot read the backup marker. Run Beresta-SSH.cmd if the key is not loaded.' }
    $metadata = ($metadataText -join "`n") | ConvertFrom-Json
    if ($metadata.file -notmatch '^beresta-[0-9]{8}T[0-9]{6}-[a-f0-9]{8}\.tar\.age$' -or
        $metadata.sha256 -notmatch '^[a-f0-9]{64}$') { throw 'Invalid backup metadata' }
    New-Item -ItemType Directory -Force $Destination | Out-Null
    $target = Join-Path $Destination $metadata.file
    if (Test-Path -LiteralPath $target) {
        if ((Get-FileHash -LiteralPath $target -Algorithm SHA256).Hash.ToLower() -ne $metadata.sha256) {
            throw 'An existing backup has a different checksum. It will not be overwritten.'
        }
    } else {
        $partial = $target + '.part'
        & scp -o BatchMode=yes -o StrictHostKeyChecking=yes -o HostKeyAlias=178.217.98.101 `
            -o ConnectTimeout=15 -o ProxyJump=root@185.106.94.251 `
            ('root@10.77.0.1:/var/backups/beresta/' + $metadata.file) $partial
        if ($LASTEXITCODE -ne 0) { throw 'Backup transfer failed. The server copy is unchanged.' }
        if ((Get-FileHash -LiteralPath $partial -Algorithm SHA256).Hash.ToLower() -ne $metadata.sha256) {
            throw 'Checksum mismatch. The partial file is retained for inspection.'
        }
        Move-Item -LiteralPath $partial -Destination $target
    }
    Write-Host ('Encrypted backup verified: ' + $target) -ForegroundColor Green
} catch {
    Write-Host $_.Exception.Message -ForegroundColor Red
}
Read-Host 'Press Enter to close'
