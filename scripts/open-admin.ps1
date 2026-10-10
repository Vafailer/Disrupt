$ErrorActionPreference = 'Stop'
$taskOptions = @('-N','-o','IPQoS=none','-o','BatchMode=yes','-o','StrictHostKeyChecking=yes',
    '-o','HostKeyAlias=178.217.98.101','-o','ConnectTimeout=20','-o','ForwardAgent=no',
    '-o','ExitOnForwardFailure=yes','-o','ServerAliveInterval=30','-o','ServerAliveCountMax=3',
    '-L','127.0.0.1:18000:10.77.0.1:18000','-J','root@185.106.94.251','root@10.77.0.1')
$taskSshProcess = Start-Process -FilePath ssh.exe -ArgumentList $taskOptions -PassThru -WindowStyle Hidden
try {
    $taskReady = $false
    for ($taskAttempt = 0; $taskAttempt -lt 20; $taskAttempt++) {
        if ($taskSshProcess.HasExited) { throw 'SSH tunnel could not start. Check ssh-agent and whether port 18000 is already in use.' }
        $taskTcp = New-Object Net.Sockets.TcpClient
        try { $taskTcp.Connect('127.0.0.1',18000); $taskReady = $true } catch {} finally { $taskTcp.Dispose() }
        if ($taskReady) { break }
        Start-Sleep -Milliseconds 500
    }
    if (!$taskReady -or $taskSshProcess.HasExited) { throw 'SSH tunnel is not ready.' }
    Start-Process 'http://127.0.0.1:18000/admin'
    Read-Host 'Keep this window open while using the admin page. Press Enter to close the tunnel' | Out-Null
} finally {
    if (!$taskSshProcess.HasExited) { $taskSshProcess.Kill() }
    $taskSshProcess.Dispose()
}
