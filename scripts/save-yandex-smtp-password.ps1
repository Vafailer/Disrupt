param([switch]$CheckOnly)
$ErrorActionPreference = 'Stop'
$OutputEncoding = New-Object Text.UTF8Encoding($false)
$taskSsh = @('-T','-o','IPQoS=none','-o','BatchMode=yes','-o','StrictHostKeyChecking=yes',
    '-o','HostKeyAlias=178.217.98.101','-o','ConnectTimeout=20','-o','ForwardAgent=no',
    '-J','root@185.106.94.251','root@10.77.0.1')
$taskScp = @('-q','-o','IPQoS=none','-o','BatchMode=yes','-o','StrictHostKeyChecking=yes',
    '-o','HostKeyAlias=178.217.98.101','-o','ConnectTimeout=20','-o','ForwardAgent=no','-J','root@185.106.94.251')
& scp @taskScp (Join-Path $PSScriptRoot 'smtp_password_receiver.py') 'root@10.77.0.1:/opt/beresta/repository/.local/smtp-password-receiver.py'
if ($LASTEXITCODE -ne 0) { throw 'Could not install receiver. No password was requested.' }
$taskCommand = 'python3 /opt/beresta/repository/.local/smtp-password-receiver.py'
& ssh @taskSsh ($taskCommand + ' --check')
if ($LASTEXITCODE -ne 0) { throw 'Receiver preflight failed. No password was requested.' }
if ($CheckOnly) { exit 0 }
$taskValue = Read-Host 'Paste Yandex MAIL APP PASSWORD, not your account password (hidden)' -AsSecureString
$taskPointer = [IntPtr]::Zero
try {
    $taskPointer = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($taskValue)
    $taskPlain = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($taskPointer)
    $taskPlain | & ssh @taskSsh $taskCommand
    if ($LASTEXITCODE -ne 0) { throw 'Password was not saved successfully. Check the SSH message above.' }
} finally {
    $taskPlain = $null
    if ($taskPointer -ne [IntPtr]::Zero) { [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($taskPointer) }
    $taskValue.Dispose()
}
