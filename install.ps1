<#
.SYNOPSIS
  bgst installer for Windows (PowerShell 5.1+ / PowerShell 7+).

.EXAMPLE
  .\install.ps1
  .\install.ps1 -WithPlayer
  .\install.ps1 -Uninstall

  Nothing here needs an administrator prompt except an optional ffmpeg
  install through winget/choco/scoop, which is shown before it runs.
#>
[CmdletBinding()]
param(
  [switch]$WithPlayer,
  [switch]$Uninstall,
  [switch]$Shortcut,
  [switch]$NoShortcut,
  [string]$Venv = "$env:LOCALAPPDATA\bgst\venv"
)

$ErrorActionPreference = 'Stop'
$src = Split-Path -Parent $MyInvocation.MyCommand.Path
$shims = "$env:LOCALAPPDATA\Microsoft\WindowsApps"
$startMenu = "$env:APPDATA\Microsoft\Windows\Start Menu\Programs\bgst.lnk"

function Step($text) { Write-Host "==> $text" -ForegroundColor Green }
function Warn($text) { Write-Host "!!  $text" -ForegroundColor Yellow }

if ($Uninstall) {
  Step 'Removing bgst'
  if (Test-Path $Venv) { Remove-Item -Recurse -Force $Venv }
  if (Test-Path $startMenu) { Remove-Item -Force $startMenu }
  $desktopLink = Join-Path ([Environment]::GetFolderPath('Desktop')) 'bgst.lnk'
  if (Test-Path $desktopLink) { Remove-Item -Force $desktopLink }
  Get-ChildItem "$shims\bgst.*" -ErrorAction SilentlyContinue | Remove-Item -Force
  Write-Host 'Removed. Your library and config were left alone.'
  return
}

# -- python ---------------------------------------------------------------
function Find-Python {
  foreach ($candidate in @('py -3', 'python', 'python3')) {
    $parts = $candidate.Split(' ')
    $exe = Get-Command $parts[0] -ErrorAction SilentlyContinue
    if (-not $exe) { continue }
    $arguments = @()
    if ($parts.Count -gt 1) { $arguments += $parts[1] }
    $arguments += @('-c', 'import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)')
    & $exe.Source @arguments 2>$null
    if ($LASTEXITCODE -eq 0) { return @{ Exe = $exe.Source; Args = $(if ($parts.Count -gt 1) { @($parts[1]) } else { @() }) } }
  }
  return $null
}

$python = Find-Python
if (-not $python) {
  Warn 'Python 3.9+ was not found.'
  Write-Host 'Install it with:  winget install -e --id Python.Python.3.12'
  Write-Host 'or from https://www.python.org/downloads/ (tick "Add python.exe to PATH").'
  exit 1
}

Step "Creating a virtual environment in $Venv"
& $python.Exe @($python.Args + @('-m', 'venv', $Venv))
$venvPython = Join-Path $Venv 'Scripts\python.exe'
& $venvPython -m pip install --quiet --upgrade pip
Step 'Installing bgst'
& $venvPython -m pip install --quiet $src

# -- shim on PATH ---------------------------------------------------------
$venvBgst = Join-Path $Venv 'Scripts\bgst.exe'
New-Item -ItemType Directory -Force -Path $shims | Out-Null
$shim = Join-Path $shims 'bgst.cmd'
"@echo off`r`n`"$venvBgst`" %*" | Set-Content -Encoding ASCII $shim
Step "Added $shim"

# -- audio player ---------------------------------------------------------
$havePlayer = @('ffplay', 'mpv', 'vlc') | Where-Object { Get-Command $_ -ErrorAction SilentlyContinue }
if ($havePlayer) {
  Step "Audio player found: $($havePlayer -join ', ')"
} else {
  $command = $null
  if (Get-Command winget -ErrorAction SilentlyContinue) { $command = 'winget install -e --id Gyan.FFmpeg' }
  elseif (Get-Command choco -ErrorAction SilentlyContinue) { $command = 'choco install -y ffmpeg' }
  elseif (Get-Command scoop -ErrorAction SilentlyContinue) { $command = 'scoop install ffmpeg' }

  if (-not $command) {
    Warn 'No audio player found. Install ffmpeg, mpv or VLC and make sure it is on PATH.'
  } elseif ($WithPlayer) {
    Step "Installing a player: $command"
    Invoke-Expression $command
  } else {
    Write-Host ''
    Write-Host 'No audio player found. bgst needs ffmpeg, mpv or VLC.'
    $reply = Read-Host "Run '$command' now? [y/N]"
    if ($reply -match '^[yY]') { Invoke-Expression $command } else { Write-Host 'Skipped.' }
  }
}

# -- shortcuts ------------------------------------------------------------
function Add-Shortcuts {
  $shell = New-Object -ComObject WScript.Shell
  foreach ($target in @($startMenu, (Join-Path ([Environment]::GetFolderPath('Desktop')) 'bgst.lnk'))) {
    $link = $shell.CreateShortcut($target)
    $link.TargetPath = Join-Path $Venv 'Scripts\pythonw.exe'
    $link.Arguments = '-m bgsoundtrack ui'
    $link.Description = 'bgst - custom game soundtrack player'
    $link.Save()
  }
  Step 'Added Start Menu and desktop shortcuts'
}

$wantShortcuts = $true
if ($NoShortcut) { $wantShortcuts = $false }
elseif (-not $Shortcut) {
  $reply = Read-Host 'Add Start Menu and desktop shortcuts? [Y/n]'
  if ($reply -match '^[nN]') { $wantShortcuts = $false }
}
if ($wantShortcuts) {
  try { Add-Shortcuts } catch { Warn "Could not create the shortcuts: $_" }
}

# -- symlink note ---------------------------------------------------------
Write-Host ''
Write-Host 'Note: Windows only allows symlinks when Developer Mode is on'
Write-Host '(Settings > System > For developers). Without it bgst falls back to'
Write-Host 'hard links, which also use no extra space but cannot cross drives.'

Write-Host ''
Step 'Done'
Write-Host '  bgst ui                      open the control panel'
Write-Host '  bgst link C:\Users\you\Music add music (links, no copies)'
Write-Host '  bgst play                    play from the terminal'
