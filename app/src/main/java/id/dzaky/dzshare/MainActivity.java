package id.dzaky.dzshare;

import android.Manifest;
import android.annotation.SuppressLint;
import android.app.Activity;
import android.content.ClipData;
import android.content.Intent;
import android.content.SharedPreferences;
import android.content.UriPermission;
import android.content.pm.PackageManager;
import android.database.Cursor;
import android.net.Uri;
import android.os.Build;
import android.os.Bundle;
import android.provider.DocumentsContract;
import android.provider.OpenableColumns;
import android.webkit.JavascriptInterface;
import android.webkit.WebChromeClient;
import android.webkit.WebSettings;
import android.webkit.WebView;
import android.widget.Toast;

import com.google.zxing.integration.android.IntentIntegrator;
import com.google.zxing.integration.android.IntentResult;

import org.json.JSONArray;
import org.json.JSONObject;

import java.io.ByteArrayOutputStream;
import java.io.InputStream;
import java.net.HttpURLConnection;
import java.net.URL;
import java.net.URLEncoder;
import java.util.ArrayList;
import java.util.List;

public class MainActivity extends Activity {

    private static final int REQ_TREE = 11, REQ_UP = 12;
    private WebView web;
    private SharedPreferences sp;
    private String upBase = "";

    @SuppressLint({"SetJavaScriptEnabled", "AddJavascriptInterface"})
    @Override
    protected void onCreate(Bundle b) {
        super.onCreate(b);
        sp = getSharedPreferences("dz", MODE_PRIVATE);
        web = new WebView(this);
        web.setBackgroundColor(0xFF0B0F17);
        WebSettings s = web.getSettings();
        s.setJavaScriptEnabled(true);
        s.setDomStorageEnabled(true);
        s.setTextZoom(100);
        web.setWebChromeClient(new WebChromeClient());
        web.addJavascriptInterface(new Bridge(), "Dz");
        web.setOverScrollMode(WebView.OVER_SCROLL_NEVER);
        setContentView(web);
        web.loadUrl("file:///android_asset/index.html");

        if (Build.VERSION.SDK_INT >= 33
                && checkSelfPermission(Manifest.permission.POST_NOTIFICATIONS) != PackageManager.PERMISSION_GRANTED) {
            requestPermissions(new String[]{Manifest.permission.POST_NOTIFICATIONS}, 1);
        }
    }

    private void js(String code) {
        runOnUiThread(() -> web.evaluateJavascript(code, null));
    }

    private static String q(String s) {
        return JSONObject.quote(s == null ? "" : s);
    }

    @SuppressWarnings("deprecation")
    @Override
    public void onBackPressed() {
        web.evaluateJavascript("window.dzBack&&dzBack()", v -> {
            if (!"true".equals(v)) finish();
        });
    }

    // ------------------------------------------------------------ folder simpan
    private Uri tree() {
        String t = sp.getString("tree", null);
        if (t == null) return null;
        Uri u = Uri.parse(t);
        for (UriPermission p : getContentResolver().getPersistedUriPermissions()) {
            if (p.getUri().equals(u) && p.isWritePermission()) return u;
        }
        return null;
    }

    private String folderName() {
        Uri u = tree();
        if (u == null) return "";
        try {
            String id = DocumentsContract.getTreeDocumentId(u); // mis. "primary:Download/DzShare"
            int c = id.indexOf(':');
            String vol = c > 0 ? id.substring(0, c) : "";
            String path = c >= 0 ? id.substring(c + 1) : id;
            String root = "primary".equals(vol) ? "Memori internal" : "Kartu SD";
            return path.isEmpty() ? root : root + "/" + path;
        } catch (Exception e) {
            return u.getLastPathSegment();
        }
    }

    // ------------------------------------------------------------ hasil activity
    @Override
    protected void onActivityResult(int req, int res, Intent data) {
        IntentResult r = IntentIntegrator.parseActivityResult(req, res, data);
        if (r != null) {
            js("onScan(" + q(r.getContents()) + ")");
            return;
        }
        super.onActivityResult(req, res, data);
        if (res != RESULT_OK || data == null) {
            if (req == REQ_TREE) js("onFolder(" + q(folderName()) + ",false)");
            return;
        }
        if (req == REQ_TREE) {
            Uri u = data.getData();
            if (u == null) return;
            try {
                getContentResolver().takePersistableUriPermission(u,
                        Intent.FLAG_GRANT_READ_URI_PERMISSION | Intent.FLAG_GRANT_WRITE_URI_PERMISSION);
            } catch (Exception ignored) { }
            // lepaskan izin folder lama
            String old = sp.getString("tree", null);
            if (old != null && !old.equals(u.toString())) {
                try {
                    getContentResolver().releasePersistableUriPermission(Uri.parse(old),
                            Intent.FLAG_GRANT_READ_URI_PERMISSION | Intent.FLAG_GRANT_WRITE_URI_PERMISSION);
                } catch (Exception ignored) { }
            }
            sp.edit().putString("tree", u.toString()).apply();
            js("onFolder(" + q(folderName()) + ",true)");
        } else if (req == REQ_UP) {
            List<Uri> uris = new ArrayList<>();
            ClipData cd = data.getClipData();
            if (cd != null) for (int i = 0; i < cd.getItemCount(); i++) uris.add(cd.getItemAt(i).getUri());
            else if (data.getData() != null) uris.add(data.getData());
            new Thread(() -> queueUploads(uris)).start();
        }
    }

    private void queueUploads(List<Uri> uris) {
        List<TransferService.Job> jobs = new ArrayList<>();
        for (Uri u : uris) {
            try {
                getContentResolver().takePersistableUriPermission(u, Intent.FLAG_GRANT_READ_URI_PERMISSION);
            } catch (Exception ignored) { }
            String name = "file";
            long size = -1;
            try (Cursor c = getContentResolver().query(u, null, null, null, null)) {
                if (c != null && c.moveToFirst()) {
                    int ni = c.getColumnIndex(OpenableColumns.DISPLAY_NAME);
                    int si = c.getColumnIndex(OpenableColumns.SIZE);
                    if (ni >= 0 && !c.isNull(ni)) name = c.getString(ni);
                    if (si >= 0 && !c.isNull(si)) size = c.getLong(si);
                }
            } catch (Exception ignored) { }
            if (size < 0) continue;
            TransferService.Job j = new TransferService.Job();
            j.up = true;
            j.src = u;
            j.size = size;
            j.rel = name;
            try {
                j.url = upBase + "&name=" + URLEncoder.encode(name, "UTF-8");
            } catch (Exception e) {
                continue;
            }
            jobs.add(j);
        }
        if (!jobs.isEmpty()) TransferService.enqueue(this, jobs);
        js("onQueued(" + jobs.size() + ")");
    }

    // ------------------------------------------------------------ jembatan JS
    private class Bridge {
        @JavascriptInterface
        public void scan() {
            runOnUiThread(() -> {
                IntentIntegrator ii = new IntentIntegrator(MainActivity.this);
                ii.setDesiredBarcodeFormats(IntentIntegrator.QR_CODE);
                ii.setPrompt("Arahkan ke QR di layar PC");
                ii.setBeepEnabled(false);
                ii.setOrientationLocked(true);
                ii.initiateScan();
            });
        }

        @JavascriptInterface
        public void get(final int id, final String url) {
            new Thread(() -> {
                int code = 0;
                String body;
                HttpURLConnection c = null;
                try {
                    c = (HttpURLConnection) new URL(url).openConnection();
                    c.setConnectTimeout(5000);
                    c.setReadTimeout(20000);
                    code = c.getResponseCode();
                    InputStream in = code < 400 ? c.getInputStream() : c.getErrorStream();
                    ByteArrayOutputStream bo = new ByteArrayOutputStream();
                    if (in != null) {
                        byte[] buf = new byte[16384];
                        int n;
                        while ((n = in.read(buf)) > 0) bo.write(buf, 0, n);
                        in.close();
                    }
                    body = bo.toString("UTF-8");
                } catch (Exception e) {
                    code = 0;
                    body = String.valueOf(e.getMessage());
                } finally {
                    if (c != null) c.disconnect();
                }
                js("onHttp(" + id + "," + code + "," + q(body) + ")");
            }).start();
        }

        @JavascriptInterface
        public void pickFolder() {
            runOnUiThread(() -> {
                Intent i = new Intent(Intent.ACTION_OPEN_DOCUMENT_TREE);
                i.addFlags(Intent.FLAG_GRANT_READ_URI_PERMISSION
                        | Intent.FLAG_GRANT_WRITE_URI_PERMISSION
                        | Intent.FLAG_GRANT_PERSISTABLE_URI_PERMISSION
                        | Intent.FLAG_GRANT_PREFIX_URI_PERMISSION);
                if (Build.VERSION.SDK_INT >= 26) {
                    Uri cur = tree();
                    Uri start = cur != null ? cur : DocumentsContract.buildDocumentUri(
                            "com.android.externalstorage.documents", "primary:Download");
                    i.putExtra(DocumentsContract.EXTRA_INITIAL_URI, start);
                }
                try {
                    startActivityForResult(i, REQ_TREE);
                } catch (Exception e) {
                    Toast.makeText(MainActivity.this, "Pemilih folder tidak tersedia", Toast.LENGTH_LONG).show();
                }
            });
        }

        @JavascriptInterface
        public String folderName() { return MainActivity.this.folderName(); }

        @JavascriptInterface
        public boolean hasFolder() { return tree() != null; }

        @JavascriptInterface
        public String download(String json) {
            Uri t = tree();
            if (t == null) return "Pilih folder simpan dulu";
            try {
                JSONArray a = new JSONArray(json);
                List<TransferService.Job> jobs = new ArrayList<>();
                for (int i = 0; i < a.length(); i++) {
                    JSONObject o = a.getJSONObject(i);
                    TransferService.Job j = new TransferService.Job();
                    j.url = o.getString("url");
                    j.rel = o.getString("rel");
                    j.size = o.optLong("size", 0);
                    j.tree = t;
                    jobs.add(j);
                }
                if (jobs.isEmpty()) return "Tidak ada file";
                TransferService.enqueue(MainActivity.this, jobs);
                return "ok";
            } catch (Exception e) {
                return "Gagal: " + e.getMessage();
            }
        }

        @JavascriptInterface
        public void pickUpload(String base) {
            upBase = base;
            runOnUiThread(() -> {
                Intent i = new Intent(Intent.ACTION_OPEN_DOCUMENT);
                i.addCategory(Intent.CATEGORY_OPENABLE);
                i.setType("*/*");
                i.putExtra(Intent.EXTRA_ALLOW_MULTIPLE, true);
                startActivityForResult(i, REQ_UP);
            });
        }

        @JavascriptInterface
        public String state() { return TransferService.stateJson(); }

        @JavascriptInterface
        public void cancel() { TransferService.cancelAll(); }

        @JavascriptInterface
        public void setPref(String k, String v) { sp.edit().putString("p_" + k, v).apply(); }

        @JavascriptInterface
        public String getPref(String k) { return sp.getString("p_" + k, ""); }

        @JavascriptInterface
        public void toast(String m) {
            runOnUiThread(() -> Toast.makeText(MainActivity.this, m, Toast.LENGTH_LONG).show());
        }
    }
}
