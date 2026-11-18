# Optional wrapper for the same installation documented for standard CMD.
$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot
py -3.13 -m pip install --user -r requirements-desktop.txt
if ($LASTEXITCODE -ne 0) { throw 'Dependency installation failed. See README.md.' }
py -3.13 scripts/acquire_model.py
if ($LASTEXITCODE -ne 0) { throw 'GitHub model installation failed. See README.md.' }
Write-Host 'Ready. Run py -3.13 app.py or double-click Start_GigaAM.cmd.'
