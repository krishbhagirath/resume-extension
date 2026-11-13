# Downloads the Tectonic LaTeX compiler into .\bin (no admin rights needed).
$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
$bin = Join-Path $root 'bin'
New-Item -ItemType Directory -Force -Path $bin | Out-Null
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
$headers = @{ 'User-Agent' = 'resume-tailor-setup' }

Write-Host 'Finding the latest Tectonic release...'
$releases = Invoke-RestMethod -Uri 'https://api.github.com/repos/tectonic-typesetting/tectonic/releases?per_page=30' -Headers $headers
$asset = $null
foreach ($r in $releases) {
  if ($r.prerelease) { continue }
  $asset = $r.assets | Where-Object { $_.name -like '*x86_64-pc-windows-msvc.zip' } | Select-Object -First 1
  if ($asset) { break }
}
if (-not $asset) { throw 'Could not find a Windows build of Tectonic on GitHub.' }

Write-Host "Downloading $($asset.name)..."
$zip = Join-Path $env:TEMP $asset.name
Invoke-WebRequest -Uri $asset.browser_download_url -OutFile $zip -Headers $headers -UseBasicParsing
Expand-Archive -Path $zip -DestinationPath $bin -Force
Remove-Item $zip

$exe = Get-ChildItem -Path $bin -Recurse -Filter 'tectonic.exe' | Select-Object -First 1
if (-not $exe) { throw 'Download finished but tectonic.exe was not in it.' }
if ($exe.DirectoryName -ne $bin) { Move-Item $exe.FullName (Join-Path $bin 'tectonic.exe') -Force }
Write-Host 'Tectonic installed.'
