param(
    [switch]$WhatIf
)

$ErrorActionPreference = "Stop"

$taskName = "DouyinCreatorMonitor"
$projectDirectory = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$batchPath = Join-Path $projectDirectory "run_scheduled.bat"
$arguments = "/d /s /c `"`"$batchPath`" --no-pause`""

if ($WhatIf) {
    [pscustomobject]@{
        TaskName = $taskName
        Execute = $env:ComSpec
        Arguments = $arguments
        WorkingDirectory = $projectDirectory
    } | ConvertTo-Json
    exit 0
}

$action = New-ScheduledTaskAction `
    -Execute $env:ComSpec `
    -Argument $arguments `
    -WorkingDirectory $projectDirectory
$trigger = New-ScheduledTaskTrigger -Daily -At "03:00"
$principal = New-ScheduledTaskPrincipal `
    -UserId ([System.Security.Principal.WindowsIdentity]::GetCurrent().Name) `
    -LogonType Interactive `
    -RunLevel Limited
$settings = New-ScheduledTaskSettingsSet `
    -ExecutionTimeLimit (New-TimeSpan -Hours 6) `
    -StartWhenAvailable `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries

Register-ScheduledTask `
    -TaskName $taskName `
    -Action $action `
    -Trigger $trigger `
    -Principal $principal `
    -Settings $settings `
    -Description "Daily Douyin creator collection and synchronization" `
    -Force | Out-Null

Get-ScheduledTask -TaskName $taskName | Out-Null
Write-Host "[OK] Task '$taskName' registered successfully."
