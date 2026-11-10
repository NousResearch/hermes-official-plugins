<#
.SYNOPSIS
    Patches Hermes Accent Picker so the chosen accent persists across restarts.
.DESCRIPTION
    Downloads the patched plugin.js (from Icarus-B4/hermes-official-plugins,
    branch accent-picker-persist-v2, commit fcc172c) into desktop-plugins/accent-picker/
    and points the plugin catalog at that fork/sha so reinstalls keep the patch.
    Safe to run multiple times (idempotent). Run after a Hermes update.
#>

$ErrorActionPreference = "Stop"

$PluginDir   = "$env:LOCALAPPDATA\hermes\desktop-plugins\accent-picker"
$PluginFile  = "$PluginDir\plugin.js"
$CatalogFile = "$env:LOCALAPPDATA\hermes\hermes-agent\plugin-catalog\accent-picker.yaml"
$CacheFile   = "$env:LOCALAPPDATA\hermes\cache\plugin-catalog.json"

$RawUrl = "https://raw.githubusercontent.com/Icarus-B4/hermes-official-plugins/accent-picker-persist-v2/accent-picker/patch/plugin.js"
$Sha    = "2948300b075e78585287a80891b796d2ca8e4d2b"

# 1. Ensure plugin dir exists
if (-not (Test-Path $PluginDir)) {
    New-Item -ItemType Directory -Path $PluginDir -Force | Out-Null
}

# 2. Download patched plugin.js
Write-Host "Downloading patched plugin.js ..." -ForegroundColor Cyan
Invoke-WebRequest -Uri $RawUrl -OutFile $PluginFile -UseBasicParsing
if (-not (Test-Path $PluginFile)) {
    Write-Error "Download failed."
    exit 1
}
Write-Host "plugin.js installed." -ForegroundColor Green

# 3. Patch plugin catalog (repo + sha)
if (Test-Path $CatalogFile) {
    $c = Get-Content -Raw -Path $CatalogFile
    $c = $c -replace 'repo: https://github.com/NousResearch/hermes-official-plugins', 'repo: https://github.com/Icarus-B4/hermes-official-plugins'
    $c = $c -replace 'sha: [0-9a-f]+', "sha: $Sha"
    $c = $c -replace 'abec9e822a23089943dd1d476a104bf746f19f86', $Sha
    $c = $c -replace 'da83ba2f2be65f348e0c5d3616b1d639c50ef1dd', $Sha
    $c = $c -replace 'df8dedfd63e8f07cdb372220faed156ac7511db1', $Sha
    $c = $c -replace '486b93451ec390ec8d8753ce246e5d048c525d58', $Sha
    $c = $c -replace "docs_url: https://github.com/NousResearch/hermes-official-plugins/tree/main/accent-picker", "docs_url: https://github.com/Icarus-B4/hermes-official-plugins/tree/main/accent-picker"
    Set-Content -Path $CatalogFile -Value $c -NoNewline
    Write-Host "Catalog patched." -ForegroundColor Green
}

# 4. Patch cache (best effort)
if (Test-Path $CacheFile) {
    try {
        $json = Get-Content -Raw -Path $CacheFile | ConvertFrom-Json
        foreach ($e in $json.entries) {
            if ($e.name -eq 'accent-picker') {
                $e.repo = "https://github.com/Icarus-B4/hermes-official-plugins"
                $e.sha  = $Sha
                if ($e.image) { $e.image = $e.image -replace 'abec9e822a23089943dd1d476a104bf746f19f86', $Sha }
            }
        }
        $json | ConvertTo-Json -Depth 10 | Set-Content -Path $CacheFile
        Write-Host "Cache patched." -ForegroundColor Green
    } catch {
        Write-Warning "Cache patch failed (non-critical): $_"
    }
}

Write-Host ""
Write-Host "Accent Picker: patched successfully." -ForegroundColor Green
Write-Host "Restart Hermes Desktop (or Reload desktop plugins) to apply." -ForegroundColor Cyan
