<#
登录 Windows 时自动在后台启动 bot：在「启动」文件夹放一个快捷方式，运行 start-bot.ps1。
任务管理器的「启动应用」里也能看到和关掉它。

    .\scripts\install-autostart.ps1                    # 数据目录按 NOOKLING_HOME（旧的 ROUNDTABLE_HOME 也认）
    .\scripts\install-autostart.ps1 -DataDir D:\data   # 指定数据目录
    .\scripts\install-autostart.ps1 -Remove            # 不再自动启动
#>
param(
    [string]$DataDir = '',
    [switch]$Remove
)
$ErrorActionPreference = 'Stop'
$startup = [Environment]::GetFolderPath('Startup')
$link = Join-Path $startup 'Nookling.lnk'
$legacy = Join-Path $startup 'tg-roundtable.lnk'  # made before the rename
if (Test-Path $legacy) { Remove-Item $legacy -Force }
if ($Remove) {
    if (Test-Path $link) { Remove-Item $link -Force; Write-Host '已取消开机自动启动。' }
    else { Write-Host '本来就没有设置开机自动启动。' }
    return
}
$start = Join-Path $PSScriptRoot 'start-bot.ps1'
$arguments = "-NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File `"$start`""
if ($DataDir) { $arguments += " -DataDir `"$DataDir`"" }
$shell = New-Object -ComObject WScript.Shell
$shortcut = $shell.CreateShortcut($link)
$shortcut.TargetPath = (Get-Command powershell.exe).Source
$shortcut.Arguments = $arguments
$shortcut.WorkingDirectory = Split-Path -Parent $PSScriptRoot
$shortcut.WindowStyle = 7  # minimized
$shortcut.Description = '后台启动 Nookling（小窝）'
$shortcut.Save()
Write-Host "已设置：登录 Windows 时自动在后台启动 bot。"
Write-Host "取消：.\scripts\install-autostart.ps1 -Remove"
