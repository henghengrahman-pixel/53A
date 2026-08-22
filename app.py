import asyncio
import html
import os
import secrets
import sqlite3
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from starlette.middleware.sessions import SessionMiddleware
from telethon import TelegramClient, events
from telethon.errors import FloodWaitError, SessionPasswordNeededError

TZ = timezone(timedelta(hours=7))
DATA_DIR = Path(os.getenv("DATA_DIR", "/data"))
if not DATA_DIR.parent.exists() or not os.access(DATA_DIR.parent, os.W_OK):
    DATA_DIR = Path("data")
DATA_DIR.mkdir(parents=True, exist_ok=True)
DB_PATH = DATA_DIR / "followup.db"
SESSION_PATH = str(DATA_DIR / "telegram_official")
ADMIN_USER = os.getenv("ADMIN_USER", "admin")
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "")
API_ID = int(os.getenv("TELEGRAM_API_ID", "0") or 0)
API_HASH = os.getenv("TELEGRAM_API_HASH", "")

db = sqlite3.connect(DB_PATH, check_same_thread=False)
db.row_factory = sqlite3.Row
db.executescript("""
CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY,value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS contacts(
 user_id INTEGER PRIMARY KEY, name TEXT, username TEXT, last_inbound TEXT,
 last_outbound TEXT, replied_after_followup INTEGER DEFAULT 0,
 blocked INTEGER DEFAULT 0, selected INTEGER DEFAULT 0, updated_at TEXT);
CREATE TABLE IF NOT EXISTS queue(
 id INTEGER PRIMARY KEY AUTOINCREMENT,user_id INTEGER,step INTEGER DEFAULT 1,
 due_at TEXT,status TEXT DEFAULT 'pending',sent_at TEXT,error TEXT,
 UNIQUE(user_id,step));
CREATE TABLE IF NOT EXISTS logs(
 id INTEGER PRIMARY KEY AUTOINCREMENT,created_at TEXT,level TEXT,message TEXT);
CREATE TABLE IF NOT EXISTS resends(
 id INTEGER PRIMARY KEY AUTOINCREMENT,user_id INTEGER,step INTEGER DEFAULT 1,
 due_at TEXT,status TEXT DEFAULT 'pending',sent_at TEXT,error TEXT,message_text TEXT,
 created_at TEXT);
""")
# Migrasi aman untuk database deployment versi sebelumnya.
queue_columns = {row[1] for row in db.execute("PRAGMA table_info(queue)")}
if "message_text" not in queue_columns:
    db.execute("ALTER TABLE queue ADD COLUMN message_text TEXT")
    db.commit()
DEFAULTS = {
    "message_1": "Halo Bosku 😊 Sebelumnya Anda pernah menghubungi Telegram Official kami. Apakah masih ada kendala yang belum terselesaikan? Silakan balas pesan ini, kami siap membantu.",
    "message_2": "Halo Bosku, kami hanya ingin memastikan apakah masih ada yang dapat kami bantu. Balas pesan ini jika membutuhkan bantuan. Ketik STOP jika tidak ingin menerima follow-up lagi.",
    "message_3": "Follow-up terakhir dari kami, Bosku. Jika membutuhkan bantuan di kemudian hari, silakan hubungi Telegram Official ini kembali. Ketik STOP untuk berhenti.",
    "max_age_days": "0", "daily_limit": "30", "interval_seconds": "90",
    "step2_hours": "24", "step3_hours": "72", "campaign_active": "0"
}
for k, v in DEFAULTS.items():
    db.execute("INSERT OR IGNORE INTO settings(key,value) VALUES(?,?)", (k, v))
db.commit()

client = TelegramClient(SESSION_PATH, API_ID, API_HASH) if API_ID and API_HASH else None
worker_task = None

def now(): return datetime.now(TZ)
def iso(dt=None): return (dt or now()).isoformat()
def setting(key):
    row = db.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    return row[0] if row else DEFAULTS.get(key, "")
def set_setting(key, value):
    db.execute("INSERT INTO settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, str(value)))
    db.commit()
def log(level, message):
    db.execute("INSERT INTO logs(created_at,level,message) VALUES(?,?,?)", (iso(), level, message[:1000]))
    db.commit()
def authorized(request): return request.session.get("auth") is True
def redirect(path="/"): return RedirectResponse(path, status_code=303)

STYLE = """
body{margin:0;background:#090b10;color:#e8e9ec;font:14px Arial}nav{background:#11151d;padding:15px 5%;display:flex;gap:18px}nav a{color:#f1c75b;text-decoration:none}.wrap{max-width:1100px;margin:24px auto;padding:0 18px}.card{background:#121720;border:1px solid #252d3a;border-radius:12px;padding:18px;margin:14px 0}.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(210px,1fr));gap:12px}.stat{font-size:26px;color:#f1c75b}input,textarea,select{width:100%;box-sizing:border-box;background:#090d13;color:white;border:1px solid #333d4e;border-radius:7px;padding:10px;margin:6px 0 12px}button,.btn{background:#e6b94b;color:#111;border:0;border-radius:7px;padding:10px 14px;font-weight:bold;cursor:pointer;text-decoration:none;display:inline-block}button.danger{background:#d95b5b;color:white}table{width:100%;border-collapse:collapse}th,td{text-align:left;padding:9px;border-bottom:1px solid #29313e}.muted{color:#98a1af}.ok{color:#70d69b}.bad{color:#ff7f7f}.flash{background:#22334a;padding:10px;border-radius:7px}@media(max-width:700px){table{font-size:12px}.hide-mobile{display:none}}
"""

def page(title, body, request=None):
    flash = request.session.pop("flash", "") if request else ""
    nav = "<nav><b>DENTOTO Follow-up</b><a href='/'>Dashboard</a><a href='/contacts'>Kontak</a><a href='/history'>Riwayat Terkirim</a><a href='/campaign/resend'>Kampanye Kirim Ulang</a><a href='/settings'>Pengaturan</a><a href='/telegram'>Telegram</a><a href='/logout'>Keluar</a></nav>" if request and authorized(request) else ""
    return HTMLResponse(f"<!doctype html><html><head><meta name='viewport' content='width=device-width'><title>{title}</title><style>{STYLE}</style></head><body>{nav}<main class='wrap'>{('<div class=flash>'+flash+'</div>') if flash else ''}{body}</main></body></html>")

@asynccontextmanager
async def lifespan(app):
    global worker_task
    if client:
        await client.connect()
        client.add_event_handler(on_new_message, events.NewMessage(incoming=True))
    worker_task = asyncio.create_task(queue_worker())
    yield
    worker_task.cancel()
    if client: await client.disconnect()

app = FastAPI(lifespan=lifespan)
app.add_middleware(SessionMiddleware, secret_key=os.getenv("SESSION_SECRET", secrets.token_hex(32)), https_only=os.getenv("COOKIE_SECURE", "1") == "1", same_site="lax")

@app.get("/health")
def health(): return {"ok": True, "telegram_configured": bool(client)}

@app.get("/login", response_class=HTMLResponse)
def login_form(request: Request):
    return page("Login", "<div class=card style='max-width:380px;margin:80px auto'><h2>Login Admin</h2><form method=post><label>Username</label><input name=username required><label>Password</label><input name=password type=password required><button>Masuk</button></form></div>", request)

@app.post("/login")
def login(request: Request, username: str = Form(...), password: str = Form(...)):
    if not ADMIN_PASSWORD:
        request.session["flash"] = "ADMIN_PASSWORD belum diatur di Railway."
        return redirect("/login")
    if secrets.compare_digest(username, ADMIN_USER) and secrets.compare_digest(password, ADMIN_PASSWORD):
        request.session["auth"] = True
        return redirect()
    request.session["flash"] = "Login salah."
    return redirect("/login")

@app.get("/logout")
def logout(request: Request): request.session.clear(); return redirect("/login")

@app.get("/", response_class=HTMLResponse)
async def dashboard(request: Request):
    if not authorized(request): return redirect("/login")
    counts = {r["status"]: r["n"] for r in db.execute("SELECT status,count(*) n FROM (SELECT status FROM queue UNION ALL SELECT status FROM resends) GROUP BY status")}
    contacts = db.execute("SELECT count(*) FROM contacts").fetchone()[0]
    selected = db.execute("SELECT count(*) FROM contacts WHERE selected=1 AND blocked=0").fetchone()[0]
    sent_today = db.execute("SELECT count(*) FROM (SELECT status,sent_at FROM queue UNION ALL SELECT status,sent_at FROM resends) WHERE status='sent' AND substr(sent_at,1,10)=?", (now().date().isoformat(),)).fetchone()[0]
    connected = bool(client and await client.is_user_authorized())
    body = f"<h1>Dashboard Follow-up</h1><div class=grid><div class=card><div class=muted>Kontak valid</div><div class=stat>{contacts}</div></div><div class=card><div class=muted>Dipilih</div><div class=stat>{selected}</div></div><div class=card><div class=muted>Terkirim hari ini</div><div class=stat>{sent_today}</div></div><div class=card><div class=muted>Telegram</div><div class='stat {'ok' if connected else 'bad'}'>{'Terhubung' if connected else 'Belum login'}</div></div></div><div class=card><h3>Kampanye</h3><p>Status: <b>{'AKTIF' if setting('campaign_active')=='1' else 'NONAKTIF'}</b> · Pending: {counts.get('pending',0)} · Dibalas: {counts.get('replied',0)} · Gagal: {counts.get('failed',0)}</p><form method=post action='/campaign/start' style='display:inline'><button>Mulai / Lanjutkan</button></form> <form method=post action='/campaign/stop' style='display:inline'><button class=danger>Hentikan</button></form></div><div class=card><h3>Cara penggunaan</h3><ol><li>Login Telegram Official pada menu Telegram.</li><li>Sinkronkan chat masuk.</li><li>Pilih penerima pada menu Kontak.</li><li>Periksa pesan dan batas pengiriman.</li><li>Mulai kampanye.</li></ol></div>"
    return page("Dashboard", body, request)

@app.get("/telegram", response_class=HTMLResponse)
async def telegram_page(request: Request):
    if not authorized(request): return redirect("/login")
    ok = bool(client and await client.is_user_authorized())
    body = f"<h1>Telegram Official</h1><div class=card><p>Status: <b class={'ok' if ok else 'bad'}>{'Terhubung' if ok else 'Belum login'}</b></p>{'<form method=post action=/telegram/sync><button>Sinkronkan Chat Masuk</button></form>' if ok else '<form method=post action=/telegram/send-code><label>Nomor Telegram (format internasional)</label><input name=phone placeholder=+62812... required><button>Kirim OTP</button></form>'}</div>"
    return page("Telegram", body, request)

@app.post("/telegram/send-code")
async def send_code(request: Request, phone: str = Form(...)):
    if not authorized(request): return redirect("/login")
    if not client:
        request.session["flash"] = "TELEGRAM_API_ID dan TELEGRAM_API_HASH belum diatur."
        return redirect("/telegram")
    try:
        result = await client.send_code_request(phone)
        request.session.update({"phone": phone, "phone_hash": result.phone_code_hash})
        return page("OTP", "<div class=card><h2>Masukkan OTP</h2><form method=post action='/telegram/verify'><input name=code autocomplete=one-time-code required><button>Verifikasi</button></form></div>", request)
    except Exception as e:
        request.session["flash"] = f"Gagal mengirim OTP: {type(e).__name__}"
        return redirect("/telegram")

@app.post("/telegram/verify")
async def verify(request: Request, code: str = Form(...)):
    if not authorized(request): return redirect("/login")
    try:
        await client.sign_in(request.session["phone"], code, phone_code_hash=request.session["phone_hash"])
    except SessionPasswordNeededError:
        return page("2FA", "<div class=card><h2>Password 2FA Telegram</h2><form method=post action='/telegram/2fa'><input type=password name=password required><button>Masuk</button></form></div>", request)
    except Exception as e:
        request.session["flash"] = f"OTP gagal: {type(e).__name__}"
        return redirect("/telegram")
    request.session["flash"] = "Telegram berhasil terhubung."
    return redirect("/telegram")

@app.post("/telegram/2fa")
async def verify_2fa(request: Request, password: str = Form(...)):
    if not authorized(request): return redirect("/login")
    try: await client.sign_in(password=password); request.session["flash"] = "Telegram berhasil terhubung."
    except Exception as e: request.session["flash"] = f"2FA gagal: {type(e).__name__}"
    return redirect("/telegram")

@app.post("/telegram/sync")
async def sync(request: Request):
    if not authorized(request): return redirect("/login")
    if not client or not await client.is_user_authorized(): return redirect("/telegram")
    max_age = int(setting("max_age_days"))
    cutoff = now() - timedelta(days=max_age) if max_age > 0 else None
    total = 0
    async for dialog in client.iter_dialogs():
        if not dialog.is_user or getattr(dialog.entity, "bot", False) or getattr(dialog.entity, "deleted", False): continue
        inbound = None; outbound = None
        # Baca lebih dalam agar pesan masuk lama tetap ditemukan walau setelahnya
        # ada banyak pesan keluar dari akun Official.
        async for msg in client.iter_messages(dialog.entity, limit=1000):
            dt = msg.date.astimezone(TZ)
            if msg.out and not outbound: outbound = dt
            if not msg.out and not inbound: inbound = dt
            if inbound and outbound: break
        if not inbound or (cutoff and inbound < cutoff): continue
        name = " ".join(filter(None, [getattr(dialog.entity,"first_name",None), getattr(dialog.entity,"last_name",None)])) or str(dialog.id)
        db.execute("""INSERT INTO contacts(user_id,name,username,last_inbound,last_outbound,updated_at) VALUES(?,?,?,?,?,?)
          ON CONFLICT(user_id) DO UPDATE SET name=excluded.name,username=excluded.username,last_inbound=excluded.last_inbound,last_outbound=excluded.last_outbound,updated_at=excluded.updated_at""",
          (dialog.id,name,getattr(dialog.entity,"username",None),iso(inbound),iso(outbound) if outbound else None,iso()))
        total += 1
    db.commit(); request.session["flash"] = f"Sinkronisasi selesai: {total} chat valid."
    return redirect("/contacts")

@app.get("/contacts", response_class=HTMLResponse)
def contacts(request: Request):
    if not authorized(request): return redirect("/login")
    rows = db.execute("SELECT * FROM contacts ORDER BY last_inbound DESC LIMIT 1000").fetchall()
    trs = "".join(f"<tr><td><input type=checkbox name=ids value='{r['user_id']}' {'checked' if r['selected'] else ''}></td><td>{r['name']}</td><td>@{r['username'] or '-'}</td><td>{(r['last_inbound'] or '')[:16].replace('T',' ')}</td><td>{'STOP' if r['blocked'] else ('Dibalas' if r['replied_after_followup'] else 'Siap')}</td></tr>" for r in rows)
    body = f"<h1>Kontak Masuk</h1><div class=card><p class=muted>Hanya chat pribadi yang pernah mengirim pesan masuk dan masih dalam batas usia.</p><form method=post action='/contacts/select'><button>Simpan Pilihan</button> <button type=button onclick=\"document.querySelectorAll('[name=ids]').forEach(x=>x.checked=true)\">Pilih Semua Tampil</button><table><thead><tr><th>Pilih</th><th>Nama</th><th>Username</th><th>Terakhir masuk</th><th>Status</th></tr></thead><tbody>{trs or '<tr><td colspan=5>Belum ada data. Sinkronkan chat dahulu.</td></tr>'}</tbody></table><button>Simpan Pilihan</button></form></div>"
    return page("Kontak", body, request)

@app.get("/history", response_class=HTMLResponse)
def history(request: Request, status: str = "all", q: str = ""):
    if not authorized(request): return redirect("/login")
    allowed = {"all", "pending", "sent", "replied", "failed"}
    status = status if status in allowed else "all"
    params = []
    conditions = []
    if status != "all":
        conditions.append("q.status=?"); params.append(status)
    if q.strip():
        conditions.append("(q.name LIKE ? OR q.username LIKE ? OR CAST(q.user_id AS TEXT) LIKE ?)")
        needle = f"%{q.strip()}%"; params.extend([needle, needle, needle])
    where = "WHERE " + " AND ".join(conditions) if conditions else ""
    rows = db.execute(f"""SELECT * FROM (
      SELECT 'normal' source,q.id,q.user_id,q.step,q.due_at,q.status,q.sent_at,q.error,q.message_text,c.name,c.username
      FROM queue q LEFT JOIN contacts c ON c.user_id=q.user_id
      UNION ALL
      SELECT 'resend' source,r.id,r.user_id,r.step,r.due_at,r.status,r.sent_at,r.error,r.message_text,c.name,c.username
      FROM resends r LEFT JOIN contacts c ON c.user_id=r.user_id
      ) q {where} ORDER BY COALESCE(q.sent_at,q.due_at) DESC,q.id DESC LIMIT 2000""", params).fetchall()
    counts = {r["status"]: r["n"] for r in db.execute("SELECT status,count(*) n FROM (SELECT status FROM queue UNION ALL SELECT status FROM resends) GROUP BY status")}
    labels = {"pending":"Menunggu", "sent":"Terkirim", "replied":"Dibalas", "failed":"Gagal"}
    options = "".join(f"<option value='{s}' {'selected' if status==s else ''}>{'Semua status' if s=='all' else labels[s]}</option>" for s in ["all","sent","replied","pending","failed"])
    trs = ""
    for r in rows:
        message = r["message_text"] or (setting(f"message_{r['step']}") if r["status"] == "pending" else "Pesan versi lama (isi belum direkam)")
        time_value = r["sent_at"] or r["due_at"] or ""
        badge_class = "ok" if r["status"] in {"sent","replied"} else ("bad" if r["status"]=="failed" else "muted")
        action = f"<a class=btn href='/history/resend/{r['source']}/{r['id']}'>Kirim Lagi</a>" if r["status"] in {"sent","replied","failed"} else "-"
        type_label = "<br><span class=muted>Pengiriman ulang</span>" if r["source"] == "resend" else ""
        trs += f"<tr><td>{html.escape(r['name'] or str(r['user_id']))}<br><span class=muted>@{html.escape(r['username'] or '-')}</span>{type_label}</td><td>{r['step']}</td><td style='max-width:380px;white-space:pre-wrap'>{html.escape(message)}</td><td>{html.escape(time_value[:16].replace('T',' '))} WIB</td><td class={badge_class}>{labels.get(r['status'],r['status'])}</td><td class=bad>{html.escape(r['error'] or '')}</td><td>{action}</td></tr>"
    body = f"<h1>Riwayat Terkirim</h1><div class=grid><div class=card><div class=muted>Terkirim</div><div class=stat>{counts.get('sent',0)}</div></div><div class=card><div class=muted>Dibalas</div><div class=stat>{counts.get('replied',0)}</div></div><div class=card><div class=muted>Menunggu</div><div class=stat>{counts.get('pending',0)}</div></div><div class=card><div class=muted>Gagal</div><div class=stat>{counts.get('failed',0)}</div></div></div><div class=card><form method=get class=grid><div><label>Cari nama, username, atau ID</label><input name=q value='{html.escape(q, quote=True)}' placeholder='Cari penerima...'></div><div><label>Status</label><select name=status>{options}</select></div><div style='align-self:end'><button>Tampilkan</button> <a class=btn href='/history'>Reset</a></div></form><div style='overflow:auto'><table><thead><tr><th>Penerima</th><th>Tahap</th><th>Isi pesan</th><th>Waktu</th><th>Status</th><th>Error</th><th>Aksi</th></tr></thead><tbody>{trs or '<tr><td colspan=7>Belum ada riwayat.</td></tr>'}</tbody></table></div></div>"
    return page("Riwayat Terkirim", body, request)

@app.get("/history/resend/{source}/{item_id}", response_class=HTMLResponse)
def resend_confirm(request: Request, source: str, item_id: int):
    if not authorized(request): return redirect("/login")
    table = "resends" if source == "resend" else "queue"
    item = db.execute(f"SELECT x.*,c.name,c.username FROM {table} x LEFT JOIN contacts c ON c.user_id=x.user_id WHERE x.id=?", (item_id,)).fetchone()
    if not item: request.session["flash"]="Riwayat tidak ditemukan."; return redirect("/history")
    message = item["message_text"] or setting(f"message_{item['step']}")
    body = f"<h1>Konfirmasi Kirim Lagi</h1><div class=card><p>Penerima: <b>{html.escape(item['name'] or str(item['user_id']))}</b> @{html.escape(item['username'] or '-')}</p><label>Pesan yang akan dikirim</label><div class=card style='white-space:pre-wrap'>{html.escape(message)}</div><p class=bad>Pesan ini sengaja dikirim ulang dan akan tercatat sebagai riwayat baru.</p><form method=post><input type=hidden name=message value='{html.escape(message,quote=True)}'><button>Kirim Lagi Sekarang</button> <a class=btn href='/history'>Batal</a></form></div>"
    return page("Konfirmasi Kirim Lagi", body, request)

@app.post("/history/resend/{source}/{item_id}")
def resend_create(request: Request, source: str, item_id: int, message: str = Form(...)):
    if not authorized(request): return redirect("/login")
    table = "resends" if source == "resend" else "queue"
    item = db.execute(f"SELECT user_id,step FROM {table} WHERE id=?", (item_id,)).fetchone()
    if not item: request.session["flash"]="Riwayat tidak ditemukan."; return redirect("/history")
    contact = db.execute("SELECT blocked FROM contacts WHERE user_id=?", (item["user_id"],)).fetchone()
    if contact and contact["blocked"]:
        request.session["flash"] = "Penerima berstatus STOP/blacklist, pengiriman ulang dibatalkan."
        return redirect("/history")
    db.execute("INSERT INTO resends(user_id,step,due_at,status,message_text,created_at) VALUES(?,?,?,'pending',?,?)", (item["user_id"],item["step"],iso(),message,iso()))
    db.commit(); set_setting("campaign_active","1")
    request.session["flash"] = "Pengiriman ulang dimasukkan ke antrean."
    return redirect("/history?status=pending")

@app.get("/campaign/resend", response_class=HTMLResponse)
def bulk_resend_page(request: Request, q: str = ""):
    if not authorized(request): return redirect("/login")
    params = []
    search_sql = ""
    if q.strip():
        search_sql = "AND (c.name LIKE ? OR c.username LIKE ? OR CAST(c.user_id AS TEXT) LIKE ?)"
        needle = f"%{q.strip()}%"; params = [needle,needle,needle]
    rows = db.execute(f"""SELECT c.user_id,c.name,c.username,c.last_inbound,
      MAX(x.sent_at) last_sent,COUNT(*) total_sent
      FROM contacts c JOIN (
        SELECT user_id,sent_at FROM queue WHERE status IN ('sent','replied')
        UNION ALL SELECT user_id,sent_at FROM resends WHERE status IN ('sent','replied')
      ) x ON x.user_id=c.user_id
      WHERE c.blocked=0 {search_sql}
      GROUP BY c.user_id,c.name,c.username,c.last_inbound
      ORDER BY last_sent DESC LIMIT 900""", params).fetchall()
    trs = "".join(f"<tr><td><input type=checkbox name=ids value='{r['user_id']}'></td><td>{html.escape(r['name'] or str(r['user_id']))}<br><span class=muted>@{html.escape(r['username'] or '-')}</span></td><td>{html.escape((r['last_sent'] or '')[:16].replace('T',' '))} WIB</td><td>{r['total_sent']}</td></tr>" for r in rows)
    default_message = "Halo Bosku 😊 Kami ingin menghubungi Anda kembali. Apakah saat ini ada yang bisa kami bantu? Silakan balas pesan ini. Ketik STOP jika tidak ingin menerima follow-up lagi."
    body = f"<h1>Kampanye Kirim Ulang</h1><div class=card><p class=muted>Daftar hanya berisi orang yang sebelumnya pernah menerima follow-up. Akun STOP/blacklist otomatis tidak ditampilkan.</p><form method=get><div class=grid><div><label>Cari penerima</label><input name=q value='{html.escape(q,quote=True)}' placeholder='Nama, username, atau ID'></div><div style='align-self:end'><button>Cari</button> <a class=btn href='/campaign/resend'>Reset</a></div></div></form></div><div class=card><form method=post><label>Pesan kampanye kirim ulang</label><textarea name=message rows=5 required>{html.escape(default_message)}</textarea><p><button type=button onclick=\"document.querySelectorAll('[name=ids]').forEach(x=>x.checked=true)\">Pilih Semua Tampil</button> <button type=button onclick=\"document.querySelectorAll('[name=ids]').forEach(x=>x.checked=false)\">Kosongkan</button></p><div style='overflow:auto;max-height:520px'><table><thead><tr><th>Pilih</th><th>Penerima</th><th>Terakhir dikirim</th><th>Total kirim</th></tr></thead><tbody>{trs or '<tr><td colspan=4>Tidak ada penerima yang sesuai.</td></tr>'}</tbody></table></div><label style='display:block;margin:18px 0'><input style='width:auto' type=checkbox name=confirm value=yes required> Saya sudah memeriksa penerima dan menyetujui kampanye ini.</label><button>Buat Kampanye Kirim Ulang</button></form></div>"
    return page("Kampanye Kirim Ulang", body, request)

@app.post("/campaign/resend")
async def bulk_resend_create(request: Request):
    if not authorized(request): return redirect("/login")
    form = await request.form()
    if form.get("confirm") != "yes":
        request.session["flash"] = "Konfirmasi kampanye wajib dicentang."
        return redirect("/campaign/resend")
    message = str(form.get("message", "")).strip()
    if not message or len(message) > 4000:
        request.session["flash"] = "Pesan wajib diisi dan maksimal 4.000 karakter."
        return redirect("/campaign/resend")
    raw_ids = list(dict.fromkeys(form.getlist("ids")))[:900]
    ids = []
    for value in raw_ids:
        try: ids.append(int(value))
        except (TypeError,ValueError): pass
    if not ids:
        request.session["flash"] = "Pilih minimal satu penerima."
        return redirect("/campaign/resend")
    placeholders = ",".join("?" for _ in ids)
    valid = db.execute(f"SELECT user_id FROM contacts WHERE blocked=0 AND user_id IN ({placeholders})", ids).fetchall()
    created = 0
    for row in valid:
        # Hindari antrean pengiriman ulang ganda yang masih menunggu untuk user yang sama.
        pending = db.execute("SELECT 1 FROM resends WHERE user_id=? AND status='pending'", (row["user_id"],)).fetchone()
        if pending: continue
        db.execute("INSERT INTO resends(user_id,step,due_at,status,message_text,created_at) VALUES(?,1,?,'pending',?,?)", (row["user_id"],iso(),message,iso()))
        created += 1
    db.commit(); set_setting("campaign_active","1")
    request.session["flash"] = f"Kampanye dibuat: {created} penerima masuk antrean; {len(valid)-created} dilewati karena sudah menunggu."
    return redirect("/history?status=pending")

@app.post("/contacts/select")
async def select_contacts(request: Request):
    if not authorized(request): return redirect("/login")
    form = await request.form(); ids = [int(x) for x in form.getlist("ids")]
    db.execute("UPDATE contacts SET selected=0")
    if ids: db.executemany("UPDATE contacts SET selected=1 WHERE user_id=? AND blocked=0", [(x,) for x in ids])
    db.commit(); request.session["flash"] = f"{len(ids)} kontak dipilih."
    return redirect("/contacts")

@app.get("/settings", response_class=HTMLResponse)
def settings_page(request: Request):
    if not authorized(request): return redirect("/login")
    body = f"<h1>Pengaturan</h1><div class=card><form method=post><label>Pesan pertama</label><textarea name=message_1 rows=4>{setting('message_1')}</textarea><label>Pesan kedua</label><textarea name=message_2 rows=4>{setting('message_2')}</textarea><label>Pesan ketiga</label><textarea name=message_3 rows=4>{setting('message_3')}</textarea><div class=grid><div><label>Usia chat (hari) — isi 0 untuk semua riwayat</label><input type=number min=0 max=3650 name=max_age_days value={setting('max_age_days')}></div><div><label>Batas kirim per hari</label><input type=number min=1 max=100 name=daily_limit value={setting('daily_limit')}></div><div><label>Jeda setiap kirim (detik)</label><input type=number min=30 max=3600 name=interval_seconds value={setting('interval_seconds')}></div><div><label>Follow-up kedua (jam)</label><input type=number min=1 name=step2_hours value={setting('step2_hours')}></div><div><label>Follow-up ketiga (jam dari pertama)</label><input type=number min=2 name=step3_hours value={setting('step3_hours')}></div></div><button>Simpan Pengaturan</button></form></div>"
    return page("Pengaturan", body, request)

@app.post("/settings")
async def save_settings(request: Request):
    if not authorized(request): return redirect("/login")
    form = await request.form()
    for key in ["message_1","message_2","message_3","max_age_days","daily_limit","interval_seconds","step2_hours","step3_hours"]: set_setting(key, form[key])
    request.session["flash"] = "Pengaturan disimpan."
    return redirect("/settings")

@app.post("/campaign/start")
def campaign_start(request: Request):
    if not authorized(request): return redirect("/login")
    users = db.execute("SELECT user_id FROM contacts WHERE selected=1 AND blocked=0 AND replied_after_followup=0").fetchall()
    for r in users: db.execute("INSERT OR IGNORE INTO queue(user_id,step,due_at) VALUES(?,1,?)", (r[0], iso()))
    db.commit(); set_setting("campaign_active", "1"); request.session["flash"] = f"Kampanye aktif untuk {len(users)} kontak."
    return redirect()

@app.post("/campaign/stop")
def campaign_stop(request: Request):
    if not authorized(request): return redirect("/login")
    set_setting("campaign_active", "0"); request.session["flash"] = "Kampanye dihentikan."
    return redirect()

async def on_new_message(event):
    if not event.is_private: return
    sender = await event.get_sender(); uid = event.sender_id
    text = (event.raw_text or "").strip().lower()
    existing = db.execute("SELECT 1 FROM (SELECT user_id,status FROM queue UNION ALL SELECT user_id,status FROM resends) WHERE user_id=? AND status='sent'", (uid,)).fetchone()
    blocked = 1 if text in {"stop","/stop","berhenti","unsubscribe"} else 0
    name = " ".join(filter(None,[getattr(sender,"first_name",None),getattr(sender,"last_name",None)])) or str(uid)
    db.execute("""INSERT INTO contacts(user_id,name,username,last_inbound,replied_after_followup,blocked,updated_at) VALUES(?,?,?,?,?,?,?)
      ON CONFLICT(user_id) DO UPDATE SET name=excluded.name,username=excluded.username,last_inbound=excluded.last_inbound,replied_after_followup=CASE WHEN ? THEN 1 ELSE contacts.replied_after_followup END,blocked=MAX(contacts.blocked,excluded.blocked),updated_at=excluded.updated_at""",
      (uid,name,getattr(sender,"username",None),iso(event.message.date.astimezone(TZ)),1 if existing else 0,blocked,iso(),1 if existing else 0))
    if existing:
        db.execute("UPDATE queue SET status='replied' WHERE user_id=? AND status='pending'", (uid,))
        db.execute("UPDATE resends SET status='replied' WHERE user_id=? AND status='pending'", (uid,))
    db.commit()

async def queue_worker():
    while True:
        try:
            await asyncio.sleep(5)
            if setting("campaign_active") != "1" or not client or not await client.is_user_authorized(): continue
            sent_today = db.execute("SELECT count(*) FROM (SELECT status,sent_at FROM queue UNION ALL SELECT status,sent_at FROM resends) WHERE status='sent' AND substr(sent_at,1,10)=?", (now().date().isoformat(),)).fetchone()[0]
            if sent_today >= int(setting("daily_limit")): continue
            item = db.execute("""SELECT r.*, 'resends' source FROM resends r JOIN contacts c ON c.user_id=r.user_id
              WHERE r.status='pending' AND r.due_at<=? AND c.blocked=0 ORDER BY r.due_at,r.id LIMIT 1""", (iso(),)).fetchone()
            if not item:
                item = db.execute("""SELECT q.*, 'queue' source FROM queue q JOIN contacts c ON c.user_id=q.user_id
                  WHERE q.status='pending' AND q.due_at<=? AND c.blocked=0 AND c.replied_after_followup=0 ORDER BY q.due_at,q.id LIMIT 1""", (iso(),)).fetchone()
            if not item: continue
            try:
                message_text = item["message_text"] or setting(f"message_{item['step']}")
                await client.send_message(item["user_id"], message_text)
                sent = now(); db.execute(f"UPDATE {item['source']} SET status='sent',sent_at=?,error=NULL,message_text=? WHERE id=?", (iso(sent),message_text,item["id"]))
                db.execute("UPDATE contacts SET last_outbound=? WHERE user_id=?", (iso(sent),item["user_id"]))
                if item["source"] == "queue" and item["step"] == 1:
                    db.execute("INSERT OR IGNORE INTO queue(user_id,step,due_at) VALUES(?,2,?)", (item["user_id"],iso(sent+timedelta(hours=int(setting('step2_hours'))))))
                    db.execute("INSERT OR IGNORE INTO queue(user_id,step,due_at) VALUES(?,3,?)", (item["user_id"],iso(sent+timedelta(hours=int(setting('step3_hours'))))))
                db.commit(); log("info", f"Follow-up tahap {item['step']} terkirim ke user {item['user_id']}")
            except FloodWaitError as e:
                log("warning", f"Telegram meminta jeda {e.seconds} detik"); await asyncio.sleep(min(e.seconds + 5, 3600))
            except Exception as e:
                db.execute(f"UPDATE {item['source']} SET status='failed',error=? WHERE id=?", (type(e).__name__,item["id"])); db.commit(); log("error", f"Gagal kirim user {item['user_id']}: {type(e).__name__}")
            await asyncio.sleep(int(setting("interval_seconds")))
        except asyncio.CancelledError: break
        except Exception as e:
            log("error", f"Worker: {type(e).__name__}"); await asyncio.sleep(10)
