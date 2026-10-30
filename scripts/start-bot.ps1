<#
在后台启动 bot：关掉终端或 Claude 应用后它照样运行。
输出写到 <数据目录>\logs\roundtable-<时间>.stdout.log / .stderr.log，只留最近 10 次。

    .\scripts\start-bot.ps1                     # 数据目录：$env:ROUNDTABLE_HOME，没有就是 ~\.tg-roundtable
    .\scripts\start-bot.ps1 -DataDir D:\data    # 指定数据目录
    .\scripts\start-bot.ps1 -Restart            # 已经在运行就先停掉再启动
#>
param(
    [string]$DataDir = $env:ROUNDTABLE_HOME,
    [int]$Port = 0,
    [switch]$Restart
)
$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
$python = Join-Path $root '.venv\Scripts\python.exe'
if (-not (Test-Path $python)) { throw "找不到 $python，先在项目目录运行一次 uv sync" }
if (-not $DataDir) { $DataDir = Join-Path $HOME '.tg-roundtable' }

$running = & (Join-Path $PSScriptRoot 'stop-bot.ps1') -List
if ($running) {
    if (-not $Restart) {
        Write-Host "bot 已经在运行（进程 $($running -join ', ')）。要重启就加 -Restart。"
        exit 1
    }
    & (Join-Path $PSScriptRoot 'stop-bot.ps1')
}

$logs = Join-Path $DataDir 'logs'
New-Item -ItemType Directory -Force $logs | Out-Null
Get-ChildItem $logs -Filter 'roundtable-*.log' | Sort-Object LastWriteTime -Descending |
    Select-Object -Skip 20 | Remove-Item -Force
$stamp = Get-Date -Format 'yyyyMMdd-HHmmss'
$out = Join-Path $logs "roundtable-$stamp.stdout.log"
$err = Join-Path $logs "roundtable-$stamp.stderr.log"

$argv = @('-X', 'utf8', '-u', '-m', 'roundtable', '--no-browser', '--data-dir', "`"$DataDir`"")
if ($Port) { $argv += @('--port', $Port) }
$p = Start-Process -FilePath $python -ArgumentList $argv -WorkingDirectory $root -WindowStyle Hidden `
    -RedirectStandardOutput $out -RedirectStandardError $err -PassThru
Start-Sleep -Seconds 3
if ($p.HasExited) {
    Write-Host "bot 没能启动，看看 $err"
    Get-Content $err -Tail 20
    exit 1
}
Write-Host "bot 已在后台运行（进程 $($p.Id)）。日志：$err"
Write-Host "停止：.\scripts\stop-bot.ps1"
