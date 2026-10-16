$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot
if (-not (Test-Path -LiteralPath './.venv/Scripts/python.exe')) { throw 'Run setup.ps1 first, or run app.py from your IDE interpreter.' }
& ./.venv/Scripts/python.exe app.py
