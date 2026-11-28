#requires -Version 7.4
[CmdletBinding(SupportsShouldProcess)]
param([string]$ConfigPath = (Join-Path $PSScriptRoot '../config/prod.json'))
. (Join-Path $PSScriptRoot 'common.ps1')
$config = Read-BotConfig $ConfigPath
Assert-GatchaConfig $config
if ($WhatIfPreference) { Write-Host "Would upsert /ping and /gatcha in: $($config.AllowedGuildIds -join ', ')"; return }
$session = New-DiscordSession $config
try {
    $definition = Get-Content -LiteralPath (Join-Path $PSScriptRoot '../commands/ping.json') -Raw | ConvertFrom-Json -AsHashtable
    if ($PSCmdlet.ShouldProcess(($config.AllowedGuildIds -join ', '), 'Register /ping and /gatcha')) {
        Sync-PingCommand $config $session $definition
        Sync-GatchaCommands $config $session (Join-Path $PSScriptRoot '../commands')
    }
} finally { $session.Token = $null }
