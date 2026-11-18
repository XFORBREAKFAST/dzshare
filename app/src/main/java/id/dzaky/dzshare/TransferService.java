package id.dzaky.dzshare;

import android.app.Notification;
import android.app.NotificationChannel;
import android.app.NotificationManager;
import android.app.PendingIntent;
import android.app.Service;
import android.content.Context;
import android.content.Intent;
import android.content.pm.ServiceInfo;
import android.net.Uri;
import android.net.wifi.WifiManager;
import android.os.Build;
import android.os.IBinder;
import android.os.ParcelFileDescriptor;
import android.os.PowerManager;
import android.webkit.MimeTypeMap;

import androidx.documentfile.provider.DocumentFile;

import org.json.JSONObject;

import java.io.FileOutputStream;
import java.io.IOException;
import java.io.InputStream;
import java.io.OutputStream;
import java.net.HttpURLConnection;
import java.net.URL;
import java.util.ArrayDeque;
import java.util.HashMap;
import java.util.List;
import java.util.Locale;
import java.util.Map;
import java.util.concurrent.atomic.AtomicInteger;
import java.util.concurrent.atomic.AtomicLong;

/**
 * Menjalankan download (PC -> HP) dan upload (HP -> PC) di latar belakang,
 * tetap jalan walau layar mati. Folder dibuat ulang persis seperti di PC.
 */
public class TransferService extends Service {

    static class Job {
        String url;   // URL lengkap (sudah termasuk ?k=token)
        String rel;   // path relatif tujuan, mis. "Project/Footage/a.mp4"
        long size;
        Uri tree;     // folder simpan (download)
        Uri src;      // file sumber (upload)
        boolean up;
    }

    private static final String CH = "transfer";
    private static final int NOTIF_ID = 1, NOTIF_DONE = 2;
    private static final int WORKERS = 3;
    private static final int BUF = 1 << 20;

    private static final Object LOCK = new Object();
    private static final ArrayDeque<Job> QUEUE = new ArrayDeque<>();
    private static TransferService inst;
    private static int active = 0;

    static volatile boolean running, cancel, finished;
    static volatile String current = "", error = "";
    static volatile long startTime, endTime, speed;
    static final AtomicLong total = new AtomicLong(), done = new AtomicLong();
    static final AtomicInteger files = new AtomicInteger(), filesDone = new AtomicInteger(),
            failed = new AtomicInteger(), skipped = new AtomicInteger();

    // cache folder agar tidak listing berulang (SAF lambat kalau dipanggil terus)
    private final Map<String, DocumentFile> dirs = new HashMap<>();
    private final Map<String, Map<String, DocumentFile>> children = new HashMap<>();

    private PowerManager.WakeLock wake;
    private WifiManager.WifiLock wifi;
    private Thread ticker;

    // ------------------------------------------------------------ API statis
    static void enqueue(Context ctx, List<Job> jobs) {
        synchronized (LOCK) {
            boolean fresh = !running;
            if (fresh) {
                QUEUE.clear();
                total.set(0); done.set(0); files.set(0); filesDone.set(0);
                failed.set(0); skipped.set(0);
                cancel = false; finished = false; error = ""; current = "";
                speed = 0; startTime = System.currentTimeMillis(); endTime = 0;
            }
            for (Job j : jobs) {
                QUEUE.add(j);
                total.addAndGet(j.size);
                files.incrementAndGet();
            }
            if (fresh) {
                running = true;
                Intent i = new Intent(ctx, TransferService.class);
                if (Build.VERSION.SDK_INT >= 26) ctx.startForegroundService(i);
                else ctx.startService(i);
            } else if (inst != null) {
                inst.spawnWorkers();
            }
        }
    }

    static void cancelAll() {
        synchronized (LOCK) {
            if (!running) return;
            cancel = true;
            QUEUE.clear();
        }
    }

    static String stateJson() {
        JSONObject o = new JSONObject();
        try {
            o.put("running", running);
            o.put("finished", finished);
            o.put("cancel", cancel);
            o.put("total", total.get());
            o.put("done", done.get());
            o.put("files", files.get());
            o.put("filesDone", filesDone.get());
            o.put("failed", failed.get());
            o.put("skipped", skipped.get());
            o.put("current", current);
            o.put("speed", speed);
            o.put("error", error);
            long end = running ? System.currentTimeMillis() : endTime;
            o.put("elapsed", Math.max(0, end - startTime));
        } catch (Exception ignored) { }
        return o.toString();
    }

    // ------------------------------------------------------------ lifecycle
    @Override
    public void onCreate() {
        super.onCreate();
        if (Build.VERSION.SDK_INT >= 26) {
            NotificationChannel ch = new NotificationChannel(CH, "Transfer file",
                    NotificationManager.IMPORTANCE_LOW);
            getSystemService(NotificationManager.class).createNotificationChannel(ch);
        }
    }

    @Override
    public int onStartCommand(Intent intent, int flags, int startId) {
        Notification n = buildNotif("Menyiapkan…", "", 0, 0, true);
        if (Build.VERSION.SDK_INT >= 29) {
            startForeground(NOTIF_ID, n, ServiceInfo.FOREGROUND_SERVICE_TYPE_DATA_SYNC);
        } else {
            startForeground(NOTIF_ID, n);
        }
        synchronized (LOCK) {
            if (inst == this && active > 0) return START_NOT_STICKY;
            inst = this;
            dirs.clear();
            children.clear();
            acquireLocks();
            startTicker();
            spawnWorkers();
            if (active == 0) finishAll(); // antrean kosong
        }
        return START_NOT_STICKY;
    }

    @Override
    public IBinder onBind(Intent intent) { return null; }

    private void spawnWorkers() {
        synchronized (LOCK) {
            int want = Math.min(WORKERS, QUEUE.size() + active);
            while (active < want) {
                active++;
                new Thread(this::worker, "dz-worker").start();
            }
        }
    }

    private void worker() {
        while (true) {
            Job j;
            synchronized (LOCK) {
                j = cancel ? null : QUEUE.poll();
                if (j == null) {
                    active--;
                    if (active == 0) finishAll();
                    return;
                }
            }
            try {
                if (j.up) upload(j); else download(j);
                filesDone.incrementAndGet();
            } catch (Exception e) {
                if (!cancel) {
                    failed.incrementAndGet();
                    error = j.rel + ": " + e.getMessage();
                }
            }
        }
    }

    private void finishAll() {
        // dipanggil dengan LOCK dipegang
        running = false;
        finished = true;
        endTime = System.currentTimeMillis();
        inst = null;
        if (ticker != null) ticker.interrupt();
        releaseLocks();
        String title;
        if (cancel) title = "Transfer dibatalkan";
        else if (failed.get() > 0) title = "Selesai, " + failed.get() + " file gagal";
        else title = "Selesai: " + filesDone.get() + " file";
        Notification n = buildNotif(title, human(done.get()) + " dalam " + secs(endTime - startTime),
                0, 0, false);
        stopForeground(STOP_FOREGROUND_REMOVE);
        getSystemService(NotificationManager.class).notify(NOTIF_DONE, n);
        stopSelf();
    }

    // ------------------------------------------------------------ download
    private void download(Job j) throws IOException {
        String rel = clean(j.rel);
        int slash = rel.lastIndexOf('/');
        String dirPath = slash < 0 ? "" : rel.substring(0, slash);
        String name = slash < 0 ? rel : rel.substring(slash + 1);
        current = rel;

        String key = j.tree.toString();
        DocumentFile dir = dir(key, j.tree, dirPath);
        DocumentFile f;
        long have = 0;
        synchronized (this) {
            f = child(key + "|" + dirPath, dir, name);
            if (f != null && f.isFile()) {
                have = f.length();
                if (have == j.size) {         // sudah ada & lengkap -> lewati
                    done.addAndGet(j.size);
                    skipped.incrementAndGet();
                    return;
                }
                if (have > j.size) { f.delete(); f = null; have = 0; }
            } else if (f != null) {
                throw new IOException("ada folder dengan nama sama");
            }
            if (f == null) {
                f = dir.createFile(mime(name), name);
                if (f == null) throw new IOException("tidak bisa membuat file");
                children.get(key + "|" + dirPath).put(name, f);
            }
        }

        HttpURLConnection c = open(j.url);
        if (have > 0) c.setRequestProperty("Range", "bytes=" + have + "-");
        int code = c.getResponseCode();
        if (code != 200 && code != 206) { c.disconnect(); throw new IOException("HTTP " + code); }
        boolean append = have > 0 && code == 206;

        ParcelFileDescriptor pfd = null;
        if (append) {
            try { pfd = getContentResolver().openFileDescriptor(f.getUri(), "wa"); }
            catch (Exception e) { pfd = null; }
            if (pfd == null) {        // penyedia tidak dukung append -> ulang dari awal
                c.disconnect();
                c = open(j.url);
                code = c.getResponseCode();
                if (code != 200) { c.disconnect(); throw new IOException("HTTP " + code); }
                append = false;
            }
        }
        if (!append) pfd = getContentResolver().openFileDescriptor(f.getUri(), "wt");
        if (pfd == null) { c.disconnect(); throw new IOException("tidak bisa menulis file"); }
        if (append) done.addAndGet(have);

        long got = 0;
        try (ParcelFileDescriptor p = pfd;
             FileOutputStream out = new FileOutputStream(p.getFileDescriptor());
             InputStream in = c.getInputStream()) {
            byte[] buf = new byte[BUF];
            int n;
            while ((n = in.read(buf)) > 0) {
                if (cancel) throw new IOException("dibatalkan");
                out.write(buf, 0, n);
                got += n;
                done.addAndGet(n);
            }
        } catch (IOException e) {
            if (!append) done.addAndGet(-got); // kalau gagal, progress dikurangi lagi
            else done.addAndGet(-(got + have));
            throw e;
        } finally {
            c.disconnect();
        }
        long expect = append ? j.size - have : j.size;
        if (got < expect) {
            done.addAndGet(-(append ? got + have : got));
            throw new IOException("koneksi terputus");
        }
    }

    // ------------------------------------------------------------ upload
    private void upload(Job j) throws IOException {
        current = j.rel;
        HttpURLConnection c = open(j.url);
        c.setRequestMethod("PUT");
        c.setDoOutput(true);
        c.setFixedLengthStreamingMode(j.size);
        c.setRequestProperty("Content-Type", "application/octet-stream");
        long sent = 0;
        try (InputStream in = getContentResolver().openInputStream(j.src);
             OutputStream out = c.getOutputStream()) {
            if (in == null) throw new IOException("file tidak bisa dibaca");
            byte[] buf = new byte[BUF];
            int n;
            while ((n = in.read(buf)) > 0) {
                if (cancel) throw new IOException("dibatalkan");
                out.write(buf, 0, n);
                sent += n;
                done.addAndGet(n);
            }
            out.flush();
            int code = c.getResponseCode();
            if (code != 200) throw new IOException("HTTP " + code);
        } catch (IOException e) {
            done.addAndGet(-sent);
            throw e;
        } finally {
            c.disconnect();
        }
    }

    // ------------------------------------------------------------ folder (SAF)
    private synchronized DocumentFile dir(String key, Uri tree, String path) throws IOException {
        String k = key + "|" + path;
        DocumentFile d = dirs.get(k);
        if (d != null) return d;
        if (path.isEmpty()) {
            d = DocumentFile.fromTreeUri(this, tree);
            if (d == null || !d.canWrite()) throw new IOException("folder simpan tidak bisa ditulis, pilih ulang");
            dirs.put(k, d);
            return d;
        }
        int slash = path.lastIndexOf('/');
        String parentPath = slash < 0 ? "" : path.substring(0, slash);
        String name = path.substring(slash + 1);
        DocumentFile parent = dir(key, tree, parentPath);
        DocumentFile c = child(key + "|" + parentPath, parent, name);
        if (c == null || !c.isDirectory()) {
            c = parent.createDirectory(name);
            if (c == null) throw new IOException("tidak bisa membuat folder " + name);
            children.get(key + "|" + parentPath).put(name, c);
        }
        dirs.put(k, c);
        return c;
    }

    private synchronized DocumentFile child(String k, DocumentFile parent, String name) {
        Map<String, DocumentFile> m = children.get(k);
        if (m == null) {
            m = new HashMap<>();
            for (DocumentFile f : parent.listFiles()) {
                String n = f.getName();
                if (n != null) m.put(n, f);
            }
            children.put(k, m);
        }
        return m.get(name);
    }

    // ------------------------------------------------------------ helper
    private static HttpURLConnection open(String url) throws IOException {
        HttpURLConnection c = (HttpURLConnection) new URL(url).openConnection();
        c.setConnectTimeout(8000);
        c.setReadTimeout(30000);
        c.setUseCaches(false);
        return c;
    }

    private static String clean(String rel) {
        StringBuilder sb = new StringBuilder();
        for (String part : rel.replace('\\', '/').split("/")) {
            if (part.isEmpty() || part.equals(".") || part.equals("..")) continue;
            if (sb.length() > 0) sb.append('/');
            sb.append(part.replaceAll("[:*?\"<>|]", "_"));
        }
        return sb.length() == 0 ? "file" : sb.toString();
    }

    private static String mime(String name) {
        int dot = name.lastIndexOf('.');
        if (dot > 0) {
            String m = MimeTypeMap.getSingleton()
                    .getMimeTypeFromExtension(name.substring(dot + 1).toLowerCase(Locale.ROOT));
            if (m != null) return m;
        }
        return "application/octet-stream";
    }

    static String human(long b) {
        if (b < 1024) return b + " B";
        String[] u = {"KB", "MB", "GB", "TB"};
        double v = b;
        int i = -1;
        do { v /= 1024; i++; } while (v >= 1024 && i < 3);
        return String.format(Locale.US, v < 10 ? "%.2f %s" : "%.1f %s", v, u[i]);
    }

    private static String secs(long ms) {
        long s = ms / 1000;
        return s >= 60 ? (s / 60) + " mnt " + (s % 60) + " dtk" : s + " dtk";
    }

    private Notification buildNotif(String title, String text, int max, int prog, boolean ongoing) {
        Notification.Builder b = Build.VERSION.SDK_INT >= 26
                ? new Notification.Builder(this, CH) : new Notification.Builder(this);
        Intent open = new Intent(this, MainActivity.class);
        open.setFlags(Intent.FLAG_ACTIVITY_SINGLE_TOP);
        PendingIntent pi = PendingIntent.getActivity(this, 0, open,
                PendingIntent.FLAG_IMMUTABLE | PendingIntent.FLAG_UPDATE_CURRENT);
        b.setSmallIcon(R.drawable.ic_notif)
                .setContentTitle(title)
                .setContentText(text)
                .setContentIntent(pi)
                .setOnlyAlertOnce(true)
                .setOngoing(ongoing)
                .setAutoCancel(!ongoing);
        if (ongoing) b.setProgress(max, prog, max == 0);
        return b.build();
    }

    private void startTicker() {
        ticker = new Thread(() -> {
            long lastDone = done.get(), lastT = System.currentTimeMillis();
            NotificationManager nm = getSystemService(NotificationManager.class);
            while (running && !Thread.currentThread().isInterrupted()) {
                try { Thread.sleep(700); } catch (InterruptedException e) { return; }
                long now = System.currentTimeMillis(), d = done.get();
                long cur = (now > lastT) ? (d - lastDone) * 1000 / (now - lastT) : 0;
                speed = speed == 0 ? cur : (speed * 6 + cur * 4) / 10; // dihaluskan
                lastDone = d; lastT = now;
                long t = Math.max(1, total.get());
                int pct = (int) (d * 1000 / t);
                if (!running) return;
                nm.notify(NOTIF_ID, buildNotif(
                        (filesDone.get() + failed.get()) + "/" + files.get() + " file · " + human(speed) + "/s",
                        current, 1000, pct, true));
            }
        }, "dz-ticker");
        ticker.start();
    }

    private void acquireLocks() {
        try {
            PowerManager pm = (PowerManager) getSystemService(POWER_SERVICE);
            wake = pm.newWakeLock(PowerManager.PARTIAL_WAKE_LOCK, "DzShare:transfer");
            wake.acquire(6 * 60 * 60 * 1000L);
            WifiManager wm = (WifiManager) getApplicationContext().getSystemService(WIFI_SERVICE);
            //noinspection deprecation
            wifi = wm.createWifiLock(WifiManager.WIFI_MODE_FULL_HIGH_PERF, "DzShare:wifi");
            wifi.acquire();
        } catch (Exception ignored) { }
    }

    private void releaseLocks() {
        try { if (wake != null && wake.isHeld()) wake.release(); } catch (Exception ignored) { }
        try { if (wifi != null && wifi.isHeld()) wifi.release(); } catch (Exception ignored) { }
    }
}
