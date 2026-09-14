$ErrorActionPreference = 'Stop'
$env:PYTHONUTF8 = '1'
foreach ($candidate in @('python', 'py', 'python3')) {
    $command = Get-Command $candidate -ErrorAction SilentlyContinue
    if (-not $command) { continue }
    $prefix = @()
    if ($candidate -eq 'py') { $prefix = @('-3') }
    & $command.Source @prefix -c 'import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)'
    if ($LASTEXITCODE -ne 0) { continue }
    & $command.Source @prefix (Join-Path $PSScriptRoot 'install_shortcut.py')
    $shortcutDir = Join-Path $env:LOCALAPPDATA 'Serein\bin'
    if (Test-Path (Join-Path $shortcutDir 'se.cmd')) {
        if ($shortcutDir -notin ($env:PATH -split ';')) { $env:PATH = "$shortcutDir;$env:PATH" }
    }
    & $command.Source @prefix (Join-Path $PSScriptRoot 'manage.py') @args
    exit $LASTEXITCODE
}
Write-Error 'Python 3.9+ is required for Docker; Python 3.11+ for local mode. Install Python and add it to PATH.'
exit 1
