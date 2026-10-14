package ir.solico.energyreport.lite;

import android.Manifest;
import android.app.Notification;
import android.app.NotificationManager;
import android.app.PendingIntent;
import android.content.Context;
import android.content.Intent;
import android.content.pm.PackageManager;
import android.os.Build;

import org.json.JSONObject;

/** Local notification fallback for a newly synchronized report. */
public final class ReportNotificationHelper {
    private ReportNotificationHelper(){}

    public static void markEventNotified(Context context,String eventId){
        if(eventId==null||eventId.trim().isEmpty()) return;
        context.getApplicationContext().getSharedPreferences("solico_local_notifications",Context.MODE_PRIVATE)
                .edit().putString("last_notified_event",eventId.trim()).apply();
    }

    public static void notifyLatest(Context context){
        Context c=context.getApplicationContext();
        if(Build.VERSION.SDK_INT>=33 && c.checkSelfPermission(Manifest.permission.POST_NOTIFICATIONS)!= PackageManager.PERMISSION_GRANTED){
            return;
        }
        try{
            String raw=new NotificationArchiveDb(c).latestDailyReportJson();
            if(raw==null||raw.trim().isEmpty()) return;
            JSONObject o=new JSONObject(raw);
            String eventId=o.optString("event_id","");
            if(eventId.isEmpty()) return;

            android.content.SharedPreferences p=c.getSharedPreferences("solico_local_notifications",Context.MODE_PRIVATE);
            if(eventId.equals(p.getString("last_notified_event",""))) return;

            String reportTitle=o.optString("report_title","").trim();
            String title="گزارش جدید Solico";
            String body=reportTitle.isEmpty()?"گزارش جدید نیروگاه آماده است.":reportTitle+" آماده است.";

            int notificationId=(int)(System.currentTimeMillis()&0x7fffffff);
            Intent i=new Intent(c,MainActivity.class);
            i.addFlags(Intent.FLAG_ACTIVITY_CLEAR_TOP|Intent.FLAG_ACTIVITY_SINGLE_TOP);
            i.putExtra("_notification_id",notificationId);
            PendingIntent pi=PendingIntent.getActivity(
                    c,notificationId,i,
                    PendingIntent.FLAG_UPDATE_CURRENT|PendingIntent.FLAG_IMMUTABLE);

            Notification.Builder b=Build.VERSION.SDK_INT>=26
                    ?new Notification.Builder(c,MainActivity.CHANNEL_ID)
                    :new Notification.Builder(c);
            b.setSmallIcon(R.drawable.ic_notification)
                    .setContentTitle(title)
                    .setContentText(body)
                    .setStyle(new Notification.BigTextStyle().bigText(body))
                    .setAutoCancel(true)
                    .setContentIntent(pi);

            NotificationManager nm=(NotificationManager)c.getSystemService(Context.NOTIFICATION_SERVICE);
            if(nm!=null){
                nm.notify(notificationId,b.build());
                p.edit().putString("last_notified_event",eventId).apply();
            }
        }catch(Exception ignored){}
    }
}
