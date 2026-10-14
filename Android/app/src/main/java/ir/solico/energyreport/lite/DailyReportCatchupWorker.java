package ir.solico.energyreport.lite;

import android.content.Context;
import android.content.SharedPreferences;

import androidx.annotation.NonNull;
import androidx.work.BackoffPolicy;
import androidx.work.Constraints;
import androidx.work.ExistingWorkPolicy;
import androidx.work.NetworkType;
import androidx.work.OneTimeWorkRequest;
import androidx.work.WorkManager;
import androidx.work.Worker;
import androidx.work.WorkerParameters;

import java.text.SimpleDateFormat;
import java.util.Calendar;
import java.util.Date;
import java.util.Locale;
import java.util.concurrent.TimeUnit;

/**
 * Fallback for the 08:00 daily report notification.
 * If the phone is offline at 08:03, WorkManager keeps this job pending and
 * starts it when Internet becomes available. FCM remains the primary path.
 */
public class DailyReportCatchupWorker extends Worker {
    private static final String PREFS="solico_daily_catchup";
    private static final String LAST_SUCCESS="last_success_day";

    public DailyReportCatchupWorker(@NonNull Context appContext,@NonNull WorkerParameters params){
        super(appContext,params);
    }

    private static String dayKey(long millis){
        return new SimpleDateFormat("yyyyMMdd",Locale.US).format(new Date(millis));
    }

    private static long atEight03(Calendar base){
        Calendar c=(Calendar)base.clone();
        c.set(Calendar.HOUR_OF_DAY,8);
        c.set(Calendar.MINUTE,3);
        c.set(Calendar.SECOND,0);
        c.set(Calendar.MILLISECOND,0);
        return c.getTimeInMillis();
    }

    public static void schedule(Context context){
        Context c=context.getApplicationContext();
        Calendar now=Calendar.getInstance();
        long nowMs=now.getTimeInMillis();
        long todayDue=atEight03(now);
        SharedPreferences p=c.getSharedPreferences(PREFS,Context.MODE_PRIVATE);
        String today=dayKey(nowMs);
        String last=p.getString(LAST_SUCCESS,"");

        long due;
        String workDay;
        if(nowMs>=todayDue && !today.equals(last)){
            // App started after 08:03 and today's fallback has not succeeded yet.
            due=nowMs+1500L;
            workDay=today;
        }else{
            Calendar next=(Calendar)now.clone();
            if(nowMs>=todayDue || today.equals(last)) next.add(Calendar.DAY_OF_YEAR,1);
            due=atEight03(next);
            workDay=dayKey(due);
        }

        Constraints constraints=new Constraints.Builder()
                .setRequiredNetworkType(NetworkType.CONNECTED)
                .build();
        OneTimeWorkRequest request=new OneTimeWorkRequest.Builder(DailyReportCatchupWorker.class)
                .setInitialDelay(Math.max(1000L,due-nowMs),TimeUnit.MILLISECONDS)
                .setConstraints(constraints)
                .setBackoffCriteria(BackoffPolicy.LINEAR,15,TimeUnit.MINUTES)
                .addTag("solico_daily_catchup")
                .build();
        WorkManager.getInstance(c).enqueueUniqueWork(
                "solico_daily_catchup_"+workDay,
                ExistingWorkPolicy.KEEP,
                request
        );
    }

    @NonNull @Override public Result doWork(){
        Context c=getApplicationContext();
        boolean loggedIn=c.getSharedPreferences("solico_local_login",Context.MODE_PRIVATE)
                .getBoolean("logged_in",false);
        if(!loggedIn){
            schedule(c);
            return Result.success();
        }
        boolean ok=DeviceRegistrar.catchUpMissedDailyReports(c);
        if(!ok) return Result.retry();
        c.getSharedPreferences(PREFS,Context.MODE_PRIVATE)
                .edit().putString(LAST_SUCCESS,dayKey(System.currentTimeMillis())).apply();
        schedule(c);
        return Result.success();
    }
}
