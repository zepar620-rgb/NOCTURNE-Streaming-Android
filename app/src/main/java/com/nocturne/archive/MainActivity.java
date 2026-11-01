package com.nocturne.archive;

import android.app.Activity;
import android.app.AlertDialog;
import android.app.DownloadManager;
import android.content.ActivityNotFoundException;
import android.content.Context;
import android.content.Intent;
import android.graphics.Color;
import android.net.Uri;
import android.os.Bundle;
import android.os.Build;
import android.os.Environment;
import android.view.Gravity;
import android.view.View;
import android.view.Window;
import android.view.WindowManager;
import android.webkit.CookieManager;
import android.webkit.DownloadListener;
import android.webkit.ValueCallback;
import android.webkit.WebChromeClient;
import android.webkit.WebResourceRequest;
import android.webkit.WebSettings;
import android.webkit.WebView;
import android.webkit.WebViewClient;
import android.widget.Button;
import android.widget.FrameLayout;
import android.widget.LinearLayout;
import android.widget.ProgressBar;
import android.widget.TextView;
import android.widget.Toast;

import com.chaquo.python.PyObject;
import com.chaquo.python.Python;

import org.json.JSONObject;

import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;

/** Android shell which runs the complete Python/Flask NOCTURNE streaming anime app locally. */
public class MainActivity extends Activity {
    private static final String HOME_URL = "http://127.0.0.1:8765/";
    private static final String LOCAL_HOST = "127.0.0.1";
    private static final int FILE_CHOOSER_REQUEST = 7314;

    private FrameLayout root;
    private WebView webView;
    private LinearLayout loadingPanel;
    private TextView loadingText;
    private ProgressBar progressBar;
    private ValueCallback<Uri[]> filePathCallback;
    private ExecutorService startupExecutor;
    private View fullscreenView;
    private WebChromeClient.CustomViewCallback fullscreenCallback;
    private int originalSystemUiVisibility;
    private Window fullscreenWindow;
    private LinearLayout playerScreen;
    private WebView playerWebView;


    @Override
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);
        getWindow().setStatusBarColor(Color.rgb(16, 17, 19));
        getWindow().setNavigationBarColor(Color.rgb(8, 9, 11));
        getWindow().setSoftInputMode(WindowManager.LayoutParams.SOFT_INPUT_ADJUST_RESIZE);

        buildLayout();
        configureWebView(webView, true);
        startLocalPythonServer();
    }

    private void buildLayout() {
        root = new FrameLayout(this);
        root.setBackgroundColor(Color.rgb(16, 17, 19));

        webView = new WebView(this);
        webView.setBackgroundColor(Color.rgb(16, 17, 19));
        root.addView(webView, new FrameLayout.LayoutParams(
                FrameLayout.LayoutParams.MATCH_PARENT,
                FrameLayout.LayoutParams.MATCH_PARENT));

        loadingPanel = new LinearLayout(this);
        loadingPanel.setOrientation(LinearLayout.VERTICAL);
        loadingPanel.setGravity(Gravity.CENTER);
        loadingPanel.setPadding(dp(28), dp(28), dp(28), dp(28));
        loadingPanel.setBackgroundColor(Color.rgb(16, 17, 19));

        TextView mark = new TextView(this);
        mark.setText("✠  N O C T U R N E  ✠");
        mark.setTextColor(Color.rgb(220, 191, 207));
        mark.setTextSize(21);
        mark.setGravity(Gravity.CENTER);
        loadingPanel.addView(mark);

        TextView subtitle = new TextView(this);
        subtitle.setText("ANIME STREAMING ARCHIVE");
        subtitle.setTextColor(Color.rgb(158, 161, 169));
        subtitle.setTextSize(10);
        subtitle.setGravity(Gravity.CENTER);
        LinearLayout.LayoutParams subtitleParams = new LinearLayout.LayoutParams(
                LinearLayout.LayoutParams.WRAP_CONTENT, LinearLayout.LayoutParams.WRAP_CONTENT);
        subtitleParams.topMargin = dp(8);
        loadingPanel.addView(subtitle, subtitleParams);

        progressBar = new ProgressBar(this);
        LinearLayout.LayoutParams progressParams = new LinearLayout.LayoutParams(dp(180), dp(3));
        progressParams.topMargin = dp(28);
        loadingPanel.addView(progressBar, progressParams);

        loadingText = new TextView(this);
        loadingText.setText("MENYIAPKAN ARSIP LOKAL...");
        loadingText.setTextColor(Color.rgb(204, 169, 181));
        loadingText.setTextSize(12);
        loadingText.setGravity(Gravity.CENTER);
        LinearLayout.LayoutParams textParams = new LinearLayout.LayoutParams(
                LinearLayout.LayoutParams.MATCH_PARENT, LinearLayout.LayoutParams.WRAP_CONTENT);
        textParams.topMargin = dp(18);
        loadingPanel.addView(loadingText, textParams);

        root.addView(loadingPanel, new FrameLayout.LayoutParams(
                FrameLayout.LayoutParams.MATCH_PARENT,
                FrameLayout.LayoutParams.MATCH_PARENT));
        setContentView(root);
    }

    private void configureWebView(WebView target, boolean mainWindow) {
        WebSettings settings = target.getSettings();
        settings.setJavaScriptEnabled(true);
        settings.setDomStorageEnabled(true);
        settings.setDatabaseEnabled(true);
        settings.setLoadsImagesAutomatically(true);
        // Permit muted MP4 hero banners to autoplay; the banner video is always muted in the UI.
        settings.setMediaPlaybackRequiresUserGesture(false);
        settings.setSupportMultipleWindows(true);
        settings.setJavaScriptCanOpenWindowsAutomatically(true);
        settings.setAllowFileAccess(false);
        settings.setAllowContentAccess(true);
        settings.setMixedContentMode(WebSettings.MIXED_CONTENT_NEVER_ALLOW);
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
            settings.setSafeBrowsingEnabled(true);
        }

        target.setOverScrollMode(View.OVER_SCROLL_IF_CONTENT_SCROLLS);
        target.setBackgroundColor(Color.rgb(16, 17, 19));
        target.setWebChromeClient(new NocturneChromeClient(getWindow()));
        target.setDownloadListener(createDownloadListener());

        if (mainWindow) {
            target.setWebViewClient(new WebViewClient() {
                @Override
                public boolean shouldOverrideUrlLoading(WebView view, WebResourceRequest request) {
                    if (!request.isForMainFrame()) return false;
                    Uri uri = request.getUrl();
                    String host = uri.getHost();
                    if (LOCAL_HOST.equalsIgnoreCase(host)) return false;
                    String scheme = uri.getScheme() == null ? "" : uri.getScheme().toLowerCase();
                    if ("https".equals(scheme) || "http".equals(scheme)) {
                        openExternal(uri);
                        return true;
                    }
                    return true;
                }

                @Override
                public void onPageStarted(WebView view, String url, android.graphics.Bitmap favicon) {
                    super.onPageStarted(view, url, favicon);
                    if (loadingPanel.getVisibility() != View.GONE) {
                        loadingText.setText("MEMUAT NOCTURNE ARCHIVE...");
                    }
                }

                @Override
                public void onPageFinished(WebView view, String url) {
                    super.onPageFinished(view, url);
                    loadingPanel.setVisibility(View.GONE);
                }

                @Override
                public void onReceivedError(WebView view, android.webkit.WebResourceRequest request,
                                            android.webkit.WebResourceError error) {
                    super.onReceivedError(view, request, error);
                    if (request.isForMainFrame() && loadingPanel.getVisibility() == View.VISIBLE) {
                        loadingText.setText("HALAMAN GAGAL DIMUAT. PERIKSA KONEKSI, LALU COBA LAGI.");
                    }
                }
            });
        } else {
            // The separate window is used for source-host players which refuse iframe embedding.
            target.setWebViewClient(new WebViewClient());
        }

        CookieManager cookies = CookieManager.getInstance();
        cookies.setAcceptCookie(true);
        cookies.setAcceptThirdPartyCookies(target, true);
    }

    private DownloadListener createDownloadListener() {
        return (url, userAgent, contentDisposition, mimeType, contentLength) -> {
            try {
                if (Build.VERSION.SDK_INT <= Build.VERSION_CODES.P
                        && checkSelfPermission(android.Manifest.permission.WRITE_EXTERNAL_STORAGE)
                        != android.content.pm.PackageManager.PERMISSION_GRANTED) {
                    requestPermissions(new String[]{android.Manifest.permission.WRITE_EXTERNAL_STORAGE}, 7315);
                    Toast.makeText(this, "Izinkan akses penyimpanan, lalu tekan unduh sekali lagi.", Toast.LENGTH_LONG).show();
                    return;
                }
                Uri uri = Uri.parse(url);
                if ("blob".equalsIgnoreCase(uri.getScheme())) {
                    Toast.makeText(this,
                            "Unduhan blob tidak didukung langsung oleh Android WebView. Gunakan ekspor data yang tersedia atau browser eksternal.",
                            Toast.LENGTH_LONG).show();
                    return;
                }
                DownloadManager.Request request = new DownloadManager.Request(uri);
                request.setMimeType(mimeType);
                request.addRequestHeader("User-Agent", userAgent);
                String cookie = CookieManager.getInstance().getCookie(url);
                if (cookie != null) request.addRequestHeader("Cookie", cookie);
                request.setTitle("NOCTURNE download");
                request.setDescription("Mengunduh file dari sumber");
                request.setNotificationVisibility(DownloadManager.Request.VISIBILITY_VISIBLE_NOTIFY_COMPLETED);
                String fileName = "nocturne-download";
                if (contentDisposition != null) {
                    java.util.regex.Matcher matcher = java.util.regex.Pattern
                            .compile("filename=\\\"?([^\\\";]+)", java.util.regex.Pattern.CASE_INSENSITIVE)
                            .matcher(contentDisposition);
                    if (matcher.find()) fileName = matcher.group(1).trim();
                }
                fileName = fileName.replaceAll("[\\\\/:*?\"<>|\\p{Cntrl}]", "_").trim();
                if (fileName.isEmpty() || fileName.equals(".") || fileName.equals("..")) fileName = "nocturne-download";
                request.setDestinationInExternalPublicDir(Environment.DIRECTORY_DOWNLOADS, fileName);
                DownloadManager manager = (DownloadManager) getSystemService(Context.DOWNLOAD_SERVICE);
                manager.enqueue(request);
                Toast.makeText(this, "Unduhan dimulai", Toast.LENGTH_SHORT).show();
            } catch (Exception ex) {
                Toast.makeText(this, "Unduhan gagal: " + ex.getMessage(), Toast.LENGTH_LONG).show();
            }
        };
    }

    private void startLocalPythonServer() {
        loadingText.setText("MENJALANKAN PYTHON ENGINE...");
        startupExecutor = Executors.newSingleThreadExecutor();
        startupExecutor.execute(() -> {
            try {
                if (!Python.isStarted()) {
                    throw new IllegalStateException("Python runtime belum diinisialisasi oleh Chaquopy.");
                }
                PyObject bridge = Python.getInstance().getModule("nocturne_android");
                String result = bridge.callAttr("start_server", getFilesDir().getAbsolutePath()).toString();
                JSONObject status = new JSONObject(result);
                if (!status.optBoolean("ok", false)) {
                    throw new IllegalStateException(status.optString("error", "Server lokal tidak merespons"));
                }
                runOnUiThread(() -> {
                    loadingText.setText("ARSIP SIAP · MEMBUKA DASHBOARD");
                    webView.loadUrl(HOME_URL);
                });
            } catch (Throwable error) {
                runOnUiThread(() -> showStartupError(error));
            }
        });
    }

    private void showStartupError(Throwable error) {
        loadingPanel.removeAllViews();
        TextView title = new TextView(this);
        title.setText("NOCTURNE TIDAK DAPAT DIMULAI");
        title.setTextColor(Color.rgb(222, 105, 133));
        title.setTextSize(18);
        title.setGravity(Gravity.CENTER);
        loadingPanel.addView(title);

        TextView detail = new TextView(this);
        detail.setText(String.valueOf(error.getMessage()) +
                "\n\nPeriksa instalasi APK dan coba mulai ulang aplikasi.");
        detail.setTextColor(Color.rgb(220, 205, 216));
        detail.setTextSize(13);
        detail.setGravity(Gravity.CENTER);
        LinearLayout.LayoutParams detailParams = new LinearLayout.LayoutParams(
                LinearLayout.LayoutParams.MATCH_PARENT, LinearLayout.LayoutParams.WRAP_CONTENT);
        detailParams.topMargin = dp(18);
        loadingPanel.addView(detail, detailParams);

        Button retry = new Button(this);
        retry.setText("COBA LAGI");
        retry.setOnClickListener(v -> {
            loadingPanel.removeAllViews();
            buildLayout();
            configureWebView(webView, true);
            startLocalPythonServer();
        });
        LinearLayout.LayoutParams retryParams = new LinearLayout.LayoutParams(
                LinearLayout.LayoutParams.WRAP_CONTENT, LinearLayout.LayoutParams.WRAP_CONTENT);
        retryParams.topMargin = dp(18);
        loadingPanel.addView(retry, retryParams);
    }

    private void openExternal(Uri uri) {
        try {
            startActivity(new Intent(Intent.ACTION_VIEW, uri));
        } catch (ActivityNotFoundException ex) {
            Toast.makeText(this, "Tidak ada browser yang bisa membuka tautan ini.", Toast.LENGTH_LONG).show();
        }
    }

    private class NocturneChromeClient extends WebChromeClient {
        private final Window targetWindow;

        NocturneChromeClient(Window targetWindow) {
            this.targetWindow = targetWindow;
        }

        @Override
        public boolean onShowFileChooser(WebView view, ValueCallback<Uri[]> callback,
                                         FileChooserParams fileChooserParams) {
            if (filePathCallback != null) filePathCallback.onReceiveValue(null);
            filePathCallback = callback;
            Intent intent = new Intent(Intent.ACTION_OPEN_DOCUMENT);
            intent.addCategory(Intent.CATEGORY_OPENABLE);
            String[] accepted = fileChooserParams == null ? null : fileChooserParams.getAcceptTypes();
            java.util.ArrayList<String> mimeTypes = new java.util.ArrayList<>();
            if (accepted != null) {
                for (String item : accepted) {
                    if (item == null) continue;
                    for (String candidate : item.split(",")) {
                        String mime = candidate.trim();
                        if (mime.matches("[A-Za-z0-9.+-]+/[A-Za-z0-9.+*-]+")
                                && !"*/*".equals(mime) && !mimeTypes.contains(mime)) {
                            mimeTypes.add(mime);
                        }
                    }
                }
            }
            if (mimeTypes.isEmpty()) {
                intent.setType("*/*");
            } else if (mimeTypes.size() == 1) {
                intent.setType(mimeTypes.get(0));
            } else {
                intent.setType("*/*");
                intent.putExtra(Intent.EXTRA_MIME_TYPES, mimeTypes.toArray(new String[0]));
            }
            try {
                startActivityForResult(Intent.createChooser(intent, "Pilih file NOCTURNE"), FILE_CHOOSER_REQUEST);
            } catch (ActivityNotFoundException ex) {
                filePathCallback = null;
                Toast.makeText(MainActivity.this, "File picker tidak tersedia.", Toast.LENGTH_LONG).show();
                return false;
            }
            return true;
        }

        @Override
        public boolean onCreateWindow(WebView view, boolean isDialog, boolean isUserGesture,
                                      android.os.Message resultMsg) {
            showExternalPlayerWindow(resultMsg);
            return true;
        }

        @Override
        public void onShowCustomView(View view, CustomViewCallback callback) {
            if (fullscreenView != null) {
                callback.onCustomViewHidden();
                return;
            }
            fullscreenView = view;
            fullscreenCallback = callback;
            fullscreenWindow = targetWindow;
            originalSystemUiVisibility = targetWindow.getDecorView().getSystemUiVisibility();
            targetWindow.getDecorView().setSystemUiVisibility(
                    View.SYSTEM_UI_FLAG_FULLSCREEN |
                    View.SYSTEM_UI_FLAG_HIDE_NAVIGATION |
                    View.SYSTEM_UI_FLAG_IMMERSIVE_STICKY |
                    View.SYSTEM_UI_FLAG_LAYOUT_FULLSCREEN |
                    View.SYSTEM_UI_FLAG_LAYOUT_HIDE_NAVIGATION |
                    View.SYSTEM_UI_FLAG_LAYOUT_STABLE);
            ((FrameLayout) targetWindow.getDecorView()).addView(fullscreenView,
                    new FrameLayout.LayoutParams(-1, -1));
        }

        @Override
        public void onHideCustomView() {
            if (fullscreenView == null) return;
            Window host = fullscreenWindow == null ? targetWindow : fullscreenWindow;
            ((FrameLayout) host.getDecorView()).removeView(fullscreenView);
            fullscreenView = null;
            host.getDecorView().setSystemUiVisibility(originalSystemUiVisibility);
            fullscreenWindow = null;
            if (fullscreenCallback != null) fullscreenCallback.onCustomViewHidden();
            fullscreenCallback = null;
        }

        @Override
        public void onProgressChanged(WebView view, int newProgress) {
            if (view == webView && newProgress == 100 && loadingPanel.getVisibility() == View.VISIBLE) {
                loadingPanel.setVisibility(View.GONE);
            }
        }
    }

    private void showExternalPlayerWindow(android.os.Message resultMsg) {
        closeExternalPlayerWindow();

        WebView popup = new WebView(this);
        configureWebView(popup, false);
        playerWebView = popup;

        LinearLayout screen = new LinearLayout(this);
        screen.setOrientation(LinearLayout.VERTICAL);
        screen.setBackgroundColor(Color.rgb(16, 17, 19));
        playerScreen = screen;

        LinearLayout toolbar = new LinearLayout(this);
        toolbar.setGravity(Gravity.CENTER_VERTICAL);
        toolbar.setPadding(dp(10), dp(6), dp(10), dp(6));
        toolbar.setBackgroundColor(Color.rgb(31, 33, 38));

        Button back = new Button(this);
        back.setText("‹ KEMBALI");
        toolbar.addView(back, new LinearLayout.LayoutParams(dp(112), dp(46)));

        TextView label = new TextView(this);
        label.setText("✠  NOCTURNE PLAYER");
        label.setTextColor(Color.rgb(226, 228, 232));
        label.setTextSize(12);
        label.setGravity(Gravity.CENTER);
        toolbar.addView(label, new LinearLayout.LayoutParams(0, dp(46), 1));

        Button reload = new Button(this);
        reload.setText("MUAT ULANG");
        toolbar.addView(reload, new LinearLayout.LayoutParams(dp(112), dp(46)));

        screen.addView(toolbar, new LinearLayout.LayoutParams(-1, dp(58)));
        screen.addView(popup, new LinearLayout.LayoutParams(-1, 0, 1));

        back.setOnClickListener(v -> closeExternalPlayerWindow());
        reload.setOnClickListener(v -> {
            if (playerWebView != null) playerWebView.reload();
        });

        popup.setWebChromeClient(new NocturneChromeClient(getWindow()));
        popup.setWebViewClient(new WebViewClient());

        webView.setVisibility(View.GONE);
        root.addView(screen, new FrameLayout.LayoutParams(-1, -1));

        WebView.WebViewTransport transport =
                (WebView.WebViewTransport) resultMsg.obj;
        transport.setWebView(popup);
        resultMsg.sendToTarget();
    }

    private void closeExternalPlayerWindow() {
        if (playerScreen == null) return;

        WebView oldPlayer = playerWebView;
        LinearLayout oldScreen = playerScreen;
        playerWebView = null;
        playerScreen = null;

        if (root != null) root.removeView(oldScreen);

        if (oldPlayer != null) {
            oldPlayer.stopLoading();
            oldPlayer.setWebChromeClient(null);
            oldPlayer.setWebViewClient(null);
            oldPlayer.destroy();
        }

        if (webView != null) webView.setVisibility(View.VISIBLE);
    }

    protected void onActivityResult(int requestCode, int resultCode, Intent data) {
        super.onActivityResult(requestCode, resultCode, data);
        if (requestCode == FILE_CHOOSER_REQUEST && filePathCallback != null) {
            Uri[] results = null;
            if (resultCode == Activity.RESULT_OK && data != null) {
                Uri uri = data.getData();
                if (uri != null) results = new Uri[]{uri};
            }
            filePathCallback.onReceiveValue(results);
            filePathCallback = null;
        }
    }

    @Override
    public void onBackPressed() {
        if (fullscreenView != null) {
            new NocturneChromeClient(fullscreenWindow == null ? getWindow() : fullscreenWindow).onHideCustomView();
            return;
        }
        if (playerWebView != null) {
            if (playerWebView.canGoBack()) {
                playerWebView.goBack();
            } else {
                closeExternalPlayerWindow();
            }
            return;
        }
        if (webView != null && webView.canGoBack()) {
            webView.goBack();
        } else {
            super.onBackPressed();
        }
    }

    protected void onDestroy() {
        if (startupExecutor != null) startupExecutor.shutdownNow();
        if (webView != null) {
            webView.stopLoading();
            webView.destroy();
            webView = null;
        }
        super.onDestroy();
    }

    private int dp(int value) {
        return (int) (value * getResources().getDisplayMetrics().density + 0.5f);
    }
}
