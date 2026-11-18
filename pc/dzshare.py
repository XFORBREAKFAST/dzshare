#!/usr/bin/env python3
"""
DzShare — kirim file besar PC -> HP (dan HP -> PC) lewat Wi-Fi yang sama.
Tidak perlu install apa pun di HP: cukup scan QR, buka di browser.

Cara pakai:
    python dzshare.py                 # buka panel di browser PC
    python dzshare.py film.mp4 folder # langsung share file/folder ini
Opsi:
    --port 8765      ganti port
    --no-browser     jangan buka panel otomatis
Hanya pakai library bawaan Python 3.9+.
"""
import os, sys, socket, json, threading, time, queue, secrets, mimetypes
import zipfile, webbrowser, subprocess, urllib.parse
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from pathlib import Path

CHUNK = 4 * 1024 * 1024          # 4 MB per potongan kirim
TOKEN = secrets.token_urlsafe(6)  # kunci akses, ada di QR
RECV_DIR = Path.home() / "Downloads" / "DzShare"

ITEMS = {}       # id -> {"path": Path, "name", "size", "dir"}
TRANSFERS = {}   # id -> info transfer (untuk progress di panel PC)
LOCK = threading.Lock()
DIALOGS = queue.Queue()  # dialog pilih file harus jalan di main thread
STATE = {"port": 8765}


# ---------------------------------------------------------------- utilitas
def lan_ips():
    ips = []
    try:  # trik UDP: cari IP yang dipakai ke router (tidak kirim paket)
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("10.255.255.255", 1))
        ips.append(s.getsockname()[0]); s.close()
    except OSError:
        pass
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ip = info[4][0]
            if ip not in ips and not ip.startswith("127."):
                ips.append(ip)
    except OSError:
        pass
    return ips or ["127.0.0.1"]


def dir_size(p):
    total = 0
    for root, _, files in os.walk(p):
        for f in files:
            try: total += os.path.getsize(os.path.join(root, f))
            except OSError: pass
    return total


def add_path(raw):
    p = Path(raw).expanduser().resolve()
    if not p.exists():
        return None
    with LOCK:
        for it in ITEMS.values():
            if it["path"] == p:
                return it
        iid = secrets.token_hex(4)
        it = {"id": iid, "path": p, "name": p.name or str(p), "dir": p.is_dir(),
              "size": dir_size(p) if p.is_dir() else p.stat().st_size}
        ITEMS[iid] = it
        return it


def new_transfer(name, direction, client, total):
    tid = secrets.token_hex(4)
    with LOCK:
        TRANSFERS[tid] = {"id": tid, "name": name, "dir": direction, "client": client,
                          "total": total, "done": 0, "start": time.time(), "end": None,
                          "ok": None}
        # simpan maksimal 30 riwayat
        if len(TRANSFERS) > 30:
            old = sorted(TRANSFERS.values(), key=lambda t: t["start"])
            for t in old[:len(TRANSFERS) - 30]:
                if t["end"]: TRANSFERS.pop(t["id"], None)
    return TRANSFERS[tid]


def finish(tr, ok):
    tr["end"] = time.time(); tr["ok"] = ok


def content_disposition(name):
    ascii_name = name.encode("ascii", "replace").decode().replace('"', "'")
    return "attachment; filename=\"%s\"; filename*=UTF-8''%s" % (
        ascii_name, urllib.parse.quote(name))


def safe_join(base, rel):
    """Gabung path relatif, tolak kalau keluar dari folder yang dibagikan."""
    try:
        p = (base / rel).resolve()
        p.relative_to(base)
        return p
    except (ValueError, OSError):
        return None


def unique_path(folder, name):
    name = Path(name).name or "file"
    target = folder / name
    stem, suf, n = target.stem, target.suffix, 1
    while target.exists():
        target = folder / f"{stem} ({n}){suf}"; n += 1
    return target


def open_folder(p):
    p.mkdir(parents=True, exist_ok=True)
    if sys.platform.startswith("win"): os.startfile(str(p))
    elif sys.platform == "darwin": subprocess.Popen(["open", str(p)])
    else: subprocess.Popen(["xdg-open", str(p)])


def pick_dialog(kind):
    """Dijalankan di main thread. Mengembalikan list path."""
    try:
        import tkinter as tk
        from tkinter import filedialog
    except ImportError:
        return None
    root = tk.Tk(); root.withdraw()
    try: root.attributes("-topmost", True)
    except Exception: pass
    if kind == "files":
        paths = list(filedialog.askopenfilenames(parent=root, title="Pilih file untuk dikirim ke HP"))
    else:
        d = filedialog.askdirectory(parent=root, title="Pilih folder untuk dikirim ke HP")
        paths = [d] if d else []
    root.destroy()
    return paths


class _SockOut:
    """Objek tulis untuk zip streaming langsung ke socket."""
    def __init__(self, sock, tr): self.sock, self.tr = sock, tr
    def write(self, b):
        self.sock.sendall(b); self.tr["done"] += len(b); return len(b)
    def flush(self): pass


# ---------------------------------------------------------------- server
class Handler(BaseHTTPRequestHandler):
    server_version = "DzShare/1.0"

    def log_message(self, *a):  # diam
        pass

    # --- helper
    def is_local(self):
        return self.client_address[0] in ("127.0.0.1", "::1")

    def authed(self):
        if self.is_local():
            return True
        q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        if q.get("k", [""])[0] == TOKEN:
            return True
        for part in self.headers.get("Cookie", "").split(";"):
            k, _, v = part.strip().partition("=")
            if k == "dzk" and v == TOKEN:
                return True
        return False

    def send_json(self, obj, code=200):
        data = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers(); self.wfile.write(data)

    def send_html(self, html, cookie=False):
        data = html.encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        if cookie:
            self.send_header("Set-Cookie", f"dzk={TOKEN}; Path=/; Max-Age=86400; SameSite=Lax")
        self.end_headers(); self.wfile.write(data)

    def deny(self):
        self.send_html("<h2 style='font-family:sans-serif'>Akses ditolak. Scan ulang QR dari PC.</h2>")

    # --- routing
    def do_GET(self):
        url = urllib.parse.urlparse(self.path)
        path = url.path
        if path == "/admin":
            if not self.is_local(): return self.deny()
            return self.send_html(ADMIN_HTML)
        if path == "/":
            if not self.authed(): return self.deny()
            return self.send_html(PHONE_HTML, cookie=True)
        if not self.authed():
            return self.send_json({"error": "unauthorized"}, 403)
        if path == "/api/list":
            with LOCK:
                items = [{k: v for k, v in it.items() if k != "path"} for it in ITEMS.values()]
            return self.send_json({"items": items})
        if path == "/api/info" and self.is_local():
            return self.send_json(self.info())
        if path == "/api/browse":
            return self.browse(urllib.parse.parse_qs(url.query))
        if path.startswith("/dl/"):
            it = ITEMS.get(path[4:])
            if not it: return self.send_json({"error": "not found"}, 404)
            if it["dir"]:
                rel = urllib.parse.parse_qs(url.query).get("f", [""])[0]
                if not rel:
                    return self.send_zip([it], it["name"] + ".zip")
                fp = safe_join(it["path"], rel)
                if not fp or not fp.is_file():
                    return self.send_json({"error": "not found"}, 404)
                return self.send_file({"path": fp, "name": fp.name})
            return self.send_file(it)
        if path == "/zip":
            with LOCK: items = list(ITEMS.values())
            return self.send_zip(items, "DzShare.zip")
        self.send_json({"error": "not found"}, 404)

    def do_POST(self):
        url = urllib.parse.urlparse(self.path)
        if not self.is_local(): return self.send_json({"error": "forbidden"}, 403)
        n = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(n) or b"{}") if n else {}
        p = url.path
        if p in ("/api/pick-files", "/api/pick-folder"):
            done, box = threading.Event(), {}
            kind = "files" if p.endswith("files") else "folder"
            DIALOGS.put((kind, box, done))
            done.wait(600)
            paths = box.get("paths")
            if paths is None:
                return self.send_json({"error": "Dialog tidak tersedia, tempel path manual."}, 500)
            added = [add_path(x) for x in paths]
            return self.send_json({"added": len([a for a in added if a])})
        if p == "/api/add-path":
            it = add_path(body.get("path", "").strip().strip('"'))
            return self.send_json({"ok": bool(it)}, 200 if it else 404)
        if p == "/api/remove":
            with LOCK: ITEMS.pop(body.get("id"), None)
            return self.send_json({"ok": True})
        if p == "/api/clear":
            with LOCK: ITEMS.clear()
            return self.send_json({"ok": True})
        if p == "/api/open-recv":
            open_folder(RECV_DIR); return self.send_json({"ok": True})
        self.send_json({"error": "not found"}, 404)

    def do_PUT(self):  # upload HP -> PC (streaming, tanpa batas ukuran)
        url = urllib.parse.urlparse(self.path)
        if url.path != "/up" or not self.authed():
            return self.send_json({"error": "forbidden"}, 403)
        name = urllib.parse.parse_qs(url.query).get("name", ["file"])[0]
        total = int(self.headers.get("Content-Length") or 0)
        RECV_DIR.mkdir(parents=True, exist_ok=True)
        target = unique_path(RECV_DIR, name)
        tmp = target.with_name(target.name + ".part")
        tr = new_transfer(target.name, "up", self.client_address[0], total)
        left = total
        try:
            with open(tmp, "wb", buffering=CHUNK) as f:
                while left > 0:
                    buf = self.rfile.read(min(CHUNK, left))
                    if not buf: raise ConnectionError("putus")
                    f.write(buf); left -= len(buf); tr["done"] += len(buf)
            tmp.replace(target); finish(tr, True)
            self.send_json({"ok": True, "saved": target.name})
        except Exception:
            finish(tr, False)
            try: tmp.unlink()
            except OSError: pass

    # --- kirim file dengan dukungan Range (bisa resume / pause)
    def send_file(self, it):
        p = it["path"]
        try:
            size = p.stat().st_size; f = open(p, "rb")
        except OSError:
            return self.send_json({"error": "file hilang"}, 410)
        start, end = 0, size - 1
        rng = self.headers.get("Range", "")
        partial = False
        if rng.startswith("bytes="):
            a, _, b = rng[6:].split(",")[0].partition("-")
            try:
                if a == "": start = max(0, size - int(b))
                else:
                    start = int(a); end = min(int(b), size - 1) if b else size - 1
                partial = True
            except ValueError:
                pass
            if start > end or start >= size:
                self.send_response(416); self.send_header("Content-Range", f"bytes */{size}")
                self.end_headers(); f.close(); return
        length = end - start + 1
        self.send_response(206 if partial else 200)
        ctype = mimetypes.guess_type(p.name)[0] or "application/octet-stream"
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(length))
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Disposition", content_disposition(it["name"]))
        if partial: self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        self.end_headers()
        if self.command == "HEAD": f.close(); return
        tr = new_transfer(it["name"], "down", self.client_address[0], length)
        sock, pos, left = self.connection, start, length
        try:
            while left > 0:  # sendfile = zero-copy di Linux/Mac
                n = sock.sendfile(f, pos, min(CHUNK, left))
                if n == 0: break
                pos += n; left -= n; tr["done"] += n
            finish(tr, left == 0)
        except (ConnectionError, OSError):
            finish(tr, False)
        finally:
            f.close()

    def do_HEAD(self):
        self.do_GET()

    # --- folder / semua file -> zip streaming (tanpa kompres = cepat)
    def send_zip(self, items, zipname):
        self.send_response(200)
        self.send_header("Content-Type", "application/zip")
        self.send_header("Content-Disposition", content_disposition(zipname))
        self.send_header("Connection", "close")
        self.end_headers()
        self.close_connection = True
        total = sum(it["size"] for it in items)
        tr = new_transfer(zipname, "down", self.client_address[0], total)
        try:
            with zipfile.ZipFile(_SockOut(self.connection, tr), "w",
                                 zipfile.ZIP_STORED, allowZip64=True) as zf:
                for it in items:
                    p = it["path"]
                    files = [(p, p.name)] if not it["dir"] else [
                        (Path(r) / fn, str((Path(r) / fn).relative_to(p.parent)))
                        for r, _, fs in os.walk(p) for fn in fs]
                    for fp, arc in files:
                        try:
                            zi = zipfile.ZipInfo.from_file(fp, arc)
                        except OSError:
                            continue
                        zi.compress_type = zipfile.ZIP_STORED
                        with open(fp, "rb") as src, zf.open(zi, "w") as dst:
                            while True:
                                b = src.read(CHUNK)
                                if not b: break
                                dst.write(b)
            finish(tr, True)
        except (ConnectionError, OSError):
            finish(tr, False)

    # --- buka isi folder di HP
    def browse(self, q):
        it = ITEMS.get(q.get("id", [""])[0])
        if not it or not it["dir"]:
            return self.send_json({"error": "not found"}, 404)
        base = it["path"]
        sub = q.get("sub", [""])[0]
        d = safe_join(base, sub) if sub else base
        if not d or not d.is_dir():
            return self.send_json({"error": "not found"}, 404)
        out = []
        if q.get("all", [""])[0]:  # semua file (rekursif) untuk "Unduh semua"
            for r, dirs, fs in os.walk(d):
                dirs.sort()
                for fn in sorted(fs):
                    fp = Path(r) / fn
                    try: size = fp.stat().st_size
                    except OSError: continue
                    out.append({"name": fn, "rel": fp.relative_to(base).as_posix(),
                                "dir": False, "size": size})
        else:
            try: entries = sorted(d.iterdir(), key=lambda e: (not e.is_dir(), e.name.lower()))
            except OSError: entries = []
            for e in entries:
                try:
                    isdir = e.is_dir()
                    out.append({"name": e.name, "rel": e.relative_to(base).as_posix(),
                                "dir": isdir, "size": dir_size(e) if isdir else e.stat().st_size})
                except OSError:
                    continue
        return self.send_json({"entries": out})

    def info(self):
        ips = lan_ips()
        port = STATE["port"]
        now = time.time()
        with LOCK:
            items = [{k: v for k, v in it.items() if k != "path"} | {"path": str(it["path"])}
                     for it in ITEMS.values()]
            trs = []
            for t in sorted(TRANSFERS.values(), key=lambda t: -t["start"]):
                el = (t["end"] or now) - t["start"]
                trs.append(dict(t, speed=t["done"] / el if el > 0 else 0))
        recv = []
        if RECV_DIR.exists():
            for f in sorted(RECV_DIR.iterdir(), key=lambda f: -f.stat().st_mtime)[:20]:
                if f.is_file() and not f.name.endswith(".part"):
                    recv.append({"name": f.name, "size": f.stat().st_size})
        return {"urls": [f"http://{ip}:{port}/?k={TOKEN}" for ip in ips],
                "items": items, "transfers": trs, "recv_dir": str(RECV_DIR), "received": recv}


# ---------------------------------------------------------------- HTML
CSS = """
*{box-sizing:border-box}body{margin:0;font-family:system-ui,-apple-system,Segoe UI,Roboto,sans-serif;
background:#0b0f17;color:#e6ebf5}a{color:inherit}
.wrap{max-width:860px;margin:auto;padding:20px}h1{font-size:22px;margin:4px 0 2px}
.sub{color:#8a96ad;font-size:13px;margin-bottom:18px}
.card{background:#131a26;border:1px solid #1f2a3c;border-radius:14px;padding:16px;margin-bottom:14px}
.row{display:flex;align-items:center;gap:12px;padding:10px 0;border-bottom:1px solid #1c2536}
.row:last-child{border-bottom:none}.grow{flex:1;min-width:0}
.name{white-space:nowrap;overflow:hidden;text-overflow:ellipsis;font-weight:600}
.meta{font-size:12px;color:#8a96ad}
.btn{background:#2f6bff;color:#fff;border:none;border-radius:10px;padding:10px 14px;font-weight:600;
font-size:14px;cursor:pointer;text-decoration:none;display:inline-block;white-space:nowrap}
.btn.ghost{background:#1c2536;color:#cfd8ea}.btn.red{background:#3a1d24;color:#ff8a9a}
.bar{height:6px;background:#1c2536;border-radius:9px;overflow:hidden;margin-top:6px}
.bar i{display:block;height:100%;background:#2f6bff;width:0}
.ok i{background:#2ecc71}.fail i{background:#e74c3c}
.empty{color:#64708a;text-align:center;padding:18px;font-size:14px}
h3{margin:0 0 10px;font-size:15px;color:#aab6cc}
.ic{width:38px;height:38px;border-radius:10px;background:#1c2536;display:grid;place-items:center;flex:none}
"""

JS_COMMON = """
function fmt(b){if(b<1024)return b+' B';const u=['KB','MB','GB','TB'];let i=-1;
do{b/=1024;i++}while(b>=1024&&i<3);return b.toFixed(b<10?2:1)+' '+u[i]}
function esc(s){return String(s).replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]))}
function icon(n,d){if(d)return '📁';const e=n.split('.').pop().toLowerCase();
if(['mp4','mov','mkv','avi','webm','m4v'].includes(e))return '🎬';
if(['jpg','jpeg','png','gif','webp','heic','psd'].includes(e))return '🖼️';
if(['mp3','wav','flac','aac','m4a'].includes(e))return '🎵';
if(['zip','rar','7z'].includes(e))return '🗜️';if(e==='apk')return '🤖';return '📄'}
"""

ADMIN_HTML = """<!doctype html><html lang=id><head><meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1"><title>DzShare — PC</title>
<style>__CSS__
.top{display:grid;grid-template-columns:auto 1fr;gap:18px;align-items:center}
#qr{background:#fff;border-radius:12px;padding:8px;width:220px;height:220px;display:grid;place-items:center;color:#000;font-size:12px;text-align:center}
#qr svg{width:100%;height:100%}.url{font-family:monospace;font-size:15px;word-break:break-all;color:#7fa6ff}
.acts{display:flex;gap:8px;flex-wrap:wrap;margin-top:12px}
input.path{flex:1;background:#0b0f17;border:1px solid #1f2a3c;color:#e6ebf5;border-radius:10px;padding:10px}
@media(max-width:600px){.top{grid-template-columns:1fr}}
</style>
<script src="https://cdn.jsdelivr.net/npm/qrcode-generator@1.4.4/qrcode.min.js"></script>
</head><body><div class=wrap>
<h1>⚡ DzShare</h1><div class=sub>Kirim file PC → HP lewat Wi-Fi yang sama. Tidak perlu internet.</div>
<div class="card top"><div id=qr>memuat QR…</div><div>
<h3>Scan pakai kamera HP</h3><div class=url id=url></div>
<div class=meta id=otherips style="margin-top:6px"></div>
<div class=meta style="margin-top:10px">HP dan PC harus di Wi-Fi/hotspot yang sama. Kalau tidak bisa terbuka, izinkan Python di Windows Firewall (jaringan Private).</div>
</div></div>
<div class=card><h3>File yang dibagikan</h3>
<div class=acts style="margin-top:0;margin-bottom:8px">
<button class=btn onclick="pick('files')">+ Tambah file</button>
<button class="btn ghost" onclick="pick('folder')">+ Tambah folder</button>
<button class="btn red" onclick="post('/api/clear').then(load)">Kosongkan</button></div>
<div class=acts style="margin-top:0"><input class=path id=pathin placeholder="…atau tempel path file/folder di sini lalu Enter">
<button class="btn ghost" onclick="addPath()">Tambah</button></div>
<div id=items></div></div>
<div class=card><h3>Transfer</h3><div id=trs></div></div>
<div class=card><h3>Diterima dari HP</h3><div class=meta id=recvdir></div>
<div id=recv></div><div class=acts><button class="btn ghost" onclick="post('/api/open-recv')">Buka folder</button></div></div>
</div><script>__JS__
let lastUrl='';
function post(u,b){return fetch(u,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(b||{})})}
async function pick(k){const r=await post('/api/pick-'+k);if(!r.ok){alert((await r.json()).error)}load()}
async function addPath(){const v=pathin.value.trim();if(!v)return;const r=await post('/api/add-path',{path:v});
if(!r.ok)alert('Path tidak ditemukan');else pathin.value='';load()}
pathin.addEventListener('keydown',e=>{if(e.key==='Enter')addPath()});
async function load(){const d=await (await fetch('/api/info')).json();
 const u=d.urls[0];if(u!==lastUrl){lastUrl=u;url.textContent=u;
  try{const q=qrcode(0,'M');q.addData(u);q.make();qr.innerHTML=q.createSvgTag({cellSize:5,margin:2,scalable:true})}
  catch(e){qr.textContent='QR butuh internet di PC. Ketik alamat di samping ke browser HP.'}
  otherips.textContent=d.urls.length>1?'IP lain: '+d.urls.slice(1).map(x=>x.split('/')[2]).join(', '):''}
 items.innerHTML=d.items.length?d.items.map(it=>`<div class=row><div class=ic>${icon(it.name,it.dir)}</div>
  <div class=grow><div class=name>${esc(it.name)}</div><div class=meta>${fmt(it.size)} · ${esc(it.path)}</div></div>
  <button class="btn red" onclick="post('/api/remove',{id:'${it.id}'}).then(load)">Hapus</button></div>`).join('')
  :'<div class=empty>Belum ada file. Klik “Tambah file”.</div>';
 trs.innerHTML=d.transfers.length?d.transfers.map(t=>{const p=t.total?t.done/t.total*100:0;
  const st=t.end?(t.ok?'selesai':'gagal/dibatalkan'):Math.round(p)+'%';
  return `<div class=row><div class=ic>${t.dir==='down'?'📲':'💻'}</div><div class=grow>
  <div class=name>${esc(t.name)}</div><div class=meta>${t.dir==='down'?'PC → HP':'HP → PC'} · ${t.client} · ${fmt(t.done)} / ${fmt(t.total)} · ${fmt(t.speed)}/s · ${st}</div>
  <div class="bar ${t.end?(t.ok?'ok':'fail'):''}"><i style="width:${t.end&&t.ok?100:p}%"></i></div></div></div>`}).join('')
  :'<div class=empty>Belum ada transfer.</div>';
 recvdir.textContent=d.recv_dir;
 recv.innerHTML=d.received.length?d.received.map(f=>`<div class=row><div class=ic>${icon(f.name)}</div>
  <div class=grow><div class=name>${esc(f.name)}</div><div class=meta>${fmt(f.size)}</div></div></div>`).join('')
  :'<div class=empty>Belum ada file dari HP.</div>';
}
load();setInterval(load,1000);
</script></body></html>"""

PHONE_HTML = """<!doctype html><html lang=id><head><meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1"><title>DzShare</title>
<style>__CSS__
.wrap{padding:16px}.btn{padding:12px 16px}
.drop{border:2px dashed #2a3850;border-radius:14px;padding:20px;text-align:center;color:#8a96ad}
.crumb{font-size:13px;color:#7fa6ff;margin-bottom:8px;word-break:break-all}
.crumb a{text-decoration:none}.tap{cursor:pointer}
.acts{display:flex;gap:8px;flex-wrap:wrap;margin-bottom:6px}
#note{font-size:12px;color:#f0c36d;margin:6px 0}
</style></head><body><div class=wrap>
<h1>⚡ DzShare</h1><div class=sub>Tap folder untuk membuka isinya. Download bisa di-pause / dilanjutkan.</div>
<div class=card><div class=crumb id=crumb></div>
<div class=acts id=acts></div><div id=note></div>
<div id=items><div class=empty>Memuat…</div></div></div>
<div class=card><h3>Kirim ke PC</h3>
<label class=drop style="display:block">Tap untuk pilih file dari HP
<input type=file multiple id=fi style="display:none"></label><div id=ups></div></div>
</div><script>__JS__
let cur=null; // {id,name,sub}
let NAV=[];function N(o){NAV.push(o);return 'nav('+(NAV.length-1)+')'}
function nav(i){go(NAV[i])}
let DLS=[];function D(url,rel){DLS.push([url,rel]);return 'dl('+(DLS.length-1)+')'}
let DIR=null;const FSA='showDirectoryPicker' in window;
async function pickDir(){try{DIR=await showDirectoryPicker({mode:'readwrite',startIn:'downloads'});
 note.textContent='Lokasi simpan: '+DIR.name+' (struktur folder ikut dibuat)';load()}catch(e){}}
async function saveTo(url,rel){const parts=rel.split('/');let d=DIR;
 for(const p of parts.slice(0,-1))d=await d.getDirectoryHandle(p,{create:true});
 const fh=await d.getFileHandle(parts[parts.length-1],{create:true});const w=await fh.createWritable();
 const r=await fetch(url);const total=+r.headers.get('Content-Length')||0;const rd=r.body.getReader();let got=0,t0=Date.now();
 while(true){const {done,value}=await rd.read();if(done)break;await w.write(value);got+=value.length;
  note.textContent=rel+' · '+(total?Math.round(got/total*100)+'% · ':'')+fmt(got/((Date.now()-t0)/1000||1))+'/s'}
 await w.close()}
async function dl(i){const [u,rel]=DLS[i];if(DIR){await saveTo(u,rel);note.textContent='Tersimpan: '+DIR.name+'/'+rel}else fire(u)}
const sleep=ms=>new Promise(r=>setTimeout(r,ms));
function go(c){cur=c;load()}
function pickBtn(){return FSA?`<button class="btn ghost" onclick="pickDir()">📂 ${DIR?'Lokasi: '+esc(DIR.name):'Pilih lokasi simpan'}</button>`:''}
function dlUrl(id,rel){return '/dl/'+id+(rel?'?f='+encodeURIComponent(rel):'')}
function fire(u){const a=document.createElement('a');a.href=u;a.download='';document.body.appendChild(a);a.click();a.remove()}
async function all(){
 let list;
 if(!cur){const d=await (await fetch('/api/list')).json();list=[];
  for(const it of d.items){ if(!it.dir){list.push([dlUrl(it.id),it.name]);continue}
   const f=await (await fetch('/api/browse?all=1&id='+it.id)).json();
   f.entries.forEach(e=>list.push([dlUrl(it.id,e.rel),it.name+'/'+e.rel]))}}
 else{const f=await (await fetch('/api/browse?all=1&id='+cur.id+'&sub='+encodeURIComponent(cur.sub))).json();
  list=f.entries.map(e=>[dlUrl(cur.id,e.rel),cur.name+'/'+e.rel])}
 if(DIR){for(const [u,rel] of list)await saveTo(u,rel);
  note.textContent='Selesai: '+list.length+' file tersimpan di '+DIR.name+' (dengan struktur folder).';return}
 note.textContent='Mengunduh '+list.length+' file… Kalau browser bertanya “izinkan download banyak file”, pilih Izinkan.';
 for(const [u] of list){fire(u);await sleep(800)}
 note.textContent='Semua '+list.length+' file sudah masuk ke folder Download HP.';
}
function row(ic,name,meta,act,click){return `<div class=row><div class="ic ${click?'tap':''}" ${click?`onclick="${click}"`:''}>${ic}</div>
 <div class="grow ${click?'tap':''}" ${click?`onclick="${click}"`:''}><div class=name>${esc(name)}</div><div class=meta>${meta}</div></div>${act}</div>`}
async function load(){try{NAV=[];DLS=[];
 if(!cur){const d=await (await fetch('/api/list')).json();
  crumb.innerHTML='Dari PC';
  acts.innerHTML=(d.items.length?'<button class=btn onclick="all()">Unduh semua</button>':'')+pickBtn();
  items.innerHTML=d.items.length?d.items.map(it=>it.dir
   ?row('📁',it.name,fmt(it.size)+' · folder','<span class=meta>›</span>',N({id:it.id,name:it.name,sub:''}))
   :row(icon(it.name),it.name,fmt(it.size),`<button class=btn onclick="${D(dlUrl(it.id),it.name)}">Unduh</button>`)).join('')
   :'<div class=empty>PC belum membagikan file.</div>';return}
 const d=await (await fetch('/api/browse?id='+cur.id+'&sub='+encodeURIComponent(cur.sub))).json();
 const parts=cur.sub?cur.sub.split('/'):[];
 let h=`<a href="#" onclick="go(null);return false">Dari PC</a> / <a href="#" onclick="${N({id:cur.id,name:cur.name,sub:''})};return false">${esc(cur.name)}</a>`;
 parts.forEach((p,i)=>{const s=parts.slice(0,i+1).join('/');h+=` / <a href="#" onclick="${N({id:cur.id,name:cur.name,sub:s})};return false">${esc(p)}</a>`});
 crumb.innerHTML=h;
 const zipUrl=cur.sub?'':`<a class="btn ghost" href="${dlUrl(cur.id)}">Unduh sebagai ZIP</a>`;
 acts.innerHTML=`<button class="btn ghost" onclick="${cur.sub?N({id:cur.id,name:cur.name,sub:parts.slice(0,-1).join('/')}):'go(null)'}">‹ Kembali</button>`
  +(d.entries.length?'<button class=btn onclick="all()">Unduh semua isi folder</button>':'')+pickBtn()+zipUrl;
 items.innerHTML=d.entries.length?d.entries.map(e=>e.dir
  ?row('📁',e.name,fmt(e.size)+' · folder','<span class=meta>›</span>',N({id:cur.id,name:cur.name,sub:e.rel}))
  :row(icon(e.name),e.name,fmt(e.size),`<button class=btn onclick="${D(dlUrl(cur.id,e.rel),cur.name+'/'+e.rel)}">Unduh</button>`)).join('')
  :'<div class=empty>Folder kosong.</div>';
}catch(e){}}
load();
fi.onchange=async()=>{for(const f of fi.files)await up(f);fi.value=''};
function up(f){return new Promise(res=>{const id='u'+Math.random().toString(36).slice(2);
 ups.insertAdjacentHTML('beforeend',`<div class=row><div class=ic>${icon(f.name)}</div><div class=grow>
 <div class=name>${esc(f.name)}</div><div class=meta id=${id}m>0%</div><div class=bar id=${id}b><i></i></div></div></div>`);
 const m=document.getElementById(id+'m'),b=document.getElementById(id+'b'),t0=Date.now();
 const x=new XMLHttpRequest();x.open('PUT','/up?name='+encodeURIComponent(f.name));
 x.upload.onprogress=e=>{const p=e.loaded/f.size*100;b.firstChild.style.width=p+'%';
  m.textContent=Math.round(p)+'% · '+fmt(e.loaded/((Date.now()-t0)/1000||1))+'/s'};
 x.onload=()=>{b.className='bar ok';b.firstChild.style.width='100%';m.textContent='Terkirim · '+fmt(f.size);res()};
 x.onerror=()=>{b.className='bar fail';m.textContent='Gagal';res()};x.send(f)})}
</script></body></html>"""

ADMIN_HTML = ADMIN_HTML.replace("__CSS__", CSS).replace("__JS__", JS_COMMON)
PHONE_HTML = PHONE_HTML.replace("__CSS__", CSS).replace("__JS__", JS_COMMON)


# ---------------------------------------------------------------- main
def main():
    args = sys.argv[1:]
    port, browser, paths = 8765, True, []
    i = 0
    while i < len(args):
        a = args[i]
        if a == "--port": port = int(args[i + 1]); i += 1
        elif a == "--no-browser": browser = False
        else: paths.append(a)
        i += 1
    for p in paths:
        if not add_path(p): print("  ! tidak ditemukan:", p)

    srv = None
    for p in range(port, port + 20):
        try:
            srv = ThreadingHTTPServer(("0.0.0.0", p), Handler); break
        except OSError:
            continue
    if not srv:
        sys.exit("Tidak ada port kosong.")
    srv.daemon_threads = True
    STATE["port"] = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()

    admin = f"http://127.0.0.1:{STATE['port']}/admin"
    print("\n  ⚡ DzShare jalan")
    print("  Panel PC :", admin)
    for ip in lan_ips():
        print("  Buka di HP:", f"http://{ip}:{STATE['port']}/?k={TOKEN}")
    print("  File dari HP disimpan di:", RECV_DIR)
    print("  Tekan Ctrl+C untuk berhenti.\n")
    if browser:
        webbrowser.open(admin)

    try:
        while True:
            try:
                kind, box, done = DIALOGS.get(timeout=0.5)
            except queue.Empty:
                continue
            try: box["paths"] = pick_dialog(kind)
            except Exception: box["paths"] = None
            done.set()
    except KeyboardInterrupt:
        print("\n  Berhenti.")
        srv.shutdown()


if __name__ == "__main__":
    main()
