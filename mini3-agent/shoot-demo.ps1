param([switch]$PreviewOnly)
$ErrorActionPreference = 'Stop'
try { $Host.UI.RawUI.WindowTitle = 'Mini3 演示提词｜8～9分钟' } catch {}
$python = (Resolve-Path "$PSScriptRoot\..\.venv\Scripts\python.exe").Path
$env:PYTHONUTF8 = '1'
if ($PreviewOnly) {
    & $python -X utf8 "$PSScriptRoot\demo_guide.py" --preview
    return
}
. "$PSScriptRoot\runtime-process.ps1"
$statePath = Join-Path $PSScriptRoot 'runtime\process.json'
$running = $false
if (Test-Path -LiteralPath $statePath) {
    $state = Get-Content -LiteralPath $statePath -Raw | ConvertFrom-Json
    $running = @(Get-Mini3Targets $state).Count -gt 0
}
if ($running) {
    $diagnosticsPath = Join-Path $PSScriptRoot 'runtime\usb-link-status.json'
    $supportsGuide = $false
    if (Test-Path -LiteralPath $diagnosticsPath) {
        try { $diagnostics = Get-Content -LiteralPath $diagnosticsPath -Raw | ConvertFrom-Json; $supportsGuide = [bool]$diagnostics.filming_events }
        catch { $supportsGuide = $false }
    }
    if (-not $supportsGuide) {
        & "$PSScriptRoot\stop.ps1"
        $running = $false
    }
}
if (-not $running) {
    if (-not $env:DEEPSEEK_API_KEY) { $env:DEEPSEEK_API_KEY = [Environment]::GetEnvironmentVariable('DEEPSEEK_API_KEY', 'User') }
    if (-not $env:DEEPSEEK_API_KEY) {
        $secureKey = Read-Host 'DeepSeek API Key（隐藏，仅供本次后台使用）' -AsSecureString
        $keyPointer = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secureKey)
        try { $env:DEEPSEEK_API_KEY = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($keyPointer) }
        finally { [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($keyPointer) }
    }
    $archive = Join-Path $PSScriptRoot ('runtime\filming-before-' + [guid]::NewGuid().ToString('N').Substring(0,8))
    New-Item -ItemType Directory -Path $archive | Out-Null
    foreach ($name in @('agent.log','agent-error.log')) {
        $source = Join-Path $PSScriptRoot "runtime\$name"
        if (Test-Path -LiteralPath $source) { Copy-Item -LiteralPath $source -Destination (Join-Path $archive $name) }
    }
    & "$PSScriptRoot\run-background.ps1" -Mode button -StartResting -DisableTF
} else {
    & "$PSScriptRoot\set-mode.ps1" -Mode button
}
# The observer has no need for the dialogue API credential.
Remove-Item Env:DEEPSEEK_API_KEY -ErrorAction SilentlyContinue
& $python -u -X utf8 "$PSScriptRoot\demo_guide.py"
