param([Parameter(Mandatory=$true)][ValidateSet('button','continuous')][string]$Mode)
$ErrorActionPreference = 'Stop'
$runtime = Join-Path $PSScriptRoot 'runtime'
New-Item -ItemType Directory -Force $runtime | Out-Null
$temporary = Join-Path $runtime ('mode-' + [guid]::NewGuid().ToString('N') + '.tmp')
try {
    [IO.File]::WriteAllText($temporary, $Mode, [Text.Encoding]::ASCII)
    Move-Item -LiteralPath $temporary -Destination (Join-Path $runtime 'mode.txt') -Force
} finally {
    if (Test-Path -LiteralPath $temporary) { Remove-Item -LiteralPath $temporary }
}
Write-Output "Requested mode: $Mode. Applies during listening or after the current reply."
