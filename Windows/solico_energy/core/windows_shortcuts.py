from __future__ import annotations
import os, subprocess, sys
from pathlib import Path


def _powershell(cmd: str):
    return subprocess.run(
        ['powershell','-NoProfile','-ExecutionPolicy','Bypass','-Command',cmd],
        capture_output=True, text=True, timeout=35
    )


def refresh_shortcuts(icon_path: Path):
    """Create/repair SolicoEnergy Desktop and Start Menu shortcuts.

    FIX24:
    - Uses a uniquely named external ICO so Windows does not reuse the stale icon cache.
    - Repairs all known old shortcut names and redirected/Public/OneDrive desktops.
    - Keeps the visible desktop label as ``SolicoEnergy``.
    """
    if os.name != 'nt':
        return

    local = Path(os.environ.get('LOCALAPPDATA',''))
    install_root = local / 'Programs' / 'SolicoEnergy'
    installed = install_root / 'SolicoEnergy.exe'
    current = Path(sys.executable)
    target = installed if installed.exists() else current
    if not target.exists():
        return

    # Prefer the cache-busting icon introduced by FIX23.
    candidates = []
    try:
        p = Path(icon_path)
        candidates += [p.parent / 'solico_power_icon_v3.ico', p]
    except Exception:
        pass
    candidates += [
        install_root / 'versions' / '1.0.16' / 'solico_energy' / 'assets' / 'solico_power_icon_v3.ico',
        install_root / 'versions' / '1.0.16' / 'solico_energy' / 'assets' / 'app_icon.ico',
    ]
    icon = next((p for p in candidates if p and p.exists()), target)
    work = target.parent

    def q(v):
        return str(v).replace("'", "''")

    ps = fr"""
$ErrorActionPreference='SilentlyContinue'
$w=New-Object -ComObject WScript.Shell
$target='{q(target)}'
$work='{q(work)}'
$icon='{q(icon)},0'

$desktops=@()
try{{$desktops += [Environment]::GetFolderPath('Desktop')}}catch{{}}
if($env:USERPROFILE){{$desktops += (Join-Path $env:USERPROFILE 'Desktop')}}
if($env:OneDrive){{$desktops += (Join-Path $env:OneDrive 'Desktop')}}
if($env:PUBLIC){{$desktops += (Join-Path $env:PUBLIC 'Desktop')}}
try{{
  $reg=(Get-ItemProperty 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Explorer\User Shell Folders' -Name Desktop -ErrorAction SilentlyContinue).Desktop
  if($reg){{$desktops += [Environment]::ExpandEnvironmentVariables($reg)}}
}}catch{{}}
$desktops=$desktops | Where-Object {{$_}} | Select-Object -Unique

$oldNames=@('SolicoEnergy.lnk','Solico Energy Report.lnk','Solico Energy.lnk','Kalleh Power.lnk')
foreach($d in $desktops){{
  try{{
    if(!(Test-Path $d)){{New-Item -ItemType Directory -Force -Path $d|Out-Null}}
    foreach($n in $oldNames){{
      $p=Join-Path $d $n
      if(Test-Path $p){{Remove-Item -LiteralPath $p -Force -ErrorAction SilentlyContinue}}
    }}
    $p=Join-Path $d 'SolicoEnergy.lnk'
    $s=$w.CreateShortcut($p)
    $s.TargetPath=$target
    $s.WorkingDirectory=$work
    $s.IconLocation=$icon
    $s.Description='SolicoEnergy - Power Plant Monitoring'
    $s.Save()
  }}catch{{}}
}}

$menus=@()
try{{$menus += [Environment]::GetFolderPath('Programs')}}catch{{}}
try{{$menus += [Environment]::GetFolderPath('CommonPrograms')}}catch{{}}
$menus=$menus|Where-Object {{$_}}|Select-Object -Unique
foreach($m in $menus){{
  try{{
    $folder=Join-Path $m 'SolicoEnergy'
    New-Item -ItemType Directory -Force -Path $folder|Out-Null
    foreach($n in $oldNames){{
      $p=Join-Path $folder $n
      if(Test-Path $p){{Remove-Item -LiteralPath $p -Force -ErrorAction SilentlyContinue}}
    }}
    $p=Join-Path $folder 'SolicoEnergy.lnk'
    $s=$w.CreateShortcut($p)
    $s.TargetPath=$target
    $s.WorkingDirectory=$work
    $s.IconLocation=$icon
    $s.Description='SolicoEnergy - Power Plant Monitoring'
    $s.Save()
  }}catch{{}}
}}

# Ask Explorer to re-read icons. Because the icon filename changed, no destructive
# cache reset or Explorer restart should be necessary.
try{{Start-Process "$env:SystemRoot\System32\ie4uinit.exe" -ArgumentList '-show' -WindowStyle Hidden -ErrorAction SilentlyContinue}}catch{{}}
"""
    _powershell(ps)
