$ErrorActionPreference = 'Stop'
$keyPath = Join-Path $env:USERPROFILE '.ssh\qq_deepseek_20261113'
Write-Host 'DeepSeek QQ bot setup: type your API Key below. It will stay hidden.'
$secureKey = Read-Host 'DeepSeek API Key' -AsSecureString
$keyPointer = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secureKey)
try {
    $plainKey = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($keyPointer)
    $plainKey | & ssh.exe -i $keyPath -o IdentitiesOnly=yes -o BatchMode=yes root@8.217.178.5 'python3 /opt/qq-deepseek/configure-server.py --set-key'
    if ($LASTEXITCODE -ne 0) { throw 'Key setup failed.' }
    Write-Host 'Saved. Return to Codex so we can verify the connection.'
} finally {
    $plainKey = $null
    [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($keyPointer)
    $secureKey.Dispose()
}
Read-Host 'Press Enter to close'
