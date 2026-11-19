param(
    [string]$Compiler = (Join-Path $PSScriptRoot '../../games/san_andreas/runtime/build/tcc/tcc.exe'),
    [string]$Python = (Join-Path $PSScriptRoot '../../venv/Scripts/python.exe')
)
$ErrorActionPreference = 'Stop'
$output = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '../../core/native'))
& $Compiler -shared -o (Join-Path $output 'GTW.BackgroundInput.asi') (Join-Path $PSScriptRoot 'background_input.c') -luser32
if ($LASTEXITCODE) { throw 'Background input compilation failed' }
& $Python (Join-Path $PSScriptRoot 'background_manifest.py')
if ($LASTEXITCODE) { throw 'Background input metadata generation failed' }
