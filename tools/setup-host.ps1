param([string]$Python = 'python', [switch]$DownloadModels)
$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
$venvPython = Join-Path $root '.venv\Scripts\python.exe'
& $Python -c "import sys; assert sys.version_info >= (3,11), 'Python 3.11 or newer is required'"
if ($LASTEXITCODE -ne 0) { throw 'Python version check failed' }
if (-not (Test-Path -LiteralPath $venvPython)) {
    & $Python -m venv (Join-Path $root '.venv')
    if ($LASTEXITCODE -ne 0) { throw 'Could not create the project virtual environment' }
}
& $venvPython -m pip install -r (Join-Path $root 'mini3-agent\requirements-lock.txt')
if ($LASTEXITCODE -ne 0) { throw 'Dependency installation failed' }
if ($DownloadModels) {
    & $venvPython (Join-Path $PSScriptRoot 'fetch_models.py')
    if ($LASTEXITCODE -ne 0) { throw 'Model download or verification failed' }
}
Write-Output 'Python environment prepared. Install official ADB in tools/platform-tools and follow docs/setup-windows.md.'
