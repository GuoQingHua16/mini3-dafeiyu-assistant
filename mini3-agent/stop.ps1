$ErrorActionPreference = 'Stop'
. "$PSScriptRoot\runtime-process.ps1"
$statePath = Join-Path $PSScriptRoot 'runtime\process.json'
if (-not (Test-Path $statePath)) { Write-Output 'No managed background agent.'; return }
$state = Get-Content $statePath -Raw | ConvertFrom-Json
$targets = @(Get-Mini3Targets $state)
if ($targets.Count -and $state.token) {
    [IO.File]::WriteAllText((Join-Path $PSScriptRoot 'runtime\stop.request'), [string]$state.token, [Text.Encoding]::ASCII)
    $stopDeadline = [DateTime]::UtcNow.AddSeconds(8)
    while ([DateTime]::UtcNow -lt $stopDeadline -and @($targets | Where-Object { Test-Mini3Identity $_ }).Count) {
        Start-Sleep -Milliseconds 200
    }
}
$remaining = @(Get-Mini3Targets $state)
if ($remaining.Count) { Stop-Mini3Targets $remaining; Start-Sleep -Milliseconds 250 }
if (@(Get-Mini3Targets $state).Count) { throw 'Managed process still running; state preserved for retry.' }
Stop-Mini3DevicePlayback "$PSScriptRoot\..\.tools\platform-tools\adb.exe"
Remove-Item -LiteralPath $statePath
$requestPath = Join-Path $PSScriptRoot 'runtime\stop.request'
if ((Test-Path -LiteralPath $requestPath) -and (Get-Content -LiteralPath $requestPath -Raw).Trim() -eq $state.token) {
    Remove-Item -LiteralPath $requestPath
}
Write-Output 'Mini3 agent stopped.'
