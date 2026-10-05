package com.cemulas.hisseradar;

import android.app.job.JobParameters;
import android.app.job.JobService;
import android.content.Context;
import android.content.SharedPreferences;

import org.json.JSONArray;
import org.json.JSONObject;

import java.io.ByteArrayOutputStream;
import java.io.InputStream;
import java.net.HttpURLConnection;
import java.net.URL;
import java.text.SimpleDateFormat;
import java.util.Calendar;
import java.util.HashMap;
import java.util.Locale;
import java.util.Map;
import java.util.TimeZone;

/**
 * Arka plan kontrolü (seans saatlerinde ~15 dakikada bir): market.json'daki son fiyatları
 * telefondaki portföyle karşılaştırır ve gerekirse bildirim gösterir. Aynı uyarı günde bir kez gelir.
 */
public class KontrolIsi extends JobService {
    private static final String PIYASA = "https://raw.githubusercontent.com/maj834/Hisse/data/market.json";
    private static final TimeZone TSI = TimeZone.getTimeZone("Europe/Istanbul");

    @Override
    public boolean onStartJob(JobParameters p) {
        final Context c = getApplicationContext();
        new Thread(() -> {
            try {
                kontrol(c, false);
            } catch (Throwable ignored) {
            }
            jobFinished(p, false);
        }).start();
        return true;
    }

    @Override
    public boolean onStopJob(JobParameters p) {
        return true;
    }

    /** deneme=true: seans dışı da olsa çalış ve sonucu her durumda bildir (Deneme düğmesi). */
    static void kontrol(Context c, boolean deneme) throws Exception {
        if (!Bildirim.acik(c) && !deneme) return;
        Calendar s = Calendar.getInstance(TSI);
        int gun = s.get(Calendar.DAY_OF_WEEK), dk = s.get(Calendar.HOUR_OF_DAY) * 60 + s.get(Calendar.MINUTE);
        boolean seans = gun != Calendar.SATURDAY && gun != Calendar.SUNDAY && dk >= 600 && dk <= 1095;
        if (!seans && !deneme) return;

        SharedPreferences t = Bildirim.tercih(c);
        JSONArray h = new JSONArray(t.getString("portfoy", "[]"));
        if (h.length() == 0) {
            if (deneme) Bildirim.goster(c, 1, "Hisse Radar", "Bildirimler açık, ama Portföyüm'de kayıtlı hisse yok.");
            return;
        }
        JSONObject m = new JSONObject(indir(PIYASA + "?t=" + System.currentTimeMillis()));
        JSONArray sut = m.getJSONArray("sutun");
        int ik = sira(sut, "k"), ifi = sira(sut, "fiyat"), idg = sira(sut, "deg");
        Map<String, double[]> fiyat = new HashMap<>();
        JSONArray satir = m.getJSONArray("hisseler");
        for (int i = 0; i < satir.length(); i++) {
            JSONArray r = satir.getJSONArray(i);
            if (r.isNull(ifi)) continue;
            fiyat.put(r.getString(ik), new double[]{r.getDouble(ifi), r.isNull(idg) ? 0 : r.getDouble(idg)});
        }
        SimpleDateFormat f = new SimpleDateFormat("yyyy-MM-dd", Locale.US);
        f.setTimeZone(TSI);
        String bugun = f.format(s.getTime());
        // veri bugüne ait değilse (tatil, gecikme) eski fiyatla uyarı verme
        boolean taze = m.optString("updatedAt", "").startsWith(bugun);

        JSONObject gonderilen = new JSONObject(t.getString("gonderilen", "{}"));
        StringBuilder ozet = new StringBuilder();
        double deger = 0, gunluk = 0;
        int no = 10;
        for (int i = 0; i < h.length(); i++) {
            JSONObject x = h.getJSONObject(i);
            String k = x.getString("k");
            double[] fd = fiyat.get(k);
            no++;
            if (fd == null) continue;
            double son = fd[0], ch = fd[1], adet = x.optDouble("adet", 0);
            deger += son * adet;
            gunluk += son * adet - son * adet / (1 + ch / 100);
            if (ozet.length() > 0) ozet.append(" · ");
            ozet.append(k).append(' ').append(yuz(ch));
            if (!taze && !deneme) continue;
            double ust = x.optDouble("ust", 0), alt = x.optDouble("alt", 0);
            if (ust > 0 && son >= ust && yeni(gonderilen, k + ":ust", bugun))
                Bildirim.goster(c, no * 10 + 1, "🎯 " + k + " " + tl(son),
                        k + " " + x.optString("ustAd", "hedef") + " seviyesine (" + tl(ust) + ") ulaştı. Planına göre satışı düşünebilirsin. Yatırım tavsiyesi değildir.");
            if (alt > 0 && son <= alt && yeni(gonderilen, k + ":alt", bugun))
                Bildirim.goster(c, no * 10 + 2, "🛑 " + k + " " + tl(son),
                        k + " " + x.optString("altAd", "zarar-kes") + " seviyesine (" + tl(alt) + ") indi. Planın neyse ona uy, panikle karar verme. Yatırım tavsiyesi değildir.");
            String sert = Math.abs(ch) >= 8 ? "8" : Math.abs(ch) >= 4 ? "4" : null;
            if (sert != null && yeni(gonderilen, k + ":sert" + sert + (ch > 0 ? "+" : "-"), bugun))
                Bildirim.goster(c, no * 10 + 3, (ch > 0 ? "📈 " : "📉 ") + k + " bugün " + yuz(ch),
                        k + " şu an " + tl(son) + ". Sert hareketlerde ilk saatte karar vermek çoğu zaman pahalıya patlar; Portföyüm'deki planına bak.");
        }
        if (taze && dk >= 605 && deger > 0 && yeni(gonderilen, "acilis", bugun))
            Bildirim.goster(c, 2, "Açılış: portföyün bugün " + (gunluk >= 0 ? "+" : "−") + tl(Math.abs(gunluk)), ozet.toString());
        if (deneme)
            Bildirim.goster(c, 3, "Deneme bildirimi", "Bildirimler çalışıyor. Şu an: " + (ozet.length() > 0 ? ozet : "fiyat yok")
                    + (taze ? "" : " (son kapanış)"));
        t.edit().putString("gonderilen", temizle(gonderilen, bugun).toString()).apply();
    }

    private static boolean yeni(JSONObject g, String anahtar, String bugun) throws Exception {
        if (bugun.equals(g.optString(anahtar))) return false;
        g.put(anahtar, bugun);
        return true;
    }

    /** Eski günlerin kayıtlarını at (dosya büyümesin). */
    private static JSONObject temizle(JSONObject g, String bugun) throws Exception {
        JSONObject y = new JSONObject();
        JSONArray adlar = g.names();
        if (adlar == null) return y;
        for (int i = 0; i < adlar.length(); i++) {
            String a = adlar.getString(i);
            if (bugun.equals(g.optString(a))) y.put(a, bugun);
        }
        return y;
    }

    private static int sira(JSONArray a, String ad) throws Exception {
        for (int i = 0; i < a.length(); i++) if (ad.equals(a.getString(i))) return i;
        throw new Exception("sütun yok: " + ad);
    }

    private static String tl(double v) {
        return "₺" + String.format(new Locale("tr", "TR"), "%,.2f", v);
    }

    private static String yuz(double v) {
        return (v >= 0 ? "+" : "−") + "%" + String.format(new Locale("tr", "TR"), "%.1f", Math.abs(v));
    }

    static String indir(String adres) throws Exception {
        HttpURLConnection c = (HttpURLConnection) new URL(adres).openConnection();
        c.setConnectTimeout(8000);
        c.setReadTimeout(15000);
        c.setUseCaches(false);
        try {
            if (c.getResponseCode() != 200) throw new Exception("HTTP " + c.getResponseCode());
            InputStream in = c.getInputStream();
            ByteArrayOutputStream b = new ByteArrayOutputStream();
            byte[] tampon = new byte[16384];
            int n;
            while ((n = in.read(tampon)) > 0) b.write(tampon, 0, n);
            in.close();
            return b.toString("UTF-8");
        } finally {
            c.disconnect();
        }
    }
}
