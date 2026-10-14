package ir.solico.energyreport.lite;

import android.app.Notification;
import android.app.NotificationManager;
import android.app.PendingIntent;
import android.content.Context;
import android.content.Intent;
import android.os.Build;

import com.google.firebase.messaging.FirebaseMessaging;
import com.google.firebase.messaging.FirebaseMessagingService;
import com.google.firebase.messaging.RemoteMessage;

import java.util.Map;

public class SolicoFirebaseMessagingService extends FirebaseMessagingService {

    @Override
    public void onNewToken(String token) {
        super.onNewToken(token);
        FirebaseMessaging.getInstance().unsubscribeFromTopic(MainActivity.REPORT_TOPIC);
        if(loggedIn() && DeviceRegistrar.isEnabledLocal(this)){
            FirebaseMessaging.getInstance().subscribeToTopic(DeviceRegistrar.deviceFcmTopic(this));
            DeviceRegistrar.registerCloud(this,token);
        }else{
            FirebaseMessaging.getInstance().unsubscribeFromTopic(DeviceRegistrar.deviceFcmTopic(this));
        }
    }

    @Override
    public void onMessageReceived(RemoteMessage remoteMessage) {
        super.onMessageReceived(remoteMessage);
        final Map<String,String> data=remoteMessage.getData();

        String action=data.get("action");
        String target=data.get("target_device_id");
        if(target!=null&&!target.trim().isEmpty()&&!DeviceRegistrar.deviceId(this).equals(target.trim())) return;

        if("device_access".equals(action)){
            boolean enabled="1".equals(data.get("enabled"));
            DeviceRegistrar.setEnabledLocal(this,enabled);
            if(!enabled){
                NotificationManager nm=(NotificationManager)getSystemService(Context.NOTIFICATION_SERVICE);
                if(nm!=null)nm.cancelAll();
            }
            return;
        }

        if(!loggedIn() || !DeviceRegistrar.isEnabledLocal(this)) return;

        // Keep internet registry fresh only for an active authenticated session.
        DeviceRegistrar.registerCloud(this);

        if("archive_offer".equals(action)){
            ArchiveTransferReceiver.showOffer(this,data.get("request_id"),true,data.get("source_instance_id"));
            return;
        }

        if("register".equals(action)) return;

        long inserted=new NotificationArchiveDb(this).insert(data);
        // Ignore duplicate delivery of an event already stored by FCM/history-sync.
        if(inserted<0) return;
        ReportNotificationHelper.markEventNotified(this,data.get("event_id"));

        String title=data.get("title");
        String body=data.get("body");
        if(title==null||title.trim().isEmpty()) title="گزارش جدید Solico";
        if(body==null||body.trim().isEmpty()) body="گزارش جدید نیروگاه آماده است.";

        showNotification(data,title,body);
    }

    private boolean loggedIn(){
        return getSharedPreferences("solico_local_login",MODE_PRIVATE)
                .getBoolean("logged_in",false);
    }

    private void showNotification(Map<String,String> data,String title,String body){
        Intent i=new Intent(this,MainActivity.class);
        i.addFlags(Intent.FLAG_ACTIVITY_CLEAR_TOP|Intent.FLAG_ACTIVITY_SINGLE_TOP);
        for(Map.Entry<String,String> e:data.entrySet()){
            if(e.getKey()!=null&&e.getValue()!=null) i.putExtra(e.getKey(),e.getValue());
        }

        int notificationId=(int)(System.currentTimeMillis()&0x7fffffff);
        i.putExtra("_notification_id",notificationId);
        PendingIntent pi=PendingIntent.getActivity(
                this,
                notificationId,
                i,
                PendingIntent.FLAG_UPDATE_CURRENT|PendingIntent.FLAG_IMMUTABLE
        );

        Notification.Builder b=Build.VERSION.SDK_INT>=26
                ? new Notification.Builder(this,MainActivity.CHANNEL_ID)
                : new Notification.Builder(this);

        b.setSmallIcon(R.drawable.ic_notification)
                .setContentTitle(title)
                .setContentText(body)
                .setStyle(new Notification.BigTextStyle().bigText(body))
                .setAutoCancel(true)
                .setContentIntent(pi);

        NotificationManager nm=(NotificationManager)getSystemService(Context.NOTIFICATION_SERVICE);
        if(nm!=null) nm.notify(notificationId,b.build());
    }
}
