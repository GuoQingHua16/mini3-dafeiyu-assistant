param([string]$HostIP = 'auto',
      [ValidateSet('button','continuous')][string]$Mode = 'button',
      [switch]$StartResting,
      [switch]$DisableTF = $true)
$ErrorActionPreference = 'Stop'
. "$PSScriptRoot\runtime-process.ps1"
$runtime = Join-Path $PSScriptRoot 'runtime'
New-Item -ItemType Directory -Force $runtime | Out-Null
$statePath = Join-Path $runtime 'process.json'
if (Test-Path $statePath) {
    $state = Get-Content $statePath -Raw | ConvertFrom-Json
    if (@(Get-Mini3Targets $state).Count) {
        throw 'Mini3 agent is already running. Use stop.ps1 first.'
    }
}
$python = (Resolve-Path "$PSScriptRoot\..\.venv\Scripts\python.exe").Path
$resolvedIP = & $python "$PSScriptRoot\networking.py" $HostIP
if ($LASTEXITCODE -ne 0) { throw 'LAN IP selection failed; specify -HostIP explicitly.' }
$resolvedIP = ([string]$resolvedIP).Trim()
if (Get-NetTCPConnection -LocalPort 8765 -State Listen -ErrorAction SilentlyContinue) {
    throw 'Port 8765 is already in use; refusing to launch a second service.'
}
if (-not $env:DEEPSEEK_API_KEY) { $env:DEEPSEEK_API_KEY = [Environment]::GetEnvironmentVariable('DEEPSEEK_API_KEY', 'User') }
if (-not $env:DEEPSEEK_API_KEY) { throw 'Configure the DeepSeek key or use start.ps1 for hidden input.' }
$env:PYTHONUTF8 = '1'
& "$PSScriptRoot\set-mode.ps1" -Mode $Mode
$token = [guid]::NewGuid().ToString('N')
$agentArgs = @('-u', ('"' + "$PSScriptRoot\agent.py" + '"'), '--host-ip', $resolvedIP, '--mode', $Mode, '--control-token', $token)
if ($StartResting) { $agentArgs += '--start-resting' }
if ($DisableTF) { $agentArgs += '--disable-tf' }
$process = Start-Process -FilePath $python -ArgumentList $agentArgs -WindowStyle Hidden -PassThru -RedirectStandardOutput "$runtime\agent.log" -RedirectStandardError "$runtime\agent-error.log"
$newState = Get-Mini3Identity $process
$newState | Add-Member -NotePropertyName token -NotePropertyValue $token
$newState | Add-Member -NotePropertyName host_ip -NotePropertyValue $resolvedIP
$newState | Add-Member -NotePropertyName disable_tf -NotePropertyValue $true
Write-Mini3State $newState $statePath
Start-Sleep -Milliseconds 600
$children = @(Get-Mini3Targets $newState | Where-Object { $_.pid -ne $process.Id })
$newState | Add-Member -NotePropertyName children -NotePropertyValue $children
Write-Mini3State $newState $statePath
if (-not @(Get-Mini3Targets $newState).Count) {
    throw 'Agent exited during startup. See runtime/agent-error.log.'
}
Write-Output "Mini3 agent started, PID $($process.Id), LAN $resolvedIP`:8765. Logs: $runtime"
