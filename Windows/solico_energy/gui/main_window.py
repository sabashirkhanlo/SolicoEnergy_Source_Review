from __future__ import annotations
import os, sys, json, shutil, traceback
from datetime import date, datetime, timedelta
from pathlib import Path
from PySide6.QtCore import Qt, QDate, QThread, Signal, QTimer
from PySide6.QtGui import QDesktopServices, QPixmap, QIcon
from PySide6.QtWidgets import (QMainWindow,QWidget,QVBoxLayout,QHBoxLayout,QTabWidget,QLabel,QPushButton,QLineEdit,QFileDialog,QFormLayout,QGroupBox,QTableWidget,QTableWidgetItem,QComboBox,QCheckBox,QMessageBox,QDateEdit,QSpinBox,QHeaderView,QProgressBar,QPlainTextEdit,QInputDialog,QFrame,QGridLayout,QScrollArea,QButtonGroup,QSizePolicy,QAbstractItemView)
from PySide6.QtCore import QUrl
from ..core import db, settings as settings_mod
from ..core.jalali import parse_jalali_date, format_jalali, previous_jalali_month_for, jalali_month_bounds
from ..services.importer import import_all, import_source
from ..sources.wincc_csv import scan_tags
from ..reports.daily import generate as generate_daily
from ..reports.monthly import generate as generate_monthly
from ..reports.comparison import generate as generate_comparison
from ..reports.generic import generate_daily as generate_generic_daily, generate_monthly as generate_generic_monthly, is_energy_source
from ..services.calculations import comparison_summary
from ..core.mobile_push import start_report_server, notify_report_async, send_test_notification, mobile_status_text, install_service_account, enable_mobile_firewall, list_mobile_devices, set_mobile_device_enabled, delete_mobile_device, set_mobile_device_owner, send_registration_ping, sync_cloud_devices, list_mobile_login_requests, respond_mobile_login, start_windows_report_sync

from ..core.archive_transfer import request_archive, cancel_for_device, status_for_device

ROLES=[('نادیده گرفته شود','ignore'),('انرژی اکتیو تجمعی','energy_active'),('انرژی راکتیو تجمعی','energy_reactive'),('حداکثر توان اکتیو','max_power'),('زمان حداکثر توان اکتیو','max_power_time'),('حداکثر توان راکتیو','max_reactive_power'),('زمان حداکثر توان راکتیو','max_reactive_power_time'),('ساعت کارکرد تجمعی','running_hours'),('وضعیت بریکر 0/1','breaker_status'),('حداقل دما','temp_min'),('زمان حداقل دما','temp_min_time'),('حداکثر دما','temp_max'),('زمان حداکثر دما','temp_max_time'),('حداقل رطوبت','humidity_min'),('زمان حداقل رطوبت','humidity_min_time'),('حداکثر رطوبت','humidity_max'),('زمان حداکثر رطوبت','humidity_max_time')]
ENTITY_TYPES=[('ژنراتور','generator'),('شبکه','grid'),('کارخانه','factory'),('محیط','environment'),('سایر','other')]
UNITS={'energy_active':'kWh','energy_reactive':'kVArh','max_power':'kW','max_reactive_power':'kVAr','running_hours':'h','temp_min':'°C','temp_max':'°C','humidity_min':'%','humidity_max':'%','breaker_status':''}
ADD_ROLE_TOKEN='__add_custom_role__'
ADD_EQUIPMENT_TOKEN='__add_custom_equipment__'

class Worker(QThread):
    ok=Signal(object); fail=Signal(str)
    def __init__(self,fn): super().__init__(); self.fn=fn
    def run(self):
        try:self.ok.emit(self.fn())
        except Exception as e:self.fail.emit(str(e) or e.__class__.__name__)

class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle('Solico Energy Report')
        icon_path=Path(__file__).resolve().parents[1]/'assets'/'app_icon.png'
        if icon_path.exists(): self.setWindowIcon(QIcon(str(icon_path)))
        self.resize(1320, 820)
        self.setMinimumSize(1120, 700)
        db.init_db(); self.settings=settings_mod.load(); self.worker=None
        self.tabs=QTabWidget(); self.tabs.tabBar().hide()
        self._home(); self._settings_tab(); self._sources_tab(); self._tags_tab(); self._reports_tab(); self._updates_tab(); self._help_tab()
        self._build_shell()
        self._apply_app_style()
        self.statusBar().showMessage('آماده')
        self.home_timer=QTimer(self); self.home_timer.timeout.connect(self.refresh_home); self.home_timer.start(30000)
        QTimer.singleShot(1200,self._sync_scheduled_tasks)
        QTimer.singleShot(1800,self._start_mobile_report_server)
        QTimer.singleShot(2400,self._start_windows_report_sync)

        # AUTO_REPORT_HOTFIX_R2: when the GUI is already open, a second frozen
        # SolicoEnergy.exe started by Task Scheduler can be rejected by the app's
        # single-instance guard.  Run the same daily scheduler in-process too.
        self.auto_report_worker=None
        self._auto_report_attempt_slots=set()
        self.auto_report_timer=QTimer(self)
        self.auto_report_timer.timeout.connect(self._in_app_auto_report_tick)
        self.auto_report_timer.timeout.connect(self._in_app_monthly_report_tick)
        self.auto_report_timer.start(60000)
        QTimer.singleShot(5000,lambda:self._in_app_auto_report_tick(True))
        QTimer.singleShot(7000,lambda:self._in_app_monthly_report_tick(True))

        # Retry a notification that was queued because Internet/Firebase was
        # temporarily unavailable.  The queue itself is stored on disk.
        self.mobile_retry_timer=QTimer(self)
        self.mobile_retry_timer.timeout.connect(self._retry_pending_mobile_notifications)
        self.mobile_retry_timer.start(300000)
        QTimer.singleShot(20000,self._retry_pending_mobile_notifications)
        self.refresh_home()

    def _home(self):
        w=QWidget(); w.setObjectName('homePage')
        page=QVBoxLayout(w); page.setContentsMargins(18,18,18,14); page.setSpacing(14)

        # top info strip
        top=QFrame(); top.setObjectName('topInfoCard'); th=QHBoxLayout(top); th.setContentsMargins(16,10,16,10); th.setSpacing(10)
        right=QVBoxLayout(); right.setSpacing(2)
        self.home_plant=QLabel(); self.home_plant.setObjectName('plantTitle'); self.home_plant.setAlignment(Qt.AlignRight)
        self.home_dept=QLabel(); self.home_dept.setObjectName('plantSub'); self.home_dept.setAlignment(Qt.AlignRight)
        right.addWidget(self.home_plant); right.addWidget(self.home_dept)
        th.addLayout(right,1)
        th.addStretch(1)
        self.home_date=QLabel(); self.home_date.setObjectName('miniPill')
        self.home_time=QLabel(); self.home_time.setObjectName('miniPill')
        logos=QWidget(); logos.setObjectName('homeLogos'); lh=QHBoxLayout(logos); lh.setContentsMargins(0,0,0,0); lh.setSpacing(0)
        self.home_logo_app=QLabel(); self.home_logo_app.setObjectName('homeLogoApp'); self.home_logo_app.setAlignment(Qt.AlignCenter)
        app_logo_path=Path(__file__).resolve().parents[1]/'assets'/'app_icon.png'
        if app_logo_path.exists():
            pm=QPixmap(str(app_logo_path))
            if not pm.isNull(): self.home_logo_app.setPixmap(pm.scaled(58,58,Qt.KeepAspectRatio,Qt.SmoothTransformation))
        lh.addWidget(self.home_logo_app)
        th.addWidget(logos)
        th.addWidget(self.home_date); th.addWidget(self.home_time)
        page.addWidget(top)

        # welcome banner with greeting + image
        hero=QFrame(); hero.setObjectName('welcomeCard'); hh=QHBoxLayout(hero); hh.setContentsMargins(20,14,20,14); hh.setSpacing(18)
        greet_wrap=QHBoxLayout(); greet_wrap.setSpacing(14)
        emoji=QLabel('☀'); emoji.setObjectName('helloEmoji'); emoji.setAlignment(Qt.AlignTop|Qt.AlignHCenter)
        greet=QVBoxLayout(); greet.setSpacing(4)
        hello=QLabel('سلام!'); hello.setObjectName('helloTitle'); hello.setAlignment(Qt.AlignRight)
        sub=QLabel('به سامانه گزارش‌گیری انرژی و نیروگاه خوش آمدید.'); sub.setObjectName('helloSub'); sub.setAlignment(Qt.AlignRight)
        greet.addWidget(hello); greet.addWidget(sub); greet.addStretch()
        greet_wrap.addWidget(emoji); greet_wrap.addLayout(greet,1)
        greet_box=QWidget(); greet_box.setLayout(greet_wrap)
        hh.addWidget(greet_box,2)
        self.home_banner=QLabel(); self.home_banner.setObjectName('heroImage'); self.home_banner.setMinimumSize(420,110); self.home_banner.setMaximumHeight(110); self.home_banner.setScaledContents(True)
        banner_path=Path(__file__).resolve().parents[1]/'assets'/'hero_banner.png'
        if banner_path.exists():
            pm=QPixmap(str(banner_path))
            if not pm.isNull():
                self.home_banner.setPixmap(pm)
        hh.addWidget(self.home_banner,3)
        page.addWidget(hero)

        # Main actions
        actions=QGridLayout(); actions.setHorizontalSpacing(12); actions.setVerticalSpacing(8)
        self.home_import_button=QPushButton('''ورود داده‌های جدید از CSV
انتخاب و پردازش فایل WinCC'''); self.home_import_button.setObjectName('greenAction'); self.home_import_button.setMinimumHeight(78); self.home_import_button.clicked.connect(self.home_import_csv)
        self.home_daily_button=QPushButton('''ساخت گزارش روزانه
ایجاد و ذخیره گزارش PDF'''); self.home_daily_button.setObjectName('blueAction'); self.home_daily_button.setMinimumHeight(78); self.home_daily_button.clicked.connect(self.make_home_daily)
        self.home_open_report_button=QPushButton('''باز کردن آخرین گزارش
مشاهده آخرین PDF ساخته‌شده'''); self.home_open_report_button.setObjectName('lightAction'); self.home_open_report_button.setMinimumHeight(78); self.home_open_report_button.clicked.connect(self.open_latest_report)
        self.home_open_folder_button=QPushButton('''باز کردن پوشه گزارش‌ها
مشاهده همه فایل‌های خروجی'''); self.home_open_folder_button.setObjectName('lightAction'); self.home_open_folder_button.setMinimumHeight(78); self.home_open_folder_button.clicked.connect(self.open_output)
        actions.addWidget(self.home_import_button,0,0); actions.addWidget(self.home_daily_button,0,1); actions.addWidget(self.home_open_report_button,0,2); actions.addWidget(self.home_open_folder_button,0,3)
        for c in range(4): actions.setColumnStretch(c,1)
        page.addLayout(actions)

        # System status strip
        status=QFrame(); status.setObjectName('card'); sv=QVBoxLayout(status); sv.setContentsMargins(16,12,16,14)
        st=QLabel('وضعیت سیستم'); st.setObjectName('sectionTitle'); st.setAlignment(Qt.AlignRight); sv.addWidget(st)
        sg=QGridLayout(); sg.setHorizontalSpacing(10)
        self.home_db_status=self._make_status_box('وضعیت دیتابیس')
        self.home_source_status=self._make_status_box('وضعیت منبع داده')
        self.home_last_import=self._make_status_box('آخرین ورود داده')
        self.home_last_record=self._make_status_box('آخرین رکورد ثبت‌شده')
        for col,box in enumerate((self.home_db_status,self.home_source_status,self.home_last_import,self.home_last_record)):
            sg.addWidget(box[0],0,col); sg.setColumnStretch(col,1)
        sv.addLayout(sg); page.addWidget(status)

        # Bottom dashboard area
        bottom=QHBoxLayout(); bottom.setSpacing(14)
        activity=QFrame(); activity.setObjectName('card'); av=QVBoxLayout(activity); av.setContentsMargins(16,14,16,14)
        ah=QHBoxLayout(); a_title=QLabel('آخرین عملیات'); a_title.setObjectName('sectionTitle'); ah.addWidget(a_title); ah.addStretch()
        a_clear=QPushButton('پاک‌کردن تاریخچه'); a_clear.setObjectName('textButton'); a_clear.clicked.connect(self.clear_activity); ah.addWidget(a_clear); av.addLayout(ah)
        self.home_activity=QLabel(); self.home_activity.setObjectName('activityText'); self.home_activity.setAlignment(Qt.AlignTop|Qt.AlignRight); self.home_activity.setWordWrap(True); av.addWidget(self.home_activity,1)
        bottom.addWidget(activity,3)

        right_col=QVBoxLayout(); right_col.setSpacing(14)
        report=QFrame(); report.setObjectName('card'); rv=QVBoxLayout(report); rv.setContentsMargins(16,14,16,14)
        rt=QLabel('آخرین گزارش'); rt.setObjectName('sectionTitle'); rt.setAlignment(Qt.AlignRight); rv.addWidget(rt)
        self.home_report_info=QLabel(); self.home_report_info.setAlignment(Qt.AlignTop|Qt.AlignRight); self.home_report_info.setWordWrap(True); self.home_report_info.setObjectName('reportInfo'); rv.addWidget(self.home_report_info)
        rr=QHBoxLayout(); open_b=QPushButton('باز کردن گزارش'); open_b.setObjectName('smallPrimary'); open_b.clicked.connect(self.open_latest_report); rebuild_b=QPushButton('ساخت مجدد'); rebuild_b.clicked.connect(self.rebuild_latest_report); rr.addWidget(open_b); rr.addWidget(rebuild_b); rv.addLayout(rr)
        right_col.addWidget(report,1)

        alerts=QFrame(); alerts.setObjectName('card'); al=QVBoxLayout(alerts); al.setContentsMargins(16,14,16,14)
        alt=QLabel('هشدارها و پیام‌ها'); alt.setObjectName('sectionTitle'); alt.setAlignment(Qt.AlignRight); al.addWidget(alt)
        self.home_alerts=QLabel(); self.home_alerts.setAlignment(Qt.AlignTop|Qt.AlignRight); self.home_alerts.setWordWrap(True); self.home_alerts.setObjectName('alertText'); al.addWidget(self.home_alerts,1)
        right_col.addWidget(alerts,1)
        bottom.addLayout(right_col,2); page.addLayout(bottom,1)

        # Use an in-box QLabel instead of the native QGroupBox title.  On some
        # Windows DPI/font combinations the native RTL title is vertically clipped.
        archive=QGroupBox(); archive.setTitle(''); ar=QVBoxLayout(archive)
        archive_title=QLabel('آرشیو گزارش‌ها — تاریخ و فایل گزارش')
        archive_title.setObjectName('sectionTitle'); archive_title.setAlignment(Qt.AlignRight|Qt.AlignVCenter)
        archive_title.setWordWrap(True); archive_title.setMinimumHeight(30)
        ar.addWidget(archive_title)
        self.home_archive_filter=QLineEdit(); self.home_archive_filter.setPlaceholderText('جستجو در تاریخ شمسی، نوع گزارش یا نام فایل')
        self.home_archive_filter.textChanged.connect(self.refresh_home_archive); ar.addWidget(self.home_archive_filter)
        self.home_archive_table=QTableWidget(0,4)
        self.home_archive_table.setHorizontalHeaderLabels(['تاریخ گزارش','نوع گزارش','نام فایل','وضعیت فایل'])
        self.home_archive_table.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        self.home_archive_table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.home_archive_table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.home_archive_table.setMinimumHeight(180)
        self.home_archive_table.cellDoubleClicked.connect(lambda *_:self.open_home_archive_pdf())
        ar.addWidget(self.home_archive_table)
        open_pdf=QPushButton('باز کردن PDF انتخاب‌شده'); open_pdf.clicked.connect(self.open_home_archive_pdf); ar.addWidget(open_pdf)
        delete_pdf=QPushButton('حذف گزارش انتخاب‌شده از آرشیو'); delete_pdf.clicked.connect(self.delete_home_archive_report); ar.addWidget(delete_pdf)
        page.addWidget(archive)
        self.schedule_notice=QLabel(); self.schedule_notice.setWordWrap(True); page.addWidget(self.schedule_notice)
        self.schedule_repair=QPushButton('اصلاح زمان‌بندی با مجوز مدیر ویندوز')
        self.schedule_repair.clicked.connect(self.repair_schedule); page.addWidget(self.schedule_repair)
        self.progress=QProgressBar(); self.progress.setRange(0,0); self.progress.setMaximumHeight(5); self.progress.hide(); page.addWidget(self.progress)
        scroll=QScrollArea(); scroll.setWidgetResizable(True); scroll.setWidget(w)
        self.tabs.addTab(scroll,'خانه')

    def refresh_home_archive(self,*_):
        if not hasattr(self,'home_archive_table'): return
        with db.connect() as con:
            rows=[dict(r) for r in con.execute('SELECT report_type,period_key,path,created_at FROM generated_reports ORDER BY created_at DESC,id DESC')]
        selected=self.home_archive_table.item(self.home_archive_table.currentRow(),0)
        selected_path=selected.data(Qt.UserRole) if selected else None
        query=self.home_archive_filter.text().strip().translate(str.maketrans('۰۱۲۳۴۵۶۷۸۹','0123456789')).lower()
        visible=[]
        for r in rows:
            period=r['period_key']; typ=r['report_type']
            if typ in ('daily','daily_partial') or typ.startswith('generic_daily_'):
                try:period=format_jalali(date.fromisoformat(period))
                except ValueError:pass
            label={'daily':'روزانه انرژی','daily_partial':'روزانه موقت','monthly':'ماهانه انرژی','monthly_partial':'ماهانه موقت'}.get(typ,typ)
            path=Path(r['path'] or '')
            vals=[period,label,path.name,'موجود' if path.is_file() else 'فایل موجود نیست']
            if query and query not in ' '.join(vals).translate(str.maketrans('۰۱۲۳۴۵۶۷۸۹','0123456789')).lower():continue
            visible.append((vals,str(path),typ,r['period_key']))
        self.home_archive_table.setRowCount(len(visible))
        for i,(vals,path,typ,key) in enumerate(visible):
            for j,v in enumerate(vals):
                item=QTableWidgetItem(v); item.setData(Qt.UserRole,path); item.setData(Qt.UserRole+1,(typ,key)); self.home_archive_table.setItem(i,j,item)
            if path==selected_path:self.home_archive_table.selectRow(i)

    def delete_home_archive_report(self):
        item=self.home_archive_table.item(self.home_archive_table.currentRow(),0)
        if not item:
            QMessageBox.information(self,'آرشیو','ابتدا یک گزارش انتخاب کنید.'); return
        typ,key=item.data(Qt.UserRole+1)
        answer=QMessageBox.question(self,'حذف گزارش',
            f'گزارش {item.text()} از آرشیو ویندوز حذف شود؟\nفایل PDF برای بازیابی در پوشه پشتیبان نگهداری می‌شود. داده‌های CSV حذف نمی‌شوند.\nنسخه موجود در گوشی با این کار حذف نمی‌شود؛ پس از بازسازی، گزارش را دوباره ارسال کنید.',
            QMessageBox.Yes|QMessageBox.No,QMessageBox.No)
        if answer!=QMessageBox.Yes:return
        try:
            db.delete_generated_report(typ,key)
            self.refresh_home_archive()
            QMessageBox.information(self,'آرشیو','گزارش از آرشیو حذف شد. اکنون همان تاریخ را در بخش گزارش‌ها انتخاب و PDF را بازسازی کنید.')
        except Exception as exc:
            QMessageBox.warning(self,'حذف ناموفق',str(exc))

    def open_home_archive_pdf(self):
        item=self.home_archive_table.item(self.home_archive_table.currentRow(),0)
        if not item:
            QMessageBox.information(self,'آرشیو','ابتدا یک گزارش انتخاب کنید.'); return
        path=Path(item.data(Qt.UserRole))
        if not path.is_file():
            QMessageBox.warning(self,'آرشیو','فایل این گزارش در مسیر ذخیره‌شده موجود نیست.'); return
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(path.resolve())))

    def repair_schedule(self):
        if getattr(self,'schedule_worker',None) is not None:return
        from ..core.windows_tasks import repair_with_admin
        self.schedule_repair.setEnabled(False)
        w=Worker(repair_with_admin); self.schedule_worker=w
        w.ok.connect(lambda _:self._sync_scheduled_tasks())
        w.fail.connect(lambda error:self.schedule_notice.setText('ثبت زمان‌بندی انجام نشد: '+str(error)))
        w.finished.connect(self._schedule_repair_finished); w.start()

    def _schedule_repair_finished(self):
        self.schedule_worker=None; self.schedule_repair.setEnabled(True)

    def _make_status_box(self, title):
        f=QFrame(); f.setObjectName('statusBox'); v=QVBoxLayout(f); v.setContentsMargins(12,8,12,8); v.setSpacing(3)
        t=QLabel(title); t.setObjectName('statusCaption'); t.setAlignment(Qt.AlignCenter)
        value=QLabel('—'); value.setObjectName('statusValue'); value.setAlignment(Qt.AlignCenter)
        v.addWidget(t); v.addWidget(value)
        return f,value

    def _build_shell(self):
        shell=QWidget(); layout=QHBoxLayout(shell); layout.setContentsMargins(0,0,0,0); layout.setSpacing(0)
        side=QFrame(); side.setObjectName('sidebar'); side.setFixedWidth(214); sv=QVBoxLayout(side); sv.setContentsMargins(12,18,12,14); sv.setSpacing(7)
        app_icon=QLabel(); app_icon.setObjectName('sideAppIcon'); app_icon.setAlignment(Qt.AlignCenter)
        app_icon_path=Path(__file__).resolve().parents[1]/'assets'/'app_icon.png'
        if app_icon_path.exists():
            pm=QPixmap(str(app_icon_path))
            if not pm.isNull(): app_icon.setPixmap(pm.scaled(64,64,Qt.KeepAspectRatio,Qt.SmoothTransformation))
        sv.addWidget(app_icon)
        # FIX22: icon-only branding in sidebar; no text below the app icon.
        sv.addSpacing(14)
        self.nav_group=QButtonGroup(self); self.nav_group.setExclusive(True); self.nav_buttons=[]
        items=[('خانه',0),('گزارش‌ها',4),('تعریف تگ‌ها',3),('منابع داده',2),('تنظیمات',1),('به‌روزرسانی',5),('راهنما',6)]
        for text,idx in items:
            b=QPushButton(text); b.setCheckable(True); b.setObjectName('navButton'); b.setMinimumHeight(44); b.clicked.connect(lambda checked=False,i=idx:self._navigate(i)); self.nav_group.addButton(b); self.nav_buttons.append((b,idx)); sv.addWidget(b)
        self.nav_buttons[0][0].setChecked(True); sv.addStretch()
        self.side_version=QLabel('نسخه 1.0.27'); self.side_version.setObjectName('sideVersion'); self.side_version.setAlignment(Qt.AlignCenter); sv.addWidget(self.side_version)
        exit_b=QPushButton('خروج'); exit_b.setObjectName('navButton'); exit_b.clicked.connect(self.close); sv.addWidget(exit_b)
        layout.addWidget(side); layout.addWidget(self.tabs,1); self.setCentralWidget(shell)

    def _navigate(self, idx):
        self.tabs.setCurrentIndex(idx)
        for b,i in self.nav_buttons:
            b.setChecked(i==idx)
        if idx==0: self.refresh_home()
        elif idx==2: self.refresh_sources()

    def _apply_app_style(self):
        self.setStyleSheet(r'''
            QMainWindow { background:#f3f7fb; }
            QWidget { font-family:"Segoe UI","Tahoma"; font-size:11pt; color:#18324b; }
            QTabWidget::pane { border:0; background:#f3f7fb; }
            #sidebar { background:#123653; border:0; }
            #sideBrand { color:white; font-size:17px; font-weight:700; padding:6px; }
            #sideSub { color:#c8d9e8; font-size:10px; padding-bottom:4px; }
            #sideVersion { color:#bcd0df; font-size:10px; padding:12px 0; border-top:1px solid #31516a; }
            QPushButton#navButton { color:#eaf3fa; background:transparent; border:0; border-radius:7px; text-align:right; padding:10px 18px; font-weight:600; }
            QPushButton#navButton:hover { background:#1c4d72; }
            QPushButton#navButton:checked { background:#1478c9; color:white; }
            #homePage { background:#f4f8fc; }
            #topInfoCard { background:transparent; border:0; }
            #homeLogos QLabel { background:transparent; }
            #sideAppIcon { padding-bottom:4px; }
            #welcomeCard { background:qlineargradient(x1:0,y1:0,x2:1,y2:0,stop:0 #ffffff,stop:1 #edf7ff); border:1px solid #d6e4ef; border-radius:12px; }
            #plantTitle { color:#133854; font-size:16px; font-weight:750; }
            #plantSub { color:#567084; font-size:10px; }
            #miniPill { background:white; border:1px solid #d7e4ee; border-radius:8px; padding:4px 10px; color:#24516f; font-size:10px; min-width:88px; }
            #helloEmoji { color:#f7b500; font-size:44px; }
            #helloTitle { color:#123f67; font-size:24px; font-weight:800; }
            #helloSub { color:#466277; font-size:12px; }
            #heroImage { background:#d8eaf8; border:1px solid #cddfeb; border-radius:10px; }
            #card { background:white; border:1px solid #d7e3ed; border-radius:10px; }
            #statusBox { background:#f9fbfd; border:1px solid #e0e9f0; border-radius:8px; }
            #sectionTitle { color:#0f4e80; font-size:15px; font-weight:750; }
            #statusCaption { color:#60798c; font-size:10px; }
            #statusValue { color:#143f5e; font-size:12px; font-weight:700; }
            #activityText, #reportInfo, #alertText { color:#36546b; line-height:1.5; }
            QPushButton { background:#ffffff; border:1px solid #bfcfdb; border-radius:7px; padding:8px 12px; }
            QPushButton:hover { border-color:#4b91c5; background:#f4faff; }
            QPushButton#greenAction { background:#17a866; color:white; border:0; font-weight:700; font-size:12px; }
            QPushButton#greenAction:hover { background:#138f57; }
            QPushButton#blueAction { background:#1679c8; color:white; border:0; font-weight:700; font-size:12px; }
            QPushButton#blueAction:hover { background:#106bb2; }
            QPushButton#lightAction { background:#ffffff; color:#1265a7; border:1px solid #b9d2e5; font-weight:700; font-size:11px; }
            QPushButton#smallPrimary { background:#1679c8; color:white; border:0; font-weight:650; }
            QPushButton#textButton { background:transparent; border:0; color:#3178ad; padding:4px; font-size:9px; }
            QLineEdit, QComboBox, QDateEdit, QSpinBox { background:white; border:1px solid #bdcfdd; border-radius:5px; padding:6px; min-height:24px; }
            QGroupBox { background:white; border:1px solid #d3e0ea; border-radius:8px; margin-top:24px; padding:18px 12px 12px 12px; font-weight:700; }
            QGroupBox::title { subcontrol-origin:margin; subcontrol-position: top right; top:-10px; right:12px; background:#f3f7fb; padding:0 14px; color:#174f79; min-width:120px; }
            QGroupBox#mobileNotifyBox { background:#ffffff; border:1px solid #a9c9df; border-radius:10px; margin-top:24px; padding:18px 14px 14px 14px; }
            QGroupBox#mobileNotifyBox::title { color:#0f5f97; font-size:12px; font-weight:800; }
            QTableWidget { background:white; alternate-background-color:#f7fafc; gridline-color:#e0e7ed; border:1px solid #d4e1ea; border-radius:6px; }
            QHeaderView::section { background:#eaf2f8; color:#244a66; padding:7px; border:0; border-right:1px solid #d3e0e9; font-weight:700; }
            QProgressBar { border:0; background:#e7eef4; border-radius:2px; }
            QProgressBar::chunk { background:#1683d8; border-radius:2px; }
            QStatusBar { background:#f8fbfd; color:#577185; border-top:1px solid #dbe6ee; }
        ''')

    def _activity_path(self):
        return settings_mod.data_dir()/'activity.json'

    def _load_activity(self):
        try:
            p=self._activity_path()
            obj=json.loads(p.read_text(encoding='utf-8')) if p.exists() else []
            return obj if isinstance(obj,list) else []
        except Exception:
            return []

    def _add_activity(self, text, kind='info', status='ok'):
        items=self._load_activity()
        items.insert(0,{'time':datetime.now().isoformat(timespec='seconds'),'text':str(text),'kind':kind,'status':status})
        items=items[:30]
        try:self._activity_path().write_text(json.dumps(items,ensure_ascii=False,indent=2),encoding='utf-8')
        except Exception:pass
        self.refresh_home()

    def clear_activity(self):
        try:self._activity_path().unlink(missing_ok=True)
        except Exception:pass
        self.refresh_home()

    def _latest_report_row(self):
        try:
            with db.connect() as con:
                r=con.execute('SELECT report_type,period_key,path,created_at FROM generated_reports ORDER BY created_at DESC,id DESC LIMIT 1').fetchone()
                return dict(r) if r else None
        except Exception:
            return None

    def open_latest_report(self):
        r=self._latest_report_row()
        if not r or not r.get('path') or not Path(r['path']).exists():
            QMessageBox.information(self,'آخرین گزارش','هنوز فایل گزارش قابل دسترسی وجود ندارد.')
            return
        self._open_path(r['path'])

    def rebuild_latest_report(self):
        r=self._latest_report_row()
        if not r:
            self.make_home_daily(); return
        if r.get('report_type') in ('daily','daily_partial'):
            try:
                d=date.fromisoformat(r.get('period_key',''))
                self.daily_date.setDate(QDate(d.year,d.month,d.day)); self.make_daily(); return
            except Exception: pass
        QMessageBox.information(self,'ساخت مجدد','برای بازسازی گزارش ماهانه، از بخش «گزارش‌ها» استفاده کنید.')

    def _help_tab(self):
        w=QWidget(); l=QVBoxLayout(w); l.setContentsMargins(28,28,28,28)
        t=QLabel('راهنمای سریع'); t.setStyleSheet('font-size:22px;font-weight:700;color:#0e4d80'); l.addWidget(t)
        text=QLabel('''<b>روال روزانه پیشنهادی</b><br><br>
        ۱) از صفحه «خانه» فایل CSV جدید WinCC را وارد کنید.<br>
        ۲) وضعیت آخرین رکورد و پیام عملیات را بررسی کنید.<br>
        ۳) «ساخت گزارش روزانه» را بزنید؛ اگر امروز را گزارش بگیرید، از ۰۰:۰۰ تا آخرین داده موجود به‌صورت موقت محاسبه می‌شود.<br>
        ۴) از «باز کردن آخرین گزارش» PDF را مشاهده کنید.<br><br>
        <b>نکته:</b> Mapping تگ‌ها و دیتابیس با نصب Update حذف نمی‌شوند. برای تغییر Mapping فقط از بخش «تعریف تگ‌ها» استفاده کنید.''')
        text.setWordWrap(True); text.setAlignment(Qt.AlignTop|Qt.AlignRight); text.setStyleSheet('background:white;border:1px solid #d7e3ed;border-radius:10px;padding:20px;line-height:1.8'); l.addWidget(text); l.addStretch(); self.tabs.addTab(w,'راهنما')

    def refresh_home(self):
        if not hasattr(self,'home_plant'): return
        s=settings_mod.load(); self.settings=s
        self.home_plant.setText(s.get('plant','پالایشگاه شیر تهران'))
        self.home_dept.setText('مدیریت نیروگاه و انرژی')
        self.home_date.setText(format_jalali(date.today()))
        self.home_time.setText(datetime.now().strftime('%H:%M'))
        if hasattr(self,'side_version'): self.side_version.setText('نسخه 1.0.27')

        # Database status and latest record
        db_ok=True; last_epoch=None
        try:
            with db.connect() as con:
                con.execute('SELECT 1').fetchone()
                r=con.execute('SELECT MAX(ts_epoch) AS mx FROM records').fetchone(); last_epoch=(r['mx'] if r else None)
        except Exception: db_ok=False
        self.home_db_status[1].setText('● سالم' if db_ok else '● خطا')
        self.home_db_status[1].setStyleSheet('color:#159455;font-weight:700' if db_ok else 'color:#c53b3b;font-weight:700')

        sources=[x for x in db.list_sources() if x.get('enabled')] if db_ok else []
        available=[x for x in sources if x.get('path') and Path(x['path']).exists()]
        if available: src_text='● متصل'
        elif sources: src_text='● مسیر در دسترس نیست'
        else: src_text='● تعریف نشده'
        self.home_source_status[1].setText(src_text)
        self.home_source_status[1].setStyleSheet('color:#159455;font-weight:700' if available else 'color:#c58a15;font-weight:700')
        self.home_last_record[1].setText(datetime.fromtimestamp(float(last_epoch)).strftime('%Y/%m/%d  %H:%M') if last_epoch is not None else '—')

        activities=self._load_activity()
        last_import=next((a for a in activities if a.get('kind')=='import'),None)
        if last_import:
            try: self.home_last_import[1].setText(datetime.fromisoformat(last_import['time']).strftime('%Y/%m/%d  %H:%M'))
            except Exception: self.home_last_import[1].setText('ثبت شده')
        else:self.home_last_import[1].setText('—')

        # Activity list
        if activities:
            lines=[]
            for a in activities[:6]:
                try: tm=datetime.fromisoformat(a.get('time','')).strftime('%H:%M')
                except Exception: tm=''
                mark='✓' if a.get('status')=='ok' else ('!' if a.get('status')=='warn' else '•')
                lines.append(f'<div style="padding:6px 2px;border-bottom:1px solid #edf2f5"><b>{mark}</b>&nbsp;&nbsp;{a.get("text","")} <span style="color:#8195a5">&nbsp;&nbsp;{tm}</span></div>')
            self.home_activity.setText(''.join(lines))
        else:self.home_activity.setText('<span style="color:#8093a2">هنوز عملیاتی در این نسخه ثبت نشده است.</span>')

        # Last report
        rep=self._latest_report_row(); self._home_last_report=rep
        if rep and rep.get('path'):
            typ={'daily':'روزانه','daily_partial':'روزانه موقت','monthly':'ماهانه','monthly_partial':'ماهانه موقت'}.get(rep.get('report_type'),rep.get('report_type','—'))
            period=rep.get('period_key','—')
            if rep.get('report_type') in ('daily','daily_partial'):
                try: period=format_jalali(date.fromisoformat(period))
                except Exception: pass
            p=Path(rep['path']); made='—'
            if p.exists():
                try: made=datetime.fromtimestamp(p.stat().st_mtime).strftime('%Y/%m/%d  %H:%M')
                except Exception: pass
            self.home_report_info.setText(f'<b>نوع گزارش:</b> {typ}<br><b>تاریخ:</b> {period}<br><b>زمان ساخت:</b> {made}<br><b>فایل:</b> {p.name}')
        else:self.home_report_info.setText('هنوز گزارشی ساخته نشده است.')

        # Alerts: only objective, non-inferred health messages.
        self.refresh_home_archive()
        alerts=[]
        if not db_ok: alerts.append(('خطا','دیتابیس در دسترس نیست.'))
        if not sources: alerts.append(('هشدار','هیچ منبع داده فعالی تعریف نشده است.'))
        elif not available: alerts.append(('هشدار','مسیر CSV منبع فعال در حال حاضر در دسترس نیست.'))
        if last_epoch is None: alerts.append(('هشدار','هنوز رکوردی در دیتابیس ثبت نشده است.'))
        if not rep: alerts.append(('اطلاع','هنوز گزارشی در سامانه ثبت نشده است.'))
        auto=self._auto_report_status() if hasattr(self,'_auto_report_status') else None
        if auto:
            state=auto.get('state')
            if state=='incomplete': alerts.append(('هشدار',auto.get('message') or 'گزارش خودکار ساخته نشد؛ داده روز کامل نیست.'))
            elif state=='error': alerts.append(('خطا',auto.get('message') or 'خطا در گزارش خودکار.'))
            elif state=='success': alerts.append(('موفق',auto.get('message') or 'گزارش خودکار ساخته شد.'))
        if not alerts: alerts.append(('موفق','دیتابیس، منبع داده و سابقه گزارش در دسترس هستند.'))
        col={'خطا':'#bd3232','هشدار':'#c78512','اطلاع':'#2674ad','موفق':'#148a50'}
        self.home_alerts.setText(''.join(f'<div style="padding:5px"><b style="color:{col[k]}">{k}:</b> {txt}</div>' for k,txt in alerts[:4]))

    def _path_row(self, edit, folder=False, image=False):
        box=QHBoxLayout(); box.addWidget(edit); b=QPushButton('انتخاب...')
        def choose():
            if folder: p=QFileDialog.getExistingDirectory(self,'انتخاب پوشه',edit.text() or str(Path.home()))
            else:
                filt='Images (*.png *.jpg *.jpeg)' if image else 'All files (*.*)'; p,_=QFileDialog.getOpenFileName(self,'انتخاب فایل',edit.text() or str(Path.home()),filt)
            if p:edit.setText(p)
        b.clicked.connect(choose); box.addWidget(b); return box
    def _settings_tab(self):
        w=QWidget(); l=QVBoxLayout(w); f=QFormLayout()
        f.setFieldGrowthPolicy(QFormLayout.AllNonFixedFieldsGrow)
        f.setLabelAlignment(Qt.AlignRight | Qt.AlignVCenter)
        fields=[('نام شرکت','company'),('نام کارخانه','plant'),('عنوان واحد','department'),('عنوان گزارش روزانه','daily_title'),('عنوان گزارش ماهانه','monthly_title'),('عنوان سامانه','smart_system_label'),('ساعت گزارش روزانه خودکار','daily_time'),('ساعت گزارش ماهانه','monthly_time'),('نام تهیه‌کننده','prepared_name'),('سمت تهیه‌کننده','prepared_title'),('نام مدیر/مسئول','manager_name'),('سمت مدیر/مسئول','manager_title')]
        self.setting_edits={}
        for label,key in fields:
            e=QLineEdit(str(self.settings.get(key,''))); self.setting_edits[key]=e; f.addRow(label,e)
        self.auto_daily_check=QCheckBox('فعال‌سازی ساخت خودکار گزارش روز قبل')
        self.auto_daily_check.setChecked(bool(self.settings.get('auto_report_enabled',True)))
        f.addRow('Auto Report',self.auto_daily_check)
        # Mobile notifications are placed in a dedicated full-width box.
        # This avoids the QFormLayout label column squeezing/clipping the controls.
        mobile_box=QGroupBox()
        mobile_box.setObjectName('mobileNotifyBox')
        mobile_box.setMinimumHeight(225)
        mobile_layout=QVBoxLayout(mobile_box)
        mobile_layout.setContentsMargins(14,14,14,14)
        mobile_layout.setSpacing(10)

        mobile_title=QLabel('اتصال و نوتیفیکیشن موبایل')
        mobile_title.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        mobile_title.setMinimumHeight(36)
        mobile_title.setStyleSheet(
            'font-size:15px;font-weight:800;color:#0f5f97;'
            'background:#eef6fb;border:1px solid #c6dceb;border-radius:7px;padding:7px 10px;'
        )
        mobile_layout.addWidget(mobile_title)

        self.mobile_notify_check=QCheckBox('ارسال نوتیفیکیشن گزارش به موبایل')
        self.mobile_notify_check.setChecked(bool(self.settings.get('mobile_notify_enabled',True)))
        self.mobile_notify_check.setMinimumHeight(30)
        mobile_layout.addWidget(self.mobile_notify_check)

        self.mobile_status_label=QLabel(mobile_status_text())
        self.mobile_status_label.setWordWrap(True)
        self.mobile_status_label.setMinimumHeight(48)
        self.mobile_status_label.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        self.mobile_status_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.mobile_status_label.setStyleSheet(
            'background:#f8fbfd;border:1px solid #c8d9e6;border-radius:7px;'
            'padding:9px 12px;color:#36546b;'
        )
        mobile_layout.addWidget(self.mobile_status_label)

        mobile_key=QPushButton('انتخاب کلید Firebase')
        mobile_key.setObjectName('lightAction')
        mobile_key.setMinimumHeight(42)
        mobile_key.setSizePolicy(QSizePolicy.Expanding,QSizePolicy.Fixed)
        mobile_key.clicked.connect(self.choose_firebase_key)
        mobile_layout.addWidget(mobile_key)

        self.mobile_test_button=QPushButton('ارسال نوتیفیکیشن تست به گوشی')
        self.mobile_test_button.setObjectName('blueAction')
        self.mobile_test_button.setMinimumHeight(48)
        self.mobile_test_button.setSizePolicy(QSizePolicy.Expanding,QSizePolicy.Fixed)
        self.mobile_test_button.clicked.connect(self.test_mobile_notification)
        mobile_layout.addWidget(self.mobile_test_button)

        self.mobile_firewall_button=QPushButton('فعال‌سازی دسترسی PDF موبایل / Firewall')
        self.mobile_firewall_button.setObjectName('lightAction')
        self.mobile_firewall_button.setMinimumHeight(46)
        self.mobile_firewall_button.setSizePolicy(QSizePolicy.Expanding,QSizePolicy.Fixed)
        self.mobile_firewall_button.clicked.connect(self.enable_mobile_pdf_access)
        mobile_layout.addWidget(self.mobile_firewall_button)

        # addRow(widget) spans both columns of QFormLayout.
        f.addRow(mobile_box)

        devices_box=QGroupBox()
        devices_layout=QVBoxLayout(devices_box)
        devices_layout.setContentsMargins(14,14,14,14)
        devices_layout.setSpacing(10)

        devices_title=QLabel('مدیریت موبایل‌ها')
        devices_title.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        devices_title.setMinimumHeight(36)
        devices_title.setStyleSheet(
            'font-size:15px;font-weight:800;color:#0f5f97;'
            'background:#eef6fb;border:1px solid #c6dceb;border-radius:7px;padding:7px 10px;'
        )
        devices_layout.addWidget(devices_title)

        info=QLabel(
            'هر گوشی پس از واردکردن نام کاربری و رمز صحیح، بدون نیاز به تأیید ویندوز وارد برنامه می‌شود. '
            'این جدول فقط گوشی‌های ثبت‌شده را برای مشاهده وضعیت اتصال نشان می‌دهد. '
            'برای ارسال گزارش‌های قبلی، گوشی را انتخاب و درخواست آرشیو را ارسال کنید؛ دریافت فقط پس از تأیید گوشی انجام می‌شود. این قابلیت به نسخه جدید اپ موبایل نیاز دارد. '
            'نام و نام خانوادگی هر دستگاه قابل ثبت است و حذف دستگاه، آن را کامل از لیست پاک می‌کند.'
        )
        info.setWordWrap(True)
        info.setStyleSheet('color:#526f83;font-size:11px;padding:2px 4px;')
        devices_layout.addWidget(info)

        self.mobile_devices_count=QLabel('تعداد دستگاه: ۰')
        self.mobile_devices_count.setAlignment(Qt.AlignRight)
        self.mobile_devices_count.setStyleSheet('color:#526f83;font-size:11px;padding:2px 4px;')
        devices_layout.addWidget(self.mobile_devices_count)

        self.mobile_devices_table=QTableWidget(0,9)
        self.mobile_devices_table.setHorizontalHeaderLabels(
            ['نام و نام خانوادگی','نام دستگاه','نام کاربری','نسخه اپ','آخرین اتصال','IP گوشی','وضعیت اتصال','روش ثبت','وضعیت آرشیو']
        )
        self.mobile_devices_table.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        self.mobile_devices_table.setMinimumHeight(260)
        devices_layout.addWidget(self.mobile_devices_table)

        row1=QHBoxLayout()
        bscan=QPushButton('دریافت دستگاه‌ها از اینترنت')
        bscan.clicked.connect(self.discover_mobile_devices)
        row1.addWidget(bscan)

        bref=QPushButton('تازه‌سازی')
        bref.clicked.connect(self.refresh_mobile_devices)
        row1.addWidget(bref)

        devices_layout.addLayout(row1)

        row2=QHBoxLayout()
        bowner=QPushButton('ثبت / ویرایش نام و نام خانوادگی')
        bowner.clicked.connect(self.edit_selected_mobile_owner)
        row2.addWidget(bowner)

        bdisable=QPushButton('حذف دستگاه انتخاب‌شده')
        bdisable.clicked.connect(lambda:self.set_selected_mobile_device(False))
        row2.addWidget(bdisable)
        devices_layout.addLayout(row2)

        archive_row=QHBoxLayout()
        self.archive_button=QPushButton('درخواست ارسال کل آرشیو به گوشی انتخاب‌شده')
        self.archive_button.clicked.connect(self.request_selected_archive)
        archive_row.addWidget(self.archive_button)
        cancel_archive=QPushButton('لغو درخواست آرشیو')
        cancel_archive.clicked.connect(self.cancel_selected_archive)
        archive_row.addWidget(cancel_archive)
        devices_layout.addLayout(archive_row)
        self.archive_worker=None
        f.addRow(devices_box)

        self.mobile_sync_worker=None
        self._mobile_sync_manual=False
        QTimer.singleShot(1200,self._auto_discover_mobile_devices)
        QTimer.singleShot(2500,self.refresh_mobile_devices)
        # UI refresh is local-only and therefore never waits for the Internet.
        self.mobile_login_timer=QTimer(self); self.mobile_login_timer.timeout.connect(self.refresh_mobile_devices); self.mobile_login_timer.start(7000)
        # Cloud registry refresh runs on a worker thread. This prevents ntfy/network
        # timeouts from freezing the entire application.
        self.mobile_cloud_timer=QTimer(self); self.mobile_cloud_timer.timeout.connect(self._auto_discover_mobile_devices); self.mobile_cloud_timer.start(45000)
        if 'daily_time' in self.setting_edits:
            self.setting_edits['daily_time'].setText('08:00')
            self.setting_edits['daily_time'].setReadOnly(True)
            self.setting_edits['daily_time'].setToolTip('گزارش روز قبل هر روز ساعت 08:00 صبح ساخته می‌شود؛ در صورت ناقص بودن داده، 08:10 و 08:30 دوباره تلاش می‌شود.')
        self.output_edit=QLineEdit(self.settings.get('output_dir','')); f.addRow('پوشه خروجی گزارش',self._wrap(self._path_row(self.output_edit,folder=True)))
        self.logo_edit=QLineEdit(self.settings.get('logo_path','')); f.addRow('لوگو',self._wrap(self._path_row(self.logo_edit,image=True)))
        self.prep_sig=QLineEdit(self.settings.get('prepared_signature','')); f.addRow('امضای تهیه‌کننده',self._wrap(self._path_row(self.prep_sig,image=True)))
        self.mgr_sig=QLineEdit(self.settings.get('manager_signature','')); f.addRow('امضای مدیر/مسئول',self._wrap(self._path_row(self.mgr_sig,image=True)))
        l.addLayout(f)
        row=QHBoxLayout()
        save=QPushButton('ذخیره تنظیمات'); save.clicked.connect(self.save_settings); row.addWidget(save)
        exp=QPushButton('خروجی تنظیمات'); exp.clicked.connect(self.export_settings); row.addWidget(exp)
        imp=QPushButton('ورود تنظیمات'); imp.clicked.connect(self.import_settings); row.addWidget(imp)
        l.addLayout(row)
        l.addStretch()

        # Settings can be taller than smaller displays. Make the page scrollable
        # instead of letting controls overlap or get hidden.
        scroll=QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        scroll.setWidget(w)
        self.tabs.addTab(scroll,'تنظیمات')
    def refresh_mobile_devices(self):
        if not hasattr(self,'mobile_devices_table'): return

        # Local-only refresh: cloud polling runs in a worker thread, so the GUI never waits on Internet.
        selected=self.mobile_devices_table.item(self.mobile_devices_table.currentRow(),0)
        selected_id=selected.data(Qt.UserRole) if selected else None
        devices=list_mobile_devices(sync=False)
        rows=[dict(d) for d in devices if d.get('device_id')]
        rows.sort(key=lambda d:str(d.get('last_seen') or ''),reverse=True)

        self.mobile_devices_table.setRowCount(len(rows))
        if hasattr(self,'mobile_devices_count'):
            fa=str(len(rows)).translate(str.maketrans('0123456789','۰۱۲۳۴۵۶۷۸۹'))
            self.mobile_devices_count.setText(f'تعداد دستگاه: {fa}')

        for r,d in enumerate(rows):
            did=str(d.get('device_id') or '')
            vals=[
                d.get('owner_name',''),
                d.get('name',''),
                d.get('username','') or 'admin',
                d.get('app_version',''),
                d.get('last_seen',''),
                d.get('phone_ip','') or '—',
                'متصل / فعال' if bool(d.get('enabled',True)) else 'غیرفعال',
                d.get('ip','') or 'Internet / ntfy',
                status_for_device(did),
            ]

            for c,val in enumerate(vals):
                item=QTableWidgetItem(str(val))
                self.mobile_devices_table.setItem(r,c,item)
                if c==0:
                    item.setData(Qt.UserRole,did)
            if did==selected_id:
                self.mobile_devices_table.selectRow(r)

        self.mobile_devices_table.resizeRowsToContents()

    def _selected_archive_device(self):
        item=self.mobile_devices_table.item(self.mobile_devices_table.currentRow(),0)
        if item is None:
            QMessageBox.warning(self,'آرشیو گزارش‌ها','ابتدا گوشی را از جدول انتخاب کنید.')
            return None
        return item.data(Qt.UserRole)

    def request_selected_archive(self):
        did=self._selected_archive_device()
        if not did or self.archive_worker is not None: return
        self.archive_button.setEnabled(False)
        w=Worker(lambda:request_archive(did)); self.archive_worker=w
        w.ok.connect(self._archive_requested)
        w.fail.connect(lambda msg:QMessageBox.warning(self,'آرشیو گزارش‌ها',str(msg)))
        w.finished.connect(self._archive_request_finished)
        w.start()

    def _archive_requested(self,result):
        self.refresh_mobile_devices()
        QMessageBox.information(self,'درخواست آرشیو',
            f"درخواست دریافت {result['count']} گزارش ارسال شد. منتظر تأیید گوشی هستیم."
            + (f"\n{result['missing_count']} گزارش غیرانرژی در آرشیو شش شاخص قرار نگرفت." if result['missing_count'] else '')
            + ('\n'+result.get('warning','') if result.get('warning') else '')
            + '\nداده‌های شش شاخص برای نمودار گوشی ارسال می‌شوند؛ فایل PDF ارسال نمی‌شود.')

    def _archive_request_finished(self):
        self.archive_worker=None
        self.archive_button.setEnabled(True)

    def cancel_selected_archive(self):
        did=self._selected_archive_device()
        if did:
            cancel_for_device(did)
            self.refresh_mobile_devices()

    def _registration_ping_silent(self):
        try:
            sync_cloud_devices()
        except Exception:
            pass

    def _start_mobile_cloud_sync(self, manual=False):
        w=getattr(self,'mobile_sync_worker',None)
        if w is not None and w.isRunning():
            if manual:
                QMessageBox.information(self,'مدیریت موبایل‌ها','دریافت دستگاه‌ها در حال انجام است؛ چند لحظه صبر کنید.')
            return
        self._mobile_sync_manual=bool(manual)
        w=Worker(sync_cloud_devices)
        self.mobile_sync_worker=w
        w.ok.connect(self._mobile_cloud_sync_ok)
        w.fail.connect(self._mobile_cloud_sync_fail)
        w.finished.connect(self._mobile_cloud_sync_finished)
        w.start()

    def _mobile_cloud_sync_ok(self, rows):
        self.refresh_mobile_devices()
        if self._mobile_sync_manual:
            QMessageBox.information(self,'مدیریت موبایل‌ها',f'{len(rows or [])} دستگاه از اینترنت دریافت شد.')

    def _mobile_cloud_sync_fail(self, message):
        # Keep local data visible even when ntfy/DNS/SSL is unavailable.
        self.refresh_mobile_devices()
        if self._mobile_sync_manual:
            QMessageBox.warning(
                self,'دستگاه‌های اینترنتی',
                'ارتباط اینترنتی با سرویس همگام‌سازی برقرار نشد.\n\n'
                'برنامه بدون هنگ کردن با اطلاعات ذخیره‌شده محلی ادامه می‌دهد.\n\n'+str(message)
            )

    def _mobile_cloud_sync_finished(self):
        self.mobile_sync_worker=None
        self._mobile_sync_manual=False

    def _auto_discover_mobile_devices(self):
        self._start_mobile_cloud_sync(False)

    def discover_mobile_devices(self):
        self._start_mobile_cloud_sync(True)

    def edit_selected_mobile_owner(self):
        if not hasattr(self,'mobile_devices_table'): return
        row=self.mobile_devices_table.currentRow()
        if row<0:
            QMessageBox.warning(self,'دستگاه موبایل','ابتدا یک دستگاه را از جدول انتخاب کنید.')
            return
        item=self.mobile_devices_table.item(row,0)
        device_id=item.data(Qt.UserRole) if item else ''
        if not device_id:return
        current=str(item.text() or '')
        name,ok=QInputDialog.getText(self,'نام و نام خانوادگی','نام و نام خانوادگی صاحب این گوشی:',text=current)
        if not ok:return
        try:
            if not set_mobile_device_owner(device_id,name.strip()):
                raise RuntimeError('دستگاه پیدا نشد.')
            self.refresh_mobile_devices()
        except Exception as e:
            QMessageBox.critical(self,'دستگاه موبایل',str(e))

    def set_selected_mobile_device(self,enabled):
        if not hasattr(self,'mobile_devices_table'): return
        row=self.mobile_devices_table.currentRow()
        if row<0:
            QMessageBox.warning(self,'دستگاه موبایل','ابتدا یک دستگاه را از جدول انتخاب کنید.')
            return
        item=self.mobile_devices_table.item(row,0)
        device_id=item.data(Qt.UserRole) if item else ''
        if not device_id:return
        if enabled:
            # Kept only for backwards compatibility; deleted devices reconnect as a fresh request.
            try:
                set_mobile_device_enabled(device_id,True)
                self.refresh_mobile_devices()
            except Exception as e:
                QMessageBox.critical(self,'دستگاه موبایل',str(e))
            return
        ans=QMessageBox.question(
            self,'حذف دستگاه',
            'این دستگاه کامل از جدول حذف شود؟\n\nاگر بعداً نام کاربری و رمز صحیح را وارد کند، دوباره به‌صورت خودکار ثبت می‌شود.'
        )
        if ans!=QMessageBox.Yes:return
        try:
            cancel_for_device(device_id)
            delete_mobile_device(device_id)
            self.refresh_mobile_devices()
        except Exception as e:
            QMessageBox.critical(self,'دستگاه موبایل',str(e))

    def decide_selected_mobile_login(self,approved):
        if not hasattr(self,'mobile_devices_table'): return
        row=self.mobile_devices_table.currentRow()
        if row<0:
            QMessageBox.warning(self,'درخواست ورود','ابتدا یک دستگاه را از جدول انتخاب کنید.')
            return

        item=self.mobile_devices_table.item(row,0)
        request_id=item.data(Qt.UserRole+1) if item else ''
        req_status=item.data(Qt.UserRole+2) if item else ''

        if not request_id:
            QMessageBox.warning(
                self,'درخواست ورود',
                'برای این دستگاه هنوز درخواست ورود ثبت نشده است.'
            )
            return

        if req_status!='pending':
            QMessageBox.information(
                self,'درخواست ورود',
                'این درخواست قبلاً تعیین تکلیف شده است.'
            )
            return

        action='تأیید' if approved else 'رد'
        ans=QMessageBox.question(
            self,'درخواست ورود',
            f'ورود این گوشی {action} شود؟'
        )
        if ans!=QMessageBox.Yes:return

        try:
            respond_mobile_login(request_id,approved)
            self.refresh_mobile_devices()
            if approved:
                QMessageBox.information(
                    self,'ورود موبایل',
                    'ورود گوشی تأیید شد و دستگاه فعال شد.\n\n'
                    'گزارش‌های قبلی در پس‌زمینه و بدون نوتیفیکیشن همگام‌سازی می‌شوند.'
                )
            else:
                QMessageBox.information(self,'ورود موبایل','درخواست ورود رد شد.')
        except Exception as e:
            QMessageBox.critical(self,'ورود موبایل',str(e))

    def _wrap(self,layout):
        w=QWidget(); w.setLayout(layout); return w
    def save_settings(self):
        s=settings_mod.load()
        for k,e in self.setting_edits.items():s[k]=e.text().strip()
        s['output_dir']=self.output_edit.text().strip()
        s['auto_report_enabled']=self.auto_daily_check.isChecked()
        s['mobile_notify_enabled']=self.mobile_notify_check.isChecked()
        s['daily_time']='08:00'
        for key,edit,name in [('logo_path',self.logo_edit,'logo'),('prepared_signature',self.prep_sig,'prepared_signature'),('manager_signature',self.mgr_sig,'manager_signature')]:
            val=edit.text().strip()
            if val and Path(val).exists() and not str(val).startswith(str(settings_mod.data_dir()) if hasattr(settings_mod,'data_dir') else '__never__'):
                try: val=settings_mod.copy_managed_asset(val,name)
                except: pass
            s[key]=val
        settings_mod.save(s); self.settings=s; self.refresh_home();
        try:
            from ..core.windows_tasks import sync_tasks; sync_tasks('08:00',s.get('monthly_time','00:10'),s.get('auto_report_enabled',True))
        except Exception:
            pass
        QMessageBox.information(self,'ذخیره شد','تنظیمات ذخیره شد.')
    def export_settings(self):
        p,_=QFileDialog.getSaveFileName(self,'ذخیره تنظیمات','SolicoEnergy_Profile.json','JSON (*.json)')
        if p: settings_mod.export_to(p)
    def import_settings(self):
        p,_=QFileDialog.getOpenFileName(self,'ورود تنظیمات','','JSON (*.json)')
        if p:
            settings_mod.import_from(p); QMessageBox.information(self,'انجام شد','تنظیمات وارد شد. برنامه را یک‌بار باز و بسته کنید.')
    def _start_mobile_report_server(self):
        try:
            if bool(settings_mod.load().get('mobile_notify_enabled',True)):
                start_report_server()
            if hasattr(self,'mobile_status_label'):
                self.mobile_status_label.setText(mobile_status_text())
        except Exception:
            if hasattr(self,'mobile_status_label'):
                self.mobile_status_label.setText('سرور گزارش موبایل فعال نشد. شبکه/Firewall را بررسی کنید.')

    def _start_windows_report_sync(self):
        try:
            start_windows_report_sync()
        except Exception:
            pass

    def choose_firebase_key(self):
        p,_=QFileDialog.getOpenFileName(
            self,
            'انتخاب Service Account کلید Firebase',
            str(Path.home()),
            'Firebase Service Account (*.json);;JSON (*.json);;All files (*.*)'
        )
        if not p:
            return
        try:
            dest=install_service_account(p)
            if hasattr(self,'mobile_status_label'):
                self.mobile_status_label.setText(mobile_status_text())
            QMessageBox.information(
                self,
                'کلید Firebase',
                'کلید Firebase با موفقیت نصب شد.\n\n'+str(dest)
            )
        except Exception as e:
            QMessageBox.critical(self,'خطای کلید Firebase',str(e))

    def enable_mobile_pdf_access(self):
        try:
            result=enable_mobile_firewall()
            if hasattr(self,'mobile_status_label'):
                self.mobile_status_label.setText(mobile_status_text())
            QMessageBox.information(
                self,
                'دسترسی PDF موبایل',
                result.get('message','درخواست Firewall ارسال شد.') +
                '\n\nاگر پنجره Windows باز شد، روی Yes بزنید.'
            )
        except Exception as e:
            QMessageBox.critical(self,'Firewall',str(e))

    def test_mobile_notification(self):
        self.run_task(send_test_notification,'نوتیفیکیشن تست به موبایل ارسال شد',callback=self._mobile_test_done)

    def _mobile_test_done(self,result):
        if hasattr(self,'mobile_status_label'):
            self.mobile_status_label.setText(mobile_status_text())

        mode=(result or {}).get('mode') if isinstance(result,dict) else None
        if mode=='topic':
            count=int((result or {}).get('active_device_count') or 0)
            QMessageBox.information(
                self,'تست موبایل',
                f'نوتیفیکیشن از Firebase ارسال شد. دستگاه فعال ثبت‌شده در لیست: {count}.\n'
                'چند ثانیه گوشی را بررسی کنید.'
            )
        else:
            QMessageBox.information(
                self,'تست موبایل',
                'درخواست تست انجام شد. وضعیت اتصال موبایل را بررسی کنید.'
            )


    def _sources_tab(self):
        w=QWidget(); l=QVBoxLayout(w); self.sources_table=QTableWidget(0,4); self.sources_table.setHorizontalHeaderLabels(['ID','نام منبع','مسیر CSV','فعال']); self.sources_table.horizontalHeader().setSectionResizeMode(2,QHeaderView.Stretch); l.addWidget(self.sources_table)
        row=QHBoxLayout(); a=QPushButton('افزودن CSV'); a.clicked.connect(self.add_source); row.addWidget(a); r=QPushButton('حذف منبع'); r.clicked.connect(self.remove_source); row.addWidget(r); rf=QPushButton('بازخوانی'); rf.clicked.connect(self.refresh_sources); row.addWidget(rf); l.addLayout(row); self.tabs.addTab(w,'منابع داده'); self.refresh_sources()
    def refresh_sources(self):
        rows=db.list_sources(); self.sources_table.setRowCount(len(rows))
        for i,s in enumerate(rows):
            vals=[str(s['id']),s['name'],s['path'],'بله' if s['enabled'] else 'خیر']
            for j,v in enumerate(vals):self.sources_table.setItem(i,j,QTableWidgetItem(v))
        self.refresh_source_combo()
        self.refresh_report_source_combo()
    def add_source(self):
        p,_=QFileDialog.getOpenFileName(self,'انتخاب CSV WinCC','','CSV (*.csv *.txt);;All files (*.*)')
        if not p:return
        name=Path(p).stem; db.add_source(name,p); self.refresh_sources()
    def remove_source(self):
        r=self.sources_table.currentRow()
        if r<0:return
        sid=int(self.sources_table.item(r,0).text())
        if QMessageBox.question(self,'تأیید','منبع و Mapping تگ‌های آن حذف شود؟')==QMessageBox.Yes: db.delete_source(sid); self.refresh_sources()
    def _tags_tab(self):
        w=QWidget(); l=QVBoxLayout(w); top=QHBoxLayout(); top.addWidget(QLabel('منبع:')); self.source_combo=QComboBox(); top.addWidget(self.source_combo); scan=QPushButton('Scan Tags'); scan.clicked.connect(self.scan_source_tags); top.addWidget(scan); load=QPushButton('بارگذاری Mapping'); load.clicked.connect(self.load_mappings); top.addWidget(load)
        l.addLayout(top)
        self.tags_table=QTableWidget(0,7)
        self.tags_table.setHorizontalHeaderLabels(['فعال','Tag در CSV','نوع تجهیز','نام تجهیز','نقش','نام نمایشی','واحد'])
        # FIX21: all tag-mapping columns are manually resizable by dragging the header separators.
        # Previously several sections used Stretch/ResizeToContents, which prevented practical resizing.
        tag_header=self.tags_table.horizontalHeader()
        tag_header.setSectionResizeMode(QHeaderView.Interactive)
        tag_header.setMinimumSectionSize(55)
        tag_header.setStretchLastSection(False)
        tag_header.setSectionsMovable(False)
        initial_widths=[70,360,130,230,240,200,90]
        for col,width in enumerate(initial_widths):
            self.tags_table.setColumnWidth(col,width)
        self.tags_table.setHorizontalScrollMode(QTableWidget.ScrollPerPixel)
        l.addWidget(self.tags_table)
        hint=QLabel('برای تعریف مورد جدید، داخل کشوی «نام تجهیز» یا «نقش» گزینه ➕ افزودن ... را انتخاب کنید.')
        hint.setAlignment(Qt.AlignRight); hint.setStyleSheet('color:#60798c;padding:2px 4px')
        l.addWidget(hint)
        save=QPushButton('ذخیره Mapping تگ‌ها'); save.clicked.connect(self.save_mappings); l.addWidget(save); self.tabs.addTab(w,'تعریف تگ‌ها'); self.refresh_source_combo()

    def _stored_custom_values(self,key):
        vals=settings_mod.load().get(key,[])
        if not isinstance(vals,list): return []
        out=[]
        for v in vals:
            v=str(v).strip()
            if v and v not in out: out.append(v)
        return out

    def _remember_custom_value(self,key,value):
        value=str(value).strip()
        if not value:return
        s=settings_mod.load(); vals=s.get(key,[])
        if not isinstance(vals,list): vals=[]
        vals=[str(v).strip() for v in vals if str(v).strip()]
        if value not in vals: vals.append(value)
        s[key]=vals; settings_mod.save(s); self.settings=s

    def _known_equipment_names(self,current=''):
        out=[]
        for v in self._stored_custom_values('custom_equipment_names'):
            if v not in out: out.append(v)
        try:
            for m in db.mappings():
                v=str(m.get('entity_name') or '').strip()
                if v and v not in out: out.append(v)
        except Exception:
            pass
        current=str(current or '').strip()
        if current and current not in out: out.append(current)
        return out

    def _known_custom_roles(self,current=''):
        builtin={val for _,val in ROLES}; out=[]
        for v in self._stored_custom_values('custom_roles'):
            if v and v not in builtin and v not in out: out.append(v)
        try:
            for m in db.mappings():
                v=str(m.get('role') or '').strip()
                if v and v not in builtin and v not in out: out.append(v)
        except Exception:
            pass
        current=str(current or '').strip()
        if current and current not in builtin and current not in out: out.append(current)
        return out

    def _equipment_combo(self,current=''):
        c=QComboBox(); c.setMinimumWidth(150)
        c.addItem('— بدون نام تجهیز —','')
        for name in self._known_equipment_names(current): c.addItem(name,name)
        c.insertSeparator(c.count())
        c.addItem('➕ افزودن تجهیز جدید',ADD_EQUIPMENT_TOKEN)
        idx=c.findData(str(current or '').strip())
        c.setCurrentIndex(idx if idx>=0 else 0)
        c.setProperty('last_valid_data',c.currentData())
        c.currentIndexChanged.connect(lambda _idx,combo=c:self._equipment_combo_changed(combo))
        return c

    def _role_combo(self,current='ignore'):
        current=str(current or 'ignore').strip() or 'ignore'
        c=QComboBox(); c.setMinimumWidth(180)
        for label,val in ROLES:c.addItem(label,val)
        for val in self._known_custom_roles(current): c.addItem(val,val)
        c.insertSeparator(c.count())
        c.addItem('➕ افزودن نقش جدید',ADD_ROLE_TOKEN)
        idx=c.findData(current)
        if idx<0:
            sep=max(0,c.findData(ADD_ROLE_TOKEN)-1); c.insertItem(sep,current,current); idx=c.findData(current)
        c.setCurrentIndex(idx if idx>=0 else 0)
        c.setProperty('last_valid_data',c.currentData())
        c.currentIndexChanged.connect(lambda _idx,combo=c:self._role_combo_changed(combo))
        return c

    def _restore_combo_previous(self,combo,default_value=''):
        prev=combo.property('last_valid_data')
        if prev is None: prev=default_value
        idx=combo.findData(prev)
        combo.blockSignals(True); combo.setCurrentIndex(idx if idx>=0 else 0); combo.blockSignals(False)

    def _add_value_to_column_combos(self,column,value,add_token):
        for row in range(self.tags_table.rowCount()):
            combo=self.tags_table.cellWidget(row,column)
            if not isinstance(combo,QComboBox) or combo.findData(value)>=0: continue
            add_idx=combo.findData(add_token)
            insert_at=add_idx-1 if add_idx>0 else combo.count()
            combo.insertItem(max(0,insert_at),value,value)

    def _equipment_combo_changed(self,combo):
        data=combo.currentData()
        if data!=ADD_EQUIPMENT_TOKEN:
            combo.setProperty('last_valid_data',data); return
        name,ok=QInputDialog.getText(self,'افزودن تجهیز جدید','نام تجهیز را وارد کنید:')
        name=name.strip() if ok else ''
        if not name:
            self._restore_combo_previous(combo,''); return
        self._remember_custom_value('custom_equipment_names',name)
        self._add_value_to_column_combos(3,name,ADD_EQUIPMENT_TOKEN)
        idx=combo.findData(name)
        combo.blockSignals(True); combo.setCurrentIndex(idx); combo.blockSignals(False); combo.setProperty('last_valid_data',name)

    def _role_combo_changed(self,combo):
        data=combo.currentData()
        if data!=ADD_ROLE_TOKEN:
            combo.setProperty('last_valid_data',data); return
        name,ok=QInputDialog.getText(self,'افزودن نقش جدید','نام نقش را وارد کنید:')
        name=name.strip() if ok else ''
        if not name:
            self._restore_combo_previous(combo,'ignore'); return
        builtin={val for _,val in ROLES}
        if name not in builtin:self._remember_custom_value('custom_roles',name)
        self._add_value_to_column_combos(4,name,ADD_ROLE_TOKEN)
        idx=combo.findData(name)
        combo.blockSignals(True); combo.setCurrentIndex(idx); combo.blockSignals(False); combo.setProperty('last_valid_data',name)

    def refresh_source_combo(self):
        if not hasattr(self,'source_combo'):return
        cur=self.source_combo.currentData(); self.source_combo.clear()
        for s in db.list_sources():self.source_combo.addItem(s['name'],s['id'])
        if cur is not None:
            i=self.source_combo.findData(cur)
            if i>=0:self.source_combo.setCurrentIndex(i)

    def refresh_report_source_combo(self):
        if not hasattr(self,'report_source_combo'):return
        cur=self.report_source_combo.currentData(); self.report_source_combo.blockSignals(True); self.report_source_combo.clear()
        rows=[s for s in db.list_sources() if s.get('enabled')]
        for src in rows:self.report_source_combo.addItem(src['name'],src['id'])
        target=-1
        if cur is not None: target=self.report_source_combo.findData(cur)
        if target<0:
            for i,src in enumerate(rows):
                if str(src.get('name') or '').strip()=='گزارش': target=i; break
        if target<0 and self.report_source_combo.count(): target=0
        if target>=0:self.report_source_combo.setCurrentIndex(target)
        self.report_source_combo.blockSignals(False)

    def _selected_report_source(self):
        if not hasattr(self,'report_source_combo'):return None,None
        sid=self.report_source_combo.currentData()
        name=self.report_source_combo.currentText().strip()
        return sid,name

    def scan_source_tags(self):
        sid=self.source_combo.currentData()
        if not sid:return
        src=next(s for s in db.list_sources() if s['id']==sid)
        self.run_task(lambda:scan_tags(src['path']), 'تگ‌ها شناسایی شدند', callback=self.populate_scanned_tags)
    def populate_scanned_tags(self,tags):
        existing={m['tag_name']:m for m in db.mappings(self.source_combo.currentData())}; self.tags_table.setRowCount(len(tags))
        for i,t in enumerate(tags): self._set_tag_row(i,t,existing.get(t))
    def load_mappings(self):
        ms=db.mappings(self.source_combo.currentData()); self.tags_table.setRowCount(len(ms))
        for i,m in enumerate(ms):self._set_tag_row(i,m['tag_name'],m)
    def _combo(self,items,current):
        c=QComboBox()
        for label,val in items:c.addItem(label,val)
        idx=c.findData(current)
        if idx>=0:c.setCurrentIndex(idx)
        return c
    def _set_tag_row(self,i,tag,m=None):
        m=m or {}
        chk=QTableWidgetItem(); chk.setFlags(chk.flags()|Qt.ItemIsUserCheckable); chk.setCheckState(Qt.Checked if (not m or m.get('enabled')) else Qt.Unchecked); self.tags_table.setItem(i,0,chk)
        tag_item=QTableWidgetItem(tag)
        tag_item.setToolTip(tag)  # Full CSV tag is always available on hover, even before widening the column.
        self.tags_table.setItem(i,1,tag_item)
        self.tags_table.setCellWidget(i,2,self._combo(ENTITY_TYPES,m.get('entity_type','other')))
        self.tags_table.setCellWidget(i,3,self._equipment_combo(m.get('entity_name','')))
        self.tags_table.setCellWidget(i,4,self._role_combo(m.get('role','ignore')))
        self.tags_table.setItem(i,5,QTableWidgetItem(m.get('display_name',''))); self.tags_table.setItem(i,6,QTableWidgetItem(m.get('unit','')))
    def save_mappings(self):
        sid=self.source_combo.currentData()
        if not sid:return
        for i in range(self.tags_table.rowCount()):
            tag=self.tags_table.item(i,1).text(); et=self.tags_table.cellWidget(i,2).currentData(); en=str(self.tags_table.cellWidget(i,3).currentData() or '').strip(); role=str(self.tags_table.cellWidget(i,4).currentData() or 'ignore').strip(); disp=(self.tags_table.item(i,5).text() if self.tags_table.item(i,5) else '').strip(); unit=(self.tags_table.item(i,6).text() if self.tags_table.item(i,6) else '').strip() or UNITS.get(role,''); enabled=self.tags_table.item(i,0).checkState()==Qt.Checked
            db.upsert_mapping({'source_id':sid,'tag_name':tag,'enabled':enabled,'entity_type':et,'entity_name':en,'role':role,'display_name':disp,'unit':unit})
        QMessageBox.information(self,'ذخیره شد','Mapping تگ‌ها ذخیره شد.')
    def _reports_tab(self):
        w=QWidget(); l=QVBoxLayout(w); l.setSpacing(12)
        # Fix RTL groupbox title clipping on Windows DPI scaling
        w.setStyleSheet("QGroupBox { margin-top: 18px; padding-top: 12px; font-weight: bold; } QGroupBox::title { subcontrol-origin: margin; subcontrol-position: top right; padding: 0 8px; }")
        source_row=QHBoxLayout(); source_row.addWidget(QLabel('منبع گزارش:')); self.report_source_combo=QComboBox(); self.report_source_combo.setMinimumWidth(280); source_row.addWidget(self.report_source_combo,1); self.report_source_hint=QLabel('گزارش فقط از داده‌ها و Mapping همین منبع ساخته می‌شود.'); self.report_source_hint.setStyleSheet('color:#60798c'); source_row.addWidget(self.report_source_hint); l.addLayout(source_row); self.refresh_report_source_combo()
        g=QGroupBox(); gv=QVBoxLayout(g); gt=QLabel('گزارش روزانه'); gt.setAlignment(Qt.AlignRight); gt.setObjectName('sectionTitle'); gv.addWidget(gt); f=QFormLayout(); f.setLabelAlignment(Qt.AlignRight); f.setFieldGrowthPolicy(QFormLayout.AllNonFixedFieldsGrow); self.daily_date=QDateEdit(QDate.currentDate().addDays(-1)); self.daily_date.setCalendarPopup(True); self.daily_j=QLineEdit(); self.daily_j.setPlaceholderText("1405/07/01"); self.daily_j.setLayoutDirection(Qt.LeftToRight); self.daily_j.editingFinished.connect(self._apply_daily_jalali); self.daily_date.dateChanged.connect(self.update_jalali_labels); f.addRow('تاریخ میلادی',self.daily_date); f.addRow('تاریخ شمسی',self.daily_j); b=QPushButton('ساخت / بازسازی PDF روزانه'); b.clicked.connect(self.make_daily); f.addRow(b); gv.addLayout(f); l.addWidget(g)
        mg=QGroupBox(); mv=QVBoxLayout(mg); mt=QLabel('گزارش ماهانه شمسی'); mt.setAlignment(Qt.AlignRight); mt.setObjectName('sectionTitle'); mv.addWidget(mt); mf=QFormLayout(); mf.setLabelAlignment(Qt.AlignRight); mf.setFieldGrowthPolicy(QFormLayout.AllNonFixedFieldsGrow); today=date.today(); jy,jm=previous_jalali_month_for(today); self.month_y=QSpinBox(); self.month_y.setRange(1300,1600); self.month_y.setValue(jy); self.month_m=QSpinBox(); self.month_m.setRange(1,12); self.month_m.setValue(jm); mf.addRow('سال شمسی',self.month_y); mf.addRow('ماه شمسی',self.month_m); mb=QPushButton('ساخت / بازسازی PDF ماهانه'); mb.clicked.connect(self.make_monthly); mf.addRow(mb); mv.addLayout(mf); l.addWidget(mg)

        cg=QGroupBox(); cv=QVBoxLayout(cg); ct=QLabel('مقایسه عملکرد'); ct.setAlignment(Qt.AlignRight); ct.setObjectName('sectionTitle'); cv.addWidget(ct)
        cr=QHBoxLayout(); cr.addWidget(QLabel('نوع مقایسه:')); self.compare_mode=QComboBox(); self.compare_mode.addItem('روز با روز','day'); self.compare_mode.addItem('هفته با هفته (۷ روزه)','week'); self.compare_mode.addItem('ماه با ماه (شمسی)','month'); cr.addWidget(self.compare_mode)
        self.compare_date_label=QLabel('تاریخ مقایسه:'); cr.addWidget(self.compare_date_label); self.compare_date=QDateEdit(QDate.currentDate().addDays(-1)); self.compare_date.setCalendarPopup(True); cr.addWidget(self.compare_date)
        self.compare_month_label=QLabel('ماه موردنظر:'); cr.addWidget(self.compare_month_label); cy,cm=previous_jalali_month_for(date.today()); self.compare_month_y=QSpinBox(); self.compare_month_y.setRange(1300,1600); self.compare_month_y.setValue(cy); self.compare_month_m=QSpinBox(); self.compare_month_m.setRange(1,12); self.compare_month_m.setValue(cm); cr.addWidget(self.compare_month_y); cr.addWidget(QLabel('/')); cr.addWidget(self.compare_month_m)
        cb=QPushButton('مقایسه'); cb.setObjectName('smallPrimary'); cb.clicked.connect(self.run_comparison); cr.addWidget(cb)
        export_b=QPushButton('خروجی PDF مقایسه'); export_b.clicked.connect(self.make_comparison_report); cr.addWidget(export_b); cr.addStretch(); cv.addLayout(cr)
        self.compare_mode.currentIndexChanged.connect(self._comparison_mode_changed)
        self.compare_caption=QLabel(''); self.compare_caption.setAlignment(Qt.AlignRight); self.compare_caption.setStyleSheet('color:#60798c;padding:4px'); cv.addWidget(self.compare_caption)
        self.report_source_combo.currentIndexChanged.connect(self._report_source_changed)
        l.addWidget(cg,1); self.tabs.addTab(w,'گزارش‌ها'); self.update_jalali_labels(); self._comparison_mode_changed(); self._report_source_changed()
    def update_jalali_labels(self):
        q=self.daily_date.date(); d=date(q.year(),q.month(),q.day()); self.daily_j.setText(format_jalali(d))
    def _apply_daily_jalali(self):
        try:
            d=parse_jalali_date(self.daily_j.text())
            self.daily_date.setDate(QDate(d.year,d.month,d.day))
            self.daily_j.setText(format_jalali(d))
            self.daily_j.setStyleSheet('')
            return True
        except ValueError as e:
            self.daily_j.setStyleSheet('border:1px solid #c62828;')
            self.statusBar().showMessage(str(e),10000)
            return False

    def _fmt_cmp(self, value, kind):
        if value is None:return '—'
        if kind=='pf':return f'{value:.3f}'
        if kind=='share':return f'{value:.1f}%'
        if kind=='kw':return f'{value:,.0f} kW'
        return f'{value:,.0f} kWh'

    def _comparison_mode_changed(self):
        mode=self.compare_mode.currentData() or 'day'; is_month=mode=='month'
        self.compare_date.setVisible(not is_month); self.compare_date_label.setVisible(not is_month)
        self.compare_month_label.setVisible(is_month); self.compare_month_y.setVisible(is_month); self.compare_month_m.setVisible(is_month)
        if mode=='day': self.compare_date_label.setText('تاریخ مقایسه:')
        elif mode=='week': self.compare_date_label.setText('پایان هفته:')
        self.run_comparison()

    def _comparison_base_day(self):
        mode=self.compare_mode.currentData() or 'day'
        if mode=='month':
            _,end=jalali_month_bounds(self.compare_month_y.value(),self.compare_month_m.value()); return end,mode
        q=self.compare_date.date(); return date(q.year(),q.month(),q.day()),mode

    def _report_source_changed(self):
        sid,sname=self._selected_report_source()
        if hasattr(self,'report_source_hint'):
            if sid is None:
                self.report_source_hint.setText('ابتدا یک منبع داده انتخاب کنید.')
            elif is_energy_source(sid,sname):
                self.report_source_hint.setText('منبع انرژی: قالب فعلی گزارش انرژی بدون تغییر استفاده می‌شود.')
            else:
                self.report_source_hint.setText('منبع پارامتری: PDF پویا از Tagهای فعال، نام تجهیز، نقش، نام نمایشی و واحد همین منبع ساخته می‌شود.')
        self.run_comparison()

    def run_comparison(self):
        try:
            d,mode=self._comparison_base_day(); sid,sname=self._selected_report_source()
            if sid is None:
                self.compare_caption.setText('ابتدا از بالای صفحه یک «منبع گزارش» انتخاب کنید.'); return
            if not is_energy_source(sid,sname):
                self.compare_caption.setText('برای منابع پارامتری، گزارش روزانه/ماهانه به‌صورت پویا ساخته می‌شود. مقایسه عملکرد فعلاً مخصوص منبع انرژی است.')
                return
            data=comparison_summary(d,mode,source_id=sid)
            prefix=f'منبع: {sname}  |  ' if sname else ''
            if mode=='month':
                self.compare_caption.setText(prefix+f"ماه فعلی: {format_jalali(data['current_start'])[:7]}  |  ماه قبل: {format_jalali(data['previous_start'])[:7]}  |  برای جزئیات کامل از «خروجی PDF مقایسه» استفاده کنید.")
            elif mode=='week':
                self.compare_caption.setText(prefix+f"دوره فعلی: {format_jalali(data['current_start'])} تا {format_jalali(data['current_end'])}  |  دوره قبل: {format_jalali(data['previous_start'])} تا {format_jalali(data['previous_end'])}  |  برای جزئیات کامل از «خروجی PDF مقایسه» استفاده کنید.")
            else:
                self.compare_caption.setText(prefix+f"روز فعلی: {format_jalali(data['current_end'])}  |  روز قبل: {format_jalali(data['previous_end'])}  |  برای جزئیات کامل از «خروجی PDF مقایسه» استفاده کنید.")
        except Exception as e:
            self.compare_caption.setText(f'خطا در محاسبه مقایسه: {e}')

    def make_comparison_report(self):
        try: d,mode=self._comparison_base_day()
        except Exception as e: QMessageBox.warning(self,'مقایسه',str(e)); return
        sid,sname=self._selected_report_source()
        if sid is None: QMessageBox.warning(self,'مقایسه','ابتدا یک منبع گزارش انتخاب کنید.'); return
        if not is_energy_source(sid,sname):
            QMessageBox.information(self,'مقایسه','مقایسه عملکرد فعلاً برای گزارش انرژی است. برای این منبع از گزارش روزانه یا ماهانه پارامتری استفاده کنید.'); return
        self.run_task(lambda:generate_comparison(d,mode,source_id=sid,source_name=sname),'گزارش مقایسه ساخته شد',open_result=True,mobile_context={'kind':'comparison','report_date':d,'source_id':sid,'compare_mode':mode})

    # AUTO_REPORT_HOTFIX_R2
    def _auto_report_slot(self, now, force=False):
        # Bucket the current time into retry windows.  This makes wake/resume robust:
        # if Windows wakes at 08:06, 08:24 or 09:13 we try immediately rather than
        # waiting for the exact Task Scheduler minute.
        prefix=now.strftime('%Y-%m-%d-')
        if now.hour==8:
            if now.minute<10: return prefix+'0800'
            if now.minute<30: return prefix+'0810'
            return prefix+'0830'
        if now.hour>8:
            bucket=0 if now.minute<30 else 30
            return prefix+f'late-{now.hour:02d}{bucket:02d}'
        return None

    def _in_app_auto_report_tick(self, force=False):
        try:
            cfg=settings_mod.load()
            if not bool(cfg.get('auto_report_enabled',True)):
                return
            now=datetime.now()
            if now.hour<8:
                return
            target=date.today()-timedelta(days=1)
            if db.report_exists('daily',target.isoformat()):
                return
            slot=self._auto_report_slot(now,bool(force))
            if not slot or slot in self._auto_report_attempt_slots:
                return
            if self.worker and self.worker.isRunning():
                return
            if self.auto_report_worker and self.auto_report_worker.isRunning():
                return
            self._auto_report_attempt_slots.add(slot)

            def job():
                from ..scheduler import run_daily
                run_daily()
                return True

            self.auto_report_worker=Worker(job)
            def ok(_):
                self.statusBar().showMessage('بررسی گزارش خودکار انجام شد.',5000)
                self.refresh_home()
            def fail(err):
                self.statusBar().showMessage('گزارش خودکار اجرا نشد: '+str(err)[:180],8000)
            self.auto_report_worker.ok.connect(ok)
            self.auto_report_worker.fail.connect(fail)
            self.auto_report_worker.start()
        except Exception:
            # Never disturb the operator UI because of a background scheduler check.
            pass

    def _in_app_monthly_report_tick(self, force=False):
        try:
            now=datetime.now()
            if now.hour==0 and now.minute<10 and not force:
                return
            from ..core.jalali import gregorian_to_jalali
            jy,jm,jd=gregorian_to_jalali(now.year,now.month,now.day)
            if jd!=1:
                return
            py,pm=previous_jalali_month_for(date.today())
            key=f'{py:04d}-{pm:02d}'
            if db.report_exists('monthly',key):
                return
            slot='monthly-'+date.today().isoformat()
            if slot in self._auto_report_attempt_slots:
                return
            if self.worker and self.worker.isRunning():
                return
            if self.auto_report_worker and self.auto_report_worker.isRunning():
                return
            self._auto_report_attempt_slots.add(slot)
            def job():
                from ..scheduler import run_monthly
                run_monthly()
                return True
            self.auto_report_worker=Worker(job)
            self.auto_report_worker.ok.connect(lambda _:(self.statusBar().showMessage('بررسی گزارش ماهانه خودکار انجام شد.',5000),self.refresh_home()))
            self.auto_report_worker.fail.connect(lambda err:self.statusBar().showMessage('گزارش ماهانه خودکار اجرا نشد: '+str(err)[:180],8000))
            self.auto_report_worker.start()
        except Exception:
            pass

    def _retry_pending_mobile_notifications(self):
        try:
            if self.worker and self.worker.isRunning():
                return
            if self.auto_report_worker and self.auto_report_worker.isRunning():
                return
            def job():
                from ..scheduler import retry_pending_notifications
                return retry_pending_notifications()
            self.auto_report_worker=Worker(job)
            def ok(result):
                if isinstance(result,dict) and int(result.get('sent') or 0)>0:
                    self.statusBar().showMessage('اعلان موبایلِ در انتظار با موفقیت ارسال شد.',6000)
            def fail(_):
                pass
            self.auto_report_worker.ok.connect(ok)
            self.auto_report_worker.fail.connect(fail)
            self.auto_report_worker.start()
        except Exception:
            pass

    def _sync_scheduled_tasks(self):
        try:
            s=settings_mod.load()
            from ..core.windows_tasks import sync_tasks
            sync_tasks('08:00',s.get('monthly_time','00:10'),s.get('auto_report_enabled',True))
            self.schedule_notice.setText('زمان‌بندی ثبت شد؛ گزارش روز قبل ساعت ۸ صبح.')
        except Exception as e:
            self.schedule_notice.setText('زمان‌بندی تأیید نشده است؛ دکمه اصلاح زمان‌بندی را بزنید. '+str(e))

    def _auto_report_status(self):
        try:
            p=settings_mod.data_dir()/'auto_report_status.json'
            if not p.exists():return None
            obj=json.loads(p.read_text(encoding='utf-8'))
            return obj if isinstance(obj,dict) else None
        except Exception:return None

    def make_daily(self):
        if not self._apply_daily_jalali():
            QMessageBox.warning(self,'تاریخ نامعتبر','تاریخ شمسی معتبر وارد کنید؛ مانند ۱۴۰۵/۰۷/۰۱.')
            return
        sid,sname=self._selected_report_source()
        if sid is None: QMessageBox.warning(self,'گزارش روزانه','ابتدا یک منبع گزارش انتخاب کنید.'); return
        q=self.daily_date.date(); d=date(q.year(),q.month(),q.day())
        energy=is_energy_source(sid,sname)
        if energy:
            as_of=datetime.now() if d==date.today() else None
            msg='گزارش روزانه موقت تا این لحظه ساخته شد' if as_of is not None else 'گزارش روزانه ساخته شد'
            self.run_task(lambda:generate_daily(d,True,source_id=sid,source_name=sname,as_of=as_of),msg,open_result=True,mobile_context={'kind':'daily','report_date':d,'source_id':sid,'as_of':as_of})
        else:
            self.run_task(lambda:generate_generic_daily(d,True,source_id=sid,source_name=sname),'گزارش روزانه ساخته شد',open_result=True,mobile_context={'kind':'generic','report_date':d,'source_id':sid})
    def make_monthly(self):
        sid,sname=self._selected_report_source()
        if sid is None: QMessageBox.warning(self,'گزارش ماهانه','ابتدا یک منبع گزارش انتخاب کنید.'); return
        fn=generate_monthly if is_energy_source(sid,sname) else generate_generic_monthly
        self.run_task(lambda:fn(self.month_y.value(),self.month_m.value(),True,source_id=sid,source_name=sname),'گزارش ماهانه ساخته شد',open_result=True,mobile_context={'kind':'monthly' if is_energy_source(sid,sname) else 'generic','jalali_year':self.month_y.value(),'jalali_month':self.month_m.value(),'source_id':sid})
    def _updates_tab(self):
        w=QWidget(); l=QVBoxLayout(w); l.addWidget(QLabel('<b>نسخه نصب‌شده: 1.0.27</b><br><br>آپدیت‌ها به‌صورت بسته نسخه‌ای نصب می‌شوند و تنظیمات، آرشیو SQLite، لوگو، امضا و Mapping تگ‌ها دست‌نخورده باقی می‌مانند. نسخه قبلی نیز برای Rollback نگه داشته می‌شود.'))
        b=QPushButton('انتخاب بسته Update (.zip/.seupdate)'); b.clicked.connect(self.stage_update); l.addWidget(b); l.addStretch(); self.tabs.addTab(w,'به‌روزرسانی')
    def stage_update(self):
        p,_=QFileDialog.getOpenFileName(self,'انتخاب Update','','Update (*.zip *.seupdate)')
        if not p:return
        from ..core.update_manager import stage_update
        try:
            v=stage_update(p); self._add_activity(f'آپدیت نسخه {v} برای نصب آماده شد.','update','ok'); QMessageBox.information(self,'آماده نصب',f'نسخه {v} آماده شد. برنامه را ببندید و دوباره اجرا کنید.')
        except Exception as e: QMessageBox.critical(self,'خطا',str(e))
    def _open_path(self, path):
        p=Path(path)
        p.mkdir(parents=True,exist_ok=True) if not p.suffix else None
        opened=QDesktopServices.openUrl(QUrl.fromLocalFile(str(p)))
        if not opened and os.name=='nt':
            os.startfile(str(p))
        return opened

    def open_output(self):
        """Open the folder that actually contains reports, not an obsolete project folder."""
        try:
            # Prefer the parent of the latest report that still exists on disk. This keeps the
            # Home button aligned with reports successfully produced from the Reports tab.
            latest=None
            with db.connect() as con:
                r=con.execute('SELECT path FROM generated_reports ORDER BY created_at DESC, id DESC LIMIT 1').fetchone()
                if r and r['path']:
                    latest=Path(r['path'])
            if latest and latest.exists():
                target=latest.parent
            else:
                root=settings_mod.ensure_output_dir()
                target=root/'Daily'
                target.mkdir(parents=True,exist_ok=True)
                (root/'Monthly').mkdir(parents=True,exist_ok=True)
            if hasattr(self,'output_edit'):
                self.output_edit.setText(str(settings_mod.ensure_output_dir()))
            self._open_path(target)
        except Exception as e:
            QMessageBox.critical(self,'خطا در پوشه گزارش‌ها',f'پوشه گزارش‌ها قابل ایجاد یا باز کردن نیست:\n{e}')
    def _show_import_result(self, result):
        # Operator-facing summary. Technical counters stay inside the importer, not in the popup.
        if isinstance(result,list):
            payloads=[p for item in result if isinstance(item,(tuple,list)) and len(item)==2 for p in [item[1]] if isinstance(p,dict)]
            if not payloads:
                self._add_activity('هیچ منبع داده فعالی برای ورود اطلاعات وجود نداشت.','import','warn')
                QMessageBox.warning(self,'ورود داده‌ها','هیچ منبع داده فعالی تعریف نشده است.'); return
            if len(payloads)==1: result=payloads[0]
            else:
                inserted=sum(int(p.get('inserted',0) or 0) for p in payloads if not p.get('error'))
                errors=[str(p.get('error')) for p in payloads if p.get('error')]
                if errors:
                    self._add_activity('ورود داده با خطا پایان یافت.','import','warn'); QMessageBox.warning(self,'ورود داده‌ها','بخشی از داده‌ها وارد نشد. جزئیات را در لاگ برنامه بررسی کنید.'); return
                result={'inserted':inserted}
        if not isinstance(result,dict):
            self._add_activity('عملیات ورود داده پایان یافت.','import','ok'); QMessageBox.information(self,'ورود داده‌ها','عملیات ورود داده پایان یافت.'); return
        if result.get('error'):
            self._add_activity('ورود داده با خطا مواجه شد.','import','warn'); QMessageBox.critical(self,'خطا در ورود داده‌ها',str(result.get('error'))); return
        inserted=int(result.get('inserted',0) or 0)
        if inserted:
            msg=f'{inserted:,} رکورد جدید با موفقیت ثبت شد.'
            self._add_activity(msg,'import','ok'); QMessageBox.information(self,'ورود داده‌ها','ورود داده‌ها با موفقیت انجام شد.\n'+msg)
        else:
            msg='فایل CSV بررسی شد؛ داده جدیدی برای ثبت وجود نداشت.'
            self._add_activity(msg,'import','ok'); QMessageBox.information(self,'ورود داده‌ها','فایل با موفقیت بررسی شد.\nداده جدیدی برای ثبت وجود نداشت.')

    def home_import_csv(self):
        """Pick a CSV explicitly, then import it into the existing mapped source."""
        try:
            sources=[x for x in db.list_sources() if x.get('enabled')]
            # Do not seed the native dialog with an old/offline E: path. A real local folder
            # makes the picker appear reliably on operator PCs.
            start=str(Path.home()/'Documents')
            if not Path(start).exists():
                start=str(Path.home())
            p,_=QFileDialog.getOpenFileName(self,'انتخاب فایل CSV جدید WinCC',start,'CSV (*.csv *.txt);;All files (*.*)')
            if not p:
                return
            p=str(Path(p))
            if not sources:
                sid=db.add_source(Path(p).stem,p)
                self.refresh_sources()
                QMessageBox.warning(self,'منبع داده اضافه شد','فایل به‌عنوان منبع جدید ثبت شد، اما هنوز Mapping تگ‌ها برای آن تعریف نشده است. ابتدا از تب «تعریف تگ‌ها» Mapping را مشخص کنید.')
                return
            if len(sources)==1:
                src=sources[0]
            else:
                labels=[f"{x['name']}  (ID {x['id']})" for x in sources]
                choice,ok=QInputDialog.getItem(self,'انتخاب منبع','این فایل مربوط به کدام منبع است؟',labels,0,False)
                if not ok:
                    return
                src=sources[labels.index(choice)]
            # Preserve source ID => preserve all verified tag mappings.
            db.update_source(src['id'],src['name'],p,1)
            self.refresh_sources()
            self.run_task(lambda:import_source(src['id']),'ورود داده‌ها تمام شد',callback=self._show_import_result)
        except Exception:
            QMessageBox.critical(self,'خطا در انتخاب/ورود CSV',traceback.format_exc()[-2500:])

    def _latest_reportable_day(self):
        """Find the newest completed day with a real factory active-energy end snapshot."""
        try:
            maps=[m for m in db.mappings(enabled_only=True) if m.get('entity_type')=='factory' and m.get('role')=='energy_active']
            max_epoch=None
            with db.connect() as con:
                for m in maps:
                    r=con.execute('SELECT MAX(ts_epoch) AS mx FROM records WHERE source_id=? AND tag_name=?',(m['source_id'],m['tag_name'])).fetchone()
                    if r and r['mx'] is not None:
                        max_epoch=max(float(r['mx']), max_epoch if max_epoch is not None else float(r['mx']))
            if max_epoch is None:
                return None
            # A daily report needs the following midnight snapshot, so begin one day earlier.
            candidate=min(datetime.fromtimestamp(max_epoch).date()-timedelta(days=1), date.today()-timedelta(days=1))
            from ..services.calculations import day_summary
            for n in range(0,63):
                d=candidate-timedelta(days=n)
                if day_summary(d).get('has_daily_snapshot'):
                    return d
            return None
        except Exception:
            return None

    def make_home_daily(self):
        """Build today's report; during the day it is explicitly a temporary 00:00-to-now report."""
        d=date.today()
        self.daily_date.setDate(QDate(d.year,d.month,d.day))
        self.update_jalali_labels()
        self.make_daily()

    def do_import(self):
        # Kept for scheduler/legacy calls; Home uses the explicit file-picker flow above.
        self.run_task(import_all,'ورود داده‌ها تمام شد',callback=self._show_import_result)

    def run_task(self,fn,msg,callback=None,open_result=False,mobile_context=None):
        if self.worker and self.worker.isRunning(): return
        self.progress.show(); self.statusBar().showMessage('در حال انجام...'); self.worker=Worker(fn)
        def ok(res):
            self.progress.hide(); self.statusBar().showMessage(msg,5000)
            if callback:
                callback(res)
            elif open_result and isinstance(res,str):
                p=Path(res)
                self._add_activity(f'{msg}: {p.name}','report','ok')
                if bool(settings_mod.load().get('mobile_notify_enabled',True)):
                    notify_report_async(p,msg,**(mobile_context or {}))
                QMessageBox.information(self,'انجام شد',msg)
                opened=QDesktopServices.openUrl(QUrl.fromLocalFile(res))
                if not opened and os.name=='nt' and p.exists(): os.startfile(res)
            else:
                self._add_activity(msg,'task','ok'); QMessageBox.information(self,'انجام شد',msg)
            self.refresh_home()
        def fail(err):
            self.progress.hide(); self.statusBar().showMessage('خطا')
            text=str(err or 'خطای نامشخص')
            self._add_activity('عملیات با خطا متوقف شد.','task','warn')
            QMessageBox.critical(self,'خطا',text[:1200])
        self.worker.ok.connect(ok); self.worker.fail.connect(fail); self.worker.start()

