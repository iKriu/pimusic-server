#requires -Version 7
<#
  Windows-ПК: сборка нативного клиента PiMusic (WinUI 3) → apps/dist/PiMusic-<v>-setup.exe (+ .sha256)
  и портативный PiMusic-<v>-win-x64.zip. Нужны .NET SDK 10 и Inno Setup 6 (winget install JRSoftware.InnoSetup).
    pwsh apps/build/build-windows-native.ps1 [-Version 1.1.1] [-NoInstaller] [-Project <папка проекта>] [-OutDir <папка>]
  Подписи кода нет (SmartScreen покажет предупреждение). Выкладка на сервер — apps/build/publish-updates.sh windows-native.
#>
param(
  [string]$Version,
  [switch]$NoInstaller,
  [string]$Project,
  [string]$OutDir
)
$ErrorActionPreference = 'Stop'
$root = Resolve-Path (Join-Path $PSScriptRoot '..\..')
if (-not $Project) { $Project = Join-Path $root 'apps\windows-native\PiMusic' }
$Project = (Resolve-Path $Project).Path
$iss = Join-Path (Split-Path $Project) 'installer\PiMusic.iss'
if (-not $OutDir) { $OutDir = Join-Path $root 'apps\dist' }
New-Item -ItemType Directory -Force $OutDir | Out-Null
$OutDir = (Resolve-Path $OutDir).Path

if (-not $Version) {
  $Version = ([xml](Get-Content (Join-Path $Project 'PiMusic.csproj') -Raw)).Project.PropertyGroup.Version | Where-Object { $_ } | Select-Object -First 1
}
if ($Version -notmatch '^\d+\.\d+\.\d+$') { throw "Версия должна быть вида 1.1.1, получено: '$Version'" }

$dotnet = (Get-Command dotnet -ErrorAction SilentlyContinue)?.Source
if (-not $dotnet) { $dotnet = 'C:\Program Files\dotnet\dotnet.exe' }
if (-not (Test-Path $dotnet)) { throw 'dotnet не найден — поставьте .NET SDK 10' }

# Каталог публикации — во временной папке (bin/obj проекта не трогаем)
$pub = Join-Path ([IO.Path]::GetTempPath()) "pimusic-publish-$Version"
if (Test-Path $pub) { Remove-Item $pub -Recurse -Force }
$log = Join-Path ([IO.Path]::GetTempPath()) "pimusic-build-$Version.log"

function Invoke-Step([string]$Title, [scriptblock]$Cmd) {
  Write-Host ">> $Title"
  & $Cmd *> $log
  if ($LASTEXITCODE -ne 0) {
    Write-Host "!! $Title — ошибка (код $LASTEXITCODE), хвост лога ($log):" -ForegroundColor Red
    Get-Content $log -Tail 25
    exit 1
  }
}

Invoke-Step "dotnet publish $Version" {
  & $dotnet publish $Project -c Release -r win-x64 --self-contained `
    "-p:Version=$Version" -p:PublishTrimmed=false -p:PublishReadyToRun=true -o $pub
}

# без своего .pri (скомпилированная разметка XAML) приложение падает при старте — не выпускать такую сборку
if (-not (Test-Path (Join-Path $pub 'PiMusic.pri'))) { throw "В публикации нет PiMusic.pri ($pub) — проверьте EnableMsixTooling в PiMusic.csproj" }

$zip = Join-Path $OutDir "PiMusic-$Version-win-x64.zip"
if (Test-Path $zip) { Remove-Item $zip -Force }
Write-Host '>> zip'
Compress-Archive -Path (Join-Path $pub '*') -DestinationPath $zip -CompressionLevel Optimal
$artifacts = @($zip)

if (-not $NoInstaller) {
  $iscc = @(
    (Get-Command ISCC.exe -ErrorAction SilentlyContinue)?.Source,
    "$env:LOCALAPPDATA\Programs\Inno Setup 6\ISCC.exe",
    "${env:ProgramFiles(x86)}\Inno Setup 6\ISCC.exe",
    "$env:ProgramFiles\Inno Setup 6\ISCC.exe"
  ) | Where-Object { $_ -and (Test-Path $_) } | Select-Object -First 1
  if (-not $iscc) { throw 'Inno Setup 6 не найден: winget install --id JRSoftware.InnoSetup -e (или ключ -NoInstaller)' }
  Invoke-Step 'Inno Setup' {
    & $iscc /Q "/DAppVersion=$Version" "/DSourceDir=$pub" "/DOutDir=$OutDir" $iss
  }
  $setup = Join-Path $OutDir "PiMusic-$Version-setup.exe"
  $hash = (Get-FileHash $setup -Algorithm SHA256).Hash.ToLower()
  # формат как у соседних .sha256 (sha256sum): «<хеш>  <имя>», LF
  [IO.File]::WriteAllText("$setup.sha256", "$hash  $(Split-Path $setup -Leaf)`n", [Text.UTF8Encoding]::new($false))
  $artifacts += $setup, "$setup.sha256"
}

Remove-Item $pub -Recurse -Force -ErrorAction SilentlyContinue
Write-Host "Готово ($Version):"
$artifacts | ForEach-Object { Get-Item $_ | ForEach-Object { '  {0,-45} {1,8:N1} МБ' -f $_.Name, ($_.Length / 1MB) } }
