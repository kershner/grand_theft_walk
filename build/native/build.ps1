param([string]$Compiler = (Join-Path $PSScriptRoot '../../games/san_andreas/runtime/build/tcc/tcc.exe'))
$ErrorActionPreference = 'Stop'
$output = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '../../core/native'))
[IO.Directory]::CreateDirectory($output) | Out-Null
& $Compiler -shared -o (Join-Path $output 'save_bridge.dll') (Join-Path $PSScriptRoot 'save_bridge.c') -luser32
if ($LASTEXITCODE) { throw 'Save bridge compilation failed' }
[IO.File]::Delete((Join-Path $output 'save_bridge.def'))
& $Compiler -o (Join-Path $output 'save_request.exe') (Join-Path $PSScriptRoot 'save_request.c') -luser32
if ($LASTEXITCODE) { throw 'Save helper compilation failed' }
$files = @{}
foreach ($name in @('save_bridge.dll', 'save_request.exe')) {
    $stream = [IO.File]::OpenRead((Join-Path $output $name))
    $hasher = [Security.Cryptography.SHA256]::Create()
    try { $files[$name] = ([BitConverter]::ToString($hasher.ComputeHash($stream))).Replace('-', '').ToLowerInvariant() }
    finally { $hasher.Dispose(); $stream.Dispose() }
}
$manifest = @{format = 1; files = $files} | ConvertTo-Json
[IO.File]::WriteAllText((Join-Path $output 'manifest.json'), $manifest)
