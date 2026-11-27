#requires -Version 7.4
[CmdletBinding()]
param()
$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
$dist = Join-Path $projectRoot 'dist'
New-Item -ItemType Directory -Path $dist -Force | Out-Null
$dist = (Resolve-Path -LiteralPath $dist).ProviderPath
$buildRoot = Join-Path $dist ('build-' + [guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory -Path $buildRoot | Out-Null
$buildRoot = (Resolve-Path -LiteralPath $buildRoot).ProviderPath
if ((Split-Path -Parent $buildRoot) -ne $dist) { throw 'Build directory must be directly inside dist' }
try {
    $package = Join-Path $buildRoot 'package'
    & uv pip install --only-binary ':all:' --python-version 3.13 --python-platform aarch64-manylinux_2_34 --target $package --requirement (Join-Path $projectRoot 'requirements.txt') | Out-Host
    if ($LASTEXITCODE -ne 0) { throw 'Linux arm64 dependency installation failed' }
    Copy-Item -LiteralPath (Join-Path $projectRoot 'lambda/app.py') -Destination $package
    Copy-Item -LiteralPath (Join-Path $projectRoot 'lambda/discord_bot') -Destination $package -Recurse
    $zipPath = Join-Path $buildRoot 'lambda.zip'
    $archive = [IO.Compression.ZipFile]::Open($zipPath, [IO.Compression.ZipArchiveMode]::Create)
    try {
        foreach ($file in (Get-ChildItem -LiteralPath $package -File -Recurse | Sort-Object FullName)) {
            if ($file.Extension -eq '.pyc' -or $file.FullName -match '[\\/]__pycache__[\\/]') { continue }
            $relative = [IO.Path]::GetRelativePath($package, $file.FullName).Replace('\','/')
            $entry = $archive.CreateEntry($relative, [IO.Compression.CompressionLevel]::Optimal)
            $entry.LastWriteTime = [DateTimeOffset]::new(2000,1,1,0,0,0,[TimeSpan]::Zero)
            $entry.ExternalAttributes = 33188 -shl 16 # regular file, POSIX 0644
            $inputStream = $file.OpenRead()
            $outputStream = $entry.Open()
            try { $inputStream.CopyTo($outputStream) }
            finally { $outputStream.Dispose(); $inputStream.Dispose() }
        }
    } finally { $archive.Dispose() }
    $hash = (Get-FileHash -LiteralPath $zipPath -Algorithm SHA256).Hash.ToLowerInvariant()
    $artifact = Join-Path $dist "lambda-$hash.zip"
    if (-not (Test-Path -LiteralPath $artifact)) { [IO.File]::Move($zipPath, $artifact) }
    Write-Output $artifact
} finally {
    if ((Split-Path -Parent $buildRoot) -ne $dist) { throw 'Unsafe cleanup path' }
    Remove-Item -LiteralPath $buildRoot -Recurse -Force
}
