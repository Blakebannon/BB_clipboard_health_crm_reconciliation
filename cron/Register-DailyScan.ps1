# Registers a Windows Task Scheduler job that runs the READ-ONLY scan daily at 09:00 local time.
# Run from an elevated or normal PowerShell in the repo root:
#   .\cron\Register-DailyScan.ps1
# Remove with:  Unregister-ScheduledTask -TaskName "Bellhaven CRM daily scan" -Confirm:$false
$repo = (Resolve-Path "$PSScriptRoot\..").Path
$python = Join-Path $repo ".venv\Scripts\python.exe"
New-Item -ItemType Directory -Force (Join-Path $repo "logs") | Out-Null
$action = New-ScheduledTaskAction -Execute "powershell.exe" `
    -Argument "-NoProfile -WindowStyle Hidden -Command `"& '$python' -m bellhaven.cli scan *>> '$repo\logs\daily-scan.log'`"" `
    -WorkingDirectory $repo
$trigger = New-ScheduledTaskTrigger -Daily -At 9:00AM
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -ExecutionTimeLimit (New-TimeSpan -Minutes 30)
Register-ScheduledTask -TaskName "Bellhaven CRM daily scan" -Action $action -Trigger $trigger -Settings $settings -Description "Read-only Bellhaven/CRM reconciliation scan. Never writes to the CRM." -Force
Write-Host "Registered 'Bellhaven CRM daily scan' (daily 09:00, read-only). Log: $repo\logs\daily-scan.log"
