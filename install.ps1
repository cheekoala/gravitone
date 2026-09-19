<#
.SYNOPSIS
  Gravitone installer for Windows (PowerShell 5.1+ or PowerShell 7+).

.DESCRIPTION
  Installs into a private virtualenv under %LOCALAPPDATA%, puts `gravitone`
  and `grav` on your PATH, offers to install ffmpeg, and adds Start Menu and
  desktop shortcuts. Nothing needs an administrator prompt except the
  optional ffmpeg install, which is shown before it runs.

.EXAMPLE
  .\install.ps1
  .\install.ps1 -Yes -WithPlayer
  .\install.ps1 -Uninstall
  .\install.ps1 -Uninstall -Purge
#>
[CmdletBinding()]
param(
  [switch]$WithPlayer,
  [switch]$Uninstall,
  [switch]$Purge,
  [switch]$Shortcut,
  [switch]$NoShortcut,
  [switch]$Yes,
  [string]$Venv = "$env:LOCALAPPDATA\Gravitone\venv"
)

$ErrorActionPreference = 'Stop'
$src = Split-Path -Parent $MyInvocation.MyCommand.Path
$shims = "$env:LOCALAPPDATA\Microsoft\WindowsApps"
$startMenu = "$env:APPDATA\Microsoft\Windows\Start Menu\Programs\Gravitone.lnk"
$desktopLink = Join-Path ([Environment]::GetFolderPath('Desktop')) 'Gravitone.lnk'
$iconSource = Join-Path $src 'packaging\gravitone.ico'
$iconTarget = "$env:LOCALAPPDATA\Gravitone\gravitone.ico"

function Step($text) { Write-Host "==> $text" -ForegroundColor Green }
function Warn($text) { Write-Host "!!  $text" -ForegroundColor Yellow }
function Note($text) { Write-Host "    $text" -ForegroundColor DarkGray }

# Take the default without asking when nobody is there to answer.
function Confirm-Step($question, $default) {
  if ($Yes -or -not [Environment]::UserInteractive) { return $default }
  $reply = Read-Host $question
  if ($reply -match '^[yY]') { return $true }
  if ($reply -match '^[nN]') { return $false }
  return $default
}

# -- uninstall ------------------------------------------------------------
if ($Uninstall) {
  Step 'Removing Gravitone'
  foreach ($path in @($Venv, $startMenu, $desktopLink, $iconTarget)) {
    if (Test-Path $path) { Remove-Item -Recurse -Force $path }
  }
  Get-ChildItem "$shims\gravitone.*", "$shims\grav.*" -ErrorAction SilentlyContinue |
    Remove-Item -Force
  $root = "$env:LOCALAPPDATA\Gravitone"
  if ((Test-Path $root) -and -not (Get-ChildItem $root -ErrorAction SilentlyContinue)) {
    Remove-Item -Force $root
  }
  if ($Purge) {
    $settings = Join-Path $env:APPDATA 'gravitone'
    if (Test-Path $settings) { Remove-Item -Recurse -Force $settings }
    Write-Host 'Removed, settings and all. Your music was never touched.'
  } else {
    Write-Host 'Removed. Your library and settings were left alone.'
    Note 'Add -Purge to remove the settings as well.'
  }
  return
}

# -- python ---------------------------------------------------------------
# Windows ships a decoy: a zero-byte python.exe in WindowsApps that opens the
# Microsoft Store instead of running anything. It has to be skipped by name,
# because asking it for its version opens a shop.
function Test-StoreStub($path) {
  if (-not $path) { return $false }
  if ($path -notlike "*\WindowsApps\*") { return $false }
  try { return ((Get-Item $path).Length -eq 0) } catch { return $true }
}

function Find-Python {
  $tries = @(
    @{ Exe = 'py';      Args = @('-3') },
    @{ Exe = 'python';  Args = @() },
    @{ Exe = 'python3'; Args = @() }
  )
  foreach ($try in $tries) {
    $found = Get-Command $try.Exe -ErrorAction SilentlyContinue
    if (-not $found) { continue }
    if (Test-StoreStub $found.Source) { continue }
    $check = $try.Args + @('-c', 'import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)')
    try { & $found.Source @check 2>$null } catch { continue }
    if ($LASTEXITCODE -eq 0) { return @{ Exe = $found.Source; Args = $try.Args } }
  }
  return $null
}

$python = Find-Python
if (-not $python) {
  Warn 'Python 3.9 or newer was not found.'
  Write-Host ''
  Write-Host '  winget install -e --id Python.Python.3.12'
  Write-Host ''
  Write-Host 'or from https://www.python.org/downloads/ - tick "Add python.exe to PATH".'
  Note 'If you have "python" but this still fails, it is probably the Microsoft'
  Note 'Store placeholder. Settings > Apps > Advanced > App execution aliases,'
  Note 'and turn the python entries off.'
  exit 1
}
$version = (& $python.Exe @($python.Args + @('-c', 'import platform; print(platform.python_version())'))).Trim()
Step "Using $($python.Exe) ($version)"

# -- what is already here -------------------------------------------------
$existing = Join-Path $Venv 'Scripts\gravitone.exe'
$before = $null
if (Test-Path $existing) {
  try { $before = (& $existing --version 2>$null) } catch { $before = $null }
}
$want = (Select-String -Path (Join-Path $src 'pyproject.toml') -Pattern '^version = "(.*)"' |
         Select-Object -First 1).Matches.Groups[1].Value
if ($before) {
  $had = ($before -split ' ')[-1]
  if ($had -eq $want) { Step "Reinstalling Gravitone $want" }
  else { Step "Upgrading Gravitone $had -> $want" }
}

# -- install --------------------------------------------------------------
Step "Installing into $Venv"
New-Item -ItemType Directory -Force -Path (Split-Path -Parent $Venv) | Out-Null
try {
  & $python.Exe @($python.Args + @('-m', 'venv', $Venv))
  if ($LASTEXITCODE -ne 0) { throw "python -m venv failed ($LASTEXITCODE)" }
} catch {
  Warn "Could not create the virtualenv: $_"
  Note "Try deleting $Venv and running this again."
  exit 1
}

$venvPython = Join-Path $Venv 'Scripts\python.exe'
& $venvPython -m pip install --quiet --upgrade pip
if ($LASTEXITCODE -ne 0) { Warn 'Could not update pip; carrying on with the one it has.' }
& $venvPython -m pip install --quiet $src
if ($LASTEXITCODE -ne 0) {
  Warn 'pip could not install Gravitone.'
  Note "Run this to see why:  & '$venvPython' -m pip install '$src'"
  exit 1
}

# -- on PATH --------------------------------------------------------------
# WindowsApps is on everybody's PATH already, so a .cmd shim there is the
# least surprising way in - no reboot, no profile editing.
New-Item -ItemType Directory -Force -Path $shims | Out-Null
$madeShims = @()
foreach ($name in @('gravitone', 'grav')) {
  $target = Join-Path $Venv "Scripts\$name.exe"
  if (-not (Test-Path $target)) { continue }
  $shim = Join-Path $shims "$name.cmd"
  "@echo off`r`n`"$target`" %*" | Set-Content -Encoding ASCII $shim
  $madeShims += $shim
}
if ($madeShims) { Step "Added $($madeShims.Count) command(s) to $shims" }

if (($env:Path -split ';') -notcontains $shims) {
  Warn "$shims is not on this session's PATH."
  $userPath = [Environment]::GetEnvironmentVariable('Path', 'User')
  if (($userPath -split ';') -notcontains $shims) {
    [Environment]::SetEnvironmentVariable('Path', "$userPath;$shims", 'User')
    Step 'Added it to your PATH - open a new terminal for it to take'
  }
}

# -- did it actually work? ------------------------------------------------
$installed = $null
try { $installed = (& (Join-Path $Venv 'Scripts\gravitone.exe') --version 2>$null) } catch { }
if (-not $installed) {
  Warn "Installed, but 'gravitone --version' did not run. Something is wrong with $Venv."
  exit 1
}
Step "Installed $installed"

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
    Warn 'No audio player found. Gravitone needs ffmpeg, mpv or VLC on PATH.'
    Note 'Install winget or download ffmpeg from https://ffmpeg.org/download.html'
  } else {
    Write-Host ''
    Write-Host 'No audio player found. Gravitone plays through ffmpeg, mpv or VLC.'
    Note $command
    if ($WithPlayer -or (Confirm-Step 'Run it now? [y/N]' $false)) {
      Step "Installing a player: $command"
      try { Invoke-Expression $command } catch { Warn "That did not work: $_" }
    } else {
      Write-Host 'Skipped - install one yourself later, then run: gravitone doctor'
    }
  }
}

# -- shortcuts ------------------------------------------------------------
function Add-Shortcuts {
  if (Test-Path $iconSource) {
    New-Item -ItemType Directory -Force -Path (Split-Path -Parent $iconTarget) | Out-Null
    Copy-Item -Force $iconSource $iconTarget
  }
  $shell = New-Object -ComObject WScript.Shell
  foreach ($target in @($startMenu, $desktopLink)) {
    $link = $shell.CreateShortcut($target)
    # pythonw, so double-clicking it does not leave a console window behind.
    $link.TargetPath = Join-Path $Venv 'Scripts\pythonw.exe'
    $link.Arguments = '-m gravitone ui'
    $link.WorkingDirectory = $Venv
    $link.Description = 'Gravitone - your own soundtrack for any game'
    if (Test-Path $iconTarget) { $link.IconLocation = $iconTarget }
    $link.Save()
  }
  Step 'Added Start Menu and desktop shortcuts'
}

$wantShortcuts = $true
if ($NoShortcut) { $wantShortcuts = $false }
elseif (-not $Shortcut) {
  $wantShortcuts = Confirm-Step 'Add Start Menu and desktop shortcuts? [Y/n]' $true
}
if ($wantShortcuts) {
  try { Add-Shortcuts } catch { Warn "Could not create the shortcuts: $_" }
}

# -- a last look over the whole thing -------------------------------------
Write-Host ''
Step 'Checking the setup'
try {
  & (Join-Path $Venv 'Scripts\gravitone.exe') doctor 2>$null |
    Select-String -Pattern '^players found|^tags & lengths|^file chooser' |
    ForEach-Object { Note $_.Line }
} catch { }

Write-Host ''
Note 'Windows only makes symlinks when Developer Mode is on (Settings > Privacy'
Note '& security > For developers). Without it Gravitone uses hard links, which'
Note 'also cost no extra space but cannot cross drives - or add a folder whole,'
Note 'which links nothing at all.'

Write-Host ''
Step 'Done'
Write-Host '  gravitone ui                       open the control panel'
Write-Host '  gravitone ui --stop                stop it again'
Write-Host '  gravitone link C:\Users\you\Music  add music (links, no copies)'
Write-Host '  gravitone play                     play from the terminal'
Write-Host ''
Note 'If "gravitone" is not found, open a new terminal first.'

if (Confirm-Step 'Open the control panel now? [Y/n]' $true) {
  Start-Process -FilePath (Join-Path $Venv 'Scripts\pythonw.exe') `
                -ArgumentList '-m', 'gravitone', 'ui' -WorkingDirectory $Venv
  Write-Host 'Started.'
}
