param([string]$Username = 'mark', [switch]$ConfirmOnly)
$ErrorActionPreference = 'Stop'
$OutputEncoding = New-Object Text.UTF8Encoding($false)
if ($Username -cnotmatch '^[a-z0-9_.-]{3,64}$') { throw 'Use a lowercase administrator login.' }
$taskSsh = @('-T','-o','IPQoS=none','-o','BatchMode=yes','-o','StrictHostKeyChecking=yes',
    '-o','HostKeyAlias=178.217.98.101','-o','ConnectTimeout=20','-o','ForwardAgent=no',
    '-J','root@185.106.94.251','root@10.77.0.1')
$taskScp = @('-q','-o','IPQoS=none','-o','BatchMode=yes','-o','StrictHostKeyChecking=yes',
    '-o','HostKeyAlias=178.217.98.101','-o','ConnectTimeout=20','-o','ForwardAgent=no','-J','root@185.106.94.251')
$taskPrivateDirectory = Join-Path (Split-Path (Split-Path $PSScriptRoot -Parent) -Parent) 'Beresta-owner-secrets'
if (!(Test-Path -LiteralPath $taskPrivateDirectory)) { New-Item -ItemType Directory -Path $taskPrivateDirectory | Out-Null }
$taskSid = [Security.Principal.WindowsIdentity]::GetCurrent().User
$taskAcl = New-Object Security.AccessControl.DirectorySecurity
$taskAcl.SetAccessRuleProtection($true,$false)
$taskAcl.SetOwner($taskSid)
$taskAcl.AddAccessRule((New-Object Security.AccessControl.FileSystemAccessRule($taskSid,'FullControl','ContainerInherit,ObjectInherit','None','Allow')))
$taskAcl.AddAccessRule((New-Object Security.AccessControl.FileSystemAccessRule('NT AUTHORITY\SYSTEM','FullControl','ContainerInherit,ObjectInherit','None','Allow')))
Set-Acl -LiteralPath $taskPrivateDirectory -AclObject $taskAcl
$taskLocalBundle = Join-Path $taskPrivateDirectory ($Username + '-authenticator.txt')
if (!$ConfirmOnly -and (Test-Path -LiteralPath $taskLocalBundle)) { throw 'Local authenticator bundle already exists. Use -ConfirmOnly if only the code needs confirmation.' }
& ssh @taskSsh 'test -f /opt/beresta/current/app/admin_cli.py'
if ($LASTEXITCODE -ne 0) { throw 'The security release is not installed or SSH is unavailable.' }
& scp @taskScp (Join-Path $PSScriptRoot 'admin_enroll_receiver.py') 'root@10.77.0.1:/opt/beresta/repository/.local/admin-enroll-receiver.py'
if ($LASTEXITCODE -ne 0) { throw 'Could not install public enrollment helper.' }
& ssh @taskSsh 'install -d -o 1000 -g 1000 -m 700 /opt/beresta/repository/.local/admin-enroll'
if ($LASTEXITCODE -ne 0) { throw 'Could not prepare private enrollment directory.' }

function Send-PrivateInput([string]$Command, [string]$Property, [Security.SecureString]$Value) {
    $taskPointer = [IntPtr]::Zero
    try {
        $taskPointer = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($Value)
        $taskPlain = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($taskPointer)
        $taskObject = @{}; $taskObject[$Property] = $taskPlain
        $taskJson = ConvertTo-Json -Compress -InputObject $taskObject
        $taskRemote = 'beresta-core run --rm -T --no-deps -e PYTHONPATH=/app -v /opt/beresta/repository/.local/admin-enroll-receiver.py:/enroll-receiver.py:ro -v /opt/beresta/repository/.local/admin-enroll:/enroll api python /enroll-receiver.py ' + $Command + ' ' + $Username
        $taskJson | & ssh @taskSsh $taskRemote
        if ($LASTEXITCODE -ne 0) { throw 'Administrator setup failed. Check the message above.' }
    } finally {
        $taskPlain = $null; $taskJson = $null; $taskObject = $null
        if ($taskPointer -ne [IntPtr]::Zero) { [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($taskPointer) }
        $Value.Dispose()
    }
}
if (!$ConfirmOnly) {
$taskPassword = Read-Host 'New administrator password, at least 12 characters (hidden)' -AsSecureString
$taskRepeat = Read-Host 'Repeat administrator password (hidden)' -AsSecureString
$taskFirstPointer = [IntPtr]::Zero; $taskSecondPointer = [IntPtr]::Zero
try {
    $taskFirstPointer = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($taskPassword)
    $taskSecondPointer = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($taskRepeat)
    if ([Runtime.InteropServices.Marshal]::PtrToStringBSTR($taskFirstPointer) -cne [Runtime.InteropServices.Marshal]::PtrToStringBSTR($taskSecondPointer)) {
        $taskPassword.Dispose(); throw 'Passwords do not match. Nothing was created.'
    }
} finally {
    if ($taskFirstPointer -ne [IntPtr]::Zero) { [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($taskFirstPointer) }
    if ($taskSecondPointer -ne [IntPtr]::Zero) { [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($taskSecondPointer) }
    $taskRepeat.Dispose()
}
Send-PrivateInput 'create' 'password' $taskPassword
$taskRemoteBundle = '/opt/beresta/repository/.local/admin-enroll/' + $Username + '.txt'
& scp @taskScp ('root@10.77.0.1:' + $taskRemoteBundle) $taskLocalBundle
if ($LASTEXITCODE -ne 0) { throw 'Account created, but the authenticator bundle was not downloaded. Keep the server copy and contact Codex.' }
$taskRemoteHash = & ssh @taskSsh ('sha256sum ' + $taskRemoteBundle)
if ($LASTEXITCODE -ne 0 -or (Get-FileHash -LiteralPath $taskLocalBundle -Algorithm SHA256).Hash.ToLower() -ne ($taskRemoteHash -split ' ')[0]) {
    throw 'Authenticator copy was not verified. The server copy has been kept.'
}
& ssh @taskSsh ('rm -- ' + $taskRemoteBundle)
if ($LASTEXITCODE -ne 0) { Write-Warning 'Private temporary server copy could not be removed.' }
Write-Host 'Authenticator setup is saved in your private folder. Keep another copy in your password manager.'
Start-Process -FilePath notepad.exe -ArgumentList ('"' + $taskLocalBundle + '"')
Write-Host 'In Google Authenticator choose Add account, Enter setup key, Time based. Use the key from the opened file.'
}
$taskCode = Read-Host 'Six-digit code from your authenticator (hidden)' -AsSecureString
Send-PrivateInput 'confirm' 'code' $taskCode
Write-Host 'Administrator confirmed. Open open-admin.cmd to access the page.'
