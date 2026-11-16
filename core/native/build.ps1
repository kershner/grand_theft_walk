param([string]$Compiler = (Join-Path $PSScriptRoot '../../games/san_andreas/runtime/build/tcc/tcc.exe'))
$ErrorActionPreference = 'Stop'
& $Compiler -shared -o (Join-Path $PSScriptRoot 'save_bridge.dll') (Join-Path $PSScriptRoot 'save_bridge.c') -luser32
if ($LASTEXITCODE) { throw 'Save bridge compilation failed' }
[IO.File]::Delete((Join-Path $PSScriptRoot 'save_bridge.def'))
& $Compiler -o (Join-Path $PSScriptRoot 'save_request.exe') (Join-Path $PSScriptRoot 'save_request.c') -luser32
if ($LASTEXITCODE) { throw 'Save helper compilation failed' }
$files = @{}
foreach ($name in @('save_bridge.dll', 'save_request.exe')) {
    $files[$name] = (Get-FileHash -LiteralPath (Join-Path $PSScriptRoot $name) -Algorithm SHA256).Hash.ToLowerInvariant()
}
$manifest = @{format = 1; files = $files} | ConvertTo-Json
[IO.File]::WriteAllText((Join-Path $PSScriptRoot 'manifest.json'), $manifest)
