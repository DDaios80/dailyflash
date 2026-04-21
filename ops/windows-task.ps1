# Windows Task Scheduler — register the Daily Flash cron.
# Run once in an elevated PowerShell after editing the paths below.

$Python = "C:\Path\To\daily-flash\.venv\Scripts\python.exe"
$Script = "C:\Path\To\daily-flash\src\cron.py"
$WorkDir = "C:\Path\To\daily-flash"
$OneDrive = "C:\Users\<you>\OneDrive - Daios Hotels\DailyFlash"

$Action = New-ScheduledTaskAction `
    -Execute $Python `
    -Argument "$Script --fallback-latest" `
    -WorkingDirectory $WorkDir

# Runs at 23:00 the previous night; cron.py defaults to tomorrow's date.
# So Tuesday 23:00 produces Wednesday's flash.
$Trigger = New-ScheduledTaskTrigger -Daily -At 11:00pm

$Settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
    -StartWhenAvailable -MultipleInstances IgnoreNew

$Principal = New-ScheduledTaskPrincipal -UserId $env:USERNAME -LogonType Interactive

Register-ScheduledTask `
    -TaskName "DaiosCoveDailyFlash" `
    -Action $Action `
    -Trigger $Trigger `
    -Settings $Settings `
    -Principal $Principal `
    -Description "Processes the morning arrivals xlsx and publishes today's Daily Flash."

# Environment variable for the task (runs once at login via session)
[Environment]::SetEnvironmentVariable("DAILY_FLASH_INBOX", $OneDrive, "User")

Write-Host "Task registered. Run on-demand with:  Start-ScheduledTask -TaskName DaiosCoveDailyFlash"
