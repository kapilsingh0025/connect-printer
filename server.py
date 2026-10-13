"""
Office Print - Cloud server (Render par chalta hai)
Env variables (Render mein set karo):
  ADMIN_USER, ADMIN_PASS   -> admin ka login
  AGENT_KEY                -> office PC wale agent ka secret key (lamba random)
  SECRET_KEY               -> session ke liye lamba random text
  DATABASE_URL (zaroori)   -> Neon/Supabase Postgres ka link (isse users kabhi nahi mitenge)
  USERS (optional)         -> "rahul:pass1,amit:pass2"  (sirf shuruaati users; baad mein admin page se)
  DATA_DIR (optional)      -> persistent disk ka path, jaise /data (agar Postgres nahi lena)
"""
import os, re, time, uuid, sqlite3, hmac, shutil, subprocess, tempfile, threading
from datetime import timedelta

from flask import Flask, request, session, redirect, jsonify, render_template_string, send_file, abort, Response
from werkzeug.security import generate_password_hash, check_password_hash
from PIL import Image, ImageOps

BASE = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.environ.get("DATA_DIR", os.path.join(BASE, "data"))
UPLOAD_DIR = os.path.join(DATA_DIR, "uploads")
os.makedirs(UPLOAD_DIR, exist_ok=True)
DB_PATH = os.path.join(DATA_DIR, "print.db")

ADMIN_USER = os.environ.get("ADMIN_USER", "").strip().lower()
ADMIN_PASS = os.environ.get("ADMIN_PASS", "")
AGENT_KEY = os.environ.get("AGENT_KEY", "")
SECRET_KEY = os.environ.get("SECRET_KEY", "")
if not (ADMIN_USER and ADMIN_PASS and AGENT_KEY and SECRET_KEY):
    raise RuntimeError("ADMIN_USER, ADMIN_PASS, AGENT_KEY, SECRET_KEY environment variables set karo.")

OFFICE_EXT = {".doc", ".docx", ".odt", ".rtf", ".txt", ".xls", ".xlsx", ".ods", ".csv", ".ppt", ".pptx", ".odp"}
CONVERT_EXT = OFFICE_EXT | {".tif", ".tiff"}          # inhe pehle PDF banana padta hai
ALLOWED = {".pdf", ".jpg", ".jpeg", ".png", ".gif", ".bmp", ".webp"} | CONVERT_EXT
NO_OFFICE_MSG = "Is server par Word/Excel/PowerPoint support chalu nahi hai. Abhi PDF ya photo bhejo."
SOFFICE = shutil.which("soffice") or shutil.which("libreoffice")   # Docker mein LibreOffice hota hai
convert_lock = threading.Lock()                                    # ek baar mein ek hi conversion (memory bachane ko)
ONLINE_SECONDS = 30       # itne second mein agent ka heartbeat aaye to printer "online"
STUCK_SECONDS = 600       # printing mein atka job error hoga; duplicate print se bachao
WAITING_SECONDS = 3600    # 1 ghante se zyada pending job expire hogi

app = Flask(__name__)
app.secret_key = SECRET_KEY
app.permanent_session_lifetime = timedelta(days=90)     # ek baar login, 90 din yaad
app.config.update(MAX_CONTENT_LENGTH=25 * 1024 * 1024,
                  SESSION_COOKIE_HTTPONLY=True,
                  SESSION_COOKIE_SAMESITE="Lax",
                  SESSION_COOKIE_SECURE=os.environ.get("INSECURE_COOKIES") != "1")

# ---------------- database ----------------
# DATABASE_URL set ho to Postgres (permanent, users kabhi nahi mitenge). Warna SQLite (Render free par restart par mit jata hai).
HASH_METHOD = "pbkdf2:sha256:100000"       # scrypt free server par bahut slow tha

def hash_pw(p):
    return generate_password_hash(p, method=HASH_METHOD)

DATABASE_URL = os.environ.get("DATABASE_URL", "").strip()
USE_PG = DATABASE_URL.startswith(("postgres://", "postgresql://"))
if USE_PG:
    import psycopg2
    import psycopg2.extras
    if DATABASE_URL.startswith("postgres://"):
        DATABASE_URL = "postgresql://" + DATABASE_URL[len("postgres://"):]

_pool = []                                  # purane connections dobara use hote hain (har baar naya SSL connection slow hota hai)
_pool_lock = threading.Lock()

def db():
    if USE_PG:
        with _pool_lock:
            while _pool:
                c = _pool.pop()
                if not c.closed:
                    return c
        return psycopg2.connect(DATABASE_URL, connect_timeout=15, keepalives=1,
                                keepalives_idle=30, keepalives_interval=10, keepalives_count=3)
    c = sqlite3.connect(DB_PATH, timeout=15)
    c.row_factory = sqlite3.Row
    return c

def _discard(c):
    try:
        c.close()
    except Exception:
        pass

def release(c):
    if not USE_PG:
        c.close()
        return
    try:
        if c.closed:
            return
        c.rollback()
        with _pool_lock:
            if len(_pool) < 8:
                _pool.append(c)
                return
        c.close()
    except Exception:
        _discard(c)

def _cursor(c):
    return c.cursor(cursor_factory=psycopg2.extras.RealDictCursor) if USE_PG else c.cursor()

def run(sql, args=(), one=False, write=False):
    if USE_PG:
        sql = sql.replace("?", "%s")
    for attempt in range(2 if USE_PG else 1):
        c = db()
        try:
            cur = _cursor(c)
            cur.execute(sql, args)
            if write:
                c.commit()
                res = cur.rowcount
            else:
                rows = cur.fetchall()
                res = (rows[0] if rows else None) if one else rows
            release(c)
            return res
        except Exception as e:
            _discard(c)
            if USE_PG and attempt == 0 and isinstance(e, (psycopg2.OperationalError, psycopg2.InterfaceError)):
                continue            # purana connection toot gaya tha, naya lekar dobara
            raise

def init():
    ts = "DOUBLE PRECISION"
    run(f"CREATE TABLE IF NOT EXISTS users(username TEXT PRIMARY KEY, pw TEXT, is_admin INTEGER DEFAULT 0, created {ts})", write=True)
    run(f"CREATE TABLE IF NOT EXISTS printers(id TEXT PRIMARY KEY, label TEXT, last_seen {ts})", write=True)
    run(f"""CREATE TABLE IF NOT EXISTS jobs(id TEXT PRIMARY KEY, owner TEXT, filename TEXT, printer_id TEXT,
            copies INTEGER, color INTEGER, pages INTEGER, status TEXT, error TEXT,
            created {ts}, claimed {ts}, finished {ts}, path TEXT, settings TEXT)""", write=True)
    if not USE_PG:      # purani SQLite database ka migration
        cols = [r["name"] for r in run("PRAGMA table_info(jobs)")]
        if "user" in cols and "owner" not in cols:
            run('ALTER TABLE jobs RENAME COLUMN "user" TO owner', write=True)
        if "settings" not in cols:
            run("ALTER TABLE jobs ADD COLUMN settings TEXT", write=True)
    run("""INSERT INTO users(username,pw,is_admin,created) VALUES(?,?,1,?)
           ON CONFLICT(username) DO UPDATE SET pw=excluded.pw, is_admin=1""",
        (ADMIN_USER, hash_pw(ADMIN_PASS), time.time()), write=True)
    for pair in os.environ.get("USERS", "").split(","):
        if ":" in pair:
            u, p = pair.split(":", 1)
            u = u.strip().lower()
            if u and p:
                run("""INSERT INTO users(username,pw,is_admin,created) VALUES(?,?,0,?)
                       ON CONFLICT(username) DO NOTHING""",
                    (u, hash_pw(p), time.time()), write=True)

init()

# ---------------- memory state (DB par bojh kam karne ke liye) ----------------
state_lock = threading.Lock()
PRINTERS = {}            # printer_id -> {id, agent, name, last_seen, saved}
WAITING = set()          # un printers ki id jin par waiting job hai
S = {"seq": 0, "maint": 0.0}
QCACHE = {"t": 0.0, "v": {}}
_ucache = {}             # username -> (row, expiry)

def load_state():
    for r in run("SELECT id,label,last_seen FROM printers"):
        agent, _, name = r["id"].partition("|")
        PRINTERS[r["id"]] = {"id": r["id"], "agent": agent, "name": name or r["id"],
                             "last_seen": r["last_seen"] or 0, "saved": time.time()}
    for r in run("SELECT DISTINCT printer_id FROM jobs WHERE status='waiting'"):
        WAITING.add(r["printer_id"])

load_state()

def queue_counts():
    now = time.time()
    if now - QCACHE["t"] < 2:
        return QCACHE["v"]
    rows = run("SELECT printer_id, COUNT(*) AS n FROM jobs WHERE status IN ('waiting','printing') GROUP BY printer_id")
    QCACHE["v"] = {r["printer_id"]: int(r["n"]) for r in rows}
    QCACHE["t"] = now
    return QCACHE["v"]

def invalidate_queue():
    QCACHE["t"] = 0.0

def maintenance():
    """Har minute: atki hui printing jobs wapas queue mein, aur WAITING set sahi karo."""
    now = time.time()
    if now - S["maint"] < 60:
        return
    with state_lock:
        if now - S["maint"] < 60:
            return
        S["maint"] = now
    # Expire abandoned waiting jobs and remove their temporary files.
    expired = run("SELECT id,path FROM jobs WHERE status='waiting' AND created<?", (now - WAITING_SECONDS,))
    run("UPDATE jobs SET status='error', error=?, finished=? WHERE status='waiting' AND created<?",
        ("Queue job expired after waiting too long. Please send again.", now, now - WAITING_SECONDS), write=True)
    for job in expired:
        if job["path"] and os.path.exists(job["path"]):
            try: os.remove(job["path"])
            except OSError: pass
    # A printing job may already have reached the physical printer. Never auto-requeue it:
    # doing so can print the same document twice. Mark stale jobs as error instead.
    stale = run("SELECT id,path,printer_id FROM jobs WHERE status='printing' AND claimed<?", (now - STUCK_SECONDS,))
    for job in stale:
        run("UPDATE jobs SET status='error', error=?, finished=? WHERE id=? AND status='printing'",
            ("Print agent stopped responding. Check the printer before sending again.", now, job["id"]), write=True)
        if job["path"] and os.path.exists(job["path"]):
            try: os.remove(job["path"])
            except OSError: pass
    # Jobs claimed by an agent whose heartbeat has gone stale are not safe to retry.
    for job in run("SELECT id,path,printer_id FROM jobs WHERE status='printing'"):
        printer = PRINTERS.get(job["printer_id"])
        if not printer or now - printer.get("last_seen", 0) >= ONLINE_SECONDS:
            run("UPDATE jobs SET status='error', error=?, finished=? WHERE id=? AND status='printing'",
                ("The printer PC went offline. Please send again after checking the printer.", now, job["id"]), write=True)
            if job["path"] and os.path.exists(job["path"]):
                try: os.remove(job["path"])
                except OSError: pass
    waiting = {r["printer_id"] for r in run("SELECT DISTINCT printer_id FROM jobs WHERE status='waiting'")}
    with state_lock:
        WAITING.clear()
        WAITING.update(waiting)
    invalidate_queue()

# ---------------- helpers ----------------
fails = {}   # ip -> [timestamps]  (login brute-force se bachav)

def client_ip():
    return (request.headers.get("X-Forwarded-For", request.remote_addr or "").split(",")[0]).strip()

def current_user():
    u = session.get("user")
    if not u:
        return None
    hit = _ucache.get(u)
    now = time.time()
    if hit and hit[1] > now:
        return hit[0]
    row = run("SELECT * FROM users WHERE username=?", (u,), one=True)
    if row:
        _ucache[u] = (row, now + 30)       # 30 second cache: har request par DB nahi jaana
    else:
        _ucache.pop(u, None)
    return row

def agent_ok():
    return hmac.compare_digest(request.headers.get("X-Agent-Key", ""), AGENT_KEY)

def ahead_count(printer_id, created=None):
    if created is None:
        r = run("SELECT COUNT(*) n FROM jobs WHERE printer_id=? AND status IN ('waiting','printing')",
                (printer_id,), one=True)
    else:
        r = run("""SELECT COUNT(*) n FROM jobs WHERE printer_id=? AND status IN ('waiting','printing')
                   AND created<?""", (printer_id, created), one=True)
    return r["n"]


PAPERS = {"A3": "A3", "A4": "A4", "A5": "A5", "letter": "letter", "legal": "legal"}
PAPER_PT = {"A3": (842, 1191), "A4": (595, 842), "A5": (420, 595), "letter": (612, 792), "legal": (612, 1008)}
ORIENTS = {"auto", "portrait", "landscape"}
SCALES = {"shrink", "fit", "noscale", "custom"}
SIDES = {"single": "simplex", "long": "duplexlong", "short": "duplexshort"}
NUP_GRID = {2: (1, 2), 4: (2, 2), 6: (2, 3), 9: (3, 3)}   # (cols, rows) portrait sheet

def select_pages(mode, rng, n):
    """Kaun se pages print honge (1-based list). Galat ho to ValueError."""
    if mode == "custom":
        txt = (rng or "").replace(" ", "")
        if not txt:
            raise ValueError
        pages = set()
        for tok in txt.split(","):
            m = re.fullmatch(r"(\d+)(?:-(\d+))?", tok)
            if not m:
                raise ValueError
            a = int(m.group(1))
            b = int(m.group(2) or a)
            if a < 1 or b < a or b > n:
                raise ValueError
            pages.update(range(a, b + 1))
        return sorted(pages)
    base = list(range(1, n + 1))
    if mode == "odd":
        base = [p for p in base if p % 2 == 1]
    elif mode == "even":
        base = [p for p in base if p % 2 == 0]
    elif mode != "all":
        raise ValueError
    if not base:
        raise ValueError
    return base

def process_pdf(src, dst, sel, orient, nup, paper, scale="shrink", pct=100):
    """Chune hue pages nikalo, orientation lagao, pages-per-sheet banao. Sumatra ke liye orientation token lautata hai."""
    from pypdf import PdfReader, PdfWriter, PageObject, Transformation
    r = PdfReader(src)
    pages = [r.pages[i - 1] for i in sel]
    for p in pages:
        try:
            p.transfer_rotation_to_content()
        except Exception:
            pass
    def size(p):
        return float(p.mediabox.width), float(p.mediabox.height)
    w = PdfWriter()
    pw0, ph0 = PAPER_PT[paper]
    if nup == 1:
        for p in pages:
            ww, hh = size(p)
            rotated = False
            if orient != "auto" and (ww > hh) != (orient == "landscape"):
                p.rotate(90)
                rotated = True
            if scale == "custom":
                if rotated:
                    p.transfer_rotation_to_content()
                ww, hh = size(p)
                land = (ww > hh) if orient == "auto" else (orient == "landscape")
                sw, sh = (ph0, pw0) if land else (pw0, ph0)
                f = pct / 100.0
                sheet = PageObject.create_blank_page(width=sw, height=sh)
                tr = (Transformation().translate(-float(p.mediabox.left), -float(p.mediabox.bottom))
                      .scale(f, f).translate((sw - ww * f) / 2, (sh - hh * f) / 2))
                sheet.merge_transformed_page(p, tr)
                w.add_page(sheet)
            else:
                w.add_page(p)
        token = orient if orient != "auto" else None
    else:
        fw, fh = size(pages[0])
        first_land = fw > fh
        if orient == "auto":
            land = (not first_land) if nup in (2, 6) else first_land
        else:
            land = orient == "landscape"
        sw, sh = (ph0, pw0) if land else (pw0, ph0)
        cols, rows = NUP_GRID[nup]
        if land:
            cols, rows = rows, cols
        m = 18.0
        cw, ch = (sw - 2 * m) / cols, (sh - 2 * m) / rows
        for i in range(0, len(pages), nup):
            sheet = PageObject.create_blank_page(width=sw, height=sh)
            for k, p in enumerate(pages[i:i + nup]):
                ww, hh = size(p)
                s = min(cw / ww, ch / hh)
                c, rr = k % cols, k // cols
                x = m + c * cw + (cw - ww * s) / 2
                y = sh - m - (rr + 1) * ch + (ch - hh * s) / 2
                tr = Transformation().translate(-float(p.mediabox.left), -float(p.mediabox.bottom)).scale(s, s).translate(x, y)
                sheet.merge_transformed_page(p, tr)
            w.add_page(sheet)
        token = "landscape" if land else "portrait"
    with open(dst, "wb") as f:
        w.write(f)
    return token

def build_settings(copies, color, paper, token, scale, sides):
    parts = [f"{copies}x", "color" if color else "monochrome", "paper=" + PAPERS[paper]]
    if token:
        parts.append(token)
    parts.append(scale)
    parts.append(SIDES[sides])
    return ",".join(parts)

def image_to_pdf(path, pdf):
    im = ImageOps.exif_transpose(Image.open(path)).convert("RGB")
    w, h = im.size
    im.thumbnail((3508, 2480) if w > h else (2480, 3508))
    w, h = im.size
    # A4 par poora aaye, aisi resolution
    res = max(w / 11.69, h / 8.27) if w > h else max(w / 8.27, h / 11.69)
    im.save(pdf, "PDF", resolution=max(res, 50))


def office_to_pdf(src, dst):
    if not SOFFICE:
        raise RuntimeError("no_office")
    outdir = tempfile.mkdtemp(prefix="conv_")
    profile = tempfile.mkdtemp(prefix="lo_")
    try:
        with convert_lock:
            subprocess.run([SOFFICE, f"-env:UserInstallation=file://{profile}", "--headless", "--norestore",
                            "--convert-to", "pdf", "--outdir", outdir, src],
                           check=True, timeout=120, capture_output=True,
                           env={**os.environ, "HOME": profile})
        result = os.path.join(outdir, os.path.splitext(os.path.basename(src))[0] + ".pdf")
        shutil.move(result, dst)
    finally:
        shutil.rmtree(outdir, ignore_errors=True)
        shutil.rmtree(profile, ignore_errors=True)

def to_pdf(src, dst, ext):
    if ext in OFFICE_EXT:
        office_to_pdf(src, dst)
    else:
        image_to_pdf(src, dst)

def count_pages(path):
    try:
        from pypdf import PdfReader
        return len(PdfReader(path).pages)
    except Exception:
        return 1

# ---------------- templates ----------------
STYLE = """<style>
body{font-family:system-ui,sans-serif;background:#f3f4f6;margin:0;padding:16px;color:#111}
.c{background:#fff;padding:18px;border-radius:14px;max-width:520px;margin:0 auto 14px;box-shadow:0 2px 10px #0001}
h2,h3{margin:0 0 10px}label{display:block;margin-top:12px;font-weight:600}
input,select,button{width:100%;padding:12px;font-size:16px;border:1px solid #ccc;border-radius:8px;box-sizing:border-box;margin-top:6px}
button{background:#2563eb;color:#fff;border:0;margin-top:14px}button:disabled{background:#9ca3af}
button.red{background:#dc2626;margin:0;padding:6px 10px;font-size:13px;width:auto}
.msg{margin-top:12px;padding:10px;border-radius:8px;background:#fef3c7;display:none}
.msg.ok{background:#d1fae5}.msg.err{background:#fee2e2}
.job{display:flex;justify-content:space-between;gap:8px;padding:8px 0;border-top:1px solid #eee;font-size:14px}
.top{display:flex;justify-content:space-between;max-width:520px;margin:0 auto 10px}
table{width:100%;border-collapse:collapse;font-size:14px}td,th{padding:6px 4px;border-top:1px solid #eee;text-align:left}
a{color:#2563eb}.e{color:#c00;margin-top:8px}
</style>"""

LOGIN_HTML = """<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><meta name="theme-color" content="#1d4ed8">
<title>Sign in · Office Print</title>
<style>
:root{--blue:#2563eb;--ink:#0f172a;--mut:#64748b;--line:#e2e8f0}
*{box-sizing:border-box}
html,body{height:100%}
body{margin:0;font-family:Inter,system-ui,-apple-system,"Segoe UI",Roboto,Arial,sans-serif;color:var(--ink);background:#f1f5f9}
.shell{min-height:100%;display:grid;grid-template-columns:1.05fr 1fr}
.hero{background:linear-gradient(145deg,#1e3a8a 0%,#2563eb 58%,#38bdf8 125%);color:#fff;padding:56px;display:flex;flex-direction:column;justify-content:space-between;position:relative;overflow:hidden}
.hero:before,.hero:after{content:"";position:absolute;border-radius:50%;background:rgba(255,255,255,.08)}
.hero:before{width:420px;height:420px;right:-140px;top:-120px}
.hero:after{width:300px;height:300px;left:-100px;bottom:-110px}
.hero>*{position:relative;z-index:1}
.brand{white-space:nowrap;display:flex;align-items:center;gap:12px;font-weight:700;font-size:20px}
.logo{width:44px;height:44px;border-radius:13px;background:rgba(255,255,255,.18);display:grid;place-items:center}
.hero h1{font-size:38px;line-height:1.15;margin:0 0 14px;letter-spacing:-.5px}
.hero p{margin:0;color:#dbeafe;font-size:16px;max-width:420px;line-height:1.5}
.feats{list-style:none;padding:0;margin:30px 0 0;display:grid;gap:14px}
.feats li{display:flex;gap:12px;align-items:center;font-size:15px}
.feats i{width:32px;height:32px;border-radius:10px;background:rgba(255,255,255,.18);display:grid;place-items:center;flex:none}
.foot{font-size:13px;color:#bfdbfe}
.side{display:flex;flex-direction:column;align-items:center;justify-content:center;padding:32px 20px}
.card{width:100%;max-width:400px;background:#fff;border-radius:18px;padding:36px 32px;border:1px solid var(--line);box-shadow:0 10px 40px rgba(15,23,42,.08),0 1px 3px rgba(15,23,42,.06)}
.mbrand{display:none;align-items:center;gap:10px;color:#fff;font-weight:700;font-size:20px;margin-bottom:20px}
.mbrand .logo{background:rgba(255,255,255,.2)}
h2{margin:0 0 6px;font-size:26px;letter-spacing:-.3px}
.sub{margin:0 0 8px;color:var(--mut);font-size:15px}
label{display:block;font-size:13px;font-weight:600;margin:18px 0 6px;color:#334155}
.field{position:relative}
.field .ic{position:absolute;left:14px;top:50%;transform:translateY(-50%);color:#94a3b8;pointer-events:none;display:flex}
input{width:100%;height:48px;padding:0 46px 0 44px;font-size:16px;border:1.5px solid var(--line);border-radius:12px;background:#f8fafc;color:var(--ink);outline:none;transition:.15s;font-family:inherit}
input:focus{border-color:var(--blue);background:#fff;box-shadow:0 0 0 4px rgba(37,99,235,.12)}
.eye{position:absolute;right:6px;top:50%;transform:translateY(-50%);width:38px;height:38px;border:0;background:none;color:#94a3b8;cursor:pointer;border-radius:8px;display:grid;place-items:center}
.eye:hover{color:#475569;background:#f1f5f9}
.err{margin-top:18px;padding:11px 14px;border-radius:10px;background:#fef2f2;color:#b91c1c;font-size:14px;border:1px solid #fecaca;display:flex;gap:8px;align-items:center}
button.go{margin-top:24px;width:100%;height:50px;border:0;border-radius:12px;background:linear-gradient(180deg,#3b82f6,#2563eb);color:#fff;font-size:16px;font-weight:600;cursor:pointer;box-shadow:0 4px 14px rgba(37,99,235,.35);transition:.15s;font-family:inherit}
button.go:hover{filter:brightness(1.06)}
button.go:active{transform:translateY(1px)}
button.go[disabled]{opacity:.7;cursor:wait}
.note{margin-top:22px;text-align:center;font-size:13px;color:var(--mut)}
@media(max-width:860px){
  body{background:linear-gradient(160deg,#1e3a8a,#2563eb 60%,#38bdf8) fixed}
  .shell{grid-template-columns:1fr}.hero{display:none}
  .mbrand{display:flex}.side{padding:24px 16px}.card{padding:28px 22px}
}
</style></head><body>
<div class="shell">
 <section class="hero">
  <div class="brand"><span class="logo"><svg width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="#fff" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polyline points="6 9 6 2 18 2 18 9"/><path d="M6 18H4a2 2 0 0 1-2-2v-5a2 2 0 0 1 2-2h16a2 2 0 0 1 2 2v5a2 2 0 0 1-2 2h-2"/><rect x="6" y="14" width="12" height="8"/></svg></span>Office Print</div>
  <div>
   <h1>Print from your phone.<br>No cables, no queues.</h1>
   <p>Upload a document, check the preview, choose your settings and it prints on the office printer.</p>
   <ul class="feats">
    <li><i><svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="#fff" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="5" y="2" width="14" height="20" rx="2"/><line x1="12" y1="18" x2="12.01" y2="18"/></svg></i>Works with any phone or computer</li>
    <li><i><svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="#fff" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="10"/><polyline points="12 6 12 12 16 14"/></svg></i>Live queue &mdash; know when your turn comes</li>
    <li><i><svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="#fff" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10z"/></svg></i>Private &mdash; files are deleted after printing</li>
   </ul>
  </div>
  <div class="foot">Secure office printing</div>
 </section>
 <main class="side">
  <div class="mbrand"><span class="logo"><svg width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="#fff" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polyline points="6 9 6 2 18 2 18 9"/><path d="M6 18H4a2 2 0 0 1-2-2v-5a2 2 0 0 1 2-2h16a2 2 0 0 1 2 2v5a2 2 0 0 1-2 2h-2"/><rect x="6" y="14" width="12" height="8"/></svg></span>Office Print</div>
  <form class="card" method="post" id="f">
   <h2>Welcome back</h2>
   <p class="sub">Sign in to start printing.</p>
   <label for="u">Username</label>
   <div class="field"><span class="ic"><svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M20 21v-2a4 4 0 0 0-4-4H8a4 4 0 0 0-4 4v2"/><circle cx="12" cy="7" r="4"/></svg></span>
    <input id="u" name="u" placeholder="Enter your username" autocapitalize="none" autocomplete="username" autofocus required></div>
   <label for="p">Password</label>
   <div class="field"><span class="ic"><svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="3" y="11" width="18" height="11" rx="2"/><path d="M7 11V7a5 5 0 0 1 10 0v4"/></svg></span>
    <input id="p" name="p" type="password" placeholder="Enter your password" autocomplete="current-password" required>
    <button type="button" class="eye" id="eye" aria-label="Show password"><svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M1 12s4-8 11-8 11 8 11 8-4 8-11 8-11-8-11-8z"/><circle cx="12" cy="12" r="3"/></svg></button></div>
   {% if err %}<div class="err" role="alert"><svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" style="flex:none"><circle cx="12" cy="12" r="10"/><line x1="12" y1="8" x2="12" y2="12"/><line x1="12" y1="16" x2="12.01" y2="16"/></svg><span>{{err}}</span></div>{% endif %}
   <button class="go" id="go">Sign in</button>
   <div class="note">Trouble signing in? Ask your office admin.</div>
  </form>
 </main>
</div>
<script>
const p=document.getElementById("p");
document.getElementById("eye").onclick=function(){p.type=p.type=="password"?"text":"password"};
document.getElementById("f").addEventListener("submit",function(){var b=document.getElementById("go");b.disabled=true;b.textContent="Signing in..."});
</script>
</body></html>"""

APP_CSS = r"""<style>
:root{--blue:#2563eb;--ink:#0f172a;--mut:#64748b;--line:#e2e8f0;--bg:#f1f5f9}
*{box-sizing:border-box}
[hidden]{display:none!important}
html{-webkit-text-size-adjust:100%}
body{margin:0;font-family:Inter,system-ui,-apple-system,"Segoe UI",Roboto,Arial,sans-serif;background:var(--bg);color:var(--ink);font-size:15px;line-height:1.45}
.appbar{position:sticky;top:0;z-index:20;background:#fff;border-bottom:1px solid var(--line)}
.appbar .in{max-width:1040px;margin:0 auto;padding:10px 16px;display:flex;align-items:center;gap:10px}
.brand{white-space:nowrap;flex:none;display:flex;align-items:center;gap:10px;font-weight:700;font-size:17px;text-decoration:none;color:var(--ink)}
.logo{width:36px;height:36px;border-radius:10px;background:linear-gradient(145deg,#1e3a8a,#2563eb);display:grid;place-items:center;flex:none}
.sp{flex:1}
.netbar{background:#fee2e2;color:#991b1b;text-align:center;font-size:13px;font-weight:600;padding:6px 12px}
.chip{display:flex;align-items:center;gap:8px;padding:3px 12px 3px 3px;border-radius:99px;background:#f1f5f9;font-weight:600;font-size:14px}
.av{width:30px;height:30px;border-radius:50%;background:var(--blue);color:#fff;display:grid;place-items:center;font-size:13px;text-transform:uppercase}
.btn{white-space:nowrap;display:inline-flex;align-items:center;justify-content:center;gap:6px;height:44px;padding:0 20px;border:0;border-radius:12px;font-size:15px;font-weight:600;cursor:pointer;font-family:inherit;text-decoration:none;transition:.15s;-webkit-tap-highlight-color:transparent}
.btn.pri{background:linear-gradient(180deg,#3b82f6,#2563eb);color:#fff;box-shadow:0 3px 10px rgba(37,99,235,.3)}
.btn.pri:disabled{opacity:.6;cursor:wait}
.btn.gray{background:#e2e8f0;color:#1e293b}
.btn.red{background:#dc2626;color:#fff}
.btn.sm{height:36px;padding:0 14px;font-size:14px;border-radius:10px}
.btn.ghost{background:transparent;color:#475569}.btn.ghost:hover{background:#f1f5f9}
.btn:disabled{opacity:.45;cursor:default}
.wrap{max-width:1040px;margin:0 auto;padding:16px}
.card{min-width:0;background:#fff;border:1px solid var(--line);border-radius:16px;padding:20px;margin-bottom:16px;box-shadow:0 1px 3px rgba(15,23,42,.04)}
.card h2{margin:0 0 14px;font-size:17px;display:flex;align-items:center}
.step{display:inline-grid;place-items:center;width:24px;height:24px;border-radius:50%;background:var(--blue);color:#fff;font-size:13px;margin-right:10px;flex:none}
.msg{display:none;padding:12px 14px;border-radius:12px;margin-bottom:14px;background:#fef3c7;color:#92400e;font-weight:500}
.msg.ok{background:#dcfce7;color:#166534}.msg.err{background:#fee2e2;color:#991b1b}
.empty{padding:18px;text-align:center;color:var(--mut);background:#f8fafc;border-radius:12px;font-size:14px}
.badge{display:inline-flex;align-items:center;gap:5px;font-size:12px;font-weight:600;padding:2px 10px;border-radius:99px;white-space:nowrap}
.badge.ok{background:#dcfce7;color:#15803d}.badge.warn{background:#fef3c7;color:#b45309}.badge.bad{background:#fee2e2;color:#b91c1c}.badge.blue{background:#dbeafe;color:#1d4ed8}.badge.gray{background:#e2e8f0;color:#475569}
.dot{width:7px;height:7px;border-radius:50%;background:currentColor;display:inline-block}
.ptiles{display:grid;grid-template-columns:repeat(auto-fit,minmax(230px,1fr));gap:10px}
.ptile{border:2px solid var(--line);border-radius:14px;padding:12px 14px;cursor:pointer;display:flex;gap:12px;align-items:center;background:#fff;text-align:left;font-family:inherit;font-size:inherit;color:inherit;transition:.15s}
.ptile:hover{border-color:#93c5fd}
.ptile.sel{border-color:var(--blue);background:#eff6ff;box-shadow:0 0 0 3px rgba(37,99,235,.12)}
.pi{width:42px;height:42px;border-radius:11px;background:#f1f5f9;display:grid;place-items:center;color:#475569;flex:none}
.ptile.sel .pi{background:#dbeafe;color:var(--blue)}
.pt{min-width:0;display:grid;gap:3px}
.pt .badge{justify-self:start}
.pn{font-weight:600;line-height:1.25;word-break:break-word}.ps{font-size:12px;color:var(--mut)}
.drop{display:block;border:2px dashed #cbd5e1;border-radius:14px;padding:28px 16px;text-align:center;cursor:pointer;background:#f8fafc;transition:.15s}
.drop:hover,.drop.over{border-color:var(--blue);background:#eff6ff}
.drop .ico{width:50px;height:50px;border-radius:14px;background:#dbeafe;color:var(--blue);display:grid;place-items:center;margin:0 auto 10px}
.drop b{display:block;font-size:16px}.drop small{display:block;color:var(--mut);margin-top:2px}
.fileinfo{display:flex;align-items:center;gap:12px;padding:12px;border:1px solid var(--line);border-radius:12px;background:#f8fafc}
.jf{width:40px;height:40px;border-radius:10px;background:#eff6ff;color:var(--blue);display:grid;place-items:center;flex:none}
.jm{flex:1;min-width:0}.jn{font-weight:600;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.jm .badge{vertical-align:middle;margin-left:6px}.js{font-size:12px;color:var(--mut)}
.dlg{display:grid;gap:16px;grid-template-columns:minmax(0,1fr)}
.dlg>*{min-width:0}
@media(min-width:900px){.dlg{grid-template-columns:minmax(0,1fr) 390px;align-items:start}.prevbox{position:sticky;top:76px}}
.prevbox{background:#e8edf3}
.sumtop{font-size:20px;font-weight:700}.subtop{font-size:13px;color:#475569;margin-bottom:12px}
.sheetwrap{display:flex;justify-content:center;min-height:120px}
.sheet{position:relative;background:#fff;box-shadow:0 2px 10px #0006;overflow:hidden;display:none}
.nav{display:flex;justify-content:center;gap:14px;align-items:center;margin-top:12px;font-size:14px;font-weight:600}
.pmsg{font-size:13px;color:#b45309;margin-top:8px;text-align:center}
.row{display:grid;grid-template-columns:130px 1fr;gap:10px;align-items:center;padding:9px 0;border-top:1px solid #f1f5f9}
.row:first-of-type{border-top:0}
.row label{font-weight:600;font-size:14px;color:#334155}
.row select,.row input{width:100%;height:42px;padding:0 12px;border:1.5px solid var(--line);border-radius:10px;background:#f8fafc;font-size:16px;font-family:inherit;color:var(--ink);outline:none}
.row select:focus,.row input:focus{border-color:var(--blue);background:#fff;box-shadow:0 0 0 3px rgba(37,99,235,.12)}
.row select:disabled{opacity:.5}
.pctw{display:flex;align-items:center;gap:8px;font-weight:600}
.rerr{color:#b91c1c;font-size:14px;min-height:0;margin-top:6px}
.hint{font-size:12px;color:var(--mut);margin:10px 0 0}
.acts{display:flex;gap:10px;margin-top:14px}.acts .btn{flex:1}
.job{display:flex;align-items:center;gap:12px;padding:12px 0;border-top:1px solid #f1f5f9}
.job:first-child{border-top:0}
.tiles{display:grid;grid-template-columns:repeat(4,1fr);gap:12px;margin-bottom:16px}
.tile{background:#fff;border:1px solid var(--line);border-radius:16px;padding:16px}
.tile b{display:block;font-size:26px;letter-spacing:-.5px}.tile span{color:var(--mut);font-size:13px}
.two{display:grid;grid-template-columns:minmax(0,1fr) minmax(0,1fr);gap:16px}.two .card{margin-bottom:16px}
.addf{display:grid;grid-template-columns:minmax(0,1fr) minmax(0,1fr) auto;gap:10px}
.addf input{height:44px;padding:0 12px;border:1.5px solid var(--line);border-radius:10px;background:#f8fafc;font-size:16px;font-family:inherit;width:100%;min-width:0}
.note{padding:11px 14px;border-radius:12px;margin-bottom:16px;font-size:14px;font-weight:500}
.note.ok{background:#dcfce7;color:#166534}.note.warn{background:#fef3c7;color:#92400e}
.modal{position:fixed;inset:0;background:rgba(15,23,42,.5);display:none;align-items:center;justify-content:center;z-index:50;padding:16px}
.modal.show{display:flex}
.mbox{background:#fff;border-radius:18px;padding:24px;max-width:380px;width:100%;box-shadow:0 20px 60px rgba(0,0,0,.3)}
.mbox h3{margin:0 0 6px;font-size:19px}.mbox p{margin:0 0 20px;color:var(--mut)}
.macts{display:flex;gap:10px;justify-content:flex-end}
@media(max-width:700px){
  .tiles{grid-template-columns:1fr 1fr}.two{grid-template-columns:minmax(0,1fr)}.addf{grid-template-columns:minmax(0,1fr)}
}
@media(max-width:600px){
  .appbar .in{padding:8px 12px;gap:8px}.wrap{padding:12px}.card{padding:16px;border-radius:14px}
  .chip .nm{display:none}.chip{padding:3px}.brand{font-size:16px;gap:8px}
  .ptiles{grid-template-columns:1fr}
  .drop{padding:24px 12px}
}
@media(max-width:480px){.row{grid-template-columns:1fr;gap:6px}}
@media(max-width:899px){.acts{position:sticky;bottom:0;background:#fff;padding:12px 0 6px;border-top:1px solid var(--line);margin:14px 0 0}}
</style>"""

HEADER_HTML = r"""<header class="appbar"><div class="in">
 <a class="brand" href="/"><span class="logo"><svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="#fff" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polyline points="6 9 6 2 18 2 18 9"/><path d="M6 18H4a2 2 0 0 1-2-2v-5a2 2 0 0 1 2-2h16a2 2 0 0 1 2 2v5a2 2 0 0 1-2 2h-2"/><rect x="6" y="14" width="12" height="8"/></svg></span><span>Office Print</span></a>
 <span class="sp"></span>
 {% if admin %}<a class="btn ghost sm" href="{{ '/' if page == 'admin' else '/admin' }}">{{ 'Print' if page == 'admin' else 'Admin' }}</a>{% endif %}
 <span class="chip"><span class="av">{{ user[:1] }}</span><span class="nm">{{ user }}</span></span>
 <button class="btn gray sm" type="button" onclick="doLogout()">Log out</button>
</div><div class="netbar" id="net" hidden>Connection problem. Retrying...</div></header>"""

MODAL_HTML = r"""<div class="modal" id="cm"><div class="mbox"><h3 id="cmt"></h3><p id="cmp"></p>
<div class="macts"><button class="btn gray" type="button" id="cmn">Cancel</button><button class="btn red" type="button" id="cmy">OK</button></div></div></div>
<script>
function confirmBox(t,p,ok,fn){
  var m=document.getElementById("cm");
  document.getElementById("cmt").textContent=t; document.getElementById("cmp").textContent=p;
  var y=document.getElementById("cmy"); y.textContent=ok;
  function close(){m.classList.remove("show")}
  y.onclick=function(){close();fn()};
  document.getElementById("cmn").onclick=close;
  m.onclick=function(e){if(e.target===m)close()};
  m.classList.add("show");
}
document.addEventListener("keydown",function(e){if(e.key==="Escape")document.getElementById("cm").classList.remove("show")});
function doLogout(){confirmBox("Log out?","You will need to sign in again to print.","Log out",function(){location.href="/logout"})}
</script>"""

MAIN_HTML = r"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><meta name="theme-color" content="#1d4ed8"><title>Print · Office Print</title><link rel="preconnect" href="https://cdnjs.cloudflare.com" crossorigin>""" + APP_CSS + r"""</head><body>""" + HEADER_HTML + r"""
<main class="wrap">
 <div class="msg" id="msg"></div>
 <section class="card">
  <h2><span class="step">1</span>Choose printer</h2>
  <div class="ptiles" id="ptiles"><div class="empty">Looking for printers...</div></div>
 </section>
 <section class="card">
  <h2><span class="step">2</span>Choose file</h2>
  <label class="drop" id="drop" for="file">
   <span class="ico"><svg width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><polyline points="17 8 12 3 7 8"/><line x1="12" y1="3" x2="12" y2="15"/></svg></span>
   <b>Tap to choose a file</b><small>or drag &amp; drop it here</small><small>PDF · Word · Excel · PowerPoint · Text · Photos</small>
  </label>
  <input type="file" id="file" hidden accept=".pdf,.jpg,.jpeg,.png,.gif,.bmp,.webp,.tif,.tiff,.doc,.docx,.odt,.rtf,.txt,.xls,.xlsx,.ods,.csv,.ppt,.pptx,.odp,image/*,application/pdf">
  <div class="fileinfo" id="fileinfo" hidden>
   <span class="jf"><svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><polyline points="14 2 14 8 20 8"/></svg></span>
   <div class="jm"><div class="jn" id="fname"></div><div class="js" id="fsize"></div></div>
   <button class="btn gray sm" type="button" onclick="cancelFile()">Remove</button>
  </div>
 </section>

 <div class="dlg" id="dlg" hidden>
  <section class="card prevbox">
   <div class="sumtop" id="sumtop"></div><div class="subtop" id="subtop"></div>
   <div class="sheetwrap" id="sheetwrap"><div class="sheet" id="sheet"></div></div>
   <div class="nav"><button class="btn gray sm" id="prev" type="button">◀</button><span id="navtxt"></span><button class="btn gray sm" id="next" type="button">▶</button></div>
   <div class="pmsg" id="pmsg"></div>
  </section>
  <section class="card">
   <h2><span class="step">3</span>Print settings</h2>
   <div class="row"><label for="copies">Copies</label><div><input type="number" id="copies" value="1" min="1" max="50" inputmode="numeric" oninput="refresh()"></div></div>
   <div class="row"><label for="pmode">Pages</label><div><select id="pmode" onchange="cur=0;refresh()"><option value="all">All</option><option value="odd">Odd pages only</option><option value="even">Even pages only</option><option value="custom">Custom</option></select></div></div>
   <div class="row" id="rangerow" style="display:none"><label for="range">Page range</label><div><input id="range" placeholder="e.g. 1-3,5" oninput="cur=0;refresh()"></div></div>
   <div class="row"><label for="orient">Layout</label><div><select id="orient" onchange="refresh()"><option value="auto">Auto</option><option value="portrait">Portrait</option><option value="landscape">Landscape</option></select></div></div>
   <div class="row"><label for="color">Color</label><div><select id="color" onchange="refresh()"><option value="0">Black and white</option><option value="1">Color</option></select></div></div>
   <div class="row"><label for="paper">Paper size</label><div><select id="paper" onchange="refresh()"><option value="A4">A4</option><option value="A3">A3</option><option value="A5">A5</option><option value="letter">Letter</option><option value="legal">Legal</option></select></div></div>
   <div class="row"><label for="nup">Pages per sheet</label><div><select id="nup" onchange="cur=0;refresh()"><option value="1">1</option><option value="2">2</option><option value="4">4</option><option value="6">6</option><option value="9">9</option></select></div></div>
   <div class="row"><label for="scale">Scale</label><div><select id="scale" onchange="refresh()"><option value="shrink">Shrink to fit</option><option value="fit">Fit to page</option><option value="noscale">Actual size (100%)</option><option value="custom">Custom...</option></select></div></div>
   <div class="row" id="pctrow" style="display:none"><label for="pct">Custom scale</label><div class="pctw"><input type="number" id="pct" value="100" min="10" max="400" step="5" inputmode="numeric" oninput="refresh()"><span>%</span></div></div>
   <div class="row"><label for="sides">Two-sided</label><div><select id="sides" onchange="refresh()"><option value="single">Off</option><option value="long">Flip on long edge</option><option value="short">Flip on short edge</option></select></div></div>
   <div class="rerr" id="rerr"></div>
   <p class="hint">Preview is approximate. Two-sided works only if the printer supports it.</p>
   <div class="acts"><button class="btn pri" id="btn" type="button" onclick="send()">Print</button><button class="btn gray" type="button" onclick="cancelFile()">Cancel</button></div>
  </section>
 </div>

 <section class="card"><h2>My recent jobs</h2><div id="jobs"><div class="empty">No jobs yet. Your prints will show up here.</div></div></section>
</main>
""" + MODAL_HTML + r"""
<script>
const $=id=>document.getElementById(id);
const label={waiting:"⏳ Waiting",printing:"🖨️ Printing",done:"✅ Done",error:"❌ Error"};
const PAPER={A3:[842,1191],A4:[595,842],A5:[420,595],letter:[612,792],legal:[612,1008]};
const GRID={2:[1,2],4:[2,2],6:[2,3],9:[3,3]};
let pdfDoc=null,imgURL=null,imgDim=null,nPages=0,cur=0,renderTok=0,workFile=null;
const CONVERT=new Set(["doc","docx","odt","rtf","txt","xls","xlsx","ods","csv","ppt","pptx","odp","tif","tiff"]);
const sizeCache={},srcCache={};

function show(t,c){const m=$("msg");m.className="msg "+(c||"");m.textContent=t;m.style.display="block";window.scrollTo(0,0)}

function parseRange(txt,n){
  txt=txt.replace(/\s/g,"");
  if(!txt) return {err:"Enter pages, e.g. 1-3,5"};
  const set=new Set(),parts=[];
  for(const tok of txt.split(",")){
    const m=/^(\d+)(?:-(\d+))?$/.exec(tok);
    if(!m) return {err:"Use format like 1-3,5"};
    const a=+m[1], b=m[2]?+m[2]:a;
    if(a<1||b<a) return {err:"Invalid page number."};
    if(n>0&&b>n) return {err:"This file has only "+n+" pages."};
    if(b>5000) return {err:"Page number too large."};
    for(let i=a;i<=b;i++) set.add(i);
    parts.push(a+"-"+b);
  }
  return {list:Array.from(set).sort((x,y)=>x-y),norm:parts.join(",")};
}

function getSel(){
  const mode=$("pmode").value;
  if(!nPages) return {unknown:true,list:[]};
  let base,norm="";
  if(mode=="custom"){
    const r=parseRange($("range").value,nPages);
    if(r.err) return {err:r.err};
    base=r.list; norm=r.norm;
  }else{
    base=[]; for(let i=1;i<=nPages;i++) base.push(i);
    if(mode=="odd") base=base.filter(p=>p%2==1);
    if(mode=="even") base=base.filter(p=>p%2==0);
  }
  if(!base.length) return {err:"No pages selected."};
  return {list:base,norm:norm};
}

/* Sheet geometry (points). Python server uses the same rules. */
function computeSheet(o,sizes,first){
  const P=PAPER[o.paper], fl=first.w>first.h;
  if(o.nup==1){
    const z=sizes[0], pl=z.w>z.h;
    const land=o.orient=="auto"?pl:o.orient=="landscape";
    const sw=land?P[1]:P[0], sh=land?P[0]:P[1];
    const rot=pl!=land, cw=rot?z.h:z.w, ch=rot?z.w:z.h;
    const fitS=Math.min(sw/cw,sh/ch);
    const s=o.scale=="custom"?(o.pct/100):(o.scale=="noscale"?1:(o.scale=="fit"?fitS:Math.min(1,fitS)));
    return {sw:sw,sh:sh,items:[{i:0,rot:rot,w:z.w*s,h:z.h*s,cx:sw/2,cy:sh/2}]};
  }
  const land=o.orient=="auto"?((o.nup==2||o.nup==6)?!fl:fl):o.orient=="landscape";
  const sw=land?P[1]:P[0], sh=land?P[0]:P[1];
  let cols=GRID[o.nup][0], rows=GRID[o.nup][1];
  if(land){const t=cols;cols=rows;rows=t}
  const m=18, cw=(sw-2*m)/cols, ch=(sh-2*m)/rows, items=[];
  sizes.forEach((z,k)=>{
    const s=Math.min(cw/z.w,ch/z.h), c=k%cols, r=Math.floor(k/cols);
    items.push({i:k,rot:false,w:z.w*s,h:z.h*s,cx:m+c*cw+cw/2,cy:m+r*ch+ch/2});
  });
  return {sw:sw,sh:sh,items:items};
}

async function getSize(p){
  if(sizeCache[p]) return sizeCache[p];
  let z;
  if(pdfDoc){const pg=await pdfDoc.getPage(p), v=pg.getViewport({scale:1}); z={w:v.width,h:v.height}}
  else{ // photo: server makes an A4-fit PDF page
    const iw=imgDim.w, ih=imgDim.h, s=iw>ih?Math.min(842/iw,595/ih):Math.min(595/iw,842/ih);
    z={w:iw*s,h:ih*s};
  }
  sizeCache[p]=z; return z;
}

async function getSrc(p){
  if(srcCache[p]) return srcCache[p];
  let u;
  if(pdfDoc){
    const pg=await pdfDoc.getPage(p), v0=pg.getViewport({scale:1}), v=pg.getViewport({scale:700/v0.width});
    const cv=document.createElement("canvas"); cv.width=v.width; cv.height=v.height;
    await pg.render({canvasContext:cv.getContext("2d"),viewport:v}).promise;
    u=cv.toDataURL("image/jpeg",0.85);
  }else u=imgURL;
  srcCache[p]=u; return u;
}

function updateSummary(count,total){
  if(!count){$("sumtop").textContent="";$("subtop").textContent="";return}
  const copies=Math.max(1,Math.min(50,parseInt($("copies").value)||1));
  const duplex=$("sides").value!="single";
  const papers=(duplex?Math.ceil(total/2):total)*copies;
  $("sumtop").textContent=papers+" sheet"+(papers>1?"s":"")+" of paper";
  $("subtop").textContent=count+" page"+(count>1?"s":"")+" × "+copies+" cop"+(copies>1?"ies":"y");
}

async function showSheet(){
  const tok=++renderTok, box=$("sheet"), sel=getSel();
  $("pmsg").textContent="";
  if(sel.err){$("rerr").textContent=sel.err;box.style.display="none";updateSummary(0);$("navtxt").textContent="";return}
  $("rerr").textContent="";
  if(sel.unknown||!sel.list.length){
    box.style.display="none"; updateSummary(0); $("navtxt").textContent="";
    $("pmsg").textContent="Preview not available, but printing will still work."; return;
  }
  const nup=+$("nup").value, total=Math.ceil(sel.list.length/nup);
  if(cur>=total) cur=total-1; if(cur<0) cur=0;
  updateSummary(sel.list.length,total);
  const chunk=sel.list.slice(cur*nup,cur*nup+nup);
  try{
    const sizes=await Promise.all(chunk.map(getSize)), first=await getSize(sel.list[0]), srcs=await Promise.all(chunk.map(getSrc));
    if(tok!=renderTok) return;
    const L=computeSheet({nup:nup,orient:$("orient").value,scale:$("scale").value,paper:$("paper").value,pct:Math.max(10,Math.min(400,parseInt($("pct").value)||100))},sizes,first);
    const avail=Math.max(180,Math.min($("sheetwrap").clientWidth-8,460));
    const k=Math.min(avail/L.sw,520/L.sh);
    box.style.display="block"; box.style.width=(L.sw*k)+"px"; box.style.height=(L.sh*k)+"px"; box.innerHTML="";
    const gray=$("color").value=="0";
    L.items.forEach(it=>{
      const im=new Image(); im.src=srcs[it.i];
      const w=it.w*k, h=it.h*k;
      im.style.cssText="position:absolute;width:"+w+"px;height:"+h+"px;left:"+(it.cx*k-w/2)+"px;top:"+(it.cy*k-h/2)+"px;"+(it.rot?"transform:rotate(90deg);":"")+(gray?"filter:grayscale(1);":"");
      box.appendChild(im);
    });
    $("navtxt").textContent="Sheet "+(cur+1)+" of "+total;
    $("prev").disabled=cur<=0; $("next").disabled=cur>=total-1;
  }catch(e){box.style.display="none";$("pmsg").textContent="Preview not available, but printing will still work."}
}

function refresh(){
  $("rangerow").style.display=$("pmode").value=="custom"?"grid":"none";
  $("scale").disabled=$("nup").value!="1";
  $("pctrow").style.display=($("scale").value=="custom"&&$("nup").value=="1")?"grid":"none";
  showSheet();
}
$("prev").onclick=()=>{cur--;showSheet()};
$("next").onclick=()=>{cur++;showSheet()};
let rt=null; window.addEventListener("resize",()=>{clearTimeout(rt);rt=setTimeout(showSheet,200)});

function resetPreview(){
  if(pdfDoc){pdfDoc.destroy();pdfDoc=null}
  if(imgURL){URL.revokeObjectURL(imgURL);imgURL=null}
  for(const k in sizeCache) delete sizeCache[k];
  for(const k in srcCache) delete srcCache[k];
  nPages=0; cur=0; $("sheet").innerHTML="";
}
function fmtSize(n){return n>1048576?(n/1048576).toFixed(1)+" MB":Math.max(1,Math.round(n/1024))+" KB"}
function cancelFile(){
  $("file").value=""; workFile=null; resetPreview();
  $("dlg").hidden=true; $("drop").hidden=false; $("fileinfo").hidden=true;
}

let pdfjsP=null;
function loadPdfJs(){
  if(window.pdfjsLib) return Promise.resolve();
  if(!pdfjsP){
    pdfjsP=new Promise(function(ok,bad){
      const s=document.createElement("script");
      s.src="https://cdnjs.cloudflare.com/ajax/libs/pdf.js/3.11.174/pdf.min.js";
      s.onload=function(){pdfjsLib.GlobalWorkerOptions.workerSrc="https://cdnjs.cloudflare.com/ajax/libs/pdf.js/3.11.174/pdf.worker.min.js";ok()};
      s.onerror=function(){pdfjsP=null;bad(new Error("pdfjs"))};
      document.head.appendChild(s);
    });
  }
  return pdfjsP;
}

async function shrinkImage(f,img){
  const m=Math.max(img.naturalWidth,img.naturalHeight), sc=Math.min(1,3508/m);
  if(sc>=1&&f.size<3000000) return f;
  const cv=document.createElement("canvas");
  cv.width=Math.round(img.naturalWidth*sc); cv.height=Math.round(img.naturalHeight*sc);
  const cx=cv.getContext("2d"); cx.fillStyle="#fff"; cx.fillRect(0,0,cv.width,cv.height);
  cx.drawImage(img,0,0,cv.width,cv.height);
  const blob=await new Promise(r=>cv.toBlob(r,"image/jpeg",0.9));
  if(!blob||blob.size>=f.size) return f;
  return mkFile([blob],f.name.replace(/\.[^.]+$/,"")+".jpg","image/jpeg");
}

async function loadFile(){
  let f=$("file").files[0]; resetPreview(); workFile=null;
  if(!f){cancelFile();return}
  $("drop").hidden=true; $("fileinfo").hidden=false;
  $("fname").textContent=f.name; $("fsize").textContent=fmtSize(f.size);
  $("dlg").hidden=false; $("pmsg").textContent="Loading preview...";
  const ext=f.name.split(".").pop().toLowerCase();
  if(CONVERT.has(ext)){
    $("pmsg").textContent="Uploading...";
    $("btn").disabled=true;
    const fd0=new FormData(); fd0.append("file",f,f.name);
    const r0=await postRetry(function(){return xhrPost("/api/convert",fd0,{blob:true,onProg:function(p){$("pmsg").textContent=p<100?"Uploading... "+p+"%":"Converting to PDF... this can take 10-30 seconds"}})},function(t){$("pmsg").textContent=t});
    $("btn").disabled=false;
    if(r0.status!=200){show(errText(r0),"err"); if(r0.status==401) setTimeout(function(){location.href="/login"},1500); cancelFile(); return}
    f=mkFile([r0.blob],f.name+".pdf","application/pdf"); workFile=f;
  }
  try{
    if(f.name.toLowerCase().endsWith(".pdf")||f.type=="application/pdf"){
      await loadPdfJs();
      const buf=await f.arrayBuffer();
      pdfDoc=await pdfjsLib.getDocument({data:buf}).promise; nPages=pdfDoc.numPages;
    }else{
      const img=new Image(); imgURL=URL.createObjectURL(f);
      await new Promise((ok,bad)=>{img.onload=ok;img.onerror=bad;img.src=imgURL});
      imgDim={w:img.naturalWidth,h:img.naturalHeight}; nPages=1;
      const small=await shrinkImage(f,img);
      if(small!==f){workFile=small; URL.revokeObjectURL(imgURL); imgURL=URL.createObjectURL(small)}
    }
  }catch(e){nPages=0}
  refresh();
}
$("file").addEventListener("change",loadFile);
const drop=$("drop");
["dragenter","dragover"].forEach(ev=>drop.addEventListener(ev,e=>{e.preventDefault();drop.classList.add("over")}));
["dragleave","drop"].forEach(ev=>drop.addEventListener(ev,e=>{e.preventDefault();drop.classList.remove("over")}));
drop.addEventListener("drop",e=>{if(e.dataTransfer&&e.dataTransfer.files.length){$("file").files=e.dataTransfer.files;loadFile()}});

/* ---- printers + jobs (ek hi request, 5 second mein) ---- */
let selPrinter=null, printers=[], pollFails=0;

/* ---- network helpers: progress, auto-retry, clear error messages ---- */
function mkFile(parts,name,type){
  try{return new File(parts,name,{type:type})}
  catch(e){var b=new Blob(parts,{type:type}); b.name=name; return b}
}
function xhrPost(url,fd,o){
  o=o||{};
  return new Promise(function(resolve){
    var x=new XMLHttpRequest(); x.open("POST",url); x.timeout=180000;
    if(o.blob) x.responseType="blob";
    x.upload.onprogress=function(e){if(e.lengthComputable&&o.onProg)o.onProg(Math.round(e.loaded*100/e.total))};
    x.onload=function(){
      if(o.blob&&x.status==200){resolve({status:200,blob:x.response});return}
      if(o.blob){
        var fr=new FileReader();
        fr.onload=function(){var j=null;try{j=JSON.parse(fr.result)}catch(e){}resolve({status:x.status,json:j})};
        fr.onerror=function(){resolve({status:x.status})};
        fr.readAsText(x.response||new Blob([])); return;
      }
      var j=null; try{j=JSON.parse(x.responseText)}catch(e){}
      resolve({status:x.status,json:j});
    };
    x.onerror=function(){resolve({status:0})};
    x.ontimeout=function(){resolve({status:-1})};
    x.send(fd);
  });
}
const RETRY_STATUS=[0,502,503,504];
async function postRetry(makeReq,onState){
  var r;
  for(var i=0;i<4;i++){
    r=await makeReq();
    if(RETRY_STATUS.indexOf(r.status)<0) return r;
    if(i<3){
      onState("Server is waking up or the connection dropped. Retrying ("+(i+1)+"/3)...");
      await new Promise(function(ok){setTimeout(ok,3000*(i+1))});
    }
  }
  return r;
}
function errText(r){
  if(r.json&&r.json.error) return r.json.error;
  if(r.status==0) return "Could not connect to the server. Check your Wi-Fi or mobile data and try again.";
  if(r.status==-1) return "Upload took too long. Check your signal or try a smaller file.";
  if(r.status==413) return "File is too large (max 25 MB).";
  if(r.status==401) return "Your session expired. Please sign in again.";
  if(r.status>=500) return "The server is busy or starting up. Please try again in a minute.";
  return "Something went wrong (code "+r.status+").";
}
const PRINTER_SVG='<svg width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polyline points="6 9 6 2 18 2 18 9"/><path d="M6 18H4a2 2 0 0 1-2-2v-5a2 2 0 0 1 2-2h16a2 2 0 0 1 2 2v5a2 2 0 0 1-2 2h-2"/><rect x="6" y="14" width="12" height="8"/></svg>';
function renderPrinters(){
  const box=$("ptiles"); box.innerHTML="";
  if(!printers.length){
    const d=document.createElement("div"); d.className="empty";
    d.textContent="No printer found yet. Make sure the print agent is running on the office PC.";
    box.appendChild(d); return;
  }
  if(!printers.some(p=>p.id==selPrinter)){const f=printers.find(p=>p.online)||printers[0]; selPrinter=f.id}
  printers.slice().sort((a,b)=>(b.online-a.online)||a.name.localeCompare(b.name)).forEach(p=>{
    const b=document.createElement("button"); b.type="button"; b.className="ptile"+(p.id==selPrinter?" sel":"");
    const ic=document.createElement("span"); ic.className="pi"; ic.innerHTML=PRINTER_SVG;
    const t=document.createElement("span"); t.className="pt";
    const n=document.createElement("span"); n.className="pn"; n.textContent=p.name;
    const s=document.createElement("span"); s.className="ps"; s.textContent=p.agent;
    const bd=document.createElement("span"); bd.className="badge "+(!p.online?"bad":(p.queue>0?"warn":"ok"));
    const dt=document.createElement("i"); dt.className="dot"; bd.appendChild(dt);
    bd.appendChild(document.createTextNode(!p.online?"Offline":(p.queue>0?"Busy · "+p.queue+" in queue":"Ready")));
    t.appendChild(n); t.appendChild(s); t.appendChild(bd);
    b.appendChild(ic); b.appendChild(t);
    b.onclick=function(){selPrinter=p.id;renderPrinters()};
    box.appendChild(b);
  });
}
const STAT={waiting:["warn","Waiting"],printing:["blue","Printing"],done:["ok","Done"],error:["bad","Failed"]};
function renderJobs(jobs){
  const box=$("jobs"); box.innerHTML="";
  if(!jobs.length){const d=document.createElement("div");d.className="empty";d.textContent="No jobs yet. Your prints will show up here.";box.appendChild(d);return}
  jobs.forEach(j=>{
    const row=document.createElement("div"); row.className="job";
    const ic=document.createElement("span"); ic.className="jf";
    ic.innerHTML='<svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><polyline points="14 2 14 8 20 8"/></svg>';
    const m=document.createElement("div"); m.className="jm";
    const n=document.createElement("div"); n.className="jn"; n.textContent=j.file;
    const s=document.createElement("div"); s.className="js";
    s.textContent=j.printer+(j.status=="waiting"&&j.ahead>0?" · "+j.ahead+" ahead of you":"")+(j.status=="error"&&j.error?" · "+j.error:"");
    m.appendChild(n); m.appendChild(s);
    const st=STAT[j.status]||["gray",j.status];
    const bd=document.createElement("span"); bd.className="badge "+st[0]; bd.textContent=st[1];
    row.appendChild(ic); row.appendChild(m); row.appendChild(bd); box.appendChild(row);
  });
}
async function poll(){
  if(document.hidden) return;
  try{
    const r=await fetch("/api/state");
    if(r.status==401){location.href="/login";return}
    const d=await r.json(); printers=d.printers; renderPrinters(); renderJobs(d.jobs);
    pollFails=0; $("net").hidden=true;
  }catch(e){pollFails++; if(pollFails>=2) $("net").hidden=false}
}
setInterval(poll,5000);
document.addEventListener("visibilitychange",function(){if(!document.hidden)poll()});
poll();

async function send(){
  const f=workFile||$("file").files[0]; if(!f){show("Choose a file first.","err");return}
  if(!selPrinter){show("Choose a printer first.","err");return}
  const sel=getSel(); if(sel.err){show(sel.err,"err");return}
  const fd=new FormData(); fd.append("file",f,f.name); fd.append("printer",selPrinter);
  fd.append("copies",$("copies").value); fd.append("color",$("color").value);
  fd.append("paper",$("paper").value); fd.append("orient",$("orient").value);
  fd.append("scale",$("scale").value); fd.append("scale_pct",$("pct").value);
  fd.append("sides",$("sides").value);
  fd.append("nup",$("nup").value); fd.append("mode",$("pmode").value);
  fd.append("range",$("pmode").value=="custom"?$("range").value:"");
  fd.append("req_id",Date.now().toString(36)+Math.random().toString(36).slice(2,10));  // retry par duplicate print na ho
  $("btn").disabled=true; $("btn").textContent="Uploading...";
  const r=await postRetry(function(){return xhrPost("/api/print",fd,{onProg:function(p){$("btn").textContent=p<100?"Uploading "+p+"%":"Sending to printer..."}})},function(t){show(t,"")});
  if(r.status==200&&r.json){
    show(r.json.message,r.json.warn?"":"ok"); cancelFile(); $("range").value=""; $("pmode").value="all";
  }else{
    show(errText(r),"err");
    if(r.status==401) setTimeout(function(){location.href="/login"},1500);
  }
  $("btn").disabled=false; $("btn").textContent="Print"; poll();
}
</script>
</body></html>"""

ADMIN_HTML = r"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><meta name="theme-color" content="#1d4ed8"><title>Admin · Office Print</title>""" + APP_CSS + r"""</head><body>""" + HEADER_HTML + r"""
<main class="wrap">
 {% if flash %}<div class="msg ok" style="display:block">{{ flash }}</div>{% endif %}
 <div class="note {{ 'ok' if permanent else 'warn' }}">{{ '✅ Permanent database: users and history are safe.' if permanent else '⚠️ Temporary storage: users can disappear when Render restarts. Set DATABASE_URL.' }}</div>
 <div class="tiles">
  <div class="tile"><b>{{ n_users }}</b><span>Users</span></div>
  <div class="tile"><b>{{ n_jobs }}</b><span>Jobs printed</span></div>
  <div class="tile"><b>{{ n_pages }}</b><span>Pages printed</span></div>
  <div class="tile"><b>{{ n_online }}/{{ printers|length }}</b><span>Printers online</span></div>
 </div>
 <section class="card"><h2>Queue controls</h2>
  <form method="post" action="/admin/clear-queue" onsubmit="return confirm('Clear all pending jobs?')"><button class="btn red">Clear all pending jobs</button></form>
  <p class="hint">Only waiting jobs are cleared. Jobs already printing are not automatically retried or interrupted.</p>
 </section>
 <div class="two">
  <section class="card"><h2>Add user / reset password</h2>
   <form class="addf" method="post" action="/admin/add">
    <input name="u" placeholder="Username" autocapitalize="none" autocomplete="off" required>
    <input name="p" placeholder="Password (min 4 chars)" autocomplete="off" required>
    <button class="btn pri">Save</button>
   </form>
   <p class="hint">Type an existing username to change that user's password.</p>
  </section>
  <section class="card"><h2>Printers</h2>
   {% for p in printers %}<div class="job"><div class="jm"><div class="jn">{{ p.name }}</div><div class="js">{{ p.agent }}</div></div>
    <span class="badge {{ 'ok' if p.online else 'bad' }}"><i class="dot"></i>{{ 'Online' if p.online else 'Offline' }}</span>
    <form method="post" action="/admin/printer-remove" onsubmit="return confirm('Remove this printer?')"><input type="hidden" name="id" value="{{ p.id }}"><button class="btn red sm">Remove</button></form></div>
   {% else %}<div class="empty">No agent has connected yet.</div>{% endfor %}
  </section>
 </div>
 <section class="card"><h2>Users</h2>
  {% for u in users %}<div class="job"><span class="av">{{ u.username[:1] }}</span>
   <div class="jm"><div class="jn">{{ u.username }} {% if u.is_admin %}<span class="badge blue">Admin</span>{% endif %}</div>
    <div class="js">{{ u.jobs }} jobs · {{ u.pages }} pages</div></div>
   {% if not u.is_admin %}<form method="post" action="/admin/delete/{{ u.username }}" data-u="{{ u.username }}" onsubmit="return askDel(this)"><button class="btn red sm">Delete</button></form>{% endif %}
  </div>{% endfor %}
 </section>
 <section class="card"><h2>Recent jobs</h2>
  {% for j in recent %}<div class="job"><div class="jm"><div class="jn">{{ j.filename }}</div><div class="js">{{ j.owner }}</div></div>
   <span class="badge {{ {'done':'ok','error':'bad','printing':'blue','waiting':'warn'}.get(j.status,'gray') }}">{{ j.status }}</span></div>
  {% else %}<div class="empty">No jobs yet.</div>{% endfor %}
 </section>
</main>""" + MODAL_HTML + r"""
<script>
function askDel(f){confirmBox("Delete user?","\""+f.dataset.u+"\" will no longer be able to sign in.","Delete",function(){f.submit()});return false}
</script></body></html>"""

# ---------------- user pages ----------------
@app.route("/login", methods=["GET", "POST"])
def login():
    err = None
    if request.method == "POST":
        ip = client_ip()
        now = time.time()
        recent = [t for t in fails.get(ip, []) if now - t < 600]
        fails[ip] = recent
        if len(recent) >= 10:
            err = "Bahut zyada galat koshish. 10 minute baad try karo."
        else:
            u = request.form["u"].strip().lower()
            row = run("SELECT * FROM users WHERE username=?", (u,), one=True)
            if row and check_password_hash(row["pw"], request.form["p"]):
                session.permanent = True
                session["user"] = u
                return redirect("/")
            fails[ip].append(now)
            err = "Username ya password galat hai."
    return render_template_string(LOGIN_HTML, err=err)

@app.route("/logout")
def logout():
    _ucache.pop(session.get("user"), None)
    session.clear()
    return redirect("/login")

@app.route("/")
def index():
    u = current_user()
    if not u:
        return redirect("/login")
    return render_template_string(MAIN_HTML, user=u["username"], admin=bool(u["is_admin"]), page="main")

@app.route("/api/state")
def api_state():
    u = current_user()
    if not u:
        return jsonify({"error": "login"}), 401
    q = queue_counts()
    now = time.time()
    ps = sorted(PRINTERS.values(), key=lambda p: (p["name"].lower(), p["agent"]))
    printers = [{"id": p["id"], "name": p["name"], "agent": p["agent"],
                 "online": now - p["last_seen"] < ONLINE_SECONDS, "queue": q.get(p["id"], 0)} for p in ps]
    rows = run("""SELECT j.id, j.filename, j.pages, j.status, j.error, j.printer_id,
                  (SELECT COUNT(*) FROM jobs k WHERE k.printer_id=j.printer_id
                     AND k.status IN ('waiting','printing') AND k.created<j.created) AS ahead
                  FROM jobs j WHERE j.owner=? ORDER BY j.created DESC LIMIT 8""", (u["username"],))
    jobs = [{"id": r["id"], "file": r["filename"], "pages": int(r["pages"] or 0),
             "printer": (PRINTERS.get(r["printer_id"]) or {}).get("name", "?"),
             "status": r["status"], "error": r["error"], "ahead": int(r["ahead"])} for r in rows]
    return jsonify({"printers": printers, "jobs": jobs})

@app.route("/api/convert", methods=["POST"])
def api_convert():
    if not current_user():
        return jsonify({"error": "Pehle login karo."}), 401
    f = request.files.get("file")
    ext = os.path.splitext((f.filename if f else "") or "")[1].lower()
    if not f or ext not in CONVERT_EXT:
        return jsonify({"error": "Ye file type convert nahi ho sakta."}), 400
    if ext in OFFICE_EXT and not SOFFICE:
        return jsonify({"error": NO_OFFICE_MSG}), 501
    jid = uuid.uuid4().hex[:10]
    src = os.path.join(UPLOAD_DIR, jid + ext)
    out = os.path.join(UPLOAD_DIR, jid + "_conv.pdf")
    f.save(src)
    try:
        to_pdf(src, out, ext)
        with open(out, "rb") as fh:
            data = fh.read()
    except subprocess.TimeoutExpired:
        return jsonify({"error": "File bahut badi hai, conversion mein zyada time laga. PDF bana ke bhejo."}), 400
    except Exception:
        return jsonify({"error": "Is file ko PDF mein nahi badal paya. File kharab ho sakti hai, ya PDF bana ke bhejo."}), 400
    finally:
        for p in (src, out):
            if os.path.exists(p):
                os.remove(p)
    return Response(data, mimetype="application/pdf")

REQS = {}      # (user, req_id) -> (expiry, response) : retry par dobara print na ho

@app.errorhandler(413)
def too_big(e):
    return jsonify({"error": "File bahut badi hai (max 25 MB). PDF chhoti karke bhejo."}), 413

@app.errorhandler(500)
def server_error(e):
    if request.path.startswith("/api/"):
        return jsonify({"error": "Server mein dikkat aayi, thodi der baad dobara try karo."}), 500
    return "Server error", 500

@app.route("/api/print", methods=["POST"])
def api_print():
    u = current_user()
    if not u:
        return jsonify({"error": "Pehle login karo."}), 401
    rid = (request.form.get("req_id") or "")[:40]
    key = (u["username"], rid) if rid else None
    now = time.time()
    if key:
        with state_lock:
            if len(REQS) > 500:
                for k in [k for k, v in REQS.items() if v[0] < now]:
                    REQS.pop(k, None)
            hit = REQS.get(key)
            if hit and hit[0] > now:
                return jsonify(hit[1] or {"message": "Job mil gayi, process ho rahi hai ✅"})
            REQS[key] = (now + 600, None)
    out = _do_print(u)
    if key:
        resp = out[0] if isinstance(out, tuple) else out
        code = out[1] if isinstance(out, tuple) else resp.status_code
        with state_lock:
            if code == 200:
                REQS[key] = (now + 600, resp.get_json())
            else:
                REQS.pop(key, None)
    return out

def _do_print(u):
    f = request.files.get("file")
    pid = request.form.get("printer", "")
    printer = PRINTERS.get(pid)
    if not f or not printer:
        return jsonify({"error": "File ya printer sahi nahi hai."}), 400
    ext = os.path.splitext(f.filename or "")[1].lower()
    if ext not in ALLOWED:
        return jsonify({"error": "Sirf PDF, JPG, PNG chalega."}), 400
    try:
        copies = max(1, min(50, int(request.form.get("copies", 1))))
        nup = int(request.form.get("nup", 1))
    except ValueError:
        return jsonify({"error": "Print settings sahi nahi hain."}), 400
    color = request.form.get("color") == "1"
    paper = request.form.get("paper", "A4")
    orient = request.form.get("orient", "auto")
    scale = request.form.get("scale", "shrink")
    sides = request.form.get("sides", "single")
    mode = request.form.get("mode", "all")
    try:
        pct = int(request.form.get("scale_pct", 100))
    except ValueError:
        pct = 100
    if scale == "custom" and nup == 1 and not (10 <= pct <= 400):
        return jsonify({"error": "Custom scale 10% se 400% ke beech rakho."}), 400
    if (paper not in PAPERS or orient not in ORIENTS or scale not in SCALES
            or sides not in SIDES or nup not in (1, 2, 4, 6, 9)):
        return jsonify({"error": "Print settings sahi nahi hain."}), 400

    jid = uuid.uuid4().hex[:10]
    path = os.path.join(UPLOAD_DIR, jid + ext)
    f.save(path)
    junk = [path]
    try:
        if ext != ".pdf":
            pdf = os.path.join(UPLOAD_DIR, jid + ".pdf")
            junk.append(pdf)
            to_pdf(path, pdf, ext)
            path = pdf
        total = count_pages(path)
        try:
            sel = select_pages(mode, request.form.get("range", ""), total)
        except ValueError:
            for j in junk:
                if os.path.exists(j):
                    os.remove(j)
            return jsonify({"error": f"Koi page select nahi hua ya pages galat hain. File mein {total} page hain. Aise likho: 1-3,5"}), 400
        out = os.path.join(UPLOAD_DIR, jid + "_out.pdf")
        junk.append(out)
        token = process_pdf(path, out, sel, orient, nup, paper, scale, pct)
        for j in junk[:-1]:
            if os.path.exists(j):
                os.remove(j)
        path = out
    except Exception as e:
        for j in junk:
            if os.path.exists(j):
                os.remove(j)
        if str(e) == "no_office":
            return jsonify({"error": NO_OFFICE_MSG}), 501
        return jsonify({"error": "File kharab hai ya khul nahi rahi."}), 400

    eff_scale = "shrink" if nup > 1 else ("noscale" if scale == "custom" else scale)
    settings = build_settings(copies, color, paper, token, eff_scale, sides)
    ahead = ahead_count(pid)
    run("""INSERT INTO jobs(id,owner,filename,printer_id,copies,color,pages,status,created,path,settings)
           VALUES(?,?,?,?,?,?,?,'waiting',?,?,?)""",
        (jid, u["username"], (f.filename or "file")[:60], pid, copies,
         1 if color else 0, len(sel), time.time(), path, settings), write=True)
    with state_lock:
        WAITING.add(pid)
        S["seq"] += 1
    invalidate_queue()

    online = time.time() - printer["last_seen"] < ONLINE_SECONDS
    if not online:
        return jsonify({"warn": True, "message": "Is printer wala PC abhi band/offline hai. Job queue mein hai, PC chalu hote hi print ho jayegi."})
    if ahead > 0:
        return jsonify({"warn": True, "message": f"Printer abhi busy hai. Aapse pehle {ahead} job hai, thoda wait karo. Baari aane par khud print ho jayega."})
    return jsonify({"message": "Job bhej di ✅ Print shuru hone wala hai."})

# ---------------- admin ----------------
def admin_required():
    u = current_user()
    return u if u and u["is_admin"] else None

@app.route("/admin")
def admin():
    u = admin_required()
    if not u:
        return redirect("/login")
    users = run("""SELECT u.username, u.is_admin,
                   (SELECT COUNT(*) FROM jobs j WHERE j.owner=u.username AND j.status='done') AS jobs,
                   (SELECT COALESCE(SUM(j.pages*j.copies),0) FROM jobs j WHERE j.owner=u.username AND j.status='done') AS pages
                   FROM users u ORDER BY u.username""")
    now = time.time()
    printers = [{"id": p["id"], "name": p["name"], "agent": p["agent"], "online": now - p["last_seen"] < ONLINE_SECONDS}
                for p in sorted(PRINTERS.values(), key=lambda p: (p["name"].lower(), p["agent"]))]
    recent = run("SELECT owner,filename,status FROM jobs ORDER BY created DESC LIMIT 20")
    return render_template_string(
        ADMIN_HTML, users=users, printers=printers, recent=recent, permanent=USE_PG,
        flash=request.args.get("m"), user=u["username"], admin=True, page="admin",
        n_users=len(users), n_jobs=sum(int(x["jobs"] or 0) for x in users),
        n_pages=sum(int(x["pages"] or 0) for x in users), n_online=sum(1 for p in printers if p["online"]))

@app.route("/admin/add", methods=["POST"])
def admin_add():
    if not admin_required():
        abort(403)
    u = request.form["u"].strip().lower()
    p = request.form["p"]
    if not u or len(p) < 4 or u == ADMIN_USER:
        return redirect("/admin?m=Invalid username or password (minimum 4 characters).")
    run("""INSERT INTO users(username,pw,is_admin,created) VALUES(?,?,0,?)
           ON CONFLICT(username) DO UPDATE SET pw=excluded.pw WHERE users.is_admin=0""",
        (u, hash_pw(p), time.time()), write=True)
    _ucache.clear()
    return redirect("/admin?m=Saved: " + u)

@app.route("/admin/delete/<username>", methods=["POST"])
def admin_delete(username):
    if not admin_required():
        abort(403)
    run("DELETE FROM users WHERE username=? AND is_admin=0", (username.lower(),), write=True)
    _ucache.clear()
    return redirect("/admin?m=User deleted.")

@app.route("/api/cancel/<jid>", methods=["POST"])
def api_cancel(jid):
    u = current_user()
    if not u:
        return jsonify({"error": "Please log in."}), 401
    job = run("SELECT id,owner,status,path FROM jobs WHERE id=?", (jid,), one=True)
    if not job or (job["owner"] != u["username"] and not u["is_admin"]):
        return jsonify({"error": "Job not found."}), 404
    if job["status"] != "waiting":
        return jsonify({"error": "Only waiting jobs can be cancelled; a printing job may already be at the printer."}), 409
    run("UPDATE jobs SET status='error', error=?, finished=? WHERE id=? AND status='waiting'",
        ("Cancelled by you", time.time(), jid), write=True)
    if job["path"] and os.path.exists(job["path"]):
        try: os.remove(job["path"])
        except OSError: pass
    invalidate_queue()
    return jsonify({"ok": True})

@app.route("/admin/clear-queue", methods=["POST"])
def admin_clear_queue():
    if not admin_required():
        abort(403)
    rows = run("SELECT id,path FROM jobs WHERE status='waiting'")
    now = time.time()
    run("UPDATE jobs SET status='error', error=?, finished=? WHERE status='waiting'",
        ("Cancelled by admin: queue cleared", now), write=True)
    for row in rows:
        if row["path"] and os.path.exists(row["path"]):
            try: os.remove(row["path"])
            except OSError: pass
    with state_lock:
        WAITING.clear()
    invalidate_queue()
    return redirect("/admin?m=Cleared " + str(len(rows)) + " pending job(s).")

@app.route("/admin/printer-remove", methods=["POST"])
def admin_printer_remove():
    if not admin_required():
        abort(403)
    pid = request.form.get("id", "")
    if not pid:
        return redirect("/admin?m=Printer ID missing.")
    # Fail active queued jobs rather than leaving them orphaned after removal.
    rows = run("SELECT id,path FROM jobs WHERE printer_id=? AND status='waiting'", (pid,))
    now = time.time()
    run("UPDATE jobs SET status='error', error=?, finished=? WHERE printer_id=? AND status='waiting'",
        ("Printer removed by admin", now, pid), write=True)
    run("DELETE FROM printers WHERE id=?", (pid,), write=True)
    for row in rows:
        if row["path"] and os.path.exists(row["path"]):
            try: os.remove(row["path"])
            except OSError: pass
    PRINTERS.pop(pid, None)
    with state_lock:
        WAITING.discard(pid)
    invalidate_queue()
    return redirect("/admin?m=Printer removed.")

# ---------------- agent API (office PC) ----------------
@app.route("/agent/heartbeat", methods=["POST"])
def agent_heartbeat():
    if not agent_ok():
        abort(403)
    d = request.get_json(force=True)
    agent = str(d.get("agent", "PC"))[:40]
    now = time.time()
    if d.get("boot"):
        active = run("SELECT id,path FROM jobs WHERE status='printing' AND printer_id LIKE ?", (agent + "|%",))
        run("UPDATE jobs SET status='error', error=?, finished=? WHERE status='printing' AND printer_id LIKE ?",
            ("The print agent restarted. Please check the printer and send again if needed.", now, agent + "|%"), write=True)
        for job in active:
            if job["path"] and os.path.exists(job["path"]):
                try: os.remove(job["path"])
                except OSError: pass
        invalidate_queue()
    for name in d.get("printers", []):
        pid = f"{agent}|{name}"
        p = PRINTERS.get(pid)
        if p is None:
            p = PRINTERS[pid] = {"id": pid, "agent": agent, "name": str(name), "last_seen": now, "saved": 0}
        p["last_seen"] = now
        if now - p["saved"] > 300:          # DB mein 5 minute mein ek baar hi likho
            run("""INSERT INTO printers(id,label,last_seen) VALUES(?,?,?)
                   ON CONFLICT(id) DO UPDATE SET last_seen=excluded.last_seen""",
                (pid, f"{name} ({agent})", now), write=True)
            p["saved"] = now
    return jsonify({"ok": True})

@app.route("/agent/claim", methods=["POST"])
def agent_claim():
    if not agent_ok():
        abort(403)
    d = request.get_json(force=True)
    pid = f"{d.get('agent')}|{d.get('printer')}"
    try:
        wait = max(0.0, min(float(d.get("wait") or 0), 20.0))
    except (TypeError, ValueError):
        wait = 0.0
    maintenance()
    p = PRINTERS.get(pid)
    if p:
        p["last_seen"] = time.time()
    deadline = time.time() + wait
    while pid not in WAITING:               # long-poll: job aate hi turant mil jaye
        if time.time() >= deadline:
            return jsonify({"job": None})
        time.sleep(0.25)
    with state_lock:
        seq0 = S["seq"]
    now = time.time()
    c = db()
    try:
        if USE_PG:
            cur = _cursor(c)
            cur.execute("""UPDATE jobs SET status='printing', claimed=%s
                           WHERE id=(SELECT id FROM jobs WHERE printer_id=%s AND status='waiting'
                                     ORDER BY created LIMIT 1 FOR UPDATE SKIP LOCKED)
                           RETURNING id, pages, copies, color, settings, path""", (now, pid))
            job = cur.fetchone()
            c.commit()
        else:
            c.execute("BEGIN IMMEDIATE")
            job = c.execute("""SELECT * FROM jobs WHERE printer_id=? AND status='waiting'
                               ORDER BY created LIMIT 1""", (pid,)).fetchone()
            if job:
                c.execute("UPDATE jobs SET status='printing', claimed=? WHERE id=?", (now, job["id"]))
            c.commit()
    except Exception:
        _discard(c)
        raise
    else:
        release(c)
    invalidate_queue()
    if not job:
        with state_lock:
            if S["seq"] == seq0:            # beech mein nayi job nahi aayi
                WAITING.discard(pid)
        return jsonify({"job": None})
    if not job["path"] or not os.path.exists(job["path"]):     # server restart mein file ud gayi
        run("UPDATE jobs SET status='error', error=?, finished=? WHERE id=?",
            ("File server restart mein hat gayi, dobara bhejo", time.time(), job["id"]), write=True)
        return jsonify({"job": None})
    return jsonify({"job": {"id": job["id"], "pages": int(job["pages"] or 0),
                            "copies": job["copies"], "color": bool(job["color"]),
                            "settings": job["settings"]}})

@app.route("/agent/file/<jid>")
def agent_file(jid):
    if not agent_ok():
        abort(403)
    job = run("SELECT path FROM jobs WHERE id=?", (jid,), one=True)
    if not job or not os.path.exists(job["path"]):
        abort(404)
    return send_file(job["path"], mimetype="application/pdf")

@app.route("/agent/done/<jid>", methods=["POST"])
def agent_done(jid):
    if not agent_ok():
        abort(403)
    d = request.get_json(force=True)
    job = run("SELECT path FROM jobs WHERE id=?", (jid,), one=True)
    run("UPDATE jobs SET status=?, error=?, finished=? WHERE id=?",
        ("done" if d.get("ok") else "error", str(d.get("error", ""))[:120], time.time(), jid), write=True)
    if job and os.path.exists(job["path"]):
        os.remove(job["path"])          # privacy: print ke baad file hata do
    invalidate_queue()
    return jsonify({"ok": True})

@app.route("/healthz")
def healthz():
    return "ok"
