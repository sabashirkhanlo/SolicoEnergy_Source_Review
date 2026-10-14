package ir.solico.energyreport.lite;

import android.app.Activity;
import android.app.AlertDialog;
import android.content.Context;
import android.content.Intent;
import android.net.Uri;
import android.os.Build;
import android.os.Handler;
import android.os.Looper;
import android.provider.Settings;
import android.widget.Toast;

import androidx.core.content.FileProvider;

import org.json.JSONObject;

import java.io.BufferedInputStream;
import java.io.File;
import java.io.FileOutputStream;
import java.net.HttpURLConnection;
import java.net.URL;
import java.security.MessageDigest;
import java.util.Locale;

/**
 * Solico in-app updater.
 *
 * The manifest is intentionally a tiny public JSON file. The APK is accepted only
 * over HTTPS and only after its SHA-256 exactly matches the manifest.
 */
public final class UpdateManager {
    private UpdateManager(){}

    // Public Firebase Hosting endpoint. See firebase-public/ and README_UPDATE_FA.md.
    public static final String UPDATE_MANIFEST_URL="https://REDACTED-PRODUCTION-ENDPOINT.invalid";
    private static final String PREFS="solico_update_state";
    private static final String K_LAST_CHECK="last_check";
    private static final String K_PENDING_URL="pending_url";
    private static final String K_PENDING_SHA="pending_sha";
    private static final long NORMAL_CHECK_INTERVAL_MS=30L*60L*1000L;
    private static final long[] AUTO_RETRY_DELAYS_MS={8000L,20000L,45000L};
    private static volatile boolean checking=false;
    private static volatile int autoRetryIndex=0;

    public static void checkForUpdate(Activity a, boolean force){
        if(a==null||a.isFinishing()||checking)return;
        long now=System.currentTimeMillis();
        long last=a.getSharedPreferences(PREFS,Context.MODE_PRIVATE).getLong(K_LAST_CHECK,0);
        if(!force && now-last<NORMAL_CHECK_INTERVAL_MS)return;
        checking=true;
        new Thread(()->{
            HttpURLConnection con=null;
            boolean successfulCheck=false;
            try{
                con=(HttpURLConnection)new URL(UPDATE_MANIFEST_URL).openConnection();
                con.setConnectTimeout(7000); con.setReadTimeout(10000);
                con.setUseCaches(false); con.setRequestProperty("Accept","application/json");
                int code=con.getResponseCode();
                if(code<200||code>=300)throw new Exception("HTTP "+code);
                byte[] raw;
                try(BufferedInputStream in=new BufferedInputStream(con.getInputStream())){
                    java.io.ByteArrayOutputStream out=new java.io.ByteArrayOutputStream();
                    byte[] buf=new byte[8192]; int n;
                    while((n=in.read(buf))!=-1 && out.size()<128*1024)out.write(buf,0,n);
                    raw=out.toByteArray();
                }
                JSONObject j=new JSONObject(new String(raw,java.nio.charset.StandardCharsets.UTF_8));
                int latest=j.optInt("versionCode",0);
                if(latest<=0)throw new Exception("Invalid update manifest");

                // Only a real successful server check starts the 30-minute cooldown.
                // A failed/no-network check must not block the next automatic attempt.
                successfulCheck=true;
                autoRetryIndex=0;
                a.getSharedPreferences(PREFS,Context.MODE_PRIVATE).edit()
                        .putLong(K_LAST_CHECK,System.currentTimeMillis()).apply();

                if(latest<=BuildConfig.VERSION_CODE)return;
                String version=j.optString("versionName","1.5.16");
                String url=j.optString("apkUrl","").trim();
                String sha=j.optString("sha256","").trim().toLowerCase(Locale.ROOT);
                String message=j.optString("message","نسخه جدید Solico آماده است.");
                boolean mandatory=j.optBoolean("mandatory",false);
                if(!isSafeHttps(url)||!sha.matches("[0-9a-f]{64}"))return;
                a.runOnUiThread(()->showOffer(a,version,message,url,sha,mandatory));
            }catch(Exception ignored){
                // Stay silent operationally, but retry automatically. This is important
                // when VPN/network becomes available a few seconds after app launch.
            }finally{
                checking=false;
                if(con!=null)con.disconnect();
                if(!successfulCheck)scheduleAutomaticRetry(a);
            }
        },"SolicoUpdateCheck").start();
    }

    private static void scheduleAutomaticRetry(Activity a){
        if(a==null||a.isFinishing())return;
        int i=autoRetryIndex;
        if(i>=AUTO_RETRY_DELAYS_MS.length)return;
        autoRetryIndex=i+1;
        long delay=AUTO_RETRY_DELAYS_MS[i];
        new Handler(Looper.getMainLooper()).postDelayed(()->{
            if(a==null||a.isFinishing())return;
            checkForUpdate(a,false);
        },delay);
    }

    private static void showOffer(Activity a,String version,String message,String url,String sha,boolean mandatory){
        if(a.isFinishing())return;
        AlertDialog.Builder b=new AlertDialog.Builder(a)
                .setTitle("بروزرسانی جدید Solico موجود است")
                .setMessage(message)
                .setPositiveButton("به‌روزرسانی",(d,w)->downloadAndInstall(a,url,sha));
        if(!mandatory)b.setNegativeButton("بعداً",null);
        b.setCancelable(!mandatory).show();
    }

    public static void manualCheck(Activity a){
        a.getSharedPreferences(PREFS,Context.MODE_PRIVATE).edit().putLong(K_LAST_CHECK,0).apply();
        Toast.makeText(a,"در حال بررسی نسخه جدید...",Toast.LENGTH_SHORT).show();
        checkForUpdate(a,true);
    }

    private static void downloadAndInstall(Activity a,String url,String sha){
        if(Build.VERSION.SDK_INT>=26 && !a.getPackageManager().canRequestPackageInstalls()){
            a.getSharedPreferences(PREFS,Context.MODE_PRIVATE).edit()
                    .putString(K_PENDING_URL,url).putString(K_PENDING_SHA,sha).apply();
            Toast.makeText(a,"اجازه نصب بروزرسانی را فعال کن و به Solico برگرد.",Toast.LENGTH_LONG).show();
            a.startActivity(new Intent(Settings.ACTION_MANAGE_UNKNOWN_APP_SOURCES, Uri.parse("package:"+a.getPackageName())));
            return;
        }
        Toast.makeText(a,"در حال دانلود بروزرسانی...",Toast.LENGTH_SHORT).show();
        new Thread(()->{
            HttpURLConnection con=null; File out=null;
            try{
                if(!isSafeHttps(url))throw new SecurityException("Update URL must use HTTPS");
                File dir=new File(a.getCacheDir(),"updates");
                if(!dir.exists()&&!dir.mkdirs())throw new Exception("Update folder could not be created");
                out=new File(dir,"SolicoEnergy-Update.apk");
                con=(HttpURLConnection)new URL(url).openConnection();
                con.setConnectTimeout(10000); con.setReadTimeout(60000);
                con.setRequestProperty("Accept","application/vnd.android.package-archive,application/octet-stream,*/*");
                if(con.getResponseCode()!=200)throw new Exception("HTTP "+con.getResponseCode());
                MessageDigest md=MessageDigest.getInstance("SHA-256");
                long total=0;
                try(BufferedInputStream in=new BufferedInputStream(con.getInputStream()); FileOutputStream os=new FileOutputStream(out)){
                    byte[] buf=new byte[64*1024]; int n;
                    while((n=in.read(buf))!=-1){
                        os.write(buf,0,n); md.update(buf,0,n); total+=n;
                        if(total>250L*1024L*1024L)throw new Exception("APK too large");
                    }
                }
                if(total<100*1024)throw new Exception("APK download is incomplete");
                String actual=hex(md.digest());
                if(!actual.equalsIgnoreCase(sha)){out.delete();throw new SecurityException("SHA-256 mismatch");}
                File apk=out;
                a.runOnUiThread(()->openInstaller(a,apk));
            }catch(Exception e){
                if(out!=null)try{out.delete();}catch(Exception ignored){}
                String msg=e.getMessage()==null?"خطای نامشخص":e.getMessage();
                a.runOnUiThread(()->Toast.makeText(a,"بروزرسانی دانلود نشد: "+msg,Toast.LENGTH_LONG).show());
            }finally{if(con!=null)con.disconnect();}
        },"SolicoUpdateDownload").start();
    }

    public static void resumePendingIfAllowed(Activity a){
        if(Build.VERSION.SDK_INT>=26 && !a.getPackageManager().canRequestPackageInstalls())return;
        String url=a.getSharedPreferences(PREFS,Context.MODE_PRIVATE).getString(K_PENDING_URL,"");
        String sha=a.getSharedPreferences(PREFS,Context.MODE_PRIVATE).getString(K_PENDING_SHA,"");
        if(url==null||url.isEmpty()||sha==null||sha.isEmpty())return;
        a.getSharedPreferences(PREFS,Context.MODE_PRIVATE).edit().remove(K_PENDING_URL).remove(K_PENDING_SHA).apply();
        downloadAndInstall(a,url,sha);
    }

    private static void openInstaller(Activity a,File apk){
        try{
            Uri u=FileProvider.getUriForFile(a,a.getPackageName()+".fileprovider",apk);
            Intent i=new Intent(Intent.ACTION_VIEW);
            i.setDataAndType(u,"application/vnd.android.package-archive");
            i.addFlags(Intent.FLAG_GRANT_READ_URI_PERMISSION|Intent.FLAG_ACTIVITY_NEW_TASK);
            a.startActivity(i);
        }catch(Exception e){
            Toast.makeText(a,"صفحه نصب بروزرسانی باز نشد.",Toast.LENGTH_LONG).show();
        }
    }

    private static boolean isSafeHttps(String url){
        try{return "https".equalsIgnoreCase(Uri.parse(url).getScheme());}catch(Exception e){return false;}
    }
    private static String hex(byte[] b){StringBuilder s=new StringBuilder();for(byte x:b)s.append(String.format(Locale.ROOT,"%02x",x));return s.toString();}
}
