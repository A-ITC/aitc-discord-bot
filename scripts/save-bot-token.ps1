#requires -Version 7.4
[CmdletBinding()]
param([string]$ConfigPath = (Join-Path $PSScriptRoot '../config/prod.json'))
. (Join-Path $PSScriptRoot 'common.ps1')
$config = Read-BotConfig $ConfigPath
Assert-AwsHistoryDisabled $config
if (-not $IsWindows) { throw 'This token-saving script requires Windows for its private temporary-directory ACL' }
$secureToken = Read-Host 'Discord Bot Token' -AsSecureString
$temporaryDirectory = Join-Path ([IO.Path]::GetTempPath()) ('aitc-bot-token-' + [guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory -Path $temporaryDirectory | Out-Null
$temporaryDirectory = (Resolve-Path -LiteralPath $temporaryDirectory).ProviderPath
try {
    $acl = [Security.AccessControl.DirectorySecurity]::new()
    $acl.SetAccessRuleProtection($true, $false)
    $sid = [Security.Principal.WindowsIdentity]::GetCurrent().User
    $acl.AddAccessRule([Security.AccessControl.FileSystemAccessRule]::new($sid, 'FullControl', 'ContainerInherit,ObjectInherit', 'None', 'Allow'))
    Set-Acl -LiteralPath $temporaryDirectory -AclObject $acl
    $actualAcl = Get-Acl -LiteralPath $temporaryDirectory
    $rules = @($actualAcl.GetAccessRules($true, $true, [Security.Principal.SecurityIdentifier]))
    if (-not $actualAcl.AreAccessRulesProtected -or $rules.Count -ne 1 -or $rules[0].IdentityReference -ne $sid -or $rules[0].AccessControlType -ne 'Allow' -or $rules[0].FileSystemRights -ne 'FullControl' -or $rules[0].IsInherited) {
        throw 'Cannot verify private temporary-directory permissions; Token was not written'
    }
    $pointer = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secureToken)
    try {
        $token = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($pointer)
        if ([string]::IsNullOrWhiteSpace($token)) { throw 'Bot Token is empty' }
        $body = @{ Name = $config.BotTokenParameterName; Value = $token; Type = 'SecureString'; Tier = 'Standard'; Overwrite = $true }
        $temporaryFile = Join-Path $temporaryDirectory 'parameter.json'
        Write-AtomicJson $temporaryFile $body
        $null = Invoke-AwsJson $config @('ssm','put-parameter','--cli-input-json',"file://$temporaryFile")
    } finally {
        [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($pointer)
        $token = $null; $body = $null
    }
} finally {
    $expectedParent = [IO.Path]::GetFullPath([IO.Path]::GetTempPath()).TrimEnd([IO.Path]::DirectorySeparatorChar)
    if ((Split-Path -Parent $temporaryDirectory) -ne $expectedParent -or (Split-Path -Leaf $temporaryDirectory) -notmatch '^aitc-bot-token-[0-9a-f]{32}$') { throw 'Unsafe token cleanup path' }
    Remove-Item -LiteralPath $temporaryDirectory -Recurse -Force
    $secureToken.Dispose()
}
Write-Host 'Bot Token saved as SecureString; no token is deployed to Lambda.'
