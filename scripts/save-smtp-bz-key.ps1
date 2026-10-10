param([switch]$CheckOnly)

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
if ($CheckOnly) {
    Write-Host 'SSH and secrets directory are ready.'
    exit 0
}

$taskReceiver = @'
import os,sys
from pathlib import Path

directory=Path('/opt/beresta/repository/secrets')
target=directory/'smtp_bz_api_key.txt'
if directory.is_symlink() or not directory.is_dir():
    raise SystemExit('Unsafe or missing secrets directory')
value=sys.stdin.buffer.read(8193).strip()
if not value or len(value)>8192 or b'\0' in value or any(c in value for c in b'\r\n\t '):
    raise SystemExit('Invalid API key; nothing saved')
try:
    fd=os.open(target,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
except FileExistsError:
    raise SystemExit('SMTP key file already exists; it was not changed')
try:
    with os.fdopen(fd,'wb') as stream:
        stream.write(value+b'\n')
        stream.flush()
        os.fchown(stream.fileno(),1000,1000)
        os.fchmod(stream.fileno(),0o400)
        os.fsync(stream.fileno())
except BaseException:
    target.unlink(missing_ok=True)
    raise SystemExit('Could not save key; incomplete file removed') from None
print('SMTP.BZ API key saved on the core VPS. No requests sent.')
'@

$taskReceiverBase64 = [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($taskReceiver.Replace("`r", '')))
$taskRemoteCommand = "python3 -c `"import base64;exec(base64.b64decode('$taskReceiverBase64'))`""
$taskSecureKey = Read-Host 'Paste SMTP.BZ API key (input hidden)' -AsSecureString
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
