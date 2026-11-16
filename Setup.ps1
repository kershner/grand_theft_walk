param(
    [ValidateSet('Install','Check','Repair','Restore')][string]$Action = 'Install',
    [ValidateSet('all','san_andreas','vice_city')][string]$Game = 'all',
    [string]$SanAndreasPath = '',
    [string]$ViceCityPath = '',
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
    $root = $PSScriptRoot
    $python = Join-Path $root 'venv\Scripts\python.exe'
    if (-not (Test-Path -LiteralPath $python -PathType Leaf)) {
        throw 'Place this suite in the project folder containing your existing venv\Scripts\python.exe.'
    }
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
        if (-not (Test-Path -LiteralPath $archive) -or -not (Test-Path -LiteralPath $checksum)) { throw 'The setup archive is missing. Run build_suite.py first.' }
        $expected = ((Get-Content -LiteralPath $checksum -Raw).Trim() -split '\s+')[0]
        if ((Get-ContentSHA256 $archive) -ne $expected) { throw 'The setup archive failed verification. Restore or rebuild the ZIP.' }
    }
    if ($Action -ne 'Check') {
        $identity = [Security.Principal.WindowsIdentity]::GetCurrent()
        $principal = New-Object Security.Principal.WindowsPrincipal($identity)
        if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
            if ($Elevated) { throw 'Administrator access is required to modify installed game files.' }
            $arguments = '-NoProfile -ExecutionPolicy Bypass -File "' + $PSCommandPath + '" -Action ' + $Action + ' -Game ' + $Game + ' -Elevated'
            if ($SanAndreasPath) { $arguments += ' -SanAndreasPath "' + $SanAndreasPath + '"' }
            if ($ViceCityPath) { $arguments += ' -ViceCityPath "' + $ViceCityPath + '"' }
            $child = Start-Process -FilePath (Join-Path $PSHOME 'powershell.exe') -ArgumentList $arguments -Verb RunAs -WindowStyle Hidden -Wait -PassThru
            $result = $child.ExitCode
            if ($result) { Write-Host "Setup failed. See runtime\setup-last.log." }
            else { Write-Host "$Action completed. See runtime\setup-last.log for the result." }
            exit $result
        }
    }
    Set-Location -LiteralPath $root
    $arguments = @('-B','-m','gtw.setup',$Action.ToLowerInvariant(),'--bundle',$root,'--game',$Game)
    if ($SanAndreasPath) { $arguments += @('--san-andreas-dir',$SanAndreasPath) }
    if ($ViceCityPath) { $arguments += @('--vice-city-dir',$ViceCityPath) }
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
