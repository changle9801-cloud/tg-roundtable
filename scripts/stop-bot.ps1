<#
停止在运行的 bot（不管是 start-bot.ps1 在后台启动的，还是在终端里启动的）。

    .\scripts\stop-bot.ps1          # 停止
    .\scripts\stop-bot.ps1 -List    # 只列出在运行的进程号
#>
param([switch]$List)
$ErrorActionPreference = 'Stop'
$procs = @(Get-CimInstance Win32_Process -Filter "Name='python.exe'" |
    Where-Object { $_.CommandLine -match '-m roundtable(\s|$)' -and $_.CommandLine -notmatch '--console' })
if ($List) { return $procs.ProcessId }
if (-not $procs) {
    Write-Host 'bot 没有在运行。'
    return
}
foreach ($proc in $procs) { Stop-Process -Id $proc.ProcessId -Force }
Write-Host "已停止 bot（进程 $($procs.ProcessId -join ', ')）。"
