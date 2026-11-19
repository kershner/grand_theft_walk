param(
    [ValidateSet('Install','Check','Repair','Restore')][string]$Action = 'Install',
    [ValidateSet('all','san_andreas','vice_city','gta_iii')][string]$Game = 'all',
    # Each path may name the game folder or the classic Rockstar game EXE.
    [string]$SanAndreasPath = '',
    [string]$ViceCityPath = '',
    [string]$GtaIIIPath = '',
    [switch]$Elevated
)
$ErrorActionPreference = 'Stop'
$result = 1
function Get-ContentSHA256([string]$Path) {
    $stream = [IO.File]::OpenRead($Path)
    $hasher = [Security.Cryptography.SHA256]::Create()
    try { return ([BitConverter]::ToString($hasher.ComputeHash($stream))).Replace('-', '').ToLowerInvariant() }
    finally { $hasher.Dispose(); $stream.Dispose() }
}
try {
    $root = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '../..'))
    $python = Join-Path $root 'venv\Scripts\python.exe'
    $indexPath = Join-Path $root 'bundle-index.json'
    if (Test-Path -LiteralPath $indexPath) {
        $index = Get-Content -LiteralPath (Join-Path $root 'bundle-index.json') -Raw | ConvertFrom-Json
        if ($index.format -ne 1) { throw 'Unsupported bundle index. Extract the ZIP again.' }
        Write-Host 'Verifying the transfer archive...'
        foreach ($entry in $index.files.PSObject.Properties) {
            $name = $entry.Name
            if ([IO.Path]::IsPathRooted($name) -or $name.Contains(':') -or ($name -split '[/\\]') -contains '..') {
                throw 'Invalid bundle path.'
            }
            $path = Join-Path $root $name
            if (-not (Test-Path -LiteralPath $path -PathType Leaf) -or
                (Get-ContentSHA256 $path) -ne $entry.Value) {
                throw "Archive file missing or damaged: $name. Extract the ZIP again."
            }
        }
    } else {
        $archive = Join-Path $root 'dist\Grand-Theft-Walk-Suite.zip'
        $checksum = $archive + '.sha256'
        if (-not (Test-Path -LiteralPath $archive) -or -not (Test-Path -LiteralPath $checksum)) { throw 'The setup archive is missing. Run venv\Scripts\python.exe build\build_suite.py first.' }
        $expected = ((Get-Content -LiteralPath $checksum -Raw).Trim() -split '\s+')[0]
        if ((Get-ContentSHA256 $archive) -ne $expected) { throw 'The setup archive failed verification. Restore or rebuild the ZIP.' }
    }
    # Use the existing project venv. A new PC needs an installed Python 3.10+
    # (we never download Python or copy a machine-specific virtual environment).
    if (-not (Test-Path -LiteralPath $python -PathType Leaf)) {
        Write-Host 'Creating the project environment using installed Python...'
        $launcher = Get-Command py.exe -ErrorAction SilentlyContinue
        if ($launcher) { & $launcher.Source -3 -m venv (Join-Path $root 'venv') }
        else {
            $launcher = Get-Command python.exe -ErrorAction SilentlyContinue
            if (-not $launcher) { throw 'Install Python 3.10+ or provide this project with a working venv. Setup never downloads Python.' }
            & $launcher.Source -m venv (Join-Path $root 'venv')
        }
        if ($LASTEXITCODE -ne 0 -or -not (Test-Path -LiteralPath $python)) { throw 'Could not create the project environment. Install Python 3.10+ first.' }
    }
    & $python -B -c "import sys; assert sys.version_info >= (3,10), 'Python 3.10+ is required'"
    if ($LASTEXITCODE -ne 0) { throw 'Python 3.10+ is required.' }
    $ErrorActionPreference = 'Continue'
    & $python -B -c "import importlib.metadata as m, pathlib, sys; rows=pathlib.Path(sys.argv[1]).read_text().splitlines(); sys.exit(0 if all(m.version(n.strip()) == v.strip() for row in rows if row.strip() and not row.lstrip().startswith('#') for n,v in [row.split('==')]) else 1)" (Join-Path $root 'requirements.txt') 2>$null
    $dependenciesMatch = $LASTEXITCODE -eq 0
    $ErrorActionPreference = 'Stop'
    if (-not $dependenciesMatch) {
        if ($Action -ne 'Install') { throw 'Python dependencies are missing. Run build\setup\Setup.cmd -Action Install first.' }
        Write-Host 'Installing project dependencies (internet required on first setup)...'
        & $python -m pip install -r (Join-Path $root 'requirements.txt')
        if ($LASTEXITCODE -ne 0) { throw 'Could not install project dependencies.' }
    }
    if ($Action -ne 'Check') {
        $identity = [Security.Principal.WindowsIdentity]::GetCurrent()
        $principal = New-Object Security.Principal.WindowsPrincipal($identity)
        if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
            if ($Elevated) { throw 'Administrator access is required to modify installed game files.' }
            $arguments = '-NoProfile -ExecutionPolicy Bypass -File "' + $PSCommandPath + '" -Action ' + $Action + ' -Game ' + $Game + ' -Elevated'
            if ($SanAndreasPath) { $arguments += ' -SanAndreasPath "' + $SanAndreasPath + '"' }
            if ($ViceCityPath) { $arguments += ' -ViceCityPath "' + $ViceCityPath + '"' }
            if ($GtaIIIPath) { $arguments += ' -GtaIIIPath "' + $GtaIIIPath + '"' }
            # PowerShell 7 and the Codex runtime use pwsh.exe; $PSHOME need not
            # contain powershell.exe. Elevate with the Windows system copy.
            $windowsPowerShell = Join-Path $env:SystemRoot 'System32\WindowsPowerShell\v1.0\powershell.exe'
            $child = Start-Process -FilePath $windowsPowerShell -ArgumentList $arguments -Verb RunAs -WindowStyle Hidden -Wait -PassThru
            $result = $child.ExitCode
            if ($result) { Write-Host "Setup failed. See runtime\setup-last.log." }
            else { Write-Host "$Action completed. See runtime\setup-last.log for the result." }
            exit $result
        }
    }
    Set-Location -LiteralPath $root
    $arguments = @('-B','-m','build.setup.setup',$Action.ToLowerInvariant(),'--bundle',$root,'--game',$Game)
    if ($SanAndreasPath) { $arguments += @('--san-andreas-dir',$SanAndreasPath) }
    if ($ViceCityPath) { $arguments += @('--vice-city-dir',$ViceCityPath) }
    if ($GtaIIIPath) { $arguments += @('--gta-iii-dir',$GtaIIIPath) }
    $logDirectory = Join-Path $root 'runtime'
    [IO.Directory]::CreateDirectory($logDirectory) | Out-Null
    # Native stderr is part of the saved result, including ordinary check failures.
    $ErrorActionPreference = 'Continue'
    & $python @arguments 2>&1 | Tee-Object -FilePath (Join-Path $logDirectory 'setup-last.log')
    $result = $LASTEXITCODE
    $ErrorActionPreference = 'Stop'
} catch {
    Write-Host "Setup stopped: $($_.Exception.Message)" -ForegroundColor Red
    $result = 1
}
exit $result
