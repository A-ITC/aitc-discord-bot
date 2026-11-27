#requires -Version 7.4
[CmdletBinding(SupportsShouldProcess)]
param([string]$ConfigPath = (Join-Path $PSScriptRoot '../config/prod.json'))
. (Join-Path $PSScriptRoot 'common.ps1')
$config = Read-BotConfig $ConfigPath
if ($WhatIfPreference) { Write-Host "Would upsert /ping in: $($config.AllowedGuildIds -join ', ')"; return }
$session = New-DiscordSession $config
try {
    $definition = Get-Content -LiteralPath (Join-Path $PSScriptRoot '../commands/ping.json') -Raw | ConvertFrom-Json -AsHashtable
    if ($PSCmdlet.ShouldProcess(($config.AllowedGuildIds -join ', '), 'Register /ping')) { Sync-PingCommand $config $session $definition }
} finally { $session.Token = $null }
