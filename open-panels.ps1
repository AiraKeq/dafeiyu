$ErrorActionPreference = 'Stop'
$sshPath = (Get-Command ssh.exe).Source
$keyPath = Join-Path $env:USERPROFILE '.ssh\qq_deepseek_20261113'
if (-not (Test-Path -LiteralPath $keyPath)) {
    throw 'Deployment SSH key was not found.'
}

$listeners = @(Get-NetTCPConnection -State Listen -ErrorAction SilentlyContinue |
    Where-Object { $_.LocalAddress -eq '127.0.0.1' -and $_.LocalPort -in @(6185, 6099) })
if ($listeners.Count -eq 0) {
    $sshArgs = @(
        '-N', '-i', $keyPath,
        '-o', 'IdentitiesOnly=yes', '-o', 'BatchMode=yes',
        '-o', 'ExitOnForwardFailure=yes', '-o', 'ConnectTimeout=10',
        '-o', 'ServerAliveInterval=30', '-o', 'ServerAliveCountMax=3',
        '-L', '127.0.0.1:6185:127.0.0.1:6185',
        '-L', '127.0.0.1:6099:127.0.0.1:6099',
        'root@8.217.178.5'
    )
    Start-Process -FilePath $sshPath -ArgumentList $sshArgs -WindowStyle Hidden | Out-Null
    Start-Sleep -Seconds 2
}

foreach ($port in @(6185, 6099)) {
    try {
        $response = Invoke-WebRequest -UseBasicParsing -Uri "http://127.0.0.1:$port/" -TimeoutSec 10
        Write-Host "Panel $port HTTP $($response.StatusCode)"
    } catch {
        throw "Panel $port is unavailable. Check the server containers and local SSH tunnel."
    }
}
Write-Host 'AstrBot: http://127.0.0.1:6185/'
Write-Host 'NapCat:  http://127.0.0.1:6099/webui/'
