package com.cemulas.hisseradar;

import android.app.Notification;
import android.app.NotificationChannel;
import android.app.NotificationManager;
import android.app.PendingIntent;
import android.app.job.JobInfo;
import android.app.job.JobScheduler;
import android.content.ComponentName;
import android.content.Context;
import android.content.Intent;
import android.content.SharedPreferences;
import android.content.pm.PackageManager;
import android.os.Build;

/**
 * Portföy bildirimleri: ayarlar, izin, zamanlama ve bildirim gösterme.
 * Portföy bilgisi yalnızca bu telefonda (SharedPreferences) tutulur; sunucuya gönderilmez.
 */
final class Bildirim {
    static final String KANAL = "portfoy";
    static final String TERCIH = "bildirim";
    static final int IS_NO = 4201;

    private Bildirim() {
    }

    static SharedPreferences tercih(Context c) {
        return c.getSharedPreferences(TERCIH, Context.MODE_PRIVATE);
    }

    static boolean acik(Context c) {
        return tercih(c).getBoolean("acik", false);
    }

    static boolean izinVar(Context c) {
        return Build.VERSION.SDK_INT < 33
                || c.checkSelfPermission("android.permission.POST_NOTIFICATIONS") == PackageManager.PERMISSION_GRANTED;
    }

    static void kanalKur(Context c) {
        NotificationManager nm = c.getSystemService(NotificationManager.class);
        if (nm == null || nm.getNotificationChannel(KANAL) != null) return;
        NotificationChannel k = new NotificationChannel(KANAL, "Portföy uyarıları", NotificationManager.IMPORTANCE_HIGH);
        k.setDescription("Hisselerin hedefe, zarar-kes seviyesine gelince ve sert hareketlerde");
        nm.createNotificationChannel(k);
    }

    /** Açıksa her 15 dakikada bir (telefon izin verdikçe) kontrolü zamanlar, kapalıysa iptal eder. */
    static void zamanla(Context c) {
        JobScheduler js = c.getSystemService(JobScheduler.class);
        if (js == null) return;
        if (!acik(c)) {
            js.cancel(IS_NO);
            return;
        }
        if (js.getPendingJob(IS_NO) != null) return;
        JobInfo is = new JobInfo.Builder(IS_NO, new ComponentName(c, KontrolIsi.class))
                .setRequiredNetworkType(JobInfo.NETWORK_TYPE_ANY)
                .setPeriodic(15 * 60 * 1000L)
                .setPersisted(true)
                .build();
        js.schedule(is);
    }

    static void goster(Context c, int no, String baslik, String metin) {
        if (!izinVar(c)) return;
        kanalKur(c);
        Intent ac = new Intent(c, MainActivity.class);
        ac.setFlags(Intent.FLAG_ACTIVITY_NEW_TASK | Intent.FLAG_ACTIVITY_CLEAR_TOP);
        ac.putExtra("sekme", "portfoy");
        PendingIntent pi = PendingIntent.getActivity(c, no, ac,
                PendingIntent.FLAG_UPDATE_CURRENT | PendingIntent.FLAG_IMMUTABLE);
        Notification n = new Notification.Builder(c, KANAL)
                .setSmallIcon(R.drawable.ic_bildirim)
                .setContentTitle(baslik)
                .setContentText(metin)
                .setStyle(new Notification.BigTextStyle().bigText(metin))
                .setContentIntent(pi)
                .setAutoCancel(true)
                .build();
        NotificationManager nm = c.getSystemService(NotificationManager.class);
        if (nm != null) nm.notify(no, n);
    }
}
