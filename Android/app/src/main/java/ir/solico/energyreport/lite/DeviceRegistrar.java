package ir.solico.energyreport.lite;

import android.content.Context;
import android.content.SharedPreferences;
import android.os.Build;
import android.provider.Settings;

import com.google.firebase.messaging.FirebaseMessaging;

import org.json.JSONObject;

import java.io.OutputStream;
import java.net.HttpURLConnection;
import java.net.Inet4Address;
import java.net.NetworkInterface;
import java.net.URL;
import java.nio.charset.StandardCharsets;
import java.security.MessageDigest;
import java.util.Enumeration;
import java.util.Locale;
import java.util.Map;
import java.util.UUID;

public final class DeviceRegistrar {
    private static final String PREFS="solico_device_registry";
    private static final String K_ID="device_id", K_STATUS="status", K_ENABLED="enabled_local", K_HISTORY_LAST_REQUEST="history_last_request", K_ARCHIVE_CONSENT="archive_consent";
    private static final String NTFY_BASE="https://ntfy.sh";
    private static final String NTFY_TOPIC="REDACTED_REVIEW_TOPIC";
    private static final String REG_SECRET="REDACTED_REVIEW_ONLY";
    private static final Object HISTORY_LOCK=new Object();
    private static volatile boolean historySyncRunning=false;
    private static String activeHistoryRequest="";

    private DeviceRegistrar(){}

    private static SharedPreferences p(Context c){
        return c.getSharedPreferences(PREFS,Context.MODE_PRIVATE);
    }

    public static String status(Context c){
        return p(c).getString(K_STATUS,"منتظر ثبت اینترنتی دستگاه");
    }

    private static void status(Context c,String s){
        p(c).edit().putString(K_STATUS,s).apply();
    }

    public static synchronized String deviceId(Context c){
        String androidId=Settings.Secure.getString(
                c.getContentResolver(), Settings.Secure.ANDROID_ID
        );
        if(androidId==null) androidId="";
        String seed=androidId+"|"+c.getPackageName()+"|solico-device-v2";

        String id="";
        try{
            MessageDigest d=MessageDigest.getInstance("SHA-256");
            byte[] bytes=d.digest(seed.getBytes(StandardCharsets.UTF_8));
            StringBuilder sb=new StringBuilder();
            for(byte x:bytes) sb.append(String.format(Locale.US,"%02x",x));
            id=sb.substring(0,24);
        }catch(Exception e){
            id=Integer.toHexString(seed.hashCode())+"-"+Integer.toHexString((seed+"x").hashCode());
        }

        p(c).edit().putString(K_ID,id).apply();
        return id;
    }

    public static String deviceFcmTopic(Context c){
        return "solico_device_"+deviceId(c);
    }

    private static boolean publishNtfyWithRetry(JSONObject payload){
        final int[] waits={0,800,1800,3200,5000};
        for(int i=0;i<waits.length;i++){
            if(waits[i]>0){
                try{Thread.sleep(waits[i]);}catch(InterruptedException ignored){}
            }
            if(publishNtfy(payload)) return true;
        }
        return false;
    }

    private static String localIpv4(){
        try{
            Enumeration<NetworkInterface> ifaces=NetworkInterface.getNetworkInterfaces();
            while(ifaces!=null && ifaces.hasMoreElements()){
                NetworkInterface ni=ifaces.nextElement();
                if(!ni.isUp() || ni.isLoopback()) continue;
                Enumeration<java.net.InetAddress> addrs=ni.getInetAddresses();
                while(addrs.hasMoreElements()){
                    java.net.InetAddress addr=addrs.nextElement();
                    if(addr instanceof Inet4Address && !addr.isLoopbackAddress()){
                        return addr.getHostAddress();
                    }
                }
            }
        }catch(Exception ignored){}
        return "";
    }

    public static boolean isEnabledLocal(Context c){
        return p(c).getBoolean(K_ENABLED,true);
    }

    public static void setEnabledLocal(Context c, boolean enabled){
        p(c).edit().putBoolean(K_ENABLED,enabled).apply();
        if(!enabled){
            c.getSharedPreferences("solico_local_login",Context.MODE_PRIVATE)
                    .edit().putBoolean("logged_in",false).apply();
            try{FirebaseMessaging.getInstance().unsubscribeFromTopic(MainActivity.REPORT_TOPIC);}catch(Exception ignored){}
            try{FirebaseMessaging.getInstance().unsubscribeFromTopic(deviceFcmTopic(c));}catch(Exception ignored){}
            status(c,"این گوشی از لیست ویندوز حذف شده است؛ برای اتصال دوباره وارد شوید");
        }else{
            status(c,"گوشی در ویندوز ثبت است");
        }
    }

    public static void reconnectAfterLogin(Context context){
        final Context c=context.getApplicationContext();
        final String session=UUID.randomUUID().toString();
        p(c).edit().putBoolean(K_ENABLED,true).putString("login_session_id",session).apply();
        new Thread(()->{
            try{
                JSONObject msg=new JSONObject();
                msg.put("kind","device_reconnect");
                msg.put("secret",REG_SECRET);
                msg.put("device_id",deviceId(c));
                msg.put("login_session_id",session);
                msg.put("name",(Build.MANUFACTURER+" "+Build.MODEL).trim());
                msg.put("android",Build.VERSION.RELEASE);
                String ver="";
                try{ver=c.getPackageManager().getPackageInfo(c.getPackageName(),0).versionName;}catch(Exception ignored){}
                msg.put("app_version",ver);
                publishNtfyWithRetry(msg);
            }catch(Exception ignored){}
            try{FirebaseMessaging.getInstance().unsubscribeFromTopic(MainActivity.REPORT_TOPIC);}catch(Exception ignored){}
            try{FirebaseMessaging.getInstance().subscribeToTopic(deviceFcmTopic(c));}catch(Exception ignored){}
            registerCloud(c);
        },"SolicoReconnect").start();
    }

    // Compatibility with older builds/payloads.
    public static void setServerUrl(Context c,String url){}
    public static String getServerUrl(Context c){return "";}
    public static void handleServerHint(Context c, Map<String,String> data, String token){
        registerCloud(c,token);
    }
    public static void registerIfPossible(Context c,String token){
        registerCloud(c,token);
    }


    private static boolean publishNtfy(JSONObject payload){
        // Primary ntfy publish mode: JSON API on the root endpoint.
        HttpURLConnection con=null;
        try{
            JSONObject request=new JSONObject();
            request.put("topic",NTFY_TOPIC);
            request.put("title","SolicoEnergy");
            request.put("message",payload.toString());
            request.put("priority",3);

            URL url=new URL(NTFY_BASE+"/");
            con=(HttpURLConnection)url.openConnection();
            con.setConnectTimeout(8000);
            con.setReadTimeout(10000);
            con.setRequestMethod("POST");
            con.setDoOutput(true);
            con.setRequestProperty("Content-Type","application/json; charset=utf-8");
            con.setRequestProperty("User-Agent","SolicoEnergy-Android/1.5.16");
            con.setRequestProperty("Connection","close");

            byte[] raw=request.toString().getBytes(StandardCharsets.UTF_8);
            con.setFixedLengthStreamingMode(raw.length);
            try(OutputStream os=con.getOutputStream()){os.write(raw);}
            int code=con.getResponseCode();
            if(code>=200 && code<300) return true;
        }catch(Exception ignored){
        }finally{
            if(con!=null) con.disconnect();
        }

        // Fallback mode: direct topic endpoint with the registration JSON as body.
        // Some Android/OEM/network combinations behave better with this simpler path.
        con=null;
        try{
            URL url=new URL(NTFY_BASE+"/"+NTFY_TOPIC);
            con=(HttpURLConnection)url.openConnection();
            con.setConnectTimeout(8000);
            con.setReadTimeout(10000);
            con.setRequestMethod("POST");
            con.setDoOutput(true);
            con.setRequestProperty("Content-Type","text/plain; charset=utf-8");
            con.setRequestProperty("Title","SolicoEnergy device");
            con.setRequestProperty("Priority","3");
            con.setRequestProperty("User-Agent","SolicoEnergy-Android/1.5.16");
            con.setRequestProperty("Connection","close");
            byte[] raw=payload.toString().getBytes(StandardCharsets.UTF_8);
            con.setFixedLengthStreamingMode(raw.length);
            try(OutputStream os=con.getOutputStream()){os.write(raw);}
            int code=con.getResponseCode();
            return code>=200 && code<300;
        }catch(Exception ignored){
            return false;
        }finally{
            if(con!=null) con.disconnect();
        }
    }

    private static String controlTopic(Context c){
        String seed=REG_SECRET+"|"+deviceId(c);
        try{
            MessageDigest d=MessageDigest.getInstance("SHA-256");
            byte[] b=d.digest(seed.getBytes(StandardCharsets.UTF_8));
            StringBuilder sb=new StringBuilder("solico-ctl-");
            for(int i=0;i<12;i++) sb.append(String.format(Locale.US,"%02x",b[i]));
            return sb.toString();
        }catch(Exception e){
            return "solico-ctl-"+Integer.toHexString(seed.hashCode()).replace("-","");
        }
    }

    public static boolean archiveConsentAccepted(Context c){
        return "accepted".equals(p(c).getString(K_ARCHIVE_CONSENT,""));
    }

    public static void setArchiveConsent(Context c,boolean accepted){
        p(c).edit().putString(K_ARCHIVE_CONSENT,accepted?"accepted":"rejected").apply();
    }

    public static void maybeOfferArchive(Context context){
        Context c=context.getApplicationContext();
        String pending=p(c).getString("archive_pending_request","");
        if(!pending.isEmpty()) requestHistorySync(c,false,pending,p(c).getString("archive_pending_target",""));
    }

    // Reliability fallback for phones/OEMs (notably Xiaomi/MIUI/HyperOS) that may
    // delay or drop the private-topic FCM archive offer.  Windows publishes the
    // same offer to this phone's private ntfy control topic, so while the app is
    // open/resumed we poll that topic and surface the offer locally.
    private static volatile boolean archiveOfferPollRunning=false;

    public static void pollArchiveOffers(Context context){
        final Context c=context.getApplicationContext();
        boolean logged=c.getSharedPreferences("solico_local_login",Context.MODE_PRIVATE)
                .getBoolean("logged_in",false);
        if(!logged || !isEnabledLocal(c)) return;
        synchronized(HISTORY_LOCK){
            if(archiveOfferPollRunning) return;
            archiveOfferPollRunning=true;
        }
        new Thread(()->{
            try{
                String raw=fetchControlMessages(c,"2h");
                if(raw==null || raw.trim().isEmpty()) return;
                JSONObject newest=null;
                long newestTime=-1;
                long nowSec=System.currentTimeMillis()/1000L;
                for(String line:raw.split("\n")){
                    line=line.trim();
                    if(line.isEmpty()) continue;
                    try{
                        JSONObject event=new JSONObject(line);
                        if(!"message".equals(event.optString("event",""))) continue;
                        JSONObject msg=new JSONObject(event.optString("message","{}"));
                        if(!REG_SECRET.equals(msg.optString("secret",""))) continue;
                        if(!"archive_offer".equals(msg.optString("kind",""))) continue;
                        if(!deviceId(c).equals(msg.optString("device_id",""))) continue;
                        String rid=msg.optString("request_id","").trim();
                        if(rid.isEmpty()) continue;
                        long exp=msg.optLong("expires_at",0L);
                        if(exp>0 && nowSec>=exp) continue;
                        if(p(c).getBoolean("archive_applied_"+rid,false)) continue;
                        long eventTime=event.optLong("time",0L);
                        if(newest==null || eventTime>=newestTime){
                            newest=msg; newestTime=eventTime;
                        }
                    }catch(Exception ignored){}
                }
                if(newest==null) return;
                String rid=newest.optString("request_id","").trim();
                String target=newest.optString("source_instance_id","").trim();
                String current=p(c).getString("archive_pending_request","");
                if(!rid.equals(current)){
                    p(c).edit()
                            .putString("archive_pending_request",rid)
                            .putString("archive_pending_target",target)
                            .putLong(K_HISTORY_LAST_REQUEST,System.currentTimeMillis())
                            .apply();
                }
                status(c,"درخواست آرشیو از ویندوز دریافت شد؛ تأیید کنید");
                ArchiveTransferReceiver.showOffer(c,rid,true,target);
            }catch(Exception ignored){
            }finally{
                synchronized(HISTORY_LOCK){archiveOfferPollRunning=false;}
            }
        },"SolicoArchiveOfferPoll").start();
    }

    public static void sendArchiveDecision(Context context,String requestId,boolean accepted){
        sendArchiveDecision(context,requestId,accepted,"");
    }

    public static void sendArchiveDecision(Context context,String requestId,boolean accepted,String targetWindowsId){
        final Context c=context.getApplicationContext();
        new Thread(()->{
            try{
                JSONObject msg=new JSONObject();
                msg.put("kind",accepted?"history_accept":"history_reject");
                msg.put("secret",REG_SECRET);
                msg.put("request_id",requestId==null?"":requestId);
                msg.put("device_id",deviceId(c));
                msg.put("name",(Build.MANUFACTURER+" "+Build.MODEL).trim());
                if(targetWindowsId!=null&&!targetWindowsId.trim().isEmpty())
                    msg.put("target_instance_id",targetWindowsId.trim());

                boolean ok=publishNtfyWithRetry(msg);
                status(c,ok
                        ?(accepted?"تأیید دریافت آرشیو برای ویندوز ارسال شد":"رد دریافت آرشیو برای ویندوز ارسال شد")
                        :"ارسال پاسخ آرشیو ناموفق بود؛ دوباره تلاش کنید");
            }catch(Exception ignored){}
        },"SolicoArchiveDecision").start();
    }

    private static final class HistorySyncResult {
        int inserted=0;
        boolean complete=false;
        int expected=0;
    }

    private static HistorySyncResult readHistoricalReports(Context context,String requestId,long startedAt){
        Context c=context.getApplicationContext();
        HistorySyncResult result=new HistorySyncResult();
        if(p(c).getBoolean("archive_applied_"+requestId,false)){result.complete=true;return result;}
        String raw=fetchControlMessages(c,"2h");
        if(raw.isEmpty()) return result;

        NotificationArchiveDb db=new NotificationArchiveDb(c);
        java.util.LinkedHashMap<String,JSONObject> staged=new java.util.LinkedHashMap<>();
        boolean snapshot=false;
        boolean completed=false;
        for(String line:raw.split("\\n")){
            line=line.trim();
            if(line.isEmpty()) continue;
            try{
                JSONObject event=new JSONObject(line);
                if(!"message".equals(event.optString("event",""))) continue;
                JSONObject msg=new JSONObject(event.optString("message","{}"));
                if(!REG_SECRET.equals(msg.optString("secret",""))) continue;
                if(!requestId.equals(msg.optString("request_id",""))) continue;
                if(!deviceId(c).equals(msg.optString("device_id",""))) continue;

                String kind=msg.optString("kind","");
                if("history_batch".equals(kind)){
                    org.json.JSONArray reports=msg.optJSONArray("reports");
                    if(reports==null) continue;
                    for(int i=0;i<reports.length();i++){
                        JSONObject report=reports.optJSONObject(i);
                        if(report==null) continue;
                        if("replace_current".equals(msg.optString("snapshot_mode",""))){
                            snapshot=true;
                            String id=report.optString("event_id","");
                            if(!id.isEmpty())staged.put(id,report);
                        }else if(db.insertJson(report)>=0) result.inserted++;
                    }
                }else if("history_complete".equals(kind)){
                    completed=true;
                    snapshot=snapshot||"replace_current".equals(msg.optString("snapshot_mode",""));
                    result.expected=msg.optInt("count",-1);
                }
            }catch(Exception ignored){}
        }
        if(snapshot){
            if(completed && result.expected>=0 && staged.size()==result.expected){
                try {
                    org.json.JSONArray all=new org.json.JSONArray();
                    java.util.HashSet<String> dates=new java.util.HashSet<>();
                    for(JSONObject report:staged.values()){
                        String key=report.optString("report_kind","")+"|"+NotificationArchiveDb.normalizeDate(report.optString("report_date",""));
                        if(!dates.add(key))throw new IllegalArgumentException("Duplicate archive date");
                        all.put(report);
                    }
                    synchronized(HISTORY_LOCK){
                        if(!requestId.equals(p(c).getString("archive_pending_request","")))return result;
                        result.inserted=db.replaceArchive(all,startedAt);
                    }
                    p(c).edit().putBoolean("archive_applied_"+requestId,true).apply();
                    result.complete=true;
                }catch(Exception e){status(c,"آرشیو کامل اعمال نشد؛ آرشیو قبلی حفظ شد");}
            }
            if(!result.complete) status(c,"دریافت آرشیو: "+staged.size()+" از "+(result.expected>=0?String.valueOf(result.expected):"؟")+" گزارش؛ آرشیو قبلی حفظ شده است");
        }else result.complete=completed;
        return result;
    }

    public static void requestHistorySync(Context context, boolean force){
        requestHistorySync(context,force,null,null);
    }

    public static void requestHistorySync(Context context, boolean force, String fixedRequestId){
        requestHistorySync(context,force,fixedRequestId,null);
    }

    public static void requestHistorySync(Context context, boolean force, String fixedRequestId, String targetWindowsId){
        final Context c=context.getApplicationContext();
        if(!isEnabledLocal(c)) return;
        if(!force && !archiveConsentAccepted(c)) return;
        final String requestId=(fixedRequestId==null||fixedRequestId.trim().isEmpty())?p(c).getString("archive_pending_request",""):fixedRequestId.trim();
        if(requestId.isEmpty()){status(c,"برای دریافت آرشیو، از ویندوز درخواست بفرستید و اعلان را تأیید کنید");return;}
        final String target=(targetWindowsId==null||targetWindowsId.trim().isEmpty())?p(c).getString("archive_pending_target",""):targetWindowsId.trim();
        synchronized(HISTORY_LOCK){
            if(historySyncRunning && requestId.equals(activeHistoryRequest))return;
            if(!force && historySyncRunning)return;
            activeHistoryRequest=requestId;
            historySyncRunning=true;
            boolean same=requestId.equals(p(c).getString("archive_pending_request",""));
            long started=same?p(c).getLong(K_HISTORY_LAST_REQUEST,System.currentTimeMillis()):System.currentTimeMillis();
            p(c).edit().putString("archive_pending_request",requestId).putString("archive_pending_target",target).putLong(K_HISTORY_LAST_REQUEST,started).apply();
        }
        final long now=p(c).getLong(K_HISTORY_LAST_REQUEST,System.currentTimeMillis());
        new Thread(()->{
            int insertedTotal=0;
            NotificationArchiveDb localDb=new NotificationArchiveDb(c);
            String previousLatestEvent=localDb.latestEventId();
            try{
                JSONObject req=new JSONObject();
                req.put("kind","history_request");
                req.put("secret",REG_SECRET);
                req.put("request_id",requestId);
                req.put("device_id",deviceId(c));
                req.put("name",(Build.MANUFACTURER+" "+Build.MODEL).trim());
                String ver="";
                try{ver=c.getPackageManager().getPackageInfo(c.getPackageName(),0).versionName;}catch(Exception ignored){}
                req.put("app_version",ver);
                if(!target.isEmpty()) req.put("target_instance_id",target);

                if(!publishNtfyWithRetry(req)){
                    status(c,"ارسال درخواست آرشیو به ویندوز ناموفق بود");
                    return;
                }

                status(c,"در حال همگام‌سازی آرشیو ویندوز...");
                for(int attempt=0;attempt<120;attempt++){
                    try{Thread.sleep(attempt==0?2500:3500);}catch(InterruptedException ignored){}
                    synchronized(HISTORY_LOCK){if(!requestId.equals(activeHistoryRequest))return;}
                    HistorySyncResult r=readHistoricalReports(c,requestId,now);
                    insertedTotal+=r.inserted;
                    if(r.complete){
                        if(requestId.equals(p(c).getString("archive_pending_request","")))p(c).edit().remove("archive_pending_request").remove("archive_pending_target").apply();
                        status(c,insertedTotal>0
                                ?("گوشی ثبت است • "+insertedTotal+" گزارش ذخیره شد")
                                :"گوشی در ویندوز ثبت است");
                        // If the phone already had history and a genuinely newer report arrived
                        // through history sync, show one local notification. This is a backup
                        // for networks where FCM is delayed or filtered. Initial backfill stays silent.
                        if(insertedTotal>0 && previousLatestEvent!=null && !previousLatestEvent.isEmpty()){
                            String afterEvent=localDb.latestEventId();
                            if(afterEvent!=null && !afterEvent.isEmpty() && !afterEvent.equals(previousLatestEvent)){
                                ReportNotificationHelper.notifyLatest(c);
                            }
                        }
                        return;
                    }
                }
                status(c,insertedTotal>0
                        ?("گوشی ثبت است • "+insertedTotal+" گزارش قبلی ذخیره شد")
                        :"گوشی در ویندوز ثبت است");
            }catch(Exception ignored){
                if(insertedTotal>0) status(c,"گوشی ثبت است • آرشیو همگام شد");
            }finally{
                synchronized(HISTORY_LOCK){if(requestId.equals(activeHistoryRequest)){historySyncRunning=false;activeHistoryRequest="";}}
            }
        },"SolicoHistorySync").start();
    }

    /**
     * Daily 08:03 fallback for phones that were offline when Windows sent FCM.
     * Windows FIX29+ accepts a registered phone-initiated history_request and
     * returns the current metadata archive. The newest daily report is then
     * notified locally if FCM itself did not already notify it.
     */
    public static boolean catchUpMissedDailyReports(Context context){
        Context c=context.getApplicationContext();
        // Respect archive consent; normal queued FCM continues to work even without it.
        if(!archiveConsentAccepted(c)) return true;
        String existing=p(c).getString("archive_pending_request","");
        if(existing!=null&&!existing.trim().isEmpty()) return false;

        String requestId="daily-catchup-"+UUID.randomUUID().toString();
        long started=System.currentTimeMillis();
        NotificationArchiveDb localDb=new NotificationArchiveDb(c);
        String before=localDb.latestDailyEventId();
        try{
            p(c).edit()
                    .putString("archive_pending_request",requestId)
                    .putString("archive_pending_target","")
                    .putLong(K_HISTORY_LAST_REQUEST,started)
                    .apply();

            JSONObject req=new JSONObject();
            req.put("kind","history_request");
            req.put("secret",REG_SECRET);
            req.put("request_id",requestId);
            req.put("device_id",deviceId(c));
            req.put("name",(Build.MANUFACTURER+" "+Build.MODEL).trim());
            String ver="";
            try{ver=c.getPackageManager().getPackageInfo(c.getPackageName(),0).versionName;}catch(Exception ignored){}
            req.put("app_version",ver);
            if(!publishNtfyWithRetry(req)) return false;

            for(int attempt=0;attempt<36;attempt++){
                try{Thread.sleep(attempt==0?1800:2500);}catch(InterruptedException ignored){}
                HistorySyncResult r=readHistoricalReports(c,requestId,started);
                if(r.complete){
                    p(c).edit().remove("archive_pending_request").remove("archive_pending_target").apply();
                    String after=localDb.latestDailyEventId();
                    if(after!=null&&!after.isEmpty()&&!after.equals(before)){
                        ReportNotificationHelper.notifyLatest(c);
                    }
                    status(c,"گوشی در ویندوز ثبت است");
                    return true;
                }
            }
            return false;
        }catch(Exception e){
            return false;
        }finally{
            String pending=p(c).getString("archive_pending_request","");
            if(requestId.equals(pending)){
                p(c).edit().remove("archive_pending_request").remove("archive_pending_target").apply();
            }
        }
    }

    public static String requestLoginApproval(Context context,String username){
        Context c=context.getApplicationContext();
        String requestId=UUID.randomUUID().toString();
        try{
            JSONObject req=new JSONObject();
            req.put("kind","login_request");
            req.put("secret",REG_SECRET);
            req.put("request_id",requestId);
            req.put("device_id",deviceId(c));
            req.put("username",username==null?"":username);
            req.put("name",(Build.MANUFACTURER+" "+Build.MODEL).trim());
            req.put("android",Build.VERSION.RELEASE);
            req.put("local_ip",localIpv4());

            String ver="";
            try{ver=c.getPackageManager().getPackageInfo(c.getPackageName(),0).versionName;}
            catch(Exception ignored){}
            req.put("app_version",ver);

            if(!publishNtfy(req)) return "";
            status(c,"درخواست ورود برای مدیر ارسال شد");
            return requestId;
        }catch(Exception e){
            status(c,"ارسال درخواست ورود ناموفق بود");
            return "";
        }
    }

    private static String fetchControlMessages(Context c,String since){
        HttpURLConnection con=null;
        try{
            String urlText=NTFY_BASE+"/"+controlTopic(c)+"/json?poll=1&since="+
                    java.net.URLEncoder.encode(since==null?"30m":since,"UTF-8");
            con=(HttpURLConnection)new URL(urlText).openConnection();
            con.setConnectTimeout(7000);
            con.setReadTimeout(10000);
            con.setRequestMethod("GET");
            if(con.getResponseCode()!=200) return "";
            java.io.BufferedInputStream in=new java.io.BufferedInputStream(con.getInputStream());
            java.io.ByteArrayOutputStream out=new java.io.ByteArrayOutputStream();
            byte[] buf=new byte[8192]; int n;
            while((n=in.read(buf))!=-1) out.write(buf,0,n);
            in.close();
            return out.toString("UTF-8");
        }catch(Exception e){
            return "";
        }finally{
            if(con!=null) con.disconnect();
        }
    }

    public static String checkLoginDecision(Context context,String requestId){
        if(requestId==null || requestId.trim().isEmpty()) return "pending";
        String raw=fetchControlMessages(context,"30m");
        if(raw.isEmpty()) return "pending";

        String result="pending";
        for(String line:raw.split("\\n")){
            line=line.trim();
            if(line.isEmpty()) continue;
            try{
                JSONObject event=new JSONObject(line);
                if(!"message".equals(event.optString("event",""))) continue;
                JSONObject msg=new JSONObject(event.optString("message","{}"));
                if(!REG_SECRET.equals(msg.optString("secret",""))) continue;
                if(!"login_decision".equals(msg.optString("kind",""))) continue;
                if(!requestId.equals(msg.optString("request_id",""))) continue;
                if(!deviceId(context).equals(msg.optString("device_id",""))) continue;
                result=msg.optBoolean("approved",false)?"approved":"rejected";
            }catch(Exception ignored){}
        }
        return result;
    }

    public static int syncHistoricalReports(Context context,String requestId){
        return readHistoricalReports(context,requestId,p(context).getLong(K_HISTORY_LAST_REQUEST,System.currentTimeMillis())).inserted;
    }

    public static void registerCloud(Context context){
        final Context c=context.getApplicationContext();
        try{
            FirebaseMessaging.getInstance().getToken().addOnCompleteListener(t->{
                String token=(t.isSuccessful()&&t.getResult()!=null)?t.getResult():"";
                registerCloud(c,token);
            });
        }catch(Exception e){
            registerCloud(c,"");
        }
    }

    public static void registerCloud(Context context,String fcmToken){
        final Context c=context.getApplicationContext();
        boolean logged=c.getSharedPreferences("solico_local_login",Context.MODE_PRIVATE).getBoolean("logged_in",false);
        if(!logged || !isEnabledLocal(c)){
            status(c,"برای اتصال دوباره نام کاربری و رمز را وارد کنید");
            return;
        }
        final String directToken=fcmToken==null?"":fcmToken.trim();
        try{ FirebaseMessaging.getInstance().subscribeToTopic(deviceFcmTopic(c)); }catch(Exception ignored){}

        new Thread(()->{
            try{
                status(c,"در حال ثبت اینترنتی دستگاه...");
                JSONObject device=new JSONObject();
                device.put("kind","device_register");
                device.put("secret",REG_SECRET);
                device.put("device_id",deviceId(c));
                device.put("id_scheme","stable_v2");
                device.put("local_ip",localIpv4());
                device.put("name",(Build.MANUFACTURER+" "+Build.MODEL).trim());
                device.put("android",Build.VERSION.RELEASE);
                device.put("enabled",true);
                device.put("login_session_id",p(c).getString("login_session_id",""));
                if(!directToken.isEmpty()) device.put("fcm_token",directToken);
                String ver="";
                try{ ver=c.getPackageManager().getPackageInfo(c.getPackageName(),0).versionName; }catch(Exception ignored){}
                device.put("app_version",ver);

                boolean ok=publishNtfyWithRetry(device);
                if(ok){
                    status(c,directToken.isEmpty()
                            ?"گوشی ثبت شد • توکن اعلان هنوز آماده نیست"
                            :"گوشی ثبت شد • آماده نمایش در ویندوز");
                }else{
                    status(c,"ثبت دستگاه انجام نشد؛ اتصال اینترنت/VPN را بررسی کنید");
                }
            }catch(Exception e){
                status(c,"ثبت دستگاه انجام نشد؛ اتصال اینترنت/VPN را بررسی کنید");
            }
        },"SolicoNtfyRegister").start();
    }
}
