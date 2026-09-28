package com.cemulas.hisseradar;

import android.app.Activity;
import android.content.Intent;
import android.graphics.Color;
import android.net.Uri;
import android.os.Bundle;
import android.os.Handler;
import android.os.Looper;
import android.view.View;
import android.view.Window;
import android.webkit.DownloadListener;
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

    @Override
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);
        Window w = getWindow();
        w.setStatusBarColor(Color.parseColor("#0A0F16"));
        w.setNavigationBarColor(Color.parseColor("#0A0F16"));

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

        web.setWebViewClient(new WebViewClient() {
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
        });

        web.setDownloadListener(new DownloadListener() {
            @Override
            public void onDownloadStart(String url, String userAgent, String contentDisposition, String mimeType, long contentLength) {
                disaAc(Uri.parse(url));
            }
        });

        if (savedInstanceState != null) {
            web.restoreState(savedInstanceState);
        } else {
            sayfayiYukle();
        }
    }

    private int surum() {
        try {
            return (int) getPackageManager().getPackageInfo(getPackageName(), 0).getLongVersionCode();
        } catch (Exception e) {
            return 0;
        }
    }

    private void disaAc(Uri u) {
        try {
            startActivity(new Intent(Intent.ACTION_VIEW, u));
        } catch (Exception ignored) {
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
            } catch (Exception ignored) {
            }
            if (html == null && kayit.exists()) {
                try (FileInputStream f = new FileInputStream(kayit)) {
                    html = oku(f);
                } catch (Exception ignored) {
                }
            }
            final String sonuc = html;
            ana.post(() -> {
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

    @Override
    protected void onSaveInstanceState(Bundle outState) {
        super.onSaveInstanceState(outState);
        web.saveState(outState);
    }

    /** Geri tuşu uygulamadan atmaz: önce uygulama içinde bir önceki ekrana döner. */
    @Override
    public void onBackPressed() {
        web.evaluateJavascript("(window.hrBack?window.hrBack():false)", sonuc -> {
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
        web.onResume();
    }

    @Override
    protected void onPause() {
        web.onPause();
        super.onPause();
    }
}
