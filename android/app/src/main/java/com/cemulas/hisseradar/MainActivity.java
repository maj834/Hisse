package com.cemulas.hisseradar;

import android.app.Activity;
import android.content.Intent;
import android.graphics.Color;
import android.net.Uri;
import android.os.Build;
import android.os.Bundle;
import android.os.Handler;
import android.os.Looper;
import android.graphics.Bitmap;
import android.view.View;
import android.view.Window;
import android.view.ViewGroup;
import android.webkit.DownloadListener;
import android.webkit.JavascriptInterface;
import android.webkit.RenderProcessGoneDetail;
import android.webkit.WebResourceRequest;
import android.webkit.WebSettings;
import android.webkit.WebView;
import android.webkit.WebViewClient;
import android.widget.Toast;

import java.io.ByteArrayOutputStream;
import java.io.File;
import java.io.FileInputStream;
import java.io.FileOutputStream;
import java.io.InputStream;
import java.net.HttpURLConnection;
import java.net.URL;
import java.nio.charset.StandardCharsets;

/**
 * Hisse Radar.
 * Arayüz bir web sayfasıdır. Her açılışta sayfanın en güncel hâli GitHub'dan indirilir
 * (tasarım güncellemeleri APK'yı yeniden kurmadan gelir). İnternet yoksa son indirilen kopya,
 * o da yoksa APK'nın içindeki kopya açılır. Fiyat, haber ve yapay zekâ verilerini sayfa kendisi
 * 5 saniyede bir çeker.
 */
public class MainActivity extends Activity {
    private static final String GUNCEL = "https://raw.githubusercontent.com/maj834/Hisse/main/index.html";
    private static final String TABAN = "https://maj834.github.io/Hisse/";
    private static final String YEREL = "file:///android_asset/index.html";
    private WebView web;
    private long sonGeri = 0;
    /** Bildirim köprüsü yalnızca uygulamanın kendi sayfası açıkken çalışır (içeride açılan haber siteleri kullanamaz). */
    private volatile boolean kendiSayfam = false;

    @Override
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);
        Window w = getWindow();
        w.setStatusBarColor(Color.parseColor("#0A0F16"));
        w.setNavigationBarColor(Color.parseColor("#0A0F16"));

        webKur();
        sayfayiYukle();
        Bildirim.zamanla(this);
    }

    /** WebView'i kurar. Tarayıcı motoru çökerse yeniden kurulur, uygulama kapanmaz. */
    private void webKur() {
        web = new WebView(this);
        web.setBackgroundColor(Color.parseColor("#0A0F16"));
        web.setOverScrollMode(View.OVER_SCROLL_NEVER);
        setContentView(web);

        WebSettings s = web.getSettings();
        s.setJavaScriptEnabled(true);
        s.setDomStorageEnabled(true);
        s.setCacheMode(WebSettings.LOAD_DEFAULT);
        s.setAllowFileAccess(true);
        s.setSupportMultipleWindows(false); // target=_blank bağlantılar aynı pencerede açılır
        s.setTextZoom(100);
        s.setUserAgentString(s.getUserAgentString() + " HisseRadarApp/" + surum());

        web.addJavascriptInterface(new Kopru(), "HRBildirim");
        web.setWebViewClient(new WebViewClient() {
            @Override
            public void onPageStarted(WebView view, String url, Bitmap favicon) {
                // loadDataWithBaseURL sürüme göre taban adresi ya da data: bildirir; başka bir web sitesi ise köprü kapalı
                kendiSayfam = url == null || !url.startsWith("http") || url.startsWith(TABAN);
            }

            @Override
            public boolean shouldOverrideUrlLoading(WebView view, WebResourceRequest request) {
                Uri u = request.getUrl();
                String adres = u.toString();
                String sema = u.getScheme() == null ? "" : u.getScheme();
                if (adres.endsWith(".apk") || adres.contains("/releases/")) {
                    disaAc(u); // yeni sürüm indirmesi telefonun tarayıcısında
                    return true;
                }
                if (sema.equals("http") || sema.equals("https") || sema.equals("file") || sema.equals("data")) {
                    return false; // haberler ve TradingView uygulamanın içinde açılır
                }
                disaAc(u);
                return true;
            }

            @Override
            public boolean onRenderProcessGone(WebView view, RenderProcessGoneDetail detail) {
                // Tarayıcı motoru (ör. hafıza yetmediği için) kapandı: uygulamayı kapatma, sayfayı yeniden aç.
                try {
                    ViewGroup ust = (ViewGroup) view.getParent();
                    if (ust != null) ust.removeView(view);
                    view.destroy();
                } catch (Throwable ignored) {
                }
                if (!isFinishing()) {
                    webKur();
                    sayfayiYukle();
                }
                return true;
            }
        });

        web.setDownloadListener(new DownloadListener() {
            @Override
            public void onDownloadStart(String url, String userAgent, String contentDisposition, String mimeType, long contentLength) {
                disaAc(Uri.parse(url));
            }
        });
    }

    /** Sayfanın (Portföyüm) bildirim ayarları için çağırdığı köprü: window.HRBildirim */
    private class Kopru {
        /** "acik", "kapali" ya da "izin_yok" */
        @JavascriptInterface
        public String durum() {
            if (!kendiSayfam) return "kapali";
            if (!Bildirim.acik(MainActivity.this)) return "kapali";
            return Bildirim.izinVar(MainActivity.this) ? "acik" : "izin_yok";
        }

        @JavascriptInterface
        public void ac() {
            if (!kendiSayfam) return;
            Bildirim.tercih(MainActivity.this).edit().putBoolean("acik", true).apply();
            Bildirim.zamanla(MainActivity.this);
            runOnUiThread(() -> {
                if (!Bildirim.izinVar(MainActivity.this) && Build.VERSION.SDK_INT >= 33) {
                    requestPermissions(new String[]{"android.permission.POST_NOTIFICATIONS"}, 77);
                } else {
                    sayfayaHaber();
                }
            });
        }

        @JavascriptInterface
        public void kapat() {
            if (!kendiSayfam) return;
            Bildirim.tercih(MainActivity.this).edit().putBoolean("acik", false).apply();
            Bildirim.zamanla(MainActivity.this);
        }

        /** Portföy ve eşikler: [{k, adet, ust, ustAd, alt, altAd}] */
        @JavascriptInterface
        public void guncelle(String json) {
            if (!kendiSayfam || json == null || json.length() > 200000) return;
            Bildirim.tercih(MainActivity.this).edit().putString("portfoy", json).apply();
        }

        @JavascriptInterface
        public void dene() {
            if (!kendiSayfam) return;
            final android.content.Context c = getApplicationContext();
            new Thread(() -> {
                try {
                    KontrolIsi.kontrol(c, true);
                } catch (Throwable e) {
                    Bildirim.goster(c, 3, "Deneme bildirimi", "Bildirimler çalışıyor, ama fiyatlar şu an alınamadı.");
                }
            }).start();
        }
    }

    private void sayfayaHaber() {
        if (web != null) web.evaluateJavascript("window.hrBildirimSonuc&&window.hrBildirimSonuc()", null);
    }

    @Override
    public void onRequestPermissionsResult(int kod, String[] izinler, int[] sonuc) {
        super.onRequestPermissionsResult(kod, izinler, sonuc);
        if (kod == 77) sayfayaHaber();
    }

    private int surum() {
        try {
            android.content.pm.PackageInfo p = getPackageManager().getPackageInfo(getPackageName(), 0);
            return Build.VERSION.SDK_INT >= 28 ? (int) p.getLongVersionCode() : p.versionCode;
        } catch (Throwable e) {
            return 0;
        }
    }

    private void disaAc(Uri u) {
        try {
            startActivity(new Intent(Intent.ACTION_VIEW, u));
        } catch (Throwable ignored) {
        }
    }

    /** Güncel sayfayı indir; olmazsa kayıtlı kopyayı, o da yoksa APK içindekini aç. */
    private void sayfayiYukle() {
        final File kayit = new File(getFilesDir(), "index.html");
        final Handler ana = new Handler(Looper.getMainLooper());
        new Thread(() -> {
            String html = null;
            try {
                HttpURLConnection c = (HttpURLConnection) new URL(GUNCEL + "?t=" + System.currentTimeMillis()).openConnection();
                c.setConnectTimeout(6000);
                c.setReadTimeout(10000);
                c.setUseCaches(false);
                if (c.getResponseCode() == 200) {
                    String metin = oku(c.getInputStream());
                    if (metin.contains("Hisse Radar") && metin.length() > 10000) {
                        html = metin;
                        try (FileOutputStream f = new FileOutputStream(kayit)) {
                            f.write(metin.getBytes(StandardCharsets.UTF_8));
                        }
                    }
                }
                c.disconnect();
            } catch (Throwable ignored) {
            }
            if (html == null && kayit.exists()) {
                try (FileInputStream f = new FileInputStream(kayit)) {
                    html = oku(f);
                } catch (Exception ignored) {
                }
            }
            final String sonuc = html;
            ana.post(() -> {
                if (isFinishing() || isDestroyed() || web == null) return;
                if (sonuc != null) {
                    web.loadDataWithBaseURL(TABAN, sonuc, "text/html", "utf-8", null);
                } else {
                    web.loadUrl(YEREL);
                }
            });
        }).start();
    }

    private static String oku(InputStream in) throws java.io.IOException {
        ByteArrayOutputStream b = new ByteArrayOutputStream();
        byte[] tampon = new byte[16384];
        int n;
        while ((n = in.read(tampon)) > 0) b.write(tampon, 0, n);
        in.close();
        return b.toString("UTF-8");
    }

    /** Geri tuşu uygulamadan atmaz: önce uygulama içinde bir önceki ekrana döner. */
    @Override
    public void onBackPressed() {
        if (web == null) {
            finish();
            return;
        }
        web.evaluateJavascript("(window.hrBack?window.hrBack():false)", sonuc -> {
            if (isFinishing() || isDestroyed()) return;
            if ("true".equals(sonuc)) return;
            if (web.canGoBack()) {
                web.goBack();
                return;
            }
            long simdi = System.currentTimeMillis();
            if (simdi - sonGeri < 2000) {
                finish();
            } else {
                sonGeri = simdi;
                Toast.makeText(this, "Çıkmak için tekrar bas", Toast.LENGTH_SHORT).show();
            }
        });
    }

    @Override
    protected void onResume() {
        super.onResume();
        if (web != null) web.onResume();
    }

    @Override
    protected void onPause() {
        if (web != null) web.onPause();
        super.onPause();
    }

    @Override
    protected void onDestroy() {
        if (web != null) {
            try {
                web.destroy();
            } catch (Throwable ignored) {
            }
            web = null;
        }
        super.onDestroy();
    }
}
