#requires -Version 7.4
[CmdletBinding()]
param()
$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
Push-Location $projectRoot
try {
    & uv run --no-project --python 3.13 --with-requirements requirements-dev.txt python -m unittest discover -s tests -v
    if ($LASTEXITCODE -ne 0) { throw 'Python tests failed' }
    & uv run --no-project --python 3.13 --with-requirements requirements-dev.txt cfn-lint template.yaml
    if ($LASTEXITCODE -ne 0) { throw 'CloudFormation lint failed' }
    & (Join-Path $projectRoot 'tests/test_management.ps1')
    & (Join-Path $projectRoot 'tests/test_gatcha_management.ps1')
    & (Join-Path $projectRoot 'tests/test_build.ps1')
    & (Join-Path $projectRoot 'tests/test_deployment.ps1')
    & (Join-Path $projectRoot 'tests/test_secret.ps1')
} finally { Pop-Location }
