#requires -Version 7.4
$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot '../scripts/common.ps1')
$config = @{
    DiscordApplicationId='111111111111111111'
    AllowedGuildIds=@('222222222222222222','222222222222222223')
    GatchaSourceGuildId='222222222222222222'
    WebLookupApiId='api123'
    WebLookupRegion='ap-northeast-1'
    WebProfileBaseUrl='https://a-i-t-c.com'
}
$script:gatchaRequests = @()
$script:gatchaDefinition = $null
function Invoke-Discord {
    param($Session,$Method,$Path,$Body)
    $script:gatchaRequests += @{Method=$Method;Path=$Path;Body=$Body}
    if ($Method -eq 'POST') {
        $script:gatchaDefinition = $Body
        return @{id='333333333333333333'}
    }
    return $script:gatchaDefinition
}
Sync-GatchaCommands $config @{} (Join-Path $PSScriptRoot '../commands')
$writes = @($script:gatchaRequests | Where-Object Method -CEQ 'POST')
if ($writes.Count -ne 4) { throw 'Expected three source-guild commands and one destination-guild command' }
$source = @($writes | Where-Object { $_.Path -like '*/guilds/222222222222222222/commands' })
$destination = @($writes | Where-Object { $_.Path -like '*/guilds/222222222222222223/commands' })
if ($source.Count -ne 3 -or $destination.Count -ne 1) { throw 'Incorrect command registration scopes' }
if ($destination[0].Body.options.Count -ne 1 -or $destination[0].Body.options[0].name -cne 'draw') { throw 'Destination guild should expose draw only' }
if (@($source | Where-Object { $_.Body.name -eq 'gatcha' })[0].Body.options.Count -ne 3) { throw 'Source guild should expose all management groups' }
if (@($script:gatchaRequests | Where-Object Method -CEQ 'GET').Count -ne 4) { throw 'All writes must be verified' }
$config.GatchaSourceGuildId = '999999999999999999'
$caught = $false
try { Assert-GatchaConfig $config } catch { $caught = $true }
if (-not $caught) { throw 'Source guild must be an allowed guild' }
Write-Host 'Gatcha management tests passed: command scopes, draw-only destinations, verification, configuration'
