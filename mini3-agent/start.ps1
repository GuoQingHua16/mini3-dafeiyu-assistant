param([string]$HostIP = 'auto',
      [ValidateSet('button','continuous')][string]$Mode = 'button')
$ErrorActionPreference = 'Stop'
if (-not $env:DEEPSEEK_API_KEY) { $env:DEEPSEEK_API_KEY = [Environment]::GetEnvironmentVariable('DEEPSEEK_API_KEY', 'User') }
$env:PYTHONUTF8 = '1'
& "$PSScriptRoot\set-mode.ps1" -Mode $Mode
& "$PSScriptRoot\..\.venv\Scripts\python.exe" "$PSScriptRoot\agent.py" --host-ip $HostIP --mode $Mode
