package ir.solico.energyreport.lite;

import android.Manifest;
import android.app.Notification;
import android.app.NotificationManager;
import android.app.PendingIntent;
import android.content.BroadcastReceiver;
import android.content.Context;
import android.content.Intent;
import android.content.pm.PackageManager;
import android.os.Build;
import android.widget.Toast;

import java.util.UUID;

public class ArchiveTransferReceiver extends BroadcastReceiver {
    public static final String ACTION_ACCEPT="ir.solico.energyreport.lite.ARCHIVE_ACCEPT";
    public static final String ACTION_REJECT="ir.solico.energyreport.lite.ARCHIVE_REJECT";
    private static final String EXTRA_REQUEST_ID="archive_request_id";
    private static final String EXTRA_NOTIFICATION_ID="archive_notification_id";
    private static final String EXTRA_TARGET_WINDOWS_ID="archive_target_windows_id";

    public static void showOffer(Context context,String requestId,boolean fromWindows){showOffer(context,requestId,fromWindows,"");}

    public static void showOffer(Context context,String requestId,boolean fromWindows,String targetWindowsId){
        Context c=context.getApplicationContext();
        if(Build.VERSION.SDK_INT>=33 && c.checkSelfPermission(Manifest.permission.POST_NOTIFICATIONS)!=PackageManager.PERMISSION_GRANTED){
            return;
        }
        if(requestId==null||requestId.trim().isEmpty()) requestId=UUID.randomUUID().toString();
        int notificationId=0x41000000 | (requestId.hashCode() & 0x00ffffff);

        Intent accept=new Intent(c,ArchiveTransferReceiver.class).setAction(ACTION_ACCEPT);
        accept.putExtra(EXTRA_REQUEST_ID,requestId);
        accept.putExtra(EXTRA_NOTIFICATION_ID,notificationId);
        accept.putExtra(EXTRA_TARGET_WINDOWS_ID,targetWindowsId==null?"":targetWindowsId);
        PendingIntent acceptPi=PendingIntent.getBroadcast(c,notificationId+1,accept,PendingIntent.FLAG_UPDATE_CURRENT|PendingIntent.FLAG_IMMUTABLE);

        Intent reject=new Intent(c,ArchiveTransferReceiver.class).setAction(ACTION_REJECT);
        reject.putExtra(EXTRA_REQUEST_ID,requestId);
        reject.putExtra(EXTRA_NOTIFICATION_ID,notificationId);
        reject.putExtra(EXTRA_TARGET_WINDOWS_ID,targetWindowsId==null?"":targetWindowsId);
        PendingIntent rejectPi=PendingIntent.getBroadcast(c,notificationId+2,reject,PendingIntent.FLAG_UPDATE_CURRENT|PendingIntent.FLAG_IMMUTABLE);

        Intent open=new Intent(c,MainActivity.class);
        open.addFlags(Intent.FLAG_ACTIVITY_CLEAR_TOP|Intent.FLAG_ACTIVITY_SINGLE_TOP);
        open.putExtra("archive_offer_only","1");
        PendingIntent openPi=PendingIntent.getActivity(c,notificationId+3,open,PendingIntent.FLAG_UPDATE_CURRENT|PendingIntent.FLAG_IMMUTABLE);

        String body=fromWindows
                ?"آرشیو روزانه و ماهانه این گوشی پس از دریافت کامل با آرشیو فعلی ویندوز جایگزین می‌شود؛ گزارش‌های حذف‌شده در ویندوز از این آرشیو کنار می‌روند. تأیید می‌کنید؟"
                :"برای نمایش تاریخچه کامل، گزارش‌های قبلی ذخیره‌شده در ویندوز روی این گوشی دریافت شوند؟";

        Notification.Builder b=Build.VERSION.SDK_INT>=26
                ?new Notification.Builder(c,MainActivity.CHANNEL_ID)
                :new Notification.Builder(c);
        b.setSmallIcon(R.drawable.ic_notification)
                .setContentTitle("دریافت آرشیو گزارش‌ها")
                .setContentText(body)
                .setStyle(new Notification.BigTextStyle().bigText(body))
                .setContentIntent(openPi)
                .setAutoCancel(false)
                .addAction(new Notification.Action.Builder(R.drawable.ic_notification,"تأیید",acceptPi).build())
                .addAction(new Notification.Action.Builder(R.drawable.ic_notification,"رد",rejectPi).build());

        NotificationManager nm=(NotificationManager)c.getSystemService(Context.NOTIFICATION_SERVICE);
        if(nm!=null) nm.notify(notificationId,b.build());
    }

    @Override public void onReceive(Context context,Intent intent){
        if(intent==null) return;
        String requestId=intent.getStringExtra(EXTRA_REQUEST_ID);
        int notificationId=intent.getIntExtra(EXTRA_NOTIFICATION_ID,0);
        String targetWindowsId=intent.getStringExtra(EXTRA_TARGET_WINDOWS_ID);
        NotificationManager nm=(NotificationManager)context.getSystemService(Context.NOTIFICATION_SERVICE);
        if(nm!=null && notificationId!=0) nm.cancel(notificationId);

        if(ACTION_ACCEPT.equals(intent.getAction())){
            DeviceRegistrar.setArchiveConsent(context,true);
            DeviceRegistrar.sendArchiveDecision(context,requestId,true,targetWindowsId);
            DeviceRegistrar.requestHistorySync(context,true,requestId,targetWindowsId);
            Toast.makeText(context,"دریافت آرشیو گزارش‌ها شروع شد",Toast.LENGTH_SHORT).show();
        }else if(ACTION_REJECT.equals(intent.getAction())){
            DeviceRegistrar.setArchiveConsent(context,false);
            DeviceRegistrar.sendArchiveDecision(context,requestId,false,targetWindowsId);
            Toast.makeText(context,"دریافت آرشیو رد شد",Toast.LENGTH_SHORT).show();
        }
    }
}
