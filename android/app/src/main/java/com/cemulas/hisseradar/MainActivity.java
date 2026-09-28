package com.cemulas.hisseradar;

import android.app.Activity;
import android.content.Intent;
import android.graphics.Color;
import android.net.Uri;
import android.os.Bundle;
import android.view.View;
import android.view.Window;
import android.webkit.DownloadListener;
import android.webkit.WebResourceError;
import android.webkit.WebResourceRequest;
import android.webkit.WebResourceResponse;
import android.webkit.WebSettings;
import android.webkit.WebView;
import android.webkit.WebViewClient;

/**
 * Hisse Radar: uygulamanın arayüzü bir web sayfasıdır.
 * Önce GitHub Pages'teki güncel sürümü açar (tasarım güncellemeleri APK'yı yeniden kurmadan gelir);
 * internet yoksa ya da sayfa bulunamazsa APK'nın içindeki kopyayı açar.
 * Fiyat, haber ve yapay zekâ verilerini sayfa kendisi 5 saniyede bir GitHub'dan çeker.
 */
public class MainActivity extends Activity {
    private static final String CANLI = "https://maj834.github.io/Hisse/";
    private static final String YEREL = "file:///android_asset/index.html";
    private WebView web;
    private boolean yereleGecti = false;

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
        s.setAllowUniversalAccessFromFileURLs(false);
        s.setSupportMultipleWindows(false); // target=_blank bağlantılar aynı pencerede açılır
        s.setMediaPlaybackRequiresUserGesture(true);
        s.setTextZoom(100);

        web.setWebViewClient(new WebViewClient() {
            @Override
            public boolean shouldOverrideUrlLoading(WebView view, WebResourceRequest request) {
                Uri u = request.getUrl();
                String sema = u.getScheme() == null ? "" : u.getScheme();
                String adres = u.toString();
                if (adres.endsWith(".apk") || adres.contains("/releases/")) {
                    try {
                        startActivity(new Intent(Intent.ACTION_VIEW, u));
                    } catch (Exception ignored) {
                    }
                    return true;
                }
                if (sema.equals("http") || sema.equals("https") || sema.equals("file")) {
                    return false; // haberler ve TradingView uygulamanın içinde açılır
                }
                try {
                    startActivity(new Intent(Intent.ACTION_VIEW, u));
                } catch (Exception ignored) {
                }
                return true;
            }

            @Override
            public void onReceivedError(WebView view, WebResourceRequest request, WebResourceError error) {
                if (request.isForMainFrame()) yereleGec();
            }

            @Override
            public void onReceivedHttpError(WebView view, WebResourceRequest request, WebResourceResponse response) {
                if (request.isForMainFrame() && request.getUrl().toString().startsWith(CANLI)) yereleGec();
            }
        });

        // İndirme bağlantıları (ör. yeni APK) telefonun tarayıcısında/indiricisinde açılsın
        web.setDownloadListener(new DownloadListener() {
            @Override
            public void onDownloadStart(String url, String userAgent, String contentDisposition, String mimeType, long contentLength) {
                try {
                    startActivity(new Intent(Intent.ACTION_VIEW, Uri.parse(url)));
                } catch (Exception ignored) {
                }
            }
        });

        if (savedInstanceState != null) {
            web.restoreState(savedInstanceState);
        } else {
            web.loadUrl(CANLI);
        }
    }

    private void yereleGec() {
        if (yereleGecti) return;
        yereleGecti = true;
        web.loadUrl(YEREL);
    }

    @Override
    protected void onSaveInstanceState(Bundle outState) {
        super.onSaveInstanceState(outState);
        web.saveState(outState);
    }

    @Override
    public void onBackPressed() {
        if (web.canGoBack()) {
            web.goBack();
        } else {
            super.onBackPressed();
        }
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
