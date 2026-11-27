#requires -Version 7.4
[CmdletBinding()]
param(
    [string]$ConfigPath = (Join-Path $PSScriptRoot '../config/prod.json'),
    [string]$ExecuteChangeSet
)
. (Join-Path $PSScriptRoot 'common.ps1')
$config = Read-BotConfig $ConfigPath
$projectRoot = Split-Path -Parent $PSScriptRoot
$identity = Invoke-AwsJson $config @('sts','get-caller-identity')
Write-Host "Account=$($identity.Account) Region=$($config.AwsRegion) Stack=$($config.StackName)"

if ($ExecuteChangeSet) {
    $change = Invoke-AwsJson $config @('cloudformation','describe-change-set','--stack-name',$config.StackName,'--change-set-name',$ExecuteChangeSet)
    if ($change.Status -ne 'CREATE_COMPLETE' -or $change.ExecutionStatus -ne 'AVAILABLE') { throw 'Change set is not ready to execute' }
    $expected = @{ DiscordApplicationId = $config.DiscordApplicationId; DiscordPublicKey = $config.DiscordPublicKey; AllowedGuildIds = ($config.AllowedGuildIds -join ',') }
    foreach ($name in $expected.Keys) {
        $actual = @($change.Parameters | Where-Object ParameterKey -EQ $name)
        if ($actual.Count -ne 1 -or $actual[0].ParameterValue -cne $expected[$name]) { throw "Change set does not match configuration: $name" }
    }
    $before = Invoke-AwsJson $config @('cloudformation','describe-stacks','--stack-name',$config.StackName)
    $status = $before.Stacks[0].StackStatus
    if ($status -notin 'REVIEW_IN_PROGRESS','CREATE_COMPLETE','UPDATE_COMPLETE','UPDATE_ROLLBACK_COMPLETE','IMPORT_COMPLETE','IMPORT_ROLLBACK_COMPLETE') { throw "Stack is not ready for deployment: $status" }
    $waiter = if ($status -eq 'REVIEW_IN_PROGRESS') { 'stack-create-complete' } else { 'stack-update-complete' }
    $change.Changes | ConvertTo-Json -Depth 30 | Write-Host
    $null = Invoke-AwsJson $config @('cloudformation','execute-change-set','--stack-name',$config.StackName,'--change-set-name',$ExecuteChangeSet)
    $null = Invoke-AwsJson $config @('cloudformation','wait',$waiter,'--stack-name',$config.StackName)
    $stack = Invoke-AwsJson $config @('cloudformation','describe-stacks','--stack-name',$config.StackName)
    $stack.Stacks[0].Outputs | ConvertTo-Json -Depth 10 | Write-Host
    return
}

& (Join-Path $PSScriptRoot 'test.ps1')
$artifact = & (Join-Path $PSScriptRoot 'build.ps1')
$location = Invoke-AwsJson $config @('s3api','get-bucket-location','--bucket',$config.ArtifactBucket)
$bucketRegion = if (-not $location.LocationConstraint) { 'us-east-1' } elseif ($location.LocationConstraint -eq 'EU') { 'eu-west-1' } else { $location.LocationConstraint }
if ($bucketRegion -cne $config.AwsRegion) { throw 'Artifact bucket must be in the Lambda region' }
$artifactKey = "aitc-discord-bot/prod/$([IO.Path]::GetFileName($artifact))"
$null = Invoke-AwsJson $config @('s3api','put-object','--bucket',$config.ArtifactBucket,'--key',$artifactKey,'--body',$artifact)
$existing = @(Invoke-AwsJson $config @('cloudformation','list-stacks','--query',"StackSummaries[?StackName == '$($config.StackName)' && StackStatus != 'DELETE_COMPLETE']"))
$type = if ($existing.Count -eq 0) { 'CREATE' } else { 'UPDATE' }
$runDirectory = Join-Path $projectRoot ('dist/deploy-' + [guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory -Path $runDirectory | Out-Null
$parameters = @(
    @{ ParameterKey = 'ArtifactBucket'; ParameterValue = $config.ArtifactBucket },
    @{ ParameterKey = 'ArtifactKey'; ParameterValue = $artifactKey },
    @{ ParameterKey = 'DiscordApplicationId'; ParameterValue = $config.DiscordApplicationId },
    @{ ParameterKey = 'DiscordPublicKey'; ParameterValue = $config.DiscordPublicKey },
    @{ ParameterKey = 'AllowedGuildIds'; ParameterValue = ($config.AllowedGuildIds -join ',') }
)
if ($type -eq 'UPDATE') {
    $previous = Invoke-AwsJson $config @('cloudformation','describe-stacks','--stack-name',$config.StackName)
    $template = Invoke-AwsJson $config @('cloudformation','get-template','--stack-name',$config.StackName,'--template-stage','Original')
    if ($template.TemplateBody -is [string]) {
        [IO.File]::WriteAllText((Join-Path $runDirectory 'previous-template.yaml'), $template.TemplateBody)
    } else { Write-AtomicJson (Join-Path $runDirectory 'previous-template.yaml') $template.TemplateBody }
    $previousParameters = @($previous.Stacks[0].Parameters | ForEach-Object { @{ ParameterKey = $_.ParameterKey; ParameterValue = $_.ParameterValue } })
    Write-AtomicJson (Join-Path $runDirectory 'previous-parameters.json') $previousParameters
    foreach ($name in 'ThrottlingRateLimit','ThrottlingBurstLimit') {
        if ($name -in $previousParameters.ParameterKey) { $parameters += @{ ParameterKey = $name; UsePreviousValue = $true } }
    }
}
$parameterPath = Join-Path $runDirectory 'parameters.json'
Write-AtomicJson $parameterPath $parameters
$changeSetName = 'manual-' + [guid]::NewGuid().ToString('N')
$templatePath = Join-Path $projectRoot 'template.yaml'
$null = Invoke-AwsJson $config @('cloudformation','validate-template','--template-body',"file://$templatePath")
$null = Invoke-AwsJson $config @('cloudformation','create-change-set','--stack-name',$config.StackName,'--change-set-name',$changeSetName,'--change-set-type',$type,'--template-body',"file://$templatePath",'--parameters',"file://$parameterPath",'--capabilities','CAPABILITY_IAM')
try { $null = Invoke-AwsJson $config @('cloudformation','wait','change-set-create-complete','--stack-name',$config.StackName,'--change-set-name',$changeSetName) }
catch {
    $failed = Invoke-AwsJson $config @('cloudformation','describe-change-set','--stack-name',$config.StackName,'--change-set-name',$changeSetName)
    if ($failed.Status -eq 'FAILED' -and $failed.StatusReason -match "didn't contain changes|No updates") { Write-Host 'No changes'; return }
    throw
}
$change = Invoke-AwsJson $config @('cloudformation','describe-change-set','--stack-name',$config.StackName,'--change-set-name',$changeSetName)
$change.Changes | ConvertTo-Json -Depth 30 | Write-Host
Write-AtomicJson (Join-Path $runDirectory 'change-set.json') $change
Write-Host "Prepared $changeSetName. Review the change set, then run:"
Write-Host ".\scripts\deploy.ps1 -ConfigPath '$ConfigPath' -ExecuteChangeSet '$changeSetName'"
Write-Host "Deployment record: $runDirectory"
