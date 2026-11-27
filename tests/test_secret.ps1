#requires -Version 7.4
$ErrorActionPreference = 'Stop'
if (-not $IsWindows) { throw 'The secret-script test requires Windows' }
$projectRoot = Split-Path -Parent $PSScriptRoot
$testDirectory = Join-Path $PSScriptRoot ('tmp-' + [guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory -Path $testDirectory | Out-Null
$configPath = Join-Path $testDirectory 'config.json'
$config = @{AwsProfile='test';AwsRegion='ap-northeast-1';StackName='bot-prod';ArtifactBucket='bucket';BotTokenParameterName='/bot/token';DiscordApplicationId='111111111111111111';DiscordPublicKey=('a'*64);AllowedGuildIds=@('222222222222222222')}
[IO.File]::WriteAllText($configPath, ($config | ConvertTo-Json))
$global:aitcSecretTest = @{Path=$null;Directory=$null;Fail=$false;Acl=$null;BadAcl=$false;Writes=0}
function Read-Host { return ConvertTo-SecureString 'fake-test-token-only' -AsPlainText -Force }
function Set-Acl {
    param($LiteralPath,$AclObject)
    $global:aitcSecretTest.Directory = $LiteralPath
    $global:aitcSecretTest.Acl = $AclObject
}
function Get-Acl {
    param($LiteralPath)
    if ($global:aitcSecretTest.BadAcl) { return [Security.AccessControl.DirectorySecurity]::new() }
    return $global:aitcSecretTest.Acl
}
function aws {
    $global:LASTEXITCODE = 0
    if ("$($args[0]) $($args[1])" -eq 'configure get') { return 'disabled' }
    if ("$($args[0]) $($args[1])" -ne 'ssm put-parameter') { throw 'Unexpected AWS command in secret test' }
    $global:aitcSecretTest.Writes++
    if (($args -join ' ') -match 'fake-test-token-only') { throw 'Token leaked into command line' }
    $index = [Array]::IndexOf($args, '--cli-input-json')
    $path = $args[$index + 1].Substring('file://'.Length)
    $global:aitcSecretTest.Path = $path
    $entry = Get-Content -LiteralPath $path -Raw | ConvertFrom-Json -AsHashtable
    if ($entry.Type -ne 'SecureString' -or $entry.Value -cne 'fake-test-token-only') { throw 'Invalid SSM payload' }
    $acl = Get-Acl -LiteralPath (Split-Path -Parent $path)
    if (-not $acl.AreAccessRulesProtected) { throw 'Token directory is not private' }
    if ($global:aitcSecretTest.Fail) { $global:LASTEXITCODE = 255; return 'failed' }
    return '{"Version":1}'
}
try {
    foreach ($fail in $false,$true) {
        $global:aitcSecretTest.Fail = $fail
        $caught = $false
        $caughtError = $null
        try { & (Join-Path $projectRoot 'scripts/save-bot-token.ps1') -ConfigPath $configPath } catch { $caught = $true; $caughtError = $_ }
        if ($caught -ne $fail) { throw "Unexpected secret script outcome: $caughtError" }
        $path = $global:aitcSecretTest.Path
        if (-not $path -or (Test-Path -LiteralPath (Split-Path -Parent $path))) { throw 'Secret temporary directory was not removed' }
    }
    $global:aitcSecretTest.BadAcl = $true
    $global:aitcSecretTest.Writes = 0
    $caught = $null
    try { & (Join-Path $projectRoot 'scripts/save-bot-token.ps1') -ConfigPath $configPath } catch { $caught = $_ }
    if ($null -eq $caught -or $caught.ToString() -notmatch 'Cannot verify private' -or $global:aitcSecretTest.Writes -ne 0) { throw 'Unverified ACL must stop before writing Token' }
    if (Test-Path -LiteralPath $global:aitcSecretTest.Directory) { throw 'Temporary directory not removed after ACL failure' }
    Write-Host 'Secret tests passed: mocked ACL verification, file input, cleanup, unverified ACL rejection'
} finally {
    Remove-Variable -Name aitcSecretTest -Scope Global -ErrorAction SilentlyContinue
    if ((Split-Path -Parent ([IO.Path]::GetFullPath($testDirectory))) -ne [IO.Path]::GetFullPath($PSScriptRoot)) { throw 'Unsafe test cleanup path' }
    Remove-Item -LiteralPath $testDirectory -Recurse -Force
}
