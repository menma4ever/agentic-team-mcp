$ErrorActionPreference = 'Stop'
$appRoot = $PSScriptRoot
$pythonPath = Join-Path $appRoot '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $pythonPath)) {
    python -m venv (Join-Path $appRoot '.venv')
    if ($LASTEXITCODE -ne 0) { throw 'Virtual environment creation failed' }
}
& $pythonPath -m pip install -r (Join-Path $appRoot 'requirements.txt')
if ($LASTEXITCODE -ne 0) { throw 'Dependency installation failed' }
Write-Host 'Setup complete. Open Launch.cmd.'

