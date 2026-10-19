# Partner ride bot — Windows PowerShell launcher
Set-Location -LiteralPath $PSScriptRoot

Write-Host ""
Write-Host " === Partner ride bot ==="
Write-Host ""

$py = $null
if (Get-Command py -ErrorAction SilentlyContinue) { $py = "py -3" }
elseif (Get-Command python -ErrorAction SilentlyContinue) { $py = "python" }
else {
    Write-Host "Python was not found. Install Python 3.11+ from https://www.python.org/downloads/"
    Write-Host 'Tick "Add python.exe to PATH", then run this script again.'
    exit 1
}

if (-not (Test-Path ".venv\Scripts\python.exe")) {
    Write-Host "Creating virtual environment..."
    Invoke-Expression "$py -m venv .venv"
    if ($LASTEXITCODE -ne 0) { exit 1 }
}

$vpy = ".venv\Scripts\python.exe"
Write-Host "Installing / updating packages..."
& $vpy -m pip install --upgrade pip | Out-Null
& $vpy -m pip install -r requirements.txt
if ($LASTEXITCODE -ne 0) { exit 1 }

Write-Host ""
& $vpy run.py @args
exit $LASTEXITCODE
