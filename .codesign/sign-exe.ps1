# sign-exe.ps1 - Sign a built exe with Azure Trusted Signing.
#
# Prerequisites (one-time):
#   - Azure CLI installed and `az login` run interactively as a user with
#     signing permission on the speakmanai-signing Trusted Signing account.
#   - This .codesign/ folder present (signtool.exe, Azure.CodeSigning.Dlib.dll,
#     metadata.json) - gitignored, not part of repo source.
#
# Usage:
#   .\.codesign\sign-exe.ps1                          # signs dist\SpeakmanAI.exe
#   .\.codesign\sign-exe.ps1 -Path path\to\other.exe   # signs a different file

param(
    [string]$Path = (Join-Path $PSScriptRoot "..\dist\SpeakmanAI.exe")
)

$ErrorActionPreference = 'Stop'
$root = $PSScriptRoot

$signtool = Join-Path $root "sdk-extracted\bin\10.0.26100.0\x64\signtool.exe"
$dlib     = Join-Path $root "extracted\bin\x64\Azure.CodeSigning.Dlib.dll"
$metadata = Join-Path $root "metadata.json"

if (-not (Test-Path $Path))     { throw "File to sign not found: $Path" }
if (-not (Test-Path $signtool)) { throw "signtool.exe not found: $signtool" }
if (-not (Test-Path $dlib))     { throw "Azure.CodeSigning.Dlib.dll not found: $dlib" }
if (-not (Test-Path $metadata)) { throw "metadata.json not found: $metadata" }

Write-Host "Signing $Path ..."

& $signtool sign /v /fd SHA256 `
    /tr "http://timestamp.acs.microsoft.com" /td SHA256 `
    /dlib $dlib /dmdf $metadata `
    $Path

if ($LASTEXITCODE -ne 0) {
    throw "signtool sign failed with exit code $LASTEXITCODE"
}

Write-Host ""
Write-Host "Verifying signature..."
& $signtool verify /pa /v $Path

if ($LASTEXITCODE -ne 0) {
    throw "signtool verify failed with exit code $LASTEXITCODE"
}

Write-Host ""
Write-Host "OK - $Path is signed and verified."
