#requires -Version 7.4
$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
$temporaryDirectory = Join-Path $PSScriptRoot ('tmp-' + [guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory -Path $temporaryDirectory | Out-Null
$config = @{AwsProfile='test';AwsRegion='ap-northeast-1';StackName='bot-prod';ArtifactBucket='bucket';BotTokenParameterName='/bot/token';DiscordApplicationId='111111111111111111';DiscordPublicKey=('a'*64);AllowedGuildIds=@('222222222222222222')}
$config.GatchaSourceGuildId = '222222222222222222'
$config.WebLookupApiId = 'webapi123'
$config.WebLookupRegion = 'ap-northeast-1'
$config.WebProfileBaseUrl = 'https://a-i-t-c.com'
$configPath = Join-Path $temporaryDirectory 'config.json'
[IO.File]::WriteAllText($configPath, ($config | ConvertTo-Json))
$global:aitcDeployTestState = @{Status='REVIEW_IN_PROGRESS';Requests=@();WrongApplication=$false;FailExecute=$false}
function aws {
    $state = $global:aitcDeployTestState
    $state.Requests += ($args -join ' ')
    $global:LASTEXITCODE = 0
    $value = switch ("$($args[0]) $($args[1])") {
        'sts get-caller-identity' { @{Account='123456789012'} }
        'cloudformation describe-change-set' {
            @{Status='CREATE_COMPLETE';ExecutionStatus='AVAILABLE';Changes=@();Parameters=@(
                @{ParameterKey='DiscordApplicationId';ParameterValue=$(if ($state.WrongApplication) {'999999999999999999'} else {'111111111111111111'})},
                @{ParameterKey='DiscordPublicKey';ParameterValue=('a'*64)},
                @{ParameterKey='AllowedGuildIds';ParameterValue='222222222222222222'},
                @{ParameterKey='GatchaSourceGuildId';ParameterValue='222222222222222222'},
                @{ParameterKey='BotTokenParameterName';ParameterValue='/bot/token'},
                @{ParameterKey='WebLookupApiId';ParameterValue='webapi123'},
                @{ParameterKey='WebLookupRegion';ParameterValue='ap-northeast-1'},
                @{ParameterKey='WebProfileBaseUrl';ParameterValue='https://a-i-t-c.com'}
            )}
        }
        'cloudformation describe-stacks' { @{Stacks=@(@{StackStatus=$state.Status;Outputs=@()})} }
        'cloudformation execute-change-set' { if ($state.FailExecute) {$global:LASTEXITCODE=255; 'private stderr must not be printed'} else {@{}} }
        'cloudformation wait' { @{} }
        default { throw 'Unexpected native command in execution test' }
    }
    if ($value -is [string]) { return $value }
    return ConvertTo-Json -InputObject $value -Depth 20 -Compress
}
try {
    foreach ($status in 'REVIEW_IN_PROGRESS','CREATE_COMPLETE','UPDATE_ROLLBACK_COMPLETE') {
        $global:aitcDeployTestState.Status = $status
        $global:aitcDeployTestState.Requests = @()
        & (Join-Path $projectRoot 'scripts/deploy.ps1') -ConfigPath $configPath -ExecuteChangeSet 'reviewed-test'
        $waiter = if ($status -eq 'REVIEW_IN_PROGRESS') { 'stack-create-complete' } else { 'stack-update-complete' }
        if (@($global:aitcDeployTestState.Requests | Where-Object { $_ -like "cloudformation wait $waiter *" }).Count -ne 1) { throw "Incorrect waiter for $status" }
    }
    $global:aitcDeployTestState.WrongApplication = $true
    $global:aitcDeployTestState.Requests = @()
    $caught = $false
    try { & (Join-Path $projectRoot 'scripts/deploy.ps1') -ConfigPath $configPath -ExecuteChangeSet 'wrong-app' } catch { $caught = $true }
    if (-not $caught -or @($global:aitcDeployTestState.Requests | Where-Object { $_ -like 'cloudformation execute*' }).Count) { throw 'Mismatched configuration must stop execution' }
    $global:aitcDeployTestState.WrongApplication = $false
    $global:aitcDeployTestState.FailExecute = $true
    $global:aitcDeployTestState.Requests = @()
    $caught = $null
    try { & (Join-Path $projectRoot 'scripts/deploy.ps1') -ConfigPath $configPath -ExecuteChangeSet 'failed' } catch { $caught = $_ }
    if ($null -eq $caught -or $caught.ToString() -match 'private stderr' -or @($global:aitcDeployTestState.Requests | Where-Object { $_ -like 'cloudformation wait *' }).Count) { throw 'Native failure must stop without disclosing output' }
    Write-Host 'Deployment tests passed: create/update waiters, configuration match, native failure'
} finally {
    Remove-Variable -Name aitcDeployTestState -Scope Global -ErrorAction SilentlyContinue
    if ((Split-Path -Parent ([IO.Path]::GetFullPath($temporaryDirectory))) -ne [IO.Path]::GetFullPath($PSScriptRoot)) { throw 'Unsafe test cleanup path' }
    Remove-Item -LiteralPath $temporaryDirectory -Recurse -Force
}
