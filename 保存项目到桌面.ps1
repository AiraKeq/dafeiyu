$ErrorActionPreference = 'Stop'
$fishSource = 'F:\work\qq-deepseek-deploy'
$fishDesktop = [Environment]::GetFolderPath('Desktop')
$fishTarget = Join-Path $fishDesktop '大肥鱼'
New-Item -ItemType Directory -Path $fishTarget -Force | Out-Null
foreach ($fishSubdir in @('deployment', 'scripts', 'docs', 'artifacts', 'plugins')) {
    New-Item -ItemType Directory -Path (Join-Path $fishTarget $fishSubdir) -Force | Out-Null
}
Copy-Item -LiteralPath (Join-Path $fishSource 'PROJECT.md') -Destination (Join-Path $fishTarget 'README.md') -Force
foreach ($fishDoc in @('STATUS.md', 'TODO.md', '正式启用.md')) {
    Copy-Item -LiteralPath (Join-Path $fishSource $fishDoc) -Destination (Join-Path $fishTarget $fishDoc) -Force
}
$fishDeploymentDoc = [System.IO.File]::ReadAllText((Join-Path $fishSource 'README.md'), [System.Text.Encoding]::UTF8)
$fishDeploymentDoc = $fishDeploymentDoc.Replace('(STATUS.md)', '(../STATUS.md)').Replace('(plugins/astrbot_plugin_whale_memory/README.md)', '(../plugins/astrbot_plugin_whale_memory/README.md)')
[System.IO.File]::WriteAllText((Join-Path $fishTarget 'docs\部署记录.md'), $fishDeploymentDoc, [System.Text.UTF8Encoding]::new($true))
foreach ($fishFile in @('compose.yaml', 'open-panels.ps1', 'set-deepseek-key.ps1')) {
    Copy-Item -LiteralPath (Join-Path $fishSource $fishFile) -Destination (Join-Path $fishTarget 'deployment') -Force
}
Get-ChildItem -LiteralPath $fishSource -File -Filter '*.py' | ForEach-Object {
    Copy-Item -LiteralPath $_.FullName -Destination (Join-Path $fishTarget 'scripts') -Force
}
$fishPluginSource = Join-Path $fishSource 'plugins\astrbot_plugin_whale_memory'
$fishPluginTarget = Join-Path $fishTarget 'plugins\astrbot_plugin_whale_memory'
New-Item -ItemType Directory -Path $fishPluginTarget -Force | Out-Null
Get-ChildItem -LiteralPath $fishPluginSource -File | ForEach-Object {
    Copy-Item -LiteralPath $_.FullName -Destination $fishPluginTarget -Force
}
New-Item -ItemType Directory -Path (Join-Path $fishPluginTarget 'tests') -Force | Out-Null
Get-ChildItem -LiteralPath (Join-Path $fishPluginSource 'tests') -File -Filter '*.py' | ForEach-Object {
    Copy-Item -LiteralPath $_.FullName -Destination (Join-Path $fishPluginTarget 'tests') -Force
}
Get-ChildItem -LiteralPath (Join-Path $fishSource 'artifacts') -File -Filter '*.zip*' | ForEach-Object {
    Copy-Item -LiteralPath $_.FullName -Destination (Join-Path $fishTarget 'artifacts') -Force
}
$fishSegmentDoc = Join-Path $fishSource 'SEGMENTATION.md'
if (Test-Path -LiteralPath $fishSegmentDoc) {
    Copy-Item -LiteralPath $fishSegmentDoc -Destination (Join-Path $fishTarget 'SEGMENTATION.md') -Force
}
Copy-Item -LiteralPath (Join-Path $fishSource 'ENHANCEMENTS.md') -Destination (Join-Path $fishTarget 'ENHANCEMENTS.md') -Force
New-Item -ItemType Directory -Path (Join-Path $fishTarget 'fixtures') -Force | Out-Null
Copy-Item -LiteralPath (Join-Path $fishSource 'fixtures\vision-test.png') -Destination (Join-Path $fishTarget 'fixtures\vision-test.png') -Force
$fishPersona = Join-Path $fishSource 'persona-魔丸风格.txt'
if (Test-Path -LiteralPath $fishPersona) {
    Copy-Item -LiteralPath $fishPersona -Destination (Join-Path $fishTarget 'persona-魔丸风格.txt') -Force
}
Copy-Item -LiteralPath $PSCommandPath -Destination (Join-Path $fishTarget '保存项目到桌面.ps1') -Force
Write-Output "Project saved: $fishTarget"
