#requires -Version 7.4
$ErrorActionPreference = 'Stop'
$common = Join-Path $PSScriptRoot '../scripts/common.ps1'
$testRoot = Join-Path $PSScriptRoot ('tmp-' + [guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory -Path $testRoot | Out-Null
$script:passed = 0
function Assert-True($Condition, [string]$Message = 'Assertion failed') { if (-not $Condition) { throw $Message } }
function Assert-Throws([scriptblock]$Body, [string]$Message = '') {
    $caught = $null
    try { & $Body } catch { $caught = $_ }
    if ($null -eq $caught) { throw 'Expected failure' }
    if ($Message -and $caught.ToString() -notmatch $Message) { throw "Unexpected error: $caught" }
}
function Test-Case([string]$Name, [scriptblock]$Body) {
    . $common
    function aws { throw 'Unexpected real AWS call in test' }
    function Invoke-WebRequest { throw 'Unexpected real HTTP call in test' }
    & $Body
    $script:passed++
    Write-Host "PASS $Name"
}
try {
    Test-Case 'Configuration validation and canonical JSON' {
        $path = Join-Path $testRoot 'config.json'
        $config = @{ AwsProfile='default'; AwsRegion='ap-northeast-1'; StackName='bot-prod'; ArtifactBucket='bucket'; BotTokenParameterName='/bot/token'; DiscordApplicationId='111111111111111111'; DiscordPublicKey=('a' * 64); AllowedGuildIds=@('222222222222222222','222222222222222222') }
        Write-AtomicJson $path $config
        Assert-True ((Read-BotConfig $path).AllowedGuildIds.Count -eq 1)
        $config.AllowedGuildIds = @([long]222222222222222222)
        Write-AtomicJson $path $config
        Assert-Throws { Read-BotConfig $path } 'snowflake strings'
        Assert-True ((ConvertTo-CanonicalJson @{b=@($null,1);a=@{}}) -ceq '{"a":{},"b":[null,1]}')
    }
    Test-Case 'Token/application mismatch stops before command writes' {
        function Assert-AwsHistoryDisabled { }
        function Invoke-AwsJson { return @{Parameter=@{Type='SecureString';Value='test-secret'}} }
        function Invoke-Discord { param($Session,$Method,$Path); Assert-True ($Method -eq 'GET' -and $Path -eq '/applications/@me'); return @{id='999999999999999999'} }
        Assert-Throws { New-DiscordSession @{DiscordApplicationId='111111111111111111';BotTokenParameterName='/bot/token'} } 'another Discord application'
    }
    Test-Case 'SSM failure stops before Discord' {
        function Assert-AwsHistoryDisabled { }
        function Invoke-AwsJson { throw 'SSM unavailable' }
        function Invoke-Discord { throw 'HTTP must not run' }
        Assert-Throws { New-DiscordSession @{BotTokenParameterName='/bot/token'} } 'SSM unavailable'
    }
    Test-Case 'AWS CLI history must be disabled' {
        function aws { $global:LASTEXITCODE = 0; return 'enabled' }
        Assert-Throws { Assert-AwsHistoryDisabled @{AwsProfile='default'} } 'Disable AWS CLI history'
    }
    Test-Case 'AWS JSON arrays remain arrays without an extra nesting level' {
        function aws { $global:LASTEXITCODE = 0; return '[]' }
        Assert-True (@(Invoke-AwsJson @{AwsProfile='test';AwsRegion='ap-northeast-1'} @('cloudformation','list-stacks')).Count -eq 0)
        function aws { $global:LASTEXITCODE = 0; return '[{"StackName":"one"},{"StackName":"two"}]' }
        Assert-True (@(Invoke-AwsJson @{AwsProfile='test';AwsRegion='ap-northeast-1'} @('cloudformation','list-stacks')).Count -eq 2)
    }
    Test-Case 'Discord rate limit is retried and arrays stay flat' {
        $script:httpCalls = 0; $script:slept = 0
        function Invoke-WebRequest {
            $script:httpCalls++
            if ($script:httpCalls -eq 1) { return @{StatusCode=429;Content='{"retry_after":0.001}'} }
            return @{StatusCode=200;Content='[{"id":"222222222222222222"},{"id":"333333333333333333"}]'}
        }
        function Start-Sleep { param($Milliseconds); $script:slept += $Milliseconds }
        $result = @(Invoke-Discord @{Token='secret'} GET '/users/@me/guilds')
        Assert-True ($result.Count -eq 2 -and $script:httpCalls -eq 2 -and $script:slept -eq 1)
    }
    Test-Case 'Rate-limit budget and transport errors do not disclose tokens' {
        function Invoke-WebRequest { return @{StatusCode=429;Content='{"retry_after":31}'} }
        Assert-Throws { Invoke-Discord @{Token='secret'} GET '/users/@me/guilds' } 'budget exhausted'
        function Invoke-WebRequest { throw 'sensitive-token' }
        $errorMessage = ''
        try { Invoke-Discord @{Token='sensitive-token'} GET '/users/@me/guilds' } catch { $errorMessage = $_.ToString() }
        Assert-True ($errorMessage -match 'transport failed' -and $errorMessage -notmatch 'sensitive-token')
    }
    Test-Case 'Guild inventory paginates past the first 200 servers' {
        function Invoke-Discord {
            param($Session,$Method,$Path)
            if ($Path -like '*after=0') {
                return @(0..199 | ForEach-Object { @{id=([string]([decimal]222222222222222000 + $_))} })
            }
            Assert-True ($Path -like '*after=222222222222222199')
            return @{id='222222222222222200'}
        }
        $guilds = @(Get-InstalledGuildIds @{})
        Assert-True ($guilds.Count -eq 201 -and '222222222222222200' -in $guilds)
    }
    Test-Case 'Sync uses guild POST upsert and validates GET result' {
        $script:requests = @()
        function Invoke-Discord {
            param($Session,$Method,$Path,$Body)
            $script:requests += "$Method $Path"
            return @{id='333333333333333333';name='ping';type=1;description='test';options=@()}
        }
        $config = @{DiscordApplicationId='111111111111111111';AllowedGuildIds=@('222222222222222222')}
        Sync-PingCommand $config @{} @{name='ping';type=1;description='test';options=@()}
        Assert-True ($script:requests.Count -eq 2 -and $script:requests[0] -like 'POST */guilds/*/commands' -and $script:requests[1] -like 'GET */commands/*')
        Assert-Throws { Sync-PingCommand $config @{} @{name='ping';description='wrong'} } 'differs'
    }
    Test-Case 'Migration rejects modified, added, and untracked missing commands' {
        $command = @{id='333333333333333333';name='old';type=1;description='old'}
        $snapshot = @{ApplicationId='111111111111111111';AllowedGuildIds=@('222222222222222222');GuildIds=@();Scopes=@(@{Scope='global';Commands=@($command)});Permissions=@{}}
        $current = (ConvertTo-Json -InputObject $snapshot -Depth 30) | ConvertFrom-Json -AsHashtable
        Assert-MigrationUnchanged $snapshot $current @()
        $current.Scopes[0].Commands[0].description = 'changed'
        Assert-Throws { Assert-MigrationUnchanged $snapshot $current @() } 'changed'
        $current.Scopes[0].Commands = @()
        Assert-Throws { Assert-MigrationUnchanged $snapshot $current @() } 'disappeared'
        Assert-MigrationUnchanged $snapshot $current @('global/333333333333333333')
    }
    Test-Case 'Backup scopes and permissions are checked before a migration' {
        $id = '222222222222222222'
        $config = @{DiscordApplicationId='111111111111111111';AllowedGuildIds=@($id)}
        $snapshot = @{SchemaVersion=1;ApplicationId=$config.DiscordApplicationId;AllowedGuildIds=@($id);GuildIds=@($id);Scopes=@(@{Scope='global';Commands=@()},@{Scope=$id;Commands=@()});Permissions=@{$id=@()}}
        Assert-CommandBackup $snapshot $config
        $snapshot.Scopes += @{Scope='unexpected/path';Commands=@()}
        Assert-Throws { Assert-CommandBackup $snapshot $config } 'scopes'
        $before = @{ApplicationId=$config.DiscordApplicationId;AllowedGuildIds=@($id);GuildIds=@($id);Scopes=@(@{Scope='global';Commands=@()});Permissions=@{$id=@(@{id='111111111111111111';permissions=@()})}}
        $after = (ConvertTo-Json -InputObject $before -Depth 30) | ConvertFrom-Json -AsHashtable
        $after.Permissions[$id][0].permissions = @(@{id=$id;type=1;permission=$false})
        Assert-Throws { Assert-MigrationUnchanged $before $after @() } 'permissions changed'
    }
    Test-Case 'Deletion journal permits restart after response loss' {
        $command = @{id='333333333333333333';name='old';type=1;description='old'}
        $snapshot = @{ApplicationId='111111111111111111';AllowedGuildIds=@();GuildIds=@();Scopes=@(@{Scope='global';Commands=@($command)});Permissions=@{}}
        $backup = Join-Path $testRoot 'backup.json'; Write-AtomicJson $backup $snapshot
        $script:live = $true; $script:deleteCalls = 0; $script:loseResponse = $true
        function Get-CommandSnapshot {
            return @{ApplicationId='111111111111111111';AllowedGuildIds=@();GuildIds=@();Scopes=@(@{Scope='global';Commands=$(if ($script:live) { ,$command } else { @() })});Permissions=@{}}
        }
        function Invoke-Discord {
            param($Session,$Method,$Path)
            if ($Method -eq 'GET') { return $command }
            if ($Method -ne 'DELETE') { throw 'Unexpected write' }
            $script:deleteCalls++; $script:live = $false
            if ($script:loseResponse) { throw 'Response lost' }
        }
        $config = @{DiscordApplicationId='111111111111111111'}
        Assert-Throws { Invoke-CommandMigration $config @{} $snapshot $backup } 'Response lost'
        $script:loseResponse = $false
        Invoke-CommandMigration $config @{} $snapshot $backup
        Assert-True ($script:deleteCalls -eq 1)
        $state = Get-Content -LiteralPath "$backup.state.json" -Raw | ConvertFrom-Json -AsHashtable
        Assert-True ($state.Attempted.Count -eq 1)
    }
    Test-Case 'Restoration refuses unrelated commands before any mutation' {
        function Get-CommandSnapshot { return @{GuildIds=@();Scopes=@(@{Scope='global';Commands=@(@{id='444444444444444444';name='unrelated';type=1})})} }
        function Invoke-Discord { throw 'No writes allowed' }
        $snapshot = @{GuildIds=@();Scopes=@(@{Scope='global';Commands=@()})}
        Assert-Throws { Restore-CommandSnapshot @{AllowedGuildIds=@()} @{} $snapshot @{name='ping'} } 'Unrelated command'
    }
    Test-Case 'Restoration removes new ping and recreates global and guild definitions' {
        $guildId = '222222222222222222'
        $ping = @{name='ping';type=1;description='new';options=@();default_member_permissions=$null;nsfw=$false}
        $script:restoreRequests = @(); $script:restored = @{}
        function Get-CommandSnapshot {
            return @{GuildIds=@($guildId);Scopes=@(@{Scope='global';Commands=@()},@{Scope=$guildId;Commands=@($ping + @{id='999999999999999999'})})}
        }
        function Invoke-Discord {
            param($Session,$Method,$Path,$Body)
            $script:restoreRequests += "$Method $Path"
            if ($Method -eq 'DELETE') { return }
            if ($Method -eq 'POST') {
                Assert-True (-not $Body.ContainsKey('id'))
                $value = $Body + @{id='333333333333333333'}
                $script:restored["$Path/333333333333333333"] = $value
                return $value
            }
            return $script:restored[$Path]
        }
        $old = @{id='444444444444444444';name='old';type=1;description='old';options=@()}
        $snapshot = @{GuildIds=@($guildId);Scopes=@(@{Scope='global';Commands=@($old)},@{Scope=$guildId;Commands=@($old)})}
        Restore-CommandSnapshot @{DiscordApplicationId='111111111111111111';AllowedGuildIds=@($guildId)} @{} $snapshot $ping
        Assert-True ($script:restoreRequests[0] -like 'DELETE */999999999999999999')
        Assert-True (@($script:restoreRequests | Where-Object { $_ -like 'POST *' }).Count -eq 2)
    }
    Write-Host "Management tests passed: $script:passed"
} finally {
    $expected = [IO.Path]::GetFullPath($PSScriptRoot)
    if ((Split-Path -Parent ([IO.Path]::GetFullPath($testRoot))) -ne $expected) { throw 'Unsafe test cleanup path' }
    Remove-Item -LiteralPath $testRoot -Recurse -Force
}
