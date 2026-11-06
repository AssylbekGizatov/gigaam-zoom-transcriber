$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot
py -3.12 -m venv .venv
if ($LASTEXITCODE -ne 0) { throw 'Install Python 3.12 x64 with Tcl/Tk first.' }
& ./.venv/Scripts/python.exe -m pip install --index-url https://download.pytorch.org/whl/cpu torch==2.10.0+cpu torchaudio==2.10.0+cpu -c constraints.txt
if ($LASTEXITCODE -ne 0) { throw 'CPU PyTorch installation failed.' }
& ./.venv/Scripts/python.exe -m pip install -r requirements-desktop.txt -c constraints.txt
if ($LASTEXITCODE -ne 0) { throw 'Dependency installation failed.' }
& ./.venv/Scripts/python.exe scripts/acquire_model.py
if ($LASTEXITCODE -ne 0) { throw 'GitHub model installation failed. See docs/WINDOWS_ZIP_VISUAL_STUDIO.md.' }
Write-Host 'Dependencies and verified local GigaAM model are ready. No Hugging Face or Git required.'
Write-Host 'Or choose an existing verified local GigaAM model folder in the UI.'
