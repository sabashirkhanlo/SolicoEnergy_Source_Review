from __future__ import annotations
import os, subprocess, sys
from pathlib import Path


def _task_command(mode:str):
    """Return a quoted Windows command that survives future in-place updates."""
    app_root=os.environ.get('SOLICO_APP_ROOT')
    if app_root:
        exe=Path(app_root)/'SolicoEnergy.exe'
        return f'"{exe}" --scheduler --mode {mode}'
    if getattr(sys,'frozen',False):
        exe=Path(sys.executable).resolve()
        return f'"{exe}" --scheduler --mode {mode}'
    # Development/source mode.
    root=Path(__file__).resolve().parents[4]
    launcher=root/'launcher.py'
    pyw=Path(sys.executable).resolve()
    return f'"{pyw}" "{launcher}" --scheduler --mode {mode}'


def _create(name, trigger_args, mode):
    clock=trigger_args[trigger_args.index('/ST')+1] if '/ST' in trigger_args else None
    if _matches(name,mode,clock):return
    tr=_task_command(mode)
    cmd=['schtasks','/Create','/F','/TN',name,'/TR',tr,*trigger_args]
    cp=subprocess.run(cmd,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True,
                      creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
    if cp.returncode!=0:
        raise RuntimeError(cp.stderr.strip() or cp.stdout.strip() or f'خطا در ساخت Task {name}')


def _delete(name):
    subprocess.run(['schtasks','/Delete','/F','/TN',name],stdout=subprocess.PIPE,stderr=subprocess.PIPE,
                   creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))

def sync_tasks(daily_time='08:00', monthly_time='00:10', daily_enabled=True):
    if os.name!='nt': return False
    if daily_enabled:
        # First attempt at 08:00 for the previous calendar day; retries are harmless because generated_reports
        # prevents duplicate reports when a manual or previous automatic report exists.
        _create('SolicoEnergy_Daily',['/SC','DAILY','/ST','08:00'],'daily')
        _create('SolicoEnergy_DailyRetry1',['/SC','DAILY','/ST','08:10'],'daily')
        _create('SolicoEnergy_DailyRetry2',['/SC','DAILY','/ST','08:30'],'daily')
    else:
        for n in ('SolicoEnergy_Daily','SolicoEnergy_DailyRetry1','SolicoEnergy_DailyRetry2'):_delete(n)
    _create('SolicoEnergy_Monthly',['/SC','DAILY','/ST',monthly_time],'monthly')
    # Repair the existing login task's old executable path without recreating its trigger.
    if _matches('SolicoEnergy_Catchup','catchup'):return True
    cp=subprocess.run(['schtasks','/Change','/TN','SolicoEnergy_Catchup','/TR',_task_command('catchup')],
                      stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True,
                      creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
    if cp.returncode!=0:
        _create('SolicoEnergy_Catchup',['/SC','ONLOGON'],'catchup')
    return True


def remove_tasks():
    if os.name!='nt': return
    for name in ('SolicoEnergy_Daily','SolicoEnergy_DailyRetry1','SolicoEnergy_DailyRetry2','SolicoEnergy_Monthly','SolicoEnergy_Catchup'):
        subprocess.run(['schtasks','/Delete','/F','/TN',name],stdout=subprocess.PIPE,stderr=subprocess.PIPE,
                       creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))


def _matches(name, mode, clock=None):
    """Read-only verification avoids rewriting protected tasks on every launch."""
    import xml.etree.ElementTree as ET
    cp=subprocess.run(['schtasks','/Query','/TN',name,'/XML'],stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True,
                      creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
    if cp.returncode: return False
    try:
        root=ET.fromstring(cp.stdout)
        ns={'t':'http://schemas.microsoft.com/windows/2004/02/mit/task'}
        actions=root.findall('t:Actions/t:Exec',ns)
        if len(actions)!=1:return False
        command=actions[0].findtext('t:Command',default='',namespaces=ns)
        args=actions[0].findtext('t:Arguments',default='',namespaces=ns)
        actual=(command.strip('"')+' '+args).replace('"','').strip()
        if actual!=_task_command(mode).replace('"','').strip():return False
        if root.findtext('t:Settings/t:Enabled',default='true',namespaces=ns)=='false':return False
        triggers=root.findall('t:Triggers/*',ns)
        if len(triggers)!=1:return False
        trigger=triggers[0]
        if trigger.findtext('t:Enabled',default='true',namespaces=ns)=='false':return False
        if clock is None:return trigger.tag.endswith('LogonTrigger')
        interval=trigger.findtext('t:ScheduleByDay/t:DaysInterval',default='',namespaces=ns)
        boundary=trigger.findtext('t:StartBoundary',default='',namespaces=ns)
        return interval=='1' and boundary[11:16]==clock
    except (ET.ParseError,ValueError):return False


def repair_with_admin():
    """Explicit user-clicked repair; UAC is handled by Windows, never bypassed."""
    import base64,json
    from . import settings
    if os.name!='nt':raise RuntimeError('این عملیات مخصوص ویندوز است.')
    cfg=settings.load()
    user=os.environ.get('USERDOMAIN','')+'\\'+os.environ.get('USERNAME','')
    specs=[]
    for name,mode,clock in [('SolicoEnergy_Daily','daily','08:00'),('SolicoEnergy_DailyRetry1','daily','08:10'),('SolicoEnergy_DailyRetry2','daily','08:30'),('SolicoEnergy_Monthly','monthly',cfg.get('monthly_time','00:10')),('SolicoEnergy_Catchup','catchup',None)]:
        specs.append(dict(name=name,command=_task_command(mode),exe=str(Path(os.environ.get('SOLICO_APP_ROOT', str(Path(sys.executable).parent)))/'SolicoEnergy.exe'),mode=mode,clock=clock,remove=(mode=='daily' and not cfg.get('auto_report_enabled',True))))
    errorlog=Path(os.environ['LOCALAPPDATA'])/'SolicoEnergy'/'logs'/'task_repair_error.log'
    errorlog.parent.mkdir(parents=True,exist_ok=True)
    if errorlog.exists(): errorlog.unlink()
    payload=base64.b64encode(json.dumps(dict(user=user,tasks=specs,errorlog=str(errorlog))).encode()).decode()
    script="""$ErrorActionPreference='Stop'
$c=[Text.Encoding]::UTF8.GetString([Convert]::FromBase64String('PAYLOAD'))|ConvertFrom-Json
try {
 foreach($t in $c.tasks){
  if($t.remove){
   Unregister-ScheduledTask -TaskName $t.name -Confirm:$false -ErrorAction SilentlyContinue
   continue
  }
  $action=New-ScheduledTaskAction -Execute $t.exe -Argument ("--scheduler --mode " + $t.mode) -WorkingDirectory (Split-Path $t.exe)
  if($t.clock){$trigger=New-ScheduledTaskTrigger -Daily -At ([datetime]::ParseExact($t.clock,'HH:mm',[Globalization.CultureInfo]::InvariantCulture))}
  else{$trigger=New-ScheduledTaskTrigger -AtLogOn -User $c.user}
  $principal=New-ScheduledTaskPrincipal -UserId $c.user -LogonType Interactive -RunLevel Limited
  $options=New-ScheduledTaskSettingsSet -StartWhenAvailable -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
  Register-ScheduledTask -TaskName $t.name -Action $action -Trigger $trigger -Principal $principal -Settings $options -Force | Out-Null
 }
} catch {
 $_.Exception.ToString() | Out-File -LiteralPath $c.errorlog -Encoding utf8
 exit 1
}
exit 0
""".replace('PAYLOAD',payload)
    encoded=base64.b64encode(script.encode('utf-16le')).decode()
    wrapper="$ErrorActionPreference='Stop'; $p=Start-Process powershell.exe -Verb RunAs -Wait -PassThru -ArgumentList '-NoProfile -NonInteractive -EncodedCommand "+encoded+"'; exit $p.ExitCode"
    cp=subprocess.run(['powershell.exe','-NoProfile','-NonInteractive','-Command',wrapper],stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True,
                      creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
    if cp.returncode:
        detail=errorlog.read_text(encoding='utf-8-sig',errors='replace') if errorlog.exists() else (cp.stderr or cp.stdout)
        raise RuntimeError('ثبت زمان‌بندی انجام نشد: '+detail[-1800:])
    return True
