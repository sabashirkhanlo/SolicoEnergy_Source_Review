package ir.solico.energyreport.lite;

import android.content.ContentValues;
import android.content.Context;
import android.database.Cursor;
import android.database.sqlite.SQLiteDatabase;
import android.database.sqlite.SQLiteOpenHelper;

import org.json.JSONArray;
import org.json.JSONObject;

import java.text.SimpleDateFormat;
import java.util.Calendar;
import java.util.Date;
import java.util.Locale;
import java.util.Map;
import java.util.HashMap;
import java.util.Iterator;

public class NotificationArchiveDb extends SQLiteOpenHelper {
    private static final String DB_NAME = "solico_notifications.db";
    private static final int DB_VERSION = 3;
    private static final String T = "notifications";

    public NotificationArchiveDb(Context context) { super(context, DB_NAME, null, DB_VERSION); }

    @Override public void onCreate(SQLiteDatabase db) {
        db.execSQL("CREATE TABLE " + T + " (" +
                "id INTEGER PRIMARY KEY AUTOINCREMENT," +
                "event_id TEXT UNIQUE," +
                "received_at INTEGER NOT NULL," +
                "report_date TEXT," +
                "report_date_key TEXT," +
                "report_date_iso TEXT," +
                "report_kind TEXT," +
                "report_title TEXT," +
                "title TEXT," +
                "body TEXT," +
                "kpi_factory TEXT,kpi_plant TEXT,kpi_grid TEXT,kpi_share TEXT,kpi_peak TEXT,kpi_avg TEXT,kpi_pf TEXT," +
                "kpi_factory_raw TEXT,kpi_plant_raw TEXT,kpi_grid_raw TEXT,kpi_share_raw TEXT,kpi_peak_raw TEXT,kpi_avg_raw TEXT,kpi_pf_raw TEXT," +
                "generator_active TEXT,generator_status TEXT,generator_breakdown TEXT," +
                "pdf_url TEXT)");
        db.execSQL("CREATE INDEX idx_notifications_date ON " + T + "(report_date_key, received_at DESC)");
        db.execSQL("CREATE INDEX idx_notifications_kind ON " + T + "(report_kind, received_at DESC)");
    }
    @Override public void onUpgrade(SQLiteDatabase db, int oldVersion, int newVersion) {
        if(oldVersion<2){
            try{db.execSQL("ALTER TABLE "+T+" ADD COLUMN kpi_avg TEXT DEFAULT ''");}catch(Exception ignored){}
            try{db.execSQL("ALTER TABLE "+T+" ADD COLUMN kpi_avg_raw TEXT DEFAULT ''");}catch(Exception ignored){}
        }
        if(oldVersion<3){
            try{db.execSQL("ALTER TABLE "+T+" ADD COLUMN generator_active TEXT DEFAULT ''");}catch(Exception ignored){}
            try{db.execSQL("ALTER TABLE "+T+" ADD COLUMN generator_status TEXT DEFAULT ''");}catch(Exception ignored){}
            try{db.execSQL("ALTER TABLE "+T+" ADD COLUMN generator_breakdown TEXT DEFAULT ''");}catch(Exception ignored){}
        }
    }

    private static String v(Map<String,String> m, String k) {
        String x=m==null?null:m.get(k); return x==null?"":x.trim();
    }
    public static String normalizeDate(String s) {
        if (s==null) return "";
        String a=s.trim();
        String fa="۰۱۲۳۴۵۶۷۸۹", ar="٠١٢٣٤٥٦٧٨٩";
        for(int i=0;i<10;i++){a=a.replace(fa.charAt(i),(char)('0'+i));a=a.replace(ar.charAt(i),(char)('0'+i));}
        return a.replace('-', '/').replace('.', '/').replaceAll("\\s+", "");
    }


    public synchronized long insertJson(JSONObject obj) {
        if(obj==null) return -1;
        Map<String,String> data=new HashMap<>();
        Iterator<String> keys=obj.keys();
        while(keys.hasNext()){
            String k=keys.next();
            Object v=obj.opt(k);
            if(v!=null && v!=JSONObject.NULL) data.put(k,String.valueOf(v));
        }
        return insert(data);
    }

    public synchronized long insert(Map<String,String> data) {
        if (data==null || data.isEmpty()) return -1;
        ContentValues c=new ContentValues();
        long now=System.currentTimeMillis();
        String event=v(data,"event_id");
        if(event.isEmpty()) event=v(data,"title")+"|"+v(data,"report_date")+"|"+now;
        c.put("event_id",event); c.put("received_at",now);
        String rd=v(data,"report_date"); c.put("report_date",rd); c.put("report_date_key",normalizeDate(rd));
        String[] keys={"report_date_iso","report_kind","report_title","title","body","kpi_factory","kpi_plant","kpi_grid","kpi_share","kpi_peak","kpi_avg","kpi_pf","kpi_factory_raw","kpi_plant_raw","kpi_grid_raw","kpi_share_raw","kpi_peak_raw","kpi_avg_raw","kpi_pf_raw","generator_active","generator_status","generator_breakdown","pdf_url"};
        for(String k:keys)c.put(k,v(data,k));
        long inserted=getWritableDatabase().insertWithOnConflict(T,null,c,SQLiteDatabase.CONFLICT_IGNORE);
        if(inserted>=0) return inserted;
        // Existing history rows are enriched when newer app versions add KPI fields,
        // without turning a duplicate delivery into a second notification.
        ContentValues u=new ContentValues(c);
        u.remove("event_id");u.remove("received_at");
        getWritableDatabase().update(T,u,"event_id=?",new String[]{event});
        return -2;
    }

    public synchronized int replaceArchive(JSONArray reports, long startedAt) throws Exception {
        // All batches must be present before this transaction starts.
        SQLiteDatabase db=getWritableDatabase();
        db.beginTransaction();
        try {
            // Preserve live reports received after the user started this transfer.
            db.delete(T,"report_kind IN ('daily','monthly') AND received_at<=?",new String[]{String.valueOf(startedAt)});
            int count=0;
            for(int i=0;i<reports.length();i++){
                JSONObject report=reports.getJSONObject(i);
                String kind=report.optString("report_kind","");
                String key=normalizeDate(report.optString("report_date",""));
                if(!("daily".equals(kind)||"monthly".equals(kind))||key.isEmpty())throw new IllegalArgumentException("Invalid archive report");
                boolean newer=false;
                try(Cursor cursor=db.rawQuery("SELECT 1 FROM "+T+" WHERE report_kind=? AND report_date_key=? AND received_at>? LIMIT 1",new String[]{kind,key,String.valueOf(startedAt)})){newer=cursor.moveToFirst();}
                if(!newer){
                    db.delete(T,"report_kind=? AND report_date_key=?",new String[]{kind,key});
                    if(insertJson(report)==-1)throw new IllegalStateException("Archive insert failed");
                }
                count++;
            }
            db.setTransactionSuccessful();return count;
        } finally {db.endTransaction();}
    }

    private JSONObject row(Cursor c) throws Exception {
        JSONObject o=new JSONObject();
        String[] cols={"id","event_id","received_at","report_date","report_date_iso","report_kind","report_title","title","body","kpi_factory","kpi_plant","kpi_grid","kpi_share","kpi_peak","kpi_avg","kpi_pf","kpi_factory_raw","kpi_plant_raw","kpi_grid_raw","kpi_share_raw","kpi_peak_raw","kpi_avg_raw","kpi_pf_raw","generator_active","generator_status","generator_breakdown","pdf_url"};
        for(String col:cols){int i=c.getColumnIndex(col);if(i>=0){if("id".equals(col)||"received_at".equals(col))o.put(col,c.getLong(i));else o.put(col,c.getString(i)==null?"":c.getString(i));}}
        long ts=o.optLong("received_at",0); if(ts>0)o.put("received_text",new SimpleDateFormat("yyyy-MM-dd HH:mm",Locale.US).format(new Date(ts)));
        return o;
    }
    private String queryJson(String sql,String[] args){
        JSONArray a=new JSONArray(); try(Cursor c=getReadableDatabase().rawQuery(sql,args)){while(c.moveToNext())a.put(row(c));}catch(Exception ignored){} return a.toString();
    }
    public synchronized String historyJson(String date){
        String key=normalizeDate(date);
        if(key.isEmpty())return queryJson("SELECT * FROM "+T+" ORDER BY received_at DESC LIMIT 200",null);
        return queryJson("SELECT * FROM "+T+" WHERE report_date_key=? ORDER BY received_at DESC LIMIT 200",new String[]{key});
    }
    public synchronized String monthHistoryJson(String month){
        String key=normalizeDate(month);
        if(key.length()>7) key=key.substring(0,7);
        if(!key.matches("\\d{4}/\\d{2}")) return "[]";
        return queryJson("SELECT * FROM "+T+" WHERE report_kind='daily' AND report_date_key LIKE ? AND kpi_factory<>'' ORDER BY report_date_key ASC, received_at DESC",new String[]{key+"/%"});
    }



    public synchronized boolean hasAnyReports(){
        try(Cursor c=getReadableDatabase().rawQuery("SELECT 1 FROM "+T+" WHERE report_title<>'' OR kpi_factory<>'' LIMIT 1",null)){return c.moveToFirst();}
        catch(Exception ignored){return false;}
    }

    public synchronized String latestEventId(){
        try(Cursor c=getReadableDatabase().rawQuery("SELECT event_id FROM "+T+" WHERE report_title<>'' OR kpi_factory<>'' ORDER BY received_at DESC LIMIT 1",null)){
            if(c.moveToFirst()){String v=c.getString(0);return v==null?"":v;}
        }catch(Exception ignored){}
        return "";
    }

    public synchronized String latestReportJson(){
        try(Cursor c=getReadableDatabase().rawQuery("SELECT * FROM "+T+" WHERE report_title<>'' OR kpi_factory<>'' ORDER BY received_at DESC LIMIT 1",null)){if(c.moveToFirst())return row(c).toString();}catch(Exception ignored){} return "";
    }

    public synchronized String latestDailyEventId(){
        try(Cursor c=getReadableDatabase().rawQuery("SELECT event_id FROM "+T+" WHERE (report_kind='daily' OR report_kind='') AND (report_title<>'' OR kpi_factory<>'') ORDER BY received_at DESC,id DESC LIMIT 1",null)){
            if(c.moveToFirst()){String v=c.getString(0);return v==null?"":v;}
        }catch(Exception ignored){}
        return "";
    }

    public synchronized String latestDailyReportJson(){
        try(Cursor c=getReadableDatabase().rawQuery("SELECT * FROM "+T+" WHERE (report_kind='daily' OR report_kind='') AND (report_title<>'' OR kpi_factory<>'') ORDER BY received_at DESC,id DESC LIMIT 1",null)){
            if(c.moveToFirst())return row(c).toString();
        }catch(Exception ignored){}
        return "";
    }
    public synchronized String byIdJson(long id){
        try(Cursor c=getReadableDatabase().rawQuery("SELECT * FROM "+T+" WHERE id=? LIMIT 1",new String[]{String.valueOf(id)})){if(c.moveToFirst())return row(c).toString();}catch(Exception ignored){} return "";
    }
    private String previousIso(String iso){
        try{SimpleDateFormat f=new SimpleDateFormat("yyyy-MM-dd",Locale.US);f.setLenient(false);Date d=f.parse(iso);Calendar cal=Calendar.getInstance();cal.setTime(d);cal.add(Calendar.DAY_OF_MONTH,-1);return f.format(cal.getTime());}catch(Exception e){return "";}
    }
    public synchronized String comparisonJson(String date){
        JSONObject out=new JSONObject(); Cursor cur=null; try{
            String key=normalizeDate(date);
            if(key.isEmpty())cur=getReadableDatabase().rawQuery("SELECT * FROM "+T+" WHERE report_kind='daily' ORDER BY received_at DESC LIMIT 1",null);
            else cur=getReadableDatabase().rawQuery("SELECT * FROM "+T+" WHERE report_kind='daily' AND report_date_key=? ORDER BY received_at DESC LIMIT 1",new String[]{key});
            if(!cur.moveToFirst())return "{}";
            JSONObject current=row(cur); out.put("current",current); long received=current.optLong("received_at",0); String iso=current.optString("report_date_iso","");
            JSONObject previous=null; String pIso=previousIso(iso);
            if(!pIso.isEmpty())try(Cursor p=getReadableDatabase().rawQuery("SELECT * FROM "+T+" WHERE report_kind='daily' AND report_date_iso=? ORDER BY received_at DESC LIMIT 1",new String[]{pIso})){if(p.moveToFirst())previous=row(p);}
            // IMPORTANT: compare only with the immediately previous calendar day.
            // Do NOT fall back to the latest older report (e.g. 26th vs 20th).
            if(previous!=null)out.put("previous",previous);
            out.put("previous_iso",pIso);
            return out.toString();
        }catch(Exception e){return "{}";}finally{if(cur!=null)cur.close();}
    }
}