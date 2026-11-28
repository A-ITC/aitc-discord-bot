#requires -Version 7.4
Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

function Read-BotConfig {
    param([Parameter(Mandatory)][string]$Path)
    $config = Get-Content -LiteralPath $Path -Raw | ConvertFrom-Json -AsHashtable
    foreach ($name in 'AwsProfile', 'AwsRegion', 'StackName', 'ArtifactBucket', 'BotTokenParameterName') {
        if (-not $config.ContainsKey($name) -or $config[$name] -isnot [string] -or [string]::IsNullOrWhiteSpace($config[$name])) {
            throw "Missing configuration: $name"
        }
    }
    if ($config.DiscordApplicationId -isnot [string] -or $config.DiscordApplicationId -cnotmatch '^[0-9]{17,20}$') { throw 'Invalid DiscordApplicationId' }
    if ($config.DiscordPublicKey -isnot [string] -or $config.DiscordPublicKey -cnotmatch '^[0-9a-fA-F]{64}$') { throw 'Invalid DiscordPublicKey' }
    if ($config.AllowedGuildIds -isnot [array] -or $config.AllowedGuildIds.Count -eq 0) { throw 'AllowedGuildIds must be a nonempty array' }
    foreach ($id in @($config.AllowedGuildIds) + @($config['LegacyGuildIds'] ?? @())) {
        if ($id -isnot [string] -or $id -cnotmatch '^[0-9]{17,20}$') { throw 'Guild IDs must be snowflake strings' }
    }
    $config.AllowedGuildIds = @($config.AllowedGuildIds | Sort-Object -Unique)
    if ($config.AwsRegion -notmatch '^[a-z]{2}(-[a-z]+)+-[0-9]+$') { throw 'Invalid AwsRegion' }
    if ($config.StackName -notmatch '^[A-Za-z][A-Za-z0-9-]{0,127}$') { throw 'Invalid StackName' }
    if ($config.BotTokenParameterName -notmatch '^/[A-Za-z0-9_./-]+$') { throw 'Invalid BotTokenParameterName' }
    return $config
}

function Invoke-AwsJson {
    param([hashtable]$Config, [string[]]$Arguments)
    $output = & aws @Arguments --profile $Config.AwsProfile --region $Config.AwsRegion --no-cli-pager --output json 2>&1
    if ($LASTEXITCODE -ne 0) { throw "AWS operation failed: $($Arguments[0]) $($Arguments[1]) (exit $LASTEXITCODE)" }
    $text = $output -join "`n"
    if (-not [string]::IsNullOrWhiteSpace($text)) {
        return ConvertFrom-Json -InputObject $text -AsHashtable -Depth 100
    }
}

function Assert-AwsHistoryDisabled {
    param([hashtable]$Config)
    $history = & aws configure get cli_history --profile $Config.AwsProfile 2>&1
    if ($LASTEXITCODE -gt 1 -or ($LASTEXITCODE -eq 0 -and ($history -join '').Trim() -ne 'disabled')) {
        throw 'Disable AWS CLI history for this profile before handling the Bot Token'
    }
}

function Invoke-Discord {
    param([hashtable]$Session, [ValidateSet('GET','POST','DELETE')][string]$Method, [string]$Path, $Body)
    if ($Path -notmatch '^/(applications|users)/' -or $Path.Contains('..')) { throw 'Invalid Discord API path' }
    $elapsed = 0.0
    for ($attempt = 0; $attempt -lt 5; $attempt++) {
        $parameters = @{
            Uri = "https://discord.com/api/v10$Path"; Method = $Method
            Headers = @{ Authorization = "Bot $($Session.Token)"; 'User-Agent' = 'DiscordBot (https://github.com/A-ITC/aitc-discord-bot, 1.0)' }
            SkipHttpErrorCheck = $true; MaximumRedirection = 0; TimeoutSec = 20
        }
        if ($null -ne $Body) {
            $parameters.ContentType = 'application/json; charset=utf-8'
            $parameters.Body = [Text.Encoding]::UTF8.GetBytes(($Body | ConvertTo-Json -Depth 100 -Compress))
        }
        try { $response = Invoke-WebRequest @parameters }
        catch { throw "Discord transport failed: $Method $Path" }
        $content = $null
        if (-not [string]::IsNullOrWhiteSpace($response.Content)) {
            try { $content = ConvertFrom-Json -InputObject $response.Content -AsHashtable -Depth 100 }
            catch { throw "Discord returned invalid JSON: $Method $Path" }
        }
        if ($response.StatusCode -eq 429) {
            $delay = if ($content -is [hashtable] -and $content.ContainsKey('retry_after')) { [double]$content.retry_after } else { -1 }
            if (-not [double]::IsFinite($delay) -or $delay -lt 0 -or ($elapsed + $delay) -gt 30 -or $attempt -eq 4) {
                throw 'Discord rate limit retry budget exhausted'
            }
            $elapsed += $delay
            Start-Sleep -Milliseconds ([int][Math]::Ceiling($delay * 1000))
            continue
        }
        if ($response.StatusCode -lt 200 -or $response.StatusCode -ge 300) {
            throw "Discord request failed: $Method $Path (HTTP $($response.StatusCode))"
        }
        return $content
    }
}

function New-DiscordSession {
    param([hashtable]$Config)
    Assert-AwsHistoryDisabled $Config
    $parameter = Invoke-AwsJson $Config @('ssm','get-parameter','--name',$Config.BotTokenParameterName,'--with-decryption')
    if ($parameter.Parameter.Type -ne 'SecureString' -or [string]::IsNullOrWhiteSpace($parameter.Parameter.Value)) { throw 'Bot token must be a nonempty SecureString' }
    $session = @{ Token = $parameter.Parameter.Value }
    $app = Invoke-Discord $session GET '/applications/@me'
    if ($app.id -cne $Config.DiscordApplicationId) { throw 'Bot token belongs to another Discord application' }
    $session.ApplicationId = $app.id
    $session.EndpointUrl = $app['interactions_endpoint_url']
    return $session
}

function Get-InstalledGuildIds {
    param([hashtable]$Session)
    $ids = [Collections.Generic.List[string]]::new()
    $after = '0'
    do {
        $page = @(Invoke-Discord $Session GET "/users/@me/guilds?limit=200&after=$after")
        foreach ($guild in $page) {
            if ($guild.id -isnot [string] -or $guild.id -notmatch '^[0-9]{17,20}$') { throw 'Invalid guild inventory response' }
            $ids.Add($guild.id)
        }
        if ($page.Count -gt 0) {
            $last = ($page.id | Sort-Object { [decimal]$_ } | Select-Object -Last 1)
            if ([decimal]$last -le [decimal]$after) { throw 'Guild inventory pagination did not advance' }
            $after = $last
        }
    } while ($page.Count -eq 200)
    return @($ids | Sort-Object -Unique)
}

function ConvertTo-CanonicalJson {
    param($Value)
    if ($Value -is [Collections.IDictionary]) {
        $pairs = foreach ($key in ($Value.Keys | Sort-Object -CaseSensitive)) {
            (ConvertTo-Json -InputObject ([string]$key) -Compress) + ':' + (ConvertTo-CanonicalJson $Value[$key])
        }
        return '{' + ($pairs -join ',') + '}'
    }
    if ($Value -is [array]) {
        $items = foreach ($item in $Value) { ConvertTo-CanonicalJson $item }
        return '[' + ($items -join ',') + ']'
    }
    return ConvertTo-Json -InputObject $Value -Depth 100 -Compress
}

function Write-AtomicJson {
    param([string]$Path, $Value)
    $fullPath = [IO.Path]::GetFullPath($Path)
    $parent = Split-Path -Parent $fullPath
    New-Item -ItemType Directory -Path $parent -Force | Out-Null
    $temporary = Join-Path $parent ([guid]::NewGuid().ToString('N') + '.tmp')
    try {
        [IO.File]::WriteAllText($temporary, (ConvertTo-Json -InputObject $Value -Depth 100), [Text.UTF8Encoding]::new($false))
        [IO.File]::Move($temporary, $fullPath, $true)
    } finally {
        if (Test-Path -LiteralPath $temporary) { Remove-Item -LiteralPath $temporary -Force }
    }
}

function Get-CommandDefinition {
    param([hashtable]$Command, [string]$Scope)
    $fields = @('type','name','name_localizations','description','description_localizations','options','default_member_permissions','nsfw')
    if ($Scope -eq 'global') { $fields += @('integration_types','contexts','dm_permission') }
    $definition = @{}
    foreach ($key in $fields) { if ($Command.ContainsKey($key)) { $definition[$key] = $Command[$key] } }
    return $definition
}

function Assert-CommandDefinition {
    param([hashtable]$Actual, [hashtable]$Expected)
    foreach ($key in $Expected.Keys) {
        $actualValue = $Actual[$key]
        if ($key -eq 'options' -and $null -eq $actualValue) { $actualValue = @() }
        if ((ConvertTo-CanonicalJson $actualValue) -cne (ConvertTo-CanonicalJson $Expected[$key])) { throw "Registered command differs: $key" }
    }
}

function Sync-PingCommand {
    param([hashtable]$Config, [hashtable]$Session, [hashtable]$Definition)
    foreach ($guildId in $Config.AllowedGuildIds) {
        $path = "/applications/$($Config.DiscordApplicationId)/guilds/$guildId/commands"
        $registered = Invoke-Discord $Session POST $path $Definition
        $actual = Invoke-Discord $Session GET "$path/$($registered.id)"
        Assert-CommandDefinition $actual $Definition
        Write-Host "Verified /ping in guild $guildId"
    }
}

function Get-CommandSnapshot {
    param([hashtable]$Config, [hashtable]$Session)
    $guildIds = @(@(Get-InstalledGuildIds $Session) + $Config.AllowedGuildIds + @($Config['LegacyGuildIds'] ?? @()) | Sort-Object -Unique)
    $scopes = [Collections.Generic.List[object]]::new()
    $appPath = "/applications/$($Config.DiscordApplicationId)"
    $scopes.Add(@{ Scope = 'global'; Commands = @(Invoke-Discord $Session GET "$appPath/commands?with_localizations=true") })
    $permissions = @{}
    foreach ($guildId in $guildIds) {
        $scopes.Add(@{ Scope = $guildId; Commands = @(Invoke-Discord $Session GET "$appPath/guilds/$guildId/commands?with_localizations=true") })
        $permissions[$guildId] = @(Invoke-Discord $Session GET "$appPath/guilds/$guildId/commands/permissions")
    }
    foreach ($scope in $scopes) {
        foreach ($command in $scope.Commands) {
            if ($command.type -notin 1,2,3 -or $command.id -notmatch '^[0-9]{17,20}$') { throw 'Inventory includes an unsupported command; review before migration' }
        }
    }
    return @{
        SchemaVersion = 1; ApplicationId = $Config.DiscordApplicationId
        EndpointUrl = $Session.EndpointUrl; AllowedGuildIds = @($Config.AllowedGuildIds)
        GuildIds = $guildIds; Scopes = $scopes.ToArray(); Permissions = $permissions
    }
}

function Assert-MigrationUnchanged {
    param([hashtable]$Snapshot, [hashtable]$Current, [string[]]$Attempted)
    foreach ($field in 'ApplicationId','AllowedGuildIds','GuildIds') {
        if ((ConvertTo-CanonicalJson $Snapshot[$field]) -cne (ConvertTo-CanonicalJson $Current[$field])) { throw "Migration inventory changed: $field" }
    }
    $removed = @{}
    foreach ($scope in $Snapshot.Scopes) {
        $liveScopes = @($Current.Scopes | Where-Object Scope -CEQ $scope.Scope)
        if ($liveScopes.Count -ne 1) { throw 'Migration scope changed' }
        $live = $liveScopes[0]
        foreach ($command in $live.Commands) {
            $saved = @($scope.Commands | Where-Object id -CEQ $command.id)
            if ($saved.Count -ne 1 -or (ConvertTo-CanonicalJson $saved[0]) -cne (ConvertTo-CanonicalJson $command)) { throw 'Command changed since backup' }
        }
        foreach ($command in $scope.Commands) {
            if ($command.id -cnotin @($live.Commands | ForEach-Object { $_.id })) {
                if ("$($scope.Scope)/$($command.id)" -cnotin $Attempted) { throw 'Command disappeared outside this migration' }
                $removed[$command.id] = $true
            }
        }
    }
    foreach ($guildId in $Snapshot.GuildIds) {
        $before = @($Snapshot.Permissions[$guildId] | Where-Object { -not $removed.ContainsKey($_.id) } | Sort-Object id)
        $after = @($Current.Permissions[$guildId] | Where-Object { -not $removed.ContainsKey($_.id) } | Sort-Object id)
        if ((ConvertTo-CanonicalJson $before) -cne (ConvertTo-CanonicalJson $after)) { throw 'Command permissions changed since backup' }
    }
}

function Assert-CommandBackup {
    param([hashtable]$Snapshot, [hashtable]$Config)
    if ($Snapshot['SchemaVersion'] -ne 1 -or $Snapshot['ApplicationId'] -cne $Config.DiscordApplicationId) { throw 'Backup does not match this application/schema' }
    foreach ($field in 'GuildIds','AllowedGuildIds','Scopes') {
        if ($Snapshot[$field] -isnot [array]) { throw "Invalid backup field: $field" }
    }
    foreach ($id in @($Snapshot.GuildIds) + @($Snapshot.AllowedGuildIds)) {
        if ($id -isnot [string] -or $id -cnotmatch '^[0-9]{17,20}$') { throw 'Invalid guild ID in backup' }
    }
    if ((ConvertTo-CanonicalJson @($Snapshot.AllowedGuildIds | Sort-Object -Unique)) -cne (ConvertTo-CanonicalJson @($Config.AllowedGuildIds | Sort-Object -Unique))) { throw 'Backup allowed guilds do not match configuration' }
    $expectedScopes = @(@('global') + $Snapshot.GuildIds | Sort-Object)
    $actualScopes = @($Snapshot.Scopes | ForEach-Object { $_.Scope } | Sort-Object)
    if ((ConvertTo-CanonicalJson $expectedScopes) -cne (ConvertTo-CanonicalJson $actualScopes)) { throw 'Invalid backup scopes' }
    foreach ($scope in $Snapshot.Scopes) {
        if ($scope.Commands -isnot [array]) { throw 'Invalid backup commands' }
        $ids = @{}
        foreach ($command in $scope.Commands) {
            if ($command -isnot [hashtable] -or $command['id'] -isnot [string] -or $command.id -notmatch '^[0-9]{17,20}$' -or $command['type'] -notin 1,2,3 -or $command['name'] -isnot [string] -or $ids.ContainsKey($command.id)) { throw 'Invalid or duplicate backup command' }
            $ids[$command.id] = $true
        }
    }
    if ($Snapshot['Permissions'] -isnot [hashtable]) { throw 'Invalid backup permissions' }
    foreach ($guildId in $Snapshot.GuildIds) {
        if ($Snapshot.Permissions[$guildId] -isnot [array]) { throw 'Missing backup permissions' }
    }
}

function Invoke-CommandMigration {
    param([hashtable]$Config, [hashtable]$Session, [hashtable]$Snapshot, [string]$BackupPath)
    $hash = (Get-FileHash -LiteralPath $BackupPath -Algorithm SHA256).Hash
    $statePath = "$BackupPath.state.json"
    $state = if (Test-Path -LiteralPath $statePath) { Get-Content -LiteralPath $statePath -Raw | ConvertFrom-Json -AsHashtable } else { @{ BackupHash = $hash; Attempted = @(); Deleted = @() } }
    if ($state.BackupHash -cne $hash) { throw 'Migration backup changed; refusing to reuse journal' }
    $current = Get-CommandSnapshot $Config $Session
    Assert-MigrationUnchanged $Snapshot $current $state.Attempted
    foreach ($scope in $Snapshot.Scopes) {
        $path = "/applications/$($Config.DiscordApplicationId)"
        if ($scope.Scope -ne 'global') { $path += "/guilds/$($scope.Scope)" }
        foreach ($command in $scope.Commands) {
            $key = "$($scope.Scope)/$($command.id)"
            $liveScope = @($current.Scopes | Where-Object Scope -CEQ $scope.Scope)[0]
            if ($command.id -cnotin @($liveScope.Commands | ForEach-Object { $_.id })) { continue }
            $liveCommands = @(Invoke-Discord $Session GET "$path/commands?with_localizations=true")
            $matches = @($liveCommands | Where-Object id -CEQ $command.id)
            if ($matches.Count -ne 1 -or (ConvertTo-CanonicalJson $command) -cne (ConvertTo-CanonicalJson $matches[0])) { throw 'Command changed immediately before deletion' }
            if ($key -cnotin $state.Attempted) { $state.Attempted += $key }
            Write-AtomicJson $statePath $state
            $null = Invoke-Discord $Session DELETE "$path/commands/$($command.id)"
            $state.Deleted = @($state.Deleted + $key | Sort-Object -Unique)
            Write-AtomicJson $statePath $state
            Write-Host "Deleted $key"
        }
    }
    $after = Get-CommandSnapshot $Config $Session
    Assert-MigrationUnchanged $Snapshot $after $state.Attempted
    if (@($after.Scopes | ForEach-Object { $_.Commands }).Count -ne 0) { throw 'Legacy commands remain; migration incomplete' }
}

function Restore-CommandSnapshot {
    param([hashtable]$Config, [hashtable]$Session, [hashtable]$Snapshot, [hashtable]$PingDefinition)
    $current = Get-CommandSnapshot $Config $Session
    if ((ConvertTo-CanonicalJson $Snapshot.GuildIds) -cne (ConvertTo-CanonicalJson $current.GuildIds)) { throw 'Guild inventory changed before restoration' }
    # Validate every current command before any write. Never overwrite an unrelated command.
    $remove = @()
    foreach ($scope in $current.Scopes) {
        $savedScope = @($Snapshot.Scopes | Where-Object Scope -CEQ $scope.Scope)[0]
        foreach ($command in $scope.Commands) {
            $isNewPing = $false
            if ($scope.Scope -cin $Config.AllowedGuildIds -and $command.name -ceq 'ping' -and $command.type -eq 1) {
                try { Assert-CommandDefinition $command $PingDefinition; $isNewPing = $true } catch { }
            }
            if ($isNewPing) { $remove += @{ Scope = $scope.Scope; Command = $command }; continue }
            $saved = @($savedScope.Commands | Where-Object { $_.name -ceq $command.name -and $_.type -eq $command.type })
            if ($saved.Count -ne 1) { throw 'Unrelated command exists; restoration stopped' }
            Assert-CommandDefinition $command (Get-CommandDefinition $saved[0] $scope.Scope)
        }
    }
    foreach ($item in $remove) {
        $null = Invoke-Discord $Session DELETE "/applications/$($Config.DiscordApplicationId)/guilds/$($item.Scope)/commands/$($item.Command.id)"
    }
    foreach ($scope in $Snapshot.Scopes) {
        $path = "/applications/$($Config.DiscordApplicationId)"
        if ($scope.Scope -ne 'global') { $path += "/guilds/$($scope.Scope)" }
        foreach ($command in $scope.Commands) {
            $definition = Get-CommandDefinition $command $scope.Scope
            $registered = Invoke-Discord $Session POST "$path/commands" $definition
            $actual = Invoke-Discord $Session GET "$path/commands/$($registered.id)"
            Assert-CommandDefinition $actual $definition
        }
    }
    Write-Host 'Restore endpoint URL, command permission overrides in Discord, and the old process manually; command IDs may have changed.'
}


function Assert-GatchaConfig {
    param([hashtable]$Config)
    if ($Config['GatchaSourceGuildId'] -isnot [string] -or $Config.GatchaSourceGuildId -cnotmatch '^[0-9]{17,20}$' -or $Config.GatchaSourceGuildId -cnotin $Config.AllowedGuildIds) { throw 'GatchaSourceGuildId must be an allowed guild ID string' }
    if ($Config['WebLookupApiId'] -isnot [string] -or $Config.WebLookupApiId -cnotmatch '^[a-z0-9]+$') { throw 'Invalid WebLookupApiId' }
    if ($Config['WebLookupRegion'] -isnot [string] -or $Config.WebLookupRegion -cnotmatch '^[a-z]{2}(-[a-z]+)+-[0-9]+$') { throw 'Invalid WebLookupRegion' }
    if ($Config['WebProfileBaseUrl'] -isnot [string] -or $Config.WebProfileBaseUrl -cnotmatch '^https://[A-Za-z0-9.-]+/?$') { throw 'Invalid WebProfileBaseUrl' }
}

function Sync-GatchaCommands {
    param([hashtable]$Config, [hashtable]$Session, [string]$DefinitionsPath)
    Assert-GatchaConfig $Config
    foreach ($guildId in $Config.AllowedGuildIds) {
        $names = if ($guildId -ceq $Config.GatchaSourceGuildId) { @('gatcha','gatcha-register','gatcha-remove') } else { @('gatcha') }
        foreach ($name in $names) {
            $definition = Get-Content -LiteralPath (Join-Path $DefinitionsPath "$name.json") -Raw | ConvertFrom-Json -AsHashtable
            if ($name -eq 'gatcha' -and $guildId -cne $Config.GatchaSourceGuildId) { $definition.options = @($definition.options | Where-Object name -CEQ 'draw') }
            $path = "/applications/$($Config.DiscordApplicationId)/guilds/$guildId/commands"
            $registered = Invoke-Discord $Session POST $path $definition
            $actual = Invoke-Discord $Session GET "$path/$($registered.id)"
            Assert-CommandDefinition $actual $definition
            Write-Host "Verified $($definition.name) in guild $guildId"
        }
    }
}
