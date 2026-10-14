package ir.solico.energyreport.lite;

import android.Manifest;
import android.app.Activity;
import android.app.NotificationChannel;
import android.app.NotificationManager;
import android.content.Context;
import android.content.Intent;
import android.content.SharedPreferences;
import android.content.pm.PackageManager;
import android.graphics.Color;
import android.net.Uri;
import android.net.ConnectivityManager;
import android.net.NetworkInfo;
import android.os.Build;
import android.os.Bundle;
import android.os.Handler;
import android.os.Looper;
import android.provider.Settings;
import android.text.InputType;
import android.view.Gravity;
import android.view.View;
import android.view.ViewGroup;
import android.webkit.JavascriptInterface;
import android.webkit.WebSettings;
import android.webkit.WebView;
import android.webkit.WebViewClient;
import android.widget.Button;
import android.widget.EditText;
import android.widget.ImageButton;
import android.widget.LinearLayout;
import android.widget.TextView;
import android.widget.Toast;

import androidx.core.content.FileProvider;

import com.google.firebase.messaging.FirebaseMessaging;

import org.json.JSONObject;

import java.io.BufferedInputStream;
import java.io.File;
import java.io.FileOutputStream;
import java.net.HttpURLConnection;
import java.net.URL;
import java.nio.charset.StandardCharsets;
import java.security.MessageDigest;
import java.util.HashMap;
import java.util.Locale;
import java.util.Map;

public class MainActivity extends Activity {
    private static final int NOTIFICATION_PERMISSION=502;
    private static final int UPDATE_APK_PICKER=804;
    public static final String REPORT_TOPIC="solico_reports";
    public static final String CHANNEL_ID="solico_reports_channel";
    private static final String LOCAL_USERNAME="REDACTED";
    private static final String LOCAL_PASSWORD_SHA256="REDACTED_REVIEW_ONLY";
    private static final String LOGIN_PREFS="solico_local_login", LOGIN_KEY="logged_in", LOGIN_APPROVED_KEY="approved_by_windows";
    private static final String FCM_PREFS="solico_fcm_state", FCM_RESET_KEY="fcm_reset_v100_done";
    private static final int MAX_FCM_RETRIES=8;
    private WebView webView; private boolean appInitialized=false; private Intent pendingLaunchIntent;
    private final Handler fcmHandler=new Handler(Looper.getMainLooper()); private int fcmRetryCount=0;
    private final Handler registryHandler=new Handler(Looper.getMainLooper()); private int registryGeneration=0;
    private volatile String fcmStatusText="در حال اتصال اعلان‌ها...";

    @Override public void onCreate(Bundle savedInstanceState){super.onCreate(savedInstanceState);createNotificationChannel();requestNotificationPermissionIfNeeded();FirebaseMessaging.getInstance().setAutoInitEnabled(true);DailyReportCatchupWorker.schedule(this);pendingLaunchIntent=getIntent();if(!isLocallyLoggedIn()||!DeviceRegistrar.isEnabledLocal(this)){showLoginScreen();return;}initializeAuthenticatedApp();}
    private void initializeAuthenticatedApp(){if(appInitialized){handleNotificationIntent(pendingLaunchIntent);DeviceRegistrar.pollArchiveOffers(this);scheduleReliableDeviceRegistration();UpdateManager.checkForUpdate(this,false);return;}appInitialized=true;ensureFirebaseMessaging();scheduleReliableDeviceRegistration();webView=new WebView(this);setContentView(webView);WebSettings s=webView.getSettings();s.setJavaScriptEnabled(true);s.setDomStorageEnabled(true);s.setAllowFileAccess(true);s.setAllowContentAccess(true);s.setSupportZoom(true);s.setBuiltInZoomControls(true);s.setDisplayZoomControls(false);webView.setWebViewClient(new WebViewClient());webView.addJavascriptInterface(new Bridge(),"AndroidBridge");webView.loadUrl("file:///android_asset/index.html");DeviceRegistrar.maybeOfferArchive(this);DeviceRegistrar.pollArchiveOffers(this);handleNotificationIntent(pendingLaunchIntent);pendingLaunchIntent=null;UpdateManager.checkForUpdate(this,true);}
    @Override protected void onNewIntent(Intent intent){super.onNewIntent(intent);setIntent(intent);pendingLaunchIntent=intent;if(isLocallyLoggedIn()&&appInitialized)handleNotificationIntent(intent);}

    private void showLoginScreen(){
        appInitialized=false;
        webView=null;

        LinearLayout root=new LinearLayout(this);
        root.setOrientation(LinearLayout.VERTICAL);
        root.setGravity(Gravity.CENTER);
        root.setPadding(dp(28),dp(34),dp(28),dp(34));
        root.setBackgroundColor(Color.rgb(244,247,250));
        root.setLayoutDirection(View.LAYOUT_DIRECTION_RTL);

        LinearLayout card=new LinearLayout(this);
        card.setOrientation(LinearLayout.VERTICAL);
        card.setPadding(dp(22),dp(24),dp(22),dp(24));
        card.setBackgroundColor(Color.WHITE);
        card.setLayoutDirection(View.LAYOUT_DIRECTION_RTL);
        card.setLayoutParams(new LinearLayout.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT,ViewGroup.LayoutParams.WRAP_CONTENT));

        TextView title=new TextView(this);
        title.setText("ورود به Solico");
        title.setTextSize(23);
        title.setTextColor(Color.rgb(21,62,92));
        title.setGravity(Gravity.CENTER);
        title.setTypeface(null,android.graphics.Typeface.BOLD);
        title.setPadding(0,0,0,dp(8));
        card.addView(title);

        TextView help=new TextView(this);
        help.setText("نام کاربری و رمز عبور را وارد کنید. پس از ورود، گوشی به‌صورت خودکار در نرم‌افزار ویندوز ثبت می‌شود.");
        help.setTextSize(14);
        help.setTextColor(Color.rgb(93,117,136));
        help.setGravity(Gravity.CENTER);
        help.setPadding(0,0,0,dp(18));
        card.addView(help);

        EditText username=new EditText(this);
        username.setHint("نام کاربری");
        username.setSingleLine(true);
        username.setLayoutDirection(View.LAYOUT_DIRECTION_LTR);
        username.setInputType(InputType.TYPE_CLASS_TEXT);
        LinearLayout.LayoutParams up=new LinearLayout.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT,dp(54));
        up.setMargins(0,0,0,dp(12));
        username.setLayoutParams(up);
        card.addView(username);

        LinearLayout passwordRow=new LinearLayout(this);
        passwordRow.setOrientation(LinearLayout.HORIZONTAL);
        passwordRow.setGravity(Gravity.CENTER_VERTICAL);
        passwordRow.setLayoutDirection(View.LAYOUT_DIRECTION_LTR);
        LinearLayout.LayoutParams rowParams=new LinearLayout.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT,dp(54));
        rowParams.setMargins(0,0,0,dp(16));
        passwordRow.setLayoutParams(rowParams);

        EditText password=new EditText(this);
        password.setHint("رمز عبور");
        password.setSingleLine(true);
        password.setLayoutDirection(View.LAYOUT_DIRECTION_LTR);
        password.setInputType(InputType.TYPE_CLASS_TEXT|InputType.TYPE_TEXT_VARIATION_PASSWORD);
        password.setLayoutParams(new LinearLayout.LayoutParams(0,ViewGroup.LayoutParams.MATCH_PARENT,1f));
        passwordRow.addView(password);

        ImageButton passwordEye=new ImageButton(this);
        passwordEye.setImageResource(R.drawable.ic_visibility);
        passwordEye.setContentDescription("نمایش رمز عبور");
        passwordEye.setBackgroundColor(Color.TRANSPARENT);
        passwordEye.setColorFilter(Color.rgb(49,83,107));
        passwordEye.setPadding(dp(12),dp(12),dp(12),dp(12));
        passwordEye.setLayoutParams(new LinearLayout.LayoutParams(dp(52),ViewGroup.LayoutParams.MATCH_PARENT));
        final boolean[] passwordVisible={false};
        passwordEye.setOnClickListener(v->{
            passwordVisible[0]=!passwordVisible[0];
            int pos=password.getSelectionStart();
            password.setInputType(InputType.TYPE_CLASS_TEXT|
                    (passwordVisible[0]?InputType.TYPE_TEXT_VARIATION_VISIBLE_PASSWORD:InputType.TYPE_TEXT_VARIATION_PASSWORD));
            password.setSelection(Math.max(0,Math.min(pos,password.length())));
            passwordEye.setImageResource(passwordVisible[0]?R.drawable.ic_visibility_off:R.drawable.ic_visibility);
            passwordEye.setContentDescription(passwordVisible[0]?"مخفی کردن رمز عبور":"نمایش رمز عبور");
        });
        passwordRow.addView(passwordEye);
        card.addView(passwordRow);

        Button login=new Button(this);
        login.setText("ورود");
        login.setTextColor(Color.WHITE);
        login.setBackgroundColor(Color.rgb(22,131,216));
        login.setAllCaps(false);
        login.setMinHeight(dp(52));
        card.addView(login);

        TextView status=new TextView(this);
        status.setTextColor(Color.rgb(177,42,42));
        status.setGravity(Gravity.CENTER);
        status.setPadding(0,dp(12),0,0);
        card.addView(status);

        root.addView(card);
        setContentView(root);

        login.setOnClickListener(v->{
            String u=username.getText().toString().trim();
            String p=password.getText().toString().trim();
            boolean ok=LOCAL_USERNAME.equalsIgnoreCase(u)&&LOCAL_PASSWORD_SHA256.equalsIgnoreCase(sha256(p));
            if(!ok){
                status.setTextColor(Color.rgb(177,42,42));
                status.setText("نام کاربری یا رمز عبور اشتباه است.");
                return;
            }

            getSharedPreferences(LOGIN_PREFS,MODE_PRIVATE)
                    .edit()
                    .putBoolean(LOGIN_KEY,true)
                    .remove(LOGIN_APPROVED_KEY)
                    .apply();
            getSharedPreferences(FCM_PREFS,MODE_PRIVATE)
                    .edit().putBoolean(FCM_RESET_KEY,false).apply();

            // A fresh credential login creates a new session. A device removed in Windows
            // may reconnect, but stale background registration cannot resurrect it.
            DeviceRegistrar.reconnectAfterLogin(MainActivity.this);
            Toast.makeText(MainActivity.this,"ورود انجام شد",Toast.LENGTH_SHORT).show();
            initializeAuthenticatedApp();
        });
    }

    private int dp(int v){return Math.round(v*getResources().getDisplayMetrics().density);} private boolean isLocallyLoggedIn(){SharedPreferences p=getSharedPreferences(LOGIN_PREFS,MODE_PRIVATE);return p.getBoolean(LOGIN_KEY,false);} private String currentUsername(){return isLocallyLoggedIn()?LOCAL_USERNAME:"";}
    private String sha256(String value){try{MessageDigest d=MessageDigest.getInstance("SHA-256");byte[] out=d.digest(value.getBytes(StandardCharsets.UTF_8));StringBuilder sb=new StringBuilder();for(byte b:out)sb.append(String.format(Locale.ROOT,"%02x",b));return sb.toString();}catch(Exception e){return "";}}
    private void logoutFromApp(){
        // "Exit" means close the app only.
        // Keep the approved session so this already-approved phone does not
        // ask for username/password or Windows approval again on next launch.
        pendingLaunchIntent=null;
        if(Build.VERSION.SDK_INT>=21){
            finishAndRemoveTask();
        }else{
            finishAffinity();
        }
    }

    private String appVersionName(){
        try{
            return getPackageManager().getPackageInfo(getPackageName(),0).versionName;
        }catch(Exception e){
            return "";
        }
    }

    private void chooseUpdateApk(){
        if(Build.VERSION.SDK_INT>=26 && !getPackageManager().canRequestPackageInstalls()){
            Toast.makeText(
                    this,
                    "یک‌بار اجازه نصب برنامه از این منبع را فعال کن؛ بعد دوباره «انتخاب فایل بروزرسانی» را بزن.",
                    Toast.LENGTH_LONG
            ).show();
            Intent settingsIntent=new Intent(
                    Settings.ACTION_MANAGE_UNKNOWN_APP_SOURCES,
                    Uri.parse("package:"+getPackageName())
            );
            startActivity(settingsIntent);
            return;
        }

        Intent pick=new Intent(Intent.ACTION_OPEN_DOCUMENT);
        pick.addCategory(Intent.CATEGORY_OPENABLE);
        pick.setType("application/vnd.android.package-archive");
        try{
            startActivityForResult(pick,UPDATE_APK_PICKER);
        }catch(Exception e){
            pick.setType("*/*");
            startActivityForResult(pick,UPDATE_APK_PICKER);
        }
    }

    private void installSelectedApk(Uri apkUri){
        if(apkUri==null){
            Toast.makeText(this,"فایل بروزرسانی انتخاب نشد",Toast.LENGTH_SHORT).show();
            return;
        }
        try{
            Intent install=new Intent(Intent.ACTION_VIEW);
            install.setDataAndType(apkUri,"application/vnd.android.package-archive");
            install.addFlags(Intent.FLAG_GRANT_READ_URI_PERMISSION);
            startActivity(install);
        }catch(Exception e){
            Toast.makeText(
                    this,
                    "نصب بروزرسانی باز نشد. فایل APK معتبر را انتخاب کن.",
                    Toast.LENGTH_LONG
            ).show();
        }
    }

    @Override
    protected void onActivityResult(int requestCode,int resultCode,Intent data){
        super.onActivityResult(requestCode,resultCode,data);
        if(requestCode==UPDATE_APK_PICKER && resultCode==RESULT_OK && data!=null){
            Uri uri=data.getData();
            if(uri!=null){
                try{
                    final int flags=data.getFlags() &
                            (Intent.FLAG_GRANT_READ_URI_PERMISSION|Intent.FLAG_GRANT_WRITE_URI_PERMISSION);
                    getContentResolver().takePersistableUriPermission(uri,flags);
                }catch(Exception ignored){}
                installSelectedApk(uri);
            }
        }
    }

    private Map<String,String> intentMap(Intent intent){Map<String,String> m=new HashMap<>();if(intent==null||intent.getExtras()==null)return m;for(String k:intent.getExtras().keySet()){Object v=intent.getExtras().get(k);if(v!=null)m.put(k,String.valueOf(v));}return m;}
    private void handleNotificationIntent(Intent intent){if(intent==null)return;int notificationId=intent.getIntExtra("_notification_id",0);if(notificationId!=0){NotificationManager nm=(NotificationManager)getSystemService(Context.NOTIFICATION_SERVICE);if(nm!=null)nm.cancel(notificationId);}Map<String,String> data=intentMap(intent);String server=data.get("server_url");if(server!=null)DeviceRegistrar.setServerUrl(this,server);boolean hasReport=data.containsKey("event_id")||data.containsKey("report_title")||data.containsKey("report_date")||data.containsKey("kpi_factory");if(hasReport&&!"1".equals(data.get("archive_offer_only")))new NotificationArchiveDb(this).insert(data);FirebaseMessaging.getInstance().getToken().addOnSuccessListener(t->DeviceRegistrar.handleServerHint(this,data,t));refreshJs();}
    private String latestReportJson(){return new NotificationArchiveDb(this).latestDailyReportJson();}
    private void openLatestReportPdf(){String raw=latestReportJson();if(raw.isEmpty()){Toast.makeText(this,"هنوز گزارشی ذخیره نشده است",Toast.LENGTH_SHORT).show();return;}try{String u=new JSONObject(raw).optString("pdf_url","");if(u.isEmpty()){Toast.makeText(this,"برای این گزارش لینک PDF ذخیره نشده است",Toast.LENGTH_SHORT).show();return;}downloadAndOpenReport(u);}catch(Exception e){Toast.makeText(this,"باز کردن گزارش انجام نشد",Toast.LENGTH_SHORT).show();}}
    private void openArchived(long id){try{String raw=new NotificationArchiveDb(this).byIdJson(id);String u=raw.isEmpty()?"":new JSONObject(raw).optString("pdf_url","");if(u.isEmpty()){Toast.makeText(this,"لینک PDF این اعلان موجود نیست",Toast.LENGTH_SHORT).show();return;}downloadAndOpenReport(u);}catch(Exception e){Toast.makeText(this,"باز کردن PDF انجام نشد",Toast.LENGTH_SHORT).show();}}

    private void downloadAndOpenReport(String pdfUrl){Uri reportUri=Uri.parse(pdfUrl);if(!isAllowedReportUrl(reportUri)){Toast.makeText(this,"لینک PDF گزارش معتبر نیست",Toast.LENGTH_SHORT).show();return;}Toast.makeText(this,"در حال دریافت PDF گزارش...",Toast.LENGTH_SHORT).show();new Thread(()->{HttpURLConnection c=null;File out=null;try{File dir=new File(getCacheDir(),"reports");if(!dir.exists()&&!dir.mkdirs())throw new Exception("پوشه گزارش ساخته نشد");out=new File(dir,"Solico_Report_"+System.currentTimeMillis()+".pdf");c=(HttpURLConnection)new URL(pdfUrl).openConnection();c.setConnectTimeout(7000);c.setReadTimeout(20000);c.setRequestProperty("Accept","application/pdf");c.connect();if(c.getResponseCode()!=200)throw new Exception("HTTP "+c.getResponseCode());try(BufferedInputStream in=new BufferedInputStream(c.getInputStream());FileOutputStream os=new FileOutputStream(out)){byte[] b=new byte[65536];int n;while((n=in.read(b))!=-1)os.write(b,0,n);}if(out.length()<100)throw new Exception("PDF خالی دریافت شد");File f=out;runOnUiThread(()->openDownloadedPdf(f));}catch(Exception e){if(out!=null)try{out.delete();}catch(Exception ignored){}String msg=e.getMessage()==null?e.getClass().getSimpleName():e.getMessage();runOnUiThread(()->Toast.makeText(this,"PDF از کامپیوتر دریافت نشد. SolicoEnergy ویندوز باید باز و گوشی و کامپیوتر در یک شبکه باشند.\n"+msg,Toast.LENGTH_LONG).show());}finally{if(c!=null)c.disconnect();}},"SolicoPdfDownload").start();}
    private void openDownloadedPdf(File f){try{Uri u=FileProvider.getUriForFile(this,getPackageName()+".fileprovider",f);Intent i=new Intent(Intent.ACTION_VIEW);i.setDataAndType(u,"application/pdf");i.addFlags(Intent.FLAG_GRANT_READ_URI_PERMISSION);startActivity(Intent.createChooser(i,"باز کردن گزارش PDF"));}catch(Exception e){Toast.makeText(this,"PDF دریافت شد ولی برنامه نمایش PDF پیدا نشد",Toast.LENGTH_LONG).show();}}
    private boolean isAllowedReportUrl(Uri u){if(u==null)return false;String scheme=u.getScheme(),host=u.getHost();if("https".equalsIgnoreCase(scheme))return true;if(!"http".equalsIgnoreCase(scheme)||host==null)return false;if("127.0.0.1".equals(host)||"localhost".equalsIgnoreCase(host))return false;String[] p=host.split("\\.");try{if(p.length!=4)return false;int a=Integer.parseInt(p[0]),b=Integer.parseInt(p[1]);return a==10||(a==192&&b==168)||(a==172&&b>=16&&b<=31)||(a==169&&b==254);}catch(Exception e){return false;}}

    private String connectionStatus(){
        try{
            ConnectivityManager cm=(ConnectivityManager)getSystemService(Context.CONNECTIVITY_SERVICE);
            if(cm==null) return "نامشخص";
            NetworkInfo info=cm.getActiveNetworkInfo();
            return info!=null&&info.isConnected()?"متصل":"آفلاین";
        }catch(Exception e){return "نامشخص";}
    }

    private boolean notificationsEnabled(){
        if(Build.VERSION.SDK_INT>=33 && checkSelfPermission(Manifest.permission.POST_NOTIFICATIONS)!=PackageManager.PERMISSION_GRANTED) return false;
        try{
            NotificationManager nm=(NotificationManager)getSystemService(Context.NOTIFICATION_SERVICE);
            return nm==null || nm.areNotificationsEnabled();
        }catch(Exception e){return true;}
    }

    private void openNotificationSettings(){
        try{
            Intent i=new Intent(Settings.ACTION_APP_NOTIFICATION_SETTINGS);
            i.putExtra(Settings.EXTRA_APP_PACKAGE,getPackageName());
            startActivity(i);
        }catch(Exception e){
            try{
                Intent i=new Intent(Settings.ACTION_APPLICATION_DETAILS_SETTINGS,Uri.parse("package:"+getPackageName()));
                startActivity(i);
            }catch(Exception ignored){}
        }
    }

    private void ensureFirebaseMessaging(){ensureFirebaseMessaging(false);}
    private void ensureFirebaseMessaging(boolean force){
        FirebaseMessaging m=FirebaseMessaging.getInstance();
        m.setAutoInitEnabled(true);
        SharedPreferences fp=getSharedPreferences(FCM_PREFS,MODE_PRIVATE);
        boolean done=fp.getBoolean(FCM_RESET_KEY,false);
        if(force||!done){
            fcmStatusText="در حال اتصال مجدد اعلان‌ها...";
            m.deleteToken().addOnCompleteListener(t->{
                fp.edit().putBoolean(FCM_RESET_KEY,true).apply();
                subscribeCurrentFcmToken();
            });
            return;
        }
        subscribeCurrentFcmToken();
    }

    private void subscribeCurrentFcmToken(){
        FirebaseMessaging m=FirebaseMessaging.getInstance();
        if(!isLocallyLoggedIn()||!DeviceRegistrar.isEnabledLocal(this)){
            m.unsubscribeFromTopic(REPORT_TOPIC);
            m.unsubscribeFromTopic(DeviceRegistrar.deviceFcmTopic(this));
            fcmStatusText="برای اتصال دوباره وارد شوید";
            refreshJs();
            return;
        }
        m.unsubscribeFromTopic(REPORT_TOPIC);
        m.getToken().addOnCompleteListener(t->{
            if(!t.isSuccessful()||t.getResult()==null||t.getResult().trim().isEmpty()){
                fcmStatusText="خطا در دریافت توکن اعلان";
                scheduleFcmRetry();
                refreshJs();
                return;
            }
            final String token=t.getResult().trim();
            // Register the token with Windows first. Direct FCM works even if a topic
            // subscription is delayed on a newly installed phone.
            DeviceRegistrar.registerCloud(this,token);
            fcmRetryCount=0;

            if(!notificationsEnabled()){
                fcmStatusText="اعلان‌های گوشی خاموش است؛ اتصال را بزنید";
                refreshJs();
            }else{
                fcmStatusText="اعلان مستقیم فعال است";
                refreshJs();
            }

            m.subscribeToTopic(DeviceRegistrar.deviceFcmTopic(this)).addOnCompleteListener(deviceTask->{
                if(!notificationsEnabled()){
                    fcmStatusText="اعلان‌های گوشی خاموش است؛ اتصال را بزنید";
                }else if(deviceTask.isSuccessful()){
                    fcmStatusText="اعلان‌ها فعال هستند";
                }else{
                    fcmStatusText="اعلان مستقیم فعال است";
                }
                refreshJs();
            });
        });
    }

    private void scheduleReliableDeviceRegistration(){
        if(!isLocallyLoggedIn()||!DeviceRegistrar.isEnabledLocal(this)) return;
        final int generation=++registryGeneration;
        final long[] delays={0L,5000L,15000L,45000L,120000L,300000L,600000L};
        for(long delay:delays){
            registryHandler.postDelayed(()->{
                if(generation!=registryGeneration) return;
                if(!isLocallyLoggedIn()||!DeviceRegistrar.isEnabledLocal(MainActivity.this)) return;
                FirebaseMessaging.getInstance().getToken().addOnCompleteListener(t->{
                    String token=(t.isSuccessful()&&t.getResult()!=null)?t.getResult():"";
                    DeviceRegistrar.registerCloud(MainActivity.this,token);
                });
            },delay);
        }
    }

    private void scheduleFcmRetry(){
        if(fcmRetryCount>=MAX_FCM_RETRIES)return;
        fcmRetryCount++;
        fcmHandler.postDelayed(this::subscribeCurrentFcmToken,Math.min(30000L,2500L*fcmRetryCount));
    }

    private void repairFcmNow(){
        if(Build.VERSION.SDK_INT>=33 && checkSelfPermission(Manifest.permission.POST_NOTIFICATIONS)!=PackageManager.PERMISSION_GRANTED){
            requestPermissions(new String[]{Manifest.permission.POST_NOTIFICATIONS},NOTIFICATION_PERMISSION);
            return;
        }
        if(!notificationsEnabled()){
            Toast.makeText(this,"اعلان‌های Solico در تنظیمات گوشی خاموش است.",Toast.LENGTH_LONG).show();
            openNotificationSettings();
            return;
        }
        fcmRetryCount=0;
        getSharedPreferences(FCM_PREFS,MODE_PRIVATE).edit().putBoolean(FCM_RESET_KEY,false).apply();
        ensureFirebaseMessaging(true);
    }
    private void registerDeviceNow(){
        if(!isLocallyLoggedIn()||!DeviceRegistrar.isEnabledLocal(this)){showLoginScreen();return;}
        Toast.makeText(this,"در حال ثبت اینترنتی این گوشی...",Toast.LENGTH_SHORT).show();
        FirebaseMessaging.getInstance().getToken()
                .addOnSuccessListener(t->{DeviceRegistrar.registerCloud(this,t);DeviceRegistrar.maybeOfferArchive(this);})
                .addOnFailureListener(e->Toast.makeText(this,"توکن اعلان دریافت نشد؛ اتصال اینترنت را بررسی کنید.",Toast.LENGTH_LONG).show());
    }
    private void refreshJs(){if(webView!=null)webView.post(()->webView.evaluateJavascript("if(window.refreshFcmStatus)refreshFcmStatus();if(window.refreshDeviceStatus)refreshDeviceStatus();if(window.refreshLatestReport)refreshLatestReport();",null));}
    @Override protected void onResume(){
        super.onResume();
        UpdateManager.resumePendingIfAllowed(this);
        if(!DeviceRegistrar.isEnabledLocal(this)){
            if(appInitialized||isLocallyLoggedIn()) showLoginScreen();
            return;
        }
        if(isLocallyLoggedIn()&&appInitialized){
            subscribeCurrentFcmToken();
            scheduleReliableDeviceRegistration();
            DeviceRegistrar.maybeOfferArchive(this);
            DeviceRegistrar.pollArchiveOffers(this);
            UpdateManager.checkForUpdate(this,false);
        }
    }

    @Override public void onRequestPermissionsResult(int requestCode,String[] permissions,int[] grantResults){
        super.onRequestPermissionsResult(requestCode,permissions,grantResults);
        if(requestCode==NOTIFICATION_PERMISSION){
            if(notificationsEnabled()){
                fcmStatusText="در حال فعال‌سازی اعلان‌ها...";
                subscribeCurrentFcmToken();
            }else{
                fcmStatusText="اعلان‌های گوشی خاموش است؛ اتصال را بزنید";
                refreshJs();
            }
        }
    }

    private void createNotificationChannel(){if(Build.VERSION.SDK_INT>=26){NotificationChannel c=new NotificationChannel(CHANNEL_ID,"گزارش‌های Solico",NotificationManager.IMPORTANCE_HIGH);c.setDescription("اعلان آماده شدن گزارش نیروگاه");NotificationManager n=getSystemService(NotificationManager.class);if(n!=null)n.createNotificationChannel(c);}}
    private void requestNotificationPermissionIfNeeded(){if(Build.VERSION.SDK_INT>=33&&checkSelfPermission(Manifest.permission.POST_NOTIFICATIONS)!=PackageManager.PERMISSION_GRANTED)requestPermissions(new String[]{Manifest.permission.POST_NOTIFICATIONS},NOTIFICATION_PERMISSION);}
    @Override public void onBackPressed(){if(webView!=null&&webView.canGoBack())webView.goBack();else super.onBackPressed();}

    public class Bridge{
        @JavascriptInterface public String getLatestReportJson(){return latestReportJson();}
        @JavascriptInterface public String getCurrentUsername(){return currentUsername();}
        @JavascriptInterface public String getFcmStatus(){return fcmStatusText;}
        @JavascriptInterface public String getDeviceStatus(){return DeviceRegistrar.status(MainActivity.this);}
        @JavascriptInterface public String getConnectionStatus(){return connectionStatus();}
        @JavascriptInterface public String getNotificationHistoryJson(String date){return new NotificationArchiveDb(MainActivity.this).historyJson(date);}
        @JavascriptInterface public String getMonthlyHistoryJson(String month){return new NotificationArchiveDb(MainActivity.this).monthHistoryJson(month);}
        @JavascriptInterface public String getComparisonJson(String date){return new NotificationArchiveDb(MainActivity.this).comparisonJson(date);}
        @JavascriptInterface public void openLatestReport(){runOnUiThread(()->openLatestReportPdf());}
        @JavascriptInterface public void openArchivedReport(String id){runOnUiThread(()->{try{openArchived(Long.parseLong(id));}catch(Exception ignored){}});}
        @JavascriptInterface public void repairNotifications(){runOnUiThread(()->repairFcmNow());}
        @JavascriptInterface public void registerDeviceNow(){runOnUiThread(()->MainActivity.this.registerDeviceNow());}
        @JavascriptInterface public void syncHistoryNow(){DeviceRegistrar.requestHistorySync(MainActivity.this,false);}
        @JavascriptInterface public String getAppVersion(){return appVersionName();}
        @JavascriptInterface public void checkForUpdates(){runOnUiThread(()->UpdateManager.manualCheck(MainActivity.this));}
        @JavascriptInterface public void chooseUpdateApk(){runOnUiThread(()->MainActivity.this.chooseUpdateApk());}
        @JavascriptInterface public void logout(){runOnUiThread(()->logoutFromApp());}
    }
}