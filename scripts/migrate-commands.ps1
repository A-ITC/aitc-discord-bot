#requires -Version 7.4
[CmdletBinding(SupportsShouldProcess, ConfirmImpact='High')]
param(
    [ValidateSet('Backup','Apply','Restore')][string]$Mode = 'Backup',
    [Parameter(Mandatory)][string]$BackupPath,
    [string]$ConfigPath = (Join-Path $PSScriptRoot '../config/prod.json'),
    [switch]$ConfirmGuildInventory
)
. (Join-Path $PSScriptRoot 'common.ps1')
$config = Read-BotConfig $ConfigPath
if ($Mode -ne 'Backup' -and -not $ConfirmGuildInventory) { throw 'Review every installed guild, including command-only installs; pass -ConfirmGuildInventory after checking the inventory' }
if ($WhatIfPreference) { Write-Host "Would perform $Mode with application $($config.DiscordApplicationId), backup $BackupPath"; return }
$snapshot = $null
if ($Mode -eq 'Backup') {
    if (Test-Path -LiteralPath $BackupPath) { throw 'Backup already exists; choose a new path' }
} else {
    $snapshot = Get-Content -LiteralPath $BackupPath -Raw | ConvertFrom-Json -AsHashtable -Depth 100
    Assert-CommandBackup $snapshot $config
}
$session = New-DiscordSession $config
try {
    switch ($Mode) {
        Backup {
            $snapshot = Get-CommandSnapshot $config $session
            Write-AtomicJson $BackupPath $snapshot
            Write-Host "Backup saved: $BackupPath"
            Write-Host "Guild inventory: $($snapshot.GuildIds -join ', ')"
            Write-Host 'Check command-only installs manually and list additional guild IDs in LegacyGuildIds before applying.'
        }
        Apply {
            foreach ($scope in $snapshot.Scopes) {
                foreach ($command in $scope.Commands) { Write-Host "DELETE $($scope.Scope)/$($command.id) $($command.name)" }
            }
            if ($PSCmdlet.ShouldProcess($config.DiscordApplicationId, 'Delete all backed-up legacy commands')) {
                Invoke-CommandMigration $config $session $snapshot $BackupPath
                Write-Host 'Legacy commands removed. Run sync-commands.ps1 next.'
            }
        }
        Restore {
            $ping = Get-Content -LiteralPath (Join-Path $PSScriptRoot '../commands/ping.json') -Raw | ConvertFrom-Json -AsHashtable
            if ($PSCmdlet.ShouldProcess($config.DiscordApplicationId, 'Remove new /ping and restore backed-up command definitions')) {
                Restore-CommandSnapshot $config $session $snapshot $ping
            }
        }
    }
} finally { $session.Token = $null }
