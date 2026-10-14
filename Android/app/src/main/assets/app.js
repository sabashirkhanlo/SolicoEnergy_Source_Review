const $=s=>document.querySelector(s);
function toast(msg){const t=$('#toast');t.textContent=msg;t.classList.remove('hidden');setTimeout(()=>t.classList.add('hidden'),3000)}
function parseJson(raw,fallback){try{return raw?JSON.parse(String(raw)):fallback}catch(e){return fallback}}
function bridge(name,...args){try{return AndroidBridge[name](...args)}catch(e){return null}}
function esc(v){return String(v??'').replace(/[&<>"']/g,m=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[m]))}
function show(v){return v!==null&&v!==undefined&&String(v).trim()?String(v):'—'}
function setGeneratorStatus(selector,row,prefix=''){
  const el=$(selector);if(!el)return;
  const text=String((row&&row.generator_status)||'').trim();
  if(!text){el.textContent='';el.classList.add('hidden');return}
  el.textContent=prefix?`${prefix}${text}`:text;
  el.classList.remove('hidden');
}
function generatorUnion(rows){
  const out=[];
  (rows||[]).forEach(r=>String(r.generator_active||'').split('+').forEach(x=>{
    const v=x.trim();if(v&&v!=='—'&&v!=='نامشخص'&&!out.includes(v))out.push(v);
  }));
  return out;
}
function latinDigits(v){return String(v??'').replace(/[۰-۹]/g,d=>'۰۱۲۳۴۵۶۷۸۹'.indexOf(d)).replace(/[٠-٩]/g,d=>'٠١٢٣٤٥٦٧٨٩'.indexOf(d))}

function refreshLatestReport(){
  const r=parseJson(bridge('getLatestReportJson'),null);
  if(!r){
    $('#latestReportTitle').textContent='هنوز گزارشی دریافت نشده است.';
    ['#kpiFactory','#kpiPlant','#kpiGrid','#kpiShare','#kpiPeak','#kpiAvg'].forEach(x=>$(x).textContent='—');
    setGeneratorStatus('#generatorStatus',null);
    return;
  }
  // report_title already contains the report date when Windows sends it.
  // Do not append report_date again, otherwise the same date is shown twice.
  $('#latestReportTitle').textContent=(r.report_title||r.title||'گزارش جدید نیروگاه');
  setGeneratorStatus('#generatorStatus',r);
  $('#kpiFactory').textContent=show(r.kpi_factory);
  $('#kpiPlant').textContent=show(r.kpi_plant);
  $('#kpiGrid').textContent=show(r.kpi_grid);
  $('#kpiShare').textContent=show(r.kpi_share);
  $('#kpiPeak').textContent=show(r.kpi_peak);
  $('#kpiAvg').textContent=show(r.kpi_avg);
}
window.refreshLatestReport=refreshLatestReport;

function isOfflineText(value){const s=String(value||'');return s.includes('آفلاین')||s.includes('قطع')||s.includes('ناموفق')||s.includes('خطا')}
function refreshStatus(){
  const fcm=$('#fcmStatus');
  const internet=$('#connectionStatus');
  if(fcm)fcm.textContent=bridge('getFcmStatus')||'نامشخص';
  if(internet){
    const connection=bridge('getConnectionStatus')||'نامشخص';
    internet.textContent=connection;
    internet.classList.toggle('status-offline',isOfflineText(connection));
  }
}
window.refreshFcmStatus=refreshStatus;
window.refreshDeviceStatus=refreshStatus;

function currentJalaliParts(){
  try{
    const f=new Intl.DateTimeFormat('fa-IR-u-ca-persian',{year:'numeric',month:'numeric',day:'numeric'});
    const p={};f.formatToParts(new Date()).forEach(x=>{if(x.type!=='literal')p[x.type]=Number(latinDigits(x.value))});
    if(p.year&&p.month&&p.day)return {y:p.year,m:p.month,d:p.day};
  }catch(e){}
  return {y:1405,m:6,d:1};
}
function updateToday(){
  try{$('#todayDate').textContent=new Intl.DateTimeFormat('fa-IR-u-ca-persian',{weekday:'long',year:'numeric',month:'long',day:'numeric'}).format(new Date())}
  catch(e){const p=currentJalaliParts();$('#todayDate').textContent=`${p.y}/${String(p.m).padStart(2,'0')}/${String(p.d).padStart(2,'0')}`}
}

function normalizeJalaliDate(value){
  const raw=latinDigits(value).trim();
  const digits=raw.replace(/\D/g,'').slice(0,8);
  if(digits.length===8)return `${digits.slice(0,4)}/${digits.slice(4,6)}/${digits.slice(6,8)}`;
  return raw.replace(/-/g,'/');
}
const JMONTHS=['فروردین','اردیبهشت','خرداد','تیر','مرداد','شهریور','مهر','آبان','آذر','دی','بهمن','اسفند'];
let calendarTarget=null,calYear=1405,calMonth=6,archiveMode='day',monthMetric='factory';
function jalaliLeap(y){const r=((y%33)+33)%33;return [1,5,9,13,17,22,26,30].includes(r)}
function jalaliMonthDays(y,m){if(m<=6)return 31;if(m<=11)return 30;return jalaliLeap(y)?30:29}
// Serial-day calculation is only used to align weekday columns in the Persian calendar.
// Using today's Persian date as the anchor avoids any Gregorian/Jalali conversion drift.
function jalaliSerial(y,m,d){
  let days=0;
  const base=1300;
  if(y>=base){for(let yy=base;yy<y;yy++)days+=jalaliLeap(yy)?366:365}
  else{for(let yy=y;yy<base;yy++)days-=jalaliLeap(yy)?366:365}
  for(let mm=1;mm<m;mm++)days+=jalaliMonthDays(y,mm);
  return days+(d-1);
}
function jalaliWeekdayIndex(y,m,d){
  // Calendar columns are Saturday..Friday => 0..6.
  const now=currentJalaliParts();
  const todayIndex=(new Date().getDay()+1)%7; // JS: Sun=0 ... Sat=6
  const delta=jalaliSerial(y,m,d)-jalaliSerial(now.y,now.m,now.d);
  return ((todayIndex+delta)%7+7)%7;
}
function parseJDate(v){const m=normalizeJalaliDate(v).match(/(\d{4})\/(\d{1,2})\/(\d{1,2})/);return m?{y:Number(m[1]),m:Number(m[2]),d:Number(m[3])}:null}
function calendarDefault(){const latest=parseJson(bridge('getLatestReportJson'),{});return parseJDate(latest.report_date||'')||currentJalaliParts()}
function setHistoryDateValue(value){
  const d=normalizeJalaliDate(value);
  $('#historyDate').dataset.value=d;
  $('#historyDate').textContent=d||'انتخاب تاریخ';
}
function openCalendar(){
  calendarTarget=$('#historyDate');
  const current=parseJDate(calendarTarget.dataset.value||'')||calendarDefault();
  calYear=current.y;calMonth=current.m;renderCalendar();$('#jalaliPicker').classList.remove('hidden');
}
function closeCalendar(){$('#jalaliPicker').classList.add('hidden')}
function renderCalendar(){
  $('#calMonthTitle').textContent=JMONTHS[calMonth-1];$('#calYearTitle').textContent=String(calYear);
  const count=jalaliMonthDays(calYear,calMonth),current=parseJDate(calendarTarget?calendarTarget.dataset.value:'');
  const firstOffset=jalaliWeekdayIndex(calYear,calMonth,1);
  let html='';
  for(let i=0;i<firstOffset;i++)html+='<span class="calendar-blank" aria-hidden="true"></span>';
  for(let d=1;d<=count;d++){
    const selected=current&&current.y===calYear&&current.m===calMonth&&current.d===d;
    html+=`<button type="button" class="calendar-day${selected?' selected':''}" data-day="${d}">${d}</button>`;
  }
  $('#calendarDays').innerHTML=html;
  document.querySelectorAll('.calendar-day').forEach(btn=>btn.onclick=()=>{
    const d=Number(btn.dataset.day),value=`${String(calYear).padStart(4,'0')}/${String(calMonth).padStart(2,'0')}/${String(d).padStart(2,'0')}`;
    setHistoryDateValue(value);closeCalendar();if(archiveMode==='day')renderArchiveDay(value);
  });
}
function moveCalendar(delta){calMonth+=delta;if(calMonth<1){calMonth=12;calYear--}if(calMonth>12){calMonth=1;calYear++}renderCalendar()}

function num(v){
  if(v===null||v===undefined||v==='')return null;
  const s=latinDigits(v).replace('٫','.').replace(/,/g,'');
  const m=s.match(/-?\d+(\.\d+)?/);return m?Number(m[0]):null;
}
function peakColorClass(v){
  const n=Number(v);if(!Number.isFinite(n))return 'bar-blue';
  if(n>7)return 'bar-red';if(n>5)return 'bar-orange';return 'bar-blue';
}
function chartBars(items,monthly=false){
  const nums=items.map(x=>Math.abs(Number(x.value))).filter(Number.isFinite);
  const globalMax=Math.max(...nums,1);
  const html=`<div class="bar-chart ${monthly?'monthly':''}" style="--bar-count:${Math.max(items.length,1)}">${items.map(x=>{
    const n=Number(x.value),valid=x.value!==null&&x.value!==undefined&&x.value!==''&&Number.isFinite(n);
    const scale=Number.isFinite(Number(x.scaleMax))&&Number(x.scaleMax)>0?Number(x.scaleMax):globalMax;
    const h=valid?Math.max(0,Math.min(100,Math.abs(n)/scale*100)):0;
    const color=x.colorClass||'bar-blue';
    return `<div class="bar-item" title="${esc(x.label)}: ${esc(x.display)}"><div class="bar-value">${esc(x.display)}</div><div class="bar-track"><div class="bar-fill ${color}" style="height:${h.toFixed(1)}%"></div></div><div class="bar-label">${esc(x.label)}</div></div>`;
  }).join('')}</div>`;
  return html;
}
function hideMonthDetails(){const s=$('#monthSummary');if(s)s.classList.add('hidden')}
function clearArchiveArea(){
  $('#archiveChart').innerHTML='';$('#archiveTitle').textContent='';setGeneratorStatus('#archiveGeneratorStatus',null);hideMonthDetails();
  $('#archiveEmpty').textContent='';$('#archiveEmpty').classList.add('hidden');
}
function showArchiveEmpty(message){$('#archiveChart').innerHTML='';$('#archiveTitle').textContent='';setGeneratorStatus('#archiveGeneratorStatus',null);hideMonthDetails();$('#archiveEmpty').textContent=message||'برای این بازه گزارشی ذخیره نشده است.';$('#archiveEmpty').classList.remove('hidden')}
function hideArchiveEmpty(){$('#archiveEmpty').classList.add('hidden')}
function pickDaily(rows){return rows.find(x=>x.report_kind==='daily')||rows.find(x=>x.kpi_factory||x.kpi_plant||x.kpi_grid||x.kpi_share||x.kpi_peak||x.kpi_avg)||rows[0]||null}
function renderArchiveDay(date){
  const d=normalizeJalaliDate(date);if(!d){clearArchiveArea();return}
  const rows=parseJson(bridge('getNotificationHistoryJson',d),[]),r=pickDaily(rows);
  if(!r){showArchiveEmpty('برای این تاریخ گزارشی ذخیره نشده است.');return}
  hideArchiveEmpty();$('#archiveTitle').textContent=`گزارش ${r.report_date||d}`;setGeneratorStatus('#archiveGeneratorStatus',r);
  const eFactory=num(r.kpi_factory_raw || r.kpi_factory),ePlant=num(r.kpi_plant_raw || r.kpi_plant),eGrid=num(r.kpi_grid_raw || r.kpi_grid);
  const energyScale=Math.max(...[eFactory,ePlant,eGrid].filter(Number.isFinite).map(Math.abs),1);
  const peak=num(r.kpi_peak_raw || r.kpi_peak),avg=num(r.kpi_avg_raw || r.kpi_avg),share=num(r.kpi_share_raw || r.kpi_share);
  const items=[
    {label:'مصرف کارخانه',value:eFactory,display:show(r.kpi_factory),scaleMax:energyScale,colorClass:'bar-blue'},
    {label:'تولید نیروگاه',value:ePlant,display:show(r.kpi_plant),scaleMax:energyScale,colorClass:'bar-blue'},
    {label:'دریافت از شبکه',value:eGrid,display:show(r.kpi_grid),scaleMax:energyScale,colorClass:'bar-blue'},
    {label:'سهم نیروگاه',value:share,display:show(r.kpi_share),scaleMax:100,colorClass:'bar-blue'},
    {label:'پیک مصرف کارخانه',value:peak,display:show(r.kpi_peak),scaleMax:9,colorClass:peakColorClass(peak)},
    {label:'متوسط توان مصرفی',value:avg,display:show(r.kpi_avg),scaleMax:9,colorClass:'bar-blue'}
  ];
  $('#archiveChart').innerHTML=chartBars(items,false);
}
function setupMonthSelects(){
  const ys=['<option value="">انتخاب سال</option>'];for(let y=1405;y<=1415;y++)ys.push(`<option value="${y}">${y}</option>`);
  $('#historyYear').innerHTML=ys.join('');$('#historyYear').value='';
  $('#historyMonth').innerHTML='<option value="">انتخاب ماه</option>'+JMONTHS.map((m,i)=>`<option value="${i+1}">${m}</option>`).join('');$('#historyMonth').value='';
}
const MONTH_METRICS={
  factory:{label:'مصرف کارخانه',key:'kpi_factory',raw:'kpi_factory_raw'},
  plant:{label:'تولید نیروگاه',key:'kpi_plant',raw:'kpi_plant_raw'},
  grid:{label:'دریافت از شبکه',key:'kpi_grid',raw:'kpi_grid_raw'},
  share:{label:'سهم نیروگاه',key:'kpi_share',raw:'kpi_share_raw'},
  peak:{label:'پیک مصرف کارخانه',key:'kpi_peak',raw:'kpi_peak_raw',scaleMax:9,peakColors:true},
  avg:{label:'متوسط توان مصرفی',key:'kpi_avg',raw:'kpi_avg_raw',scaleMax:9}
};
function fmtNumber(v,digits=0){
  if(v===null||v===undefined||!Number.isFinite(Number(v)))return '—';
  return Number(v).toLocaleString('en-US',{minimumFractionDigits:digits,maximumFractionDigits:digits});
}
function monthlySummary(sortedRows){
  let factory=0,plant=0,grid=0,hours=0;let haveFactory=false,havePlant=false,haveGrid=false,peak=null;
  sortedRows.forEach(([d,r])=>{
    const f=num(r.kpi_factory_raw??r.kpi_factory),p=num(r.kpi_plant_raw??r.kpi_plant),g=num(r.kpi_grid_raw??r.kpi_grid);
    const pk=num(r.kpi_peak_raw??r.kpi_peak),av=num(r.kpi_avg_raw??r.kpi_avg);
    if(Number.isFinite(f)){factory+=f;haveFactory=true;if(Number.isFinite(av)&&av>0&&f>=0)hours+=f/av}
    if(Number.isFinite(p)){plant+=p;havePlant=true}
    if(Number.isFinite(g)){grid+=g;haveGrid=true}
    if(Number.isFinite(pk))peak=peak===null?pk:Math.max(peak,pk);
  });
  const share=(havePlant&&haveGrid&&(plant+grid)>0)?plant/(plant+grid)*100:null;
  const avg=(haveFactory&&hours>0)?factory/hours:null;
  return {factory:haveFactory?factory:null,plant:havePlant?plant:null,grid:haveGrid?grid:null,share,peak,avg,hours};
}
function renderMonthDetails(y,m,sortedRows){
  const summary=monthlySummary(sortedRows);
  $('#monthTotalFactory').textContent=summary.factory===null?'—':`${fmtNumber(summary.factory,1)} MWh`;
  $('#monthTotalPlant').textContent=summary.plant===null?'—':`${fmtNumber(summary.plant,1)} MWh`;
  $('#monthTotalGrid').textContent=summary.grid===null?'—':`${fmtNumber(summary.grid,1)} MWh`;
  $('#monthTotalShare').textContent=summary.share===null?'—':`${fmtNumber(summary.share,1)} %`;
  $('#monthTotalPeak').textContent=summary.peak===null?'—':`${fmtNumber(summary.peak,2)} MW`;
  $('#monthTotalAvg').textContent=summary.avg===null?'—':`${fmtNumber(summary.avg,2)} MW`;
  const expected=jalaliMonthDays(y,m);
  const validDays=sortedRows.filter(([d,r])=>{
    const f=num(r.kpi_factory_raw??r.kpi_factory),p=num(r.kpi_plant_raw??r.kpi_plant),g=num(r.kpi_grid_raw??r.kpi_grid);
    return Number.isFinite(f)||Number.isFinite(p)||Number.isFinite(g);
  }).length;
  $('#monthCoverage').textContent=`داده موجود: ${validDays} روز از ${expected} روز`;
  $('#monthSummary').classList.remove('hidden');
}
function renderArchiveMonth(){
  const y=Number($('#historyYear').value),m=Number($('#historyMonth').value);if(!y||!m){clearArchiveArea();return;}
  const ym=`${String(y).padStart(4,'0')}/${String(m).padStart(2,'0')}`,rows=parseJson(bridge('getMonthlyHistoryJson',ym),[]),byDay=new Map();
  rows.forEach(r=>{const d=normalizeJalaliDate(r.report_date||'');if(!d.startsWith(ym+'/'))return;if(r.report_kind&&r.report_kind!=='daily')return;if(!byDay.has(d))byDay.set(d,r)});
  const sortedRows=[...byDay.entries()].sort((a,b)=>a[0].localeCompare(b[0]));
  const metric=MONTH_METRICS[monthMetric]||MONTH_METRICS.factory;
  if(!sortedRows.length){showArchiveEmpty(`برای ${JMONTHS[m-1]} ${y} داده‌ای ذخیره نشده است.`);return}
  const expectedDays=jalaliMonthDays(y,m);
  const items=Array.from({length:expectedDays},(_,i)=>{
    const day=i+1,d=`${ym}/${String(day).padStart(2,'0')}`,r=byDay.get(d)||null;
    const v=r?num(r[metric.raw]??r[metric.key]):null;
    return {label:String(day),value:v,display:r?show(r[metric.key]):'—',scaleMax:metric.key==='kpi_share'?100:metric.scaleMax,colorClass:metric.peakColors?peakColorClass(v):'bar-blue'};
  });
  hideArchiveEmpty();
  $('#archiveTitle').textContent=`${metric.label} — ${JMONTHS[m-1]} ${y}`;
  const gens=generatorUnion([...byDay.values()]);
  const genRow=gens.length?{generator_status:(gens.length===1?'ژنراتور فعال در این ماه: ':'ژنراتورهای فعال در این ماه: ')+gens.join(' + ')}:null;
  setGeneratorStatus('#archiveGeneratorStatus',genRow);
  renderMonthDetails(y,m,sortedRows);
  $('#archiveChart').innerHTML=chartBars(items,true);
}
function setMonthMetric(metric){
  monthMetric=MONTH_METRICS[metric]?metric:'factory';
  document.querySelectorAll('.metric-btn').forEach(b=>b.classList.toggle('active',b.dataset.metric===monthMetric));
  if(archiveMode==='month')renderArchiveMonth();
}
function setArchiveMode(mode){
  archiveMode=mode;const day=mode==='day';
  $('#archiveDayTab').classList.toggle('active',day);$('#archiveMonthTab').classList.toggle('active',!day);
  $('#archiveDayControls').classList.toggle('hidden',!day);$('#archiveMonthControls').classList.toggle('hidden',day);$('#monthMetricTabs').classList.toggle('hidden',day);
  if(day){
    hideMonthDetails();
    const selected=$('#historyDate').dataset.value||'';
    if(selected)renderArchiveDay(selected);else clearArchiveArea();
  }else{
    const y=$('#historyYear').value,m=$('#historyMonth').value;
    if(y&&m)renderArchiveMonth();else clearArchiveArea();
  }
}

$('#historyDate').onclick=openCalendar;
$('#calPrev').onclick=()=>moveCalendar(-1);$('#calNext').onclick=()=>moveCalendar(1);$('#calCancel').onclick=closeCalendar;
$('#calClear').onclick=()=>{setHistoryDateValue('');closeCalendar();clearArchiveArea()};
$('#jalaliPicker').onclick=e=>{if(e.target.id==='jalaliPicker')closeCalendar()};
$('#archiveDayTab').onclick=()=>setArchiveMode('day');$('#archiveMonthTab').onclick=()=>setArchiveMode('month');
$('#historyYear').onchange=renderArchiveMonth;$('#historyMonth').onchange=renderArchiveMonth;
document.querySelectorAll('.metric-btn').forEach(b=>b.onclick=()=>setMonthMetric(b.dataset.metric));

function openMenu(){$('#menuOverlay').classList.remove('hidden')}
function closeMenu(){$('#menuOverlay').classList.add('hidden')}
$('#menuBtn').onclick=openMenu;$('#menuClose').onclick=closeMenu;$('#menuOverlay').onclick=e=>{if(e.target.id==='menuOverlay')closeMenu()};
$('#repairFcmBtn').onclick=()=>{bridge('repairNotifications');toast('درخواست اتصال مجدد ارسال شد');closeMenu();setTimeout(refreshStatus,4500)};
$('#logoutBtn').onclick=()=>{closeMenu();bridge('logout')};

const appVersion=bridge('getAppVersion');if(appVersion)$('#appVersion').textContent=appVersion;
if($('#checkUpdateBtn'))$('#checkUpdateBtn').onclick=()=>{bridge('checkForUpdates');toast('در حال بررسی نسخه جدید...')};
$('#updateApkBtn').onclick=()=>{bridge('chooseUpdateApk');toast('فایل APK بروزرسانی را انتخاب کن')};

function init(){
  updateToday();refreshLatestReport();refreshStatus();setupMonthSelects();
  setHistoryDateValue('');setArchiveMode('day');
  bridge('syncHistoryNow');
}
setTimeout(init,300);
document.addEventListener('visibilitychange',()=>{if(!document.hidden){updateToday();refreshLatestReport();refreshStatus();bridge('syncHistoryNow');if(archiveMode==='day')renderArchiveDay($('#historyDate').dataset.value);else renderArchiveMonth()}});
setInterval(()=>{if(!document.hidden){refreshLatestReport();refreshStatus()}},7000);
// Backup path when an FCM push is delayed/blocked: pull Windows history once a minute.
setInterval(()=>{if(!document.hidden)bridge('syncHistoryNow')},60000);
