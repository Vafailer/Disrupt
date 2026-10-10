param([switch]$CheckOnly, [switch]$SelfTest)

$ErrorActionPreference = 'Stop'
$taskSshOptions = @(
    '-T', '-o', 'IPQoS=none', '-o', 'BatchMode=yes',
    '-o', 'StrictHostKeyChecking=yes', '-o', 'HostKeyAlias=178.217.98.101',
    '-o', 'ConnectTimeout=20', '-o', 'ForwardAgent=no',
    '-J', 'root@185.106.94.251', 'root@10.77.0.1'
)

& ssh @taskSshOptions 'test -d /opt/beresta/repository/secrets'
if ($LASTEXITCODE -ne 0) {
    throw 'SSH check failed. No key was requested or saved.'
}
$taskReceiverPath = Join-Path $PSScriptRoot 'smtp_bz_key_receiver.py'
$taskReceiverBase64 = [Convert]::ToBase64String([IO.File]::ReadAllBytes($taskReceiverPath))
$taskInstaller = @"
import base64,os
from pathlib import Path
directory=Path('/opt/beresta/repository/.local')
if directory.is_symlink():
    raise SystemExit('Unsafe receiver directory')
directory.mkdir(mode=0o700,exist_ok=True)
target=directory/'smtp-bz-key-receiver.py'
fd=os.open(target,os.O_WRONLY|os.O_CREAT|os.O_TRUNC|os.O_NOFOLLOW,0o600)
with os.fdopen(fd,'wb') as stream:
    stream.write(base64.b64decode('$taskReceiverBase64'))
    os.fchmod(stream.fileno(),0o600)
"@
# Public Python source uses stdin; the remote shell never has to parse quoted Python code.
$taskInstaller | & ssh @taskSshOptions 'python3 -'
if ($LASTEXITCODE -ne 0) { throw 'Could not install receiver. No key was requested or saved.' }
$taskRemoteCommand = 'python3 /opt/beresta/repository/.local/smtp-bz-key-receiver.py'
& ssh @taskSshOptions ($taskRemoteCommand + ' --check')
if ($LASTEXITCODE -ne 0) { throw 'Receiver check failed. No key was requested or saved.' }
if ($CheckOnly) { exit 0 }
if ($SelfTest) {
    $taskSecureKey = ConvertTo-SecureString 'synthetic-smtp-bz-key-not-a-real-credential' -AsPlainText -Force
    $taskRemoteCommand += ' --self-test'
} else {
    $taskSecureKey = Read-Host 'Paste SMTP.BZ API key (input hidden)' -AsSecureString
}
$taskKeyPointer = [IntPtr]::Zero
$taskPlainKey = $null
try {
    $taskKeyPointer = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($taskSecureKey)
    $taskPlainKey = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($taskKeyPointer)
    $taskPlainKey | & ssh @taskSshOptions $taskRemoteCommand
    if ($LASTEXITCODE -ne 0) { throw 'Key was not saved successfully. Check the SSH message above.' }
} finally {
    $taskPlainKey = $null
    if ($taskKeyPointer -ne [IntPtr]::Zero) {
        [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($taskKeyPointer)
    }
    $taskSecureKey.Dispose()
}
