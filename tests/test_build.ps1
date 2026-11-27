#requires -Version 7.4
$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
$global:aitcBuildTestFail = $false
$artifact = $null
function uv {
    $targetIndex = [Array]::IndexOf($args, '--target')
    if ($targetIndex -lt 0 -or 'aarch64-manylinux_2_34' -notin $args -or '3.13' -notin $args -or '--only-binary' -notin $args) { throw 'Build must request Linux arm64 Python 3.13 wheels' }
    if ($global:aitcBuildTestFail) { $global:LASTEXITCODE = 1; return }
    $package = $args[$targetIndex + 1]
    New-Item -ItemType Directory -Path $package -Force | Out-Null
    [IO.File]::WriteAllText((Join-Path $package 'dependency.so'), 'test fixture only')
    $global:LASTEXITCODE = 0
}
try {
    $artifact = & (Join-Path $root 'scripts/build.ps1')
    $archive = [IO.Compression.ZipFile]::OpenRead($artifact)
    try {
        $names = @($archive.Entries | ForEach-Object FullName)
        foreach ($required in 'app.py','discord_bot/config.py','discord_bot/commands/ping.py','dependency.so') {
            if ($required -notin $names) { throw "Missing ZIP entry: $required" }
        }
        if (@($names | Where-Object { $_ -match '\\|\.pyc$|^config/|^scripts/|^tests/' }).Count) { throw 'ZIP includes unexpected files' }
    } finally { $archive.Dispose() }
    $hash = (Get-FileHash -LiteralPath $artifact -Algorithm SHA256).Hash.ToLowerInvariant()
    if ([IO.Path]::GetFileName($artifact) -cne "lambda-$hash.zip") { throw 'Artifact key must include its content hash' }
    $global:aitcBuildTestFail = $true
    $failed = $false
    try { & (Join-Path $root 'scripts/build.ps1') } catch { $failed = $true }
    if (-not $failed) { throw 'Build must stop when dependency installation fails' }
    Write-Host 'Build tests passed: ZIP layout, target platform, immutable key, dependency failure'
} finally {
    Remove-Variable -Name aitcBuildTestFail -Scope Global -ErrorAction SilentlyContinue
    if ($artifact -and (Test-Path -LiteralPath $artifact)) {
        if ((Split-Path -Parent ([IO.Path]::GetFullPath($artifact))) -ne (Join-Path $root 'dist')) { throw 'Unsafe artifact cleanup path' }
        Remove-Item -LiteralPath $artifact -Force
    }
}
