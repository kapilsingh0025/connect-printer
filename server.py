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
STUCK_SECONDS = 600       # printing mein itni der atka job wapas queue mein

app = Flask(__name__)
app.secret_key = SECRET_KEY
app.permanent_session_lifetime = timedelta(days=90)     # ek baar login, 90 din yaad
app.config.update(MAX_CONTENT_LENGTH=25 * 1024 * 1024,
                  SESSION_COOKIE_HTTPONLY=True,
                  SESSION_COOKIE_SAMESITE="Lax",
                  SESSION_COOKIE_SECURE=os.environ.get("INSECURE_COOKIES") != "1")

# ---------------- database ----------------
# DATABASE_URL set ho to Postgres (permanent, users kabhi nahi mitenge). Warna SQLite (Render free par restart par mit jata hai).
DATABASE_URL = os.environ.get("DATABASE_URL", "").strip()
USE_PG = DATABASE_URL.startswith(("postgres://", "postgresql://"))
if USE_PG:
    import psycopg2
    import psycopg2.extras
    if DATABASE_URL.startswith("postgres://"):
        DATABASE_URL = "postgresql://" + DATABASE_URL[len("postgres://"):]

def db():
    if USE_PG:
        return psycopg2.connect(DATABASE_URL, connect_timeout=15)
    c = sqlite3.connect(DB_PATH, timeout=15)
    c.row_factory = sqlite3.Row
    return c

def _cursor(c):
    return c.cursor(cursor_factory=psycopg2.extras.RealDictCursor) if USE_PG else c.cursor()

def run(sql, args=(), one=False, write=False):
    if USE_PG:
        sql = sql.replace("?", "%s")
    c = db()
    try:
        cur = _cursor(c)
        cur.execute(sql, args)
        if write:
            c.commit()
            return cur.rowcount
        rows = cur.fetchall()
        return (rows[0] if rows else None) if one else rows
    finally:
        c.close()

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
    # admin: har start par .env ke hisaab se
    run("""INSERT INTO users(username,pw,is_admin,created) VALUES(?,?,1,?)
           ON CONFLICT(username) DO UPDATE SET pw=excluded.pw, is_admin=1""",
        (ADMIN_USER, generate_password_hash(ADMIN_PASS), time.time()), write=True)
    # USERS env: sirf shuruaati users (pehle se ho to chhedta nahi)
    for pair in os.environ.get("USERS", "").split(","):
        if ":" in pair:
            u, p = pair.split(":", 1)
            u = u.strip().lower()
            if u and p:
                run("""INSERT INTO users(username,pw,is_admin,created) VALUES(?,?,0,?)
                       ON CONFLICT(username) DO NOTHING""",
                    (u, generate_password_hash(p), time.time()), write=True)

init()

# ---------------- helpers ----------------
fails = {}   # ip -> [timestamps]  (login brute-force se bachav)

def client_ip():
    return (request.headers.get("X-Forwarded-For", request.remote_addr or "").split(",")[0]).strip()

def current_user():
    u = session.get("user")
    if not u:
        return None
    return run("SELECT * FROM users WHERE username=?", (u,), one=True)

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
.brand{display:flex;align-items:center;gap:12px;font-weight:700;font-size:20px}
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

MAIN_HTML = r"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>Print</title>""" + STYLE + r"""
<style>
.top{max-width:980px}.page{max-width:980px;margin:0 auto}
.c.w{max-width:none}
.msg{margin:0 auto 14px}
.dlg{display:grid;gap:14px}
@media(min-width:820px){.dlg{grid-template-columns:1fr 360px;align-items:start}.prevbox{position:sticky;top:10px}}
.prevbox{background:#e5e7eb;text-align:center}
.sumtop{font-size:20px;font-weight:700;text-align:left;margin-bottom:2px}
.subtop{font-size:13px;color:#555;text-align:left;margin-bottom:12px}
.sheetwrap{display:flex;justify-content:center;min-height:120px}
.sheet{position:relative;background:#fff;box-shadow:0 2px 10px #0006;overflow:hidden;display:none}
.nav{display:flex;justify-content:center;gap:14px;align-items:center;margin-top:12px;font-size:14px}
.nav button{width:auto;margin:0;padding:6px 16px;background:#374151}
.pmsg{font-size:13px;color:#b45309;margin-top:8px}
.row{display:flex;align-items:center;justify-content:space-between;gap:10px;padding:9px 0;border-top:1px solid #eee}
.row:first-of-type{border-top:0}
.row label{margin:0;font-weight:600;flex:0 0 38%;font-size:15px}
.row select,.row input{margin:0;flex:1;padding:9px;font-size:15px;min-width:0}
.acts{display:flex;gap:10px;margin-top:14px;position:sticky;bottom:0;background:#fff;padding:10px 0}
.acts button{margin:0}.acts .gray{background:#e5e7eb;color:#111}
small{color:#666}
</style>
<script src="https://cdnjs.cloudflare.com/ajax/libs/pdf.js/3.11.174/pdf.min.js"></script>
</head><body><div class="page">
<div class="top"><b>👤 {{user}}</b><span>{% if admin %}<a href="/admin">Admin</a> · {% endif %}<a href="/logout">Logout</a></span></div>
<div class="msg" id="msg"></div>
<div class="c w"><h2>🖨️ Print</h2>
<label>File (PDF, Word, Excel, PowerPoint, Text, Photo)</label><input type="file" id="file" accept=".pdf,.jpg,.jpeg,.png,.gif,.bmp,.webp,.tif,.tiff,.doc,.docx,.odt,.rtf,.txt,.xls,.xlsx,.ods,.csv,.ppt,.pptx,.odp,image/*,application/pdf"></div>

<div class="dlg" id="dlg" style="display:none">
 <div class="c w prevbox">
  <div class="sumtop" id="sumtop"></div><div class="subtop" id="subtop"></div>
  <div class="sheetwrap" id="sheetwrap"><div class="sheet" id="sheet"></div></div>
  <div class="nav"><button id="prev">◀</button><span id="navtxt"></span><button id="next">▶</button></div>
  <div class="pmsg" id="pmsg"></div>
 </div>
 <div class="c w">
  <div class="row"><label>Printer</label><select id="printer"></select></div>
  <div class="row"><label>Copies</label><input type="number" id="copies" value="1" min="1" max="50" oninput="refresh()"></div>
  <div class="row"><label>Pages</label><select id="pmode" onchange="cur=0;refresh()"><option value="all">All</option><option value="odd">Odd pages only</option><option value="even">Even pages only</option><option value="custom">Custom</option></select></div>
  <div class="row" id="rangerow" style="display:none"><label></label><input id="range" placeholder="e.g. 1-3,5" oninput="cur=0;refresh()"></div>
  <div class="row"><label>Layout</label><select id="orient" onchange="refresh()"><option value="auto">Auto</option><option value="portrait">Portrait</option><option value="landscape">Landscape</option></select></div>
  <div class="row"><label>Color</label><select id="color" onchange="refresh()"><option value="0">Black and white</option><option value="1">Color</option></select></div>
  <div class="row"><label>Paper size</label><select id="paper" onchange="refresh()"><option value="A4">A4</option><option value="A3">A3</option><option value="A5">A5</option><option value="letter">Letter</option><option value="legal">Legal</option></select></div>
  <div class="row"><label>Pages per sheet</label><select id="nup" onchange="cur=0;refresh()"><option value="1">1</option><option value="2">2</option><option value="4">4</option><option value="6">6</option><option value="9">9</option></select></div>
  <div class="row"><label>Scale</label><select id="scale" onchange="refresh()"><option value="shrink">Shrink to fit</option><option value="fit">Fit to page</option><option value="noscale">Actual size (100%)</option><option value="custom">Custom...</option></select></div>
  <div class="row" id="pctrow" style="display:none"><label>Custom scale</label><input type="number" id="pct" value="100" min="10" max="400" step="5" oninput="refresh()"><span style="font-weight:600">%</span></div>
  <div class="row"><label>Two-sided</label><select id="sides" onchange="refresh()"><option value="single">Off</option><option value="long">Flip on long edge</option><option value="short">Flip on short edge</option></select></div>
  <div class="rerr" id="rerr" style="color:#c00;font-size:14px"></div>
  <small>Preview is approximate. Two-sided works only if the printer supports it.</small>
  <div class="acts"><button id="btn" onclick="send()">Print</button><button class="gray" onclick="cancelFile()">Cancel</button></div>
 </div>
</div>

<div class="c w"><h3>My jobs</h3><div id="jobs">No jobs yet.</div></div>
</div>
<script>
const $=id=>document.getElementById(id);
const label={waiting:"⏳ Waiting",printing:"🖨️ Printing",done:"✅ Done",error:"❌ Error"};
const PAPER={A3:[842,1191],A4:[595,842],A5:[420,595],letter:[612,792],legal:[612,1008]};
const GRID={2:[1,2],4:[2,2],6:[2,3],9:[3,3]};
let pdfDoc=null,imgURL=null,imgDim=null,nPages=0,cur=0,renderTok=0,workFile=null;
const CONVERT=new Set(["doc","docx","odt","rtf","txt","xls","xlsx","ods","csv","ppt","pptx","odp","tif","tiff"]);
const sizeCache={},srcCache={};
if(window.pdfjsLib) pdfjsLib.GlobalWorkerOptions.workerSrc="https://cdnjs.cloudflare.com/ajax/libs/pdf.js/3.11.174/pdf.worker.min.js";

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
  $("rangerow").style.display=$("pmode").value=="custom"?"flex":"none";
  $("scale").disabled=$("nup").value!="1";
  $("pctrow").style.display=($("scale").value=="custom"&&$("nup").value=="1")?"flex":"none";
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
function cancelFile(){$("file").value="";workFile=null;resetPreview();$("dlg").style.display="none"}

async function loadFile(){
  let f=$("file").files[0]; resetPreview(); workFile=null;
  if(!f){$("dlg").style.display="none";return}
  $("dlg").style.display="grid"; $("pmsg").textContent="Loading preview...";
  const ext=f.name.split(".").pop().toLowerCase();
  if(CONVERT.has(ext)){
    $("pmsg").textContent="Converting to PDF... (10-30 seconds lag sakte hain)";
    $("btn").disabled=true;
    try{
      const fd0=new FormData(); fd0.append("file",f);
      const r0=await fetch("/api/convert",{method:"POST",body:fd0});
      if(!r0.ok){let m="Could not convert this file."; try{m=(await r0.json()).error||m}catch(e){} show(m,"err"); cancelFile(); $("btn").disabled=false; return}
      f=new File([await r0.blob()],f.name+".pdf",{type:"application/pdf"}); workFile=f;
    }catch(e){show("Conversion failed. Try again.","err"); cancelFile(); $("btn").disabled=false; return}
    $("btn").disabled=false;
  }
  try{
    if(f.name.toLowerCase().endsWith(".pdf")||f.type=="application/pdf"){
      const buf=await f.arrayBuffer();
      pdfDoc=await pdfjsLib.getDocument({data:buf}).promise; nPages=pdfDoc.numPages;
    }else{
      const img=new Image(); imgURL=URL.createObjectURL(f);
      await new Promise((ok,bad)=>{img.onload=ok;img.onerror=bad;img.src=imgURL});
      imgDim={w:img.naturalWidth,h:img.naturalHeight}; nPages=1;
    }
  }catch(e){nPages=0}
  refresh();
}
$("file").addEventListener("change",loadFile);

async function loadPrinters(){
  const r=await (await fetch("/api/printers")).json(), s=$("printer"), cur0=s.value;
  s.innerHTML="";
  r.forEach(p=>{const o=document.createElement("option");o.value=p.id;
    o.textContent=p.label+" — "+(!p.online?"PC offline ❌":(p.queue>0?"busy ("+p.queue+" ahead)":"ready ✅"));s.appendChild(o)});
  if(!r.length){const o=document.createElement("option");o.textContent="No printer online";s.appendChild(o)}
  if(cur0) s.value=cur0;
}

async function loadJobs(){
  const r=await (await fetch("/api/jobs")).json(); if(!r.length) return;
  const box=$("jobs"); box.innerHTML="";
  r.forEach(j=>{const d=document.createElement("div");d.className="job";
    const a=document.createElement("span");a.textContent=j.file+" ("+j.printer+")";
    const b=document.createElement("span");b.textContent=label[j.status]+(j.status=="waiting"?" ("+j.ahead+" ahead)":"")+(j.status=="error"?": "+(j.error||""):"");
    d.appendChild(a);d.appendChild(b);box.appendChild(d)});
}

async function send(){
  const f=workFile||$("file").files[0]; if(!f){show("Choose a file first.","err");return}
  const sel=getSel(); if(sel.err){show(sel.err,"err");return}
  const fd=new FormData(); fd.append("file",f); fd.append("printer",$("printer").value);
  fd.append("copies",$("copies").value); fd.append("color",$("color").value);
  fd.append("paper",$("paper").value); fd.append("orient",$("orient").value);
  fd.append("scale",$("scale").value); fd.append("scale_pct",$("pct").value); fd.append("sides",$("sides").value);
  fd.append("nup",$("nup").value); fd.append("mode",$("pmode").value);
  fd.append("range",$("pmode").value=="custom"?$("range").value:"");
  $("btn").disabled=true; show("Sending...");
  try{
    const res=await fetch("/api/print",{method:"POST",body:fd}), d=await res.json();
    if(res.ok){show(d.message,d.warn?"":"ok"); cancelFile(); $("range").value=""; $("pmode").value="all"}
    else show(d.error||"Error","err");
  }catch(e){show("Could not reach the server.","err")}
  $("btn").disabled=false; loadPrinters(); loadJobs();
}
loadPrinters(); loadJobs(); setInterval(()=>{loadPrinters();loadJobs()},4000);
</script></body></html>"""

ADMIN_HTML = """<!doctype html><html lang="hi"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>Admin</title>""" + STYLE + """</head><body>
<div class="top"><a href="/">← Print page</a><a href="/logout">Logout</a></div>
{% if flash %}<div class="c" style="background:#d1fae5">{{flash}}</div>{% endif %}
<div class="c" style="background:{{'#d1fae5' if permanent else '#fef3c7'}}"><small>{{ '✅ Permanent database: users aur history safe hain.' if permanent else '⚠️ Temporary storage: Render restart par users mit sakte hain. DATABASE_URL set karo.' }}</small></div>
<div class="c"><h3>Naya user / password badlo</h3>
<form method="post" action="/admin/add">
<input name="u" placeholder="Username" autocapitalize="none" required>
<input name="p" placeholder="Password (kam se kam 4 akshar)" required>
<button>Save</button></form></div>
<div class="c"><h3>Users</h3><table>
<tr><th>User</th><th>Jobs</th><th>Pages</th><th></th></tr>
{% for u in users %}<tr><td>{{u.username}}{% if u.is_admin %} (admin){% endif %}</td><td>{{u.jobs}}</td><td>{{u.pages}}</td>
<td>{% if not u.is_admin %}<form method="post" action="/admin/delete/{{u.username}}" onsubmit="return confirm('{{u.username}} hatana hai?')"><button class="red">Hatao</button></form>{% endif %}</td></tr>{% endfor %}
</table><small>Jobs/Pages sirf poori ho chuki prints ke hain.</small></div>
<div class="c"><h3>Printers</h3><table>
{% for p in printers %}<tr><td>{{p.label}}</td><td>{{"🟢 online" if p.online else "🔴 band"}}</td></tr>{% endfor %}
{% if not printers %}<tr><td>Abhi koi agent connect nahi hua.</td></tr>{% endif %}</table></div>
<div class="c"><h3>Haal ki jobs</h3><table>
{% for j in recent %}<tr><td>{{j.owner}}</td><td>{{j.filename}}</td><td>{{j.status}}</td></tr>{% endfor %}
</table></div></body></html>"""

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
    session.clear()
    return redirect("/login")

@app.route("/")
def index():
    u = current_user()
    if not u:
        return redirect("/login")
    return render_template_string(MAIN_HTML, user=u["username"], admin=bool(u["is_admin"]))

@app.route("/api/printers")
def api_printers():
    if not current_user():
        return jsonify([]), 401
    now = time.time()
    out = []
    for p in run("SELECT * FROM printers ORDER BY label"):
        out.append({"id": p["id"], "label": p["label"],
                    "online": now - p["last_seen"] < ONLINE_SECONDS,
                    "queue": ahead_count(p["id"])})
    return jsonify(out)

@app.route("/api/jobs")
def api_jobs():
    u = current_user()
    if not u:
        return jsonify([]), 401
    rows = run("""SELECT j.*, p.label FROM jobs j LEFT JOIN printers p ON p.id=j.printer_id
                  WHERE j.owner=? ORDER BY j.created DESC LIMIT 8""", (u["username"],))
    return jsonify([{"file": r["filename"], "printer": r["label"] or "?", "status": r["status"],
                     "error": r["error"], "ahead": ahead_count(r["printer_id"], r["created"])} for r in rows])

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

@app.route("/api/print", methods=["POST"])
def api_print():
    u = current_user()
    if not u:
        return jsonify({"error": "Pehle login karo."}), 401
    f = request.files.get("file")
    pid = request.form.get("printer", "")
    printer = run("SELECT * FROM printers WHERE id=?", (pid,), one=True)
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
    if not admin_required():
        return redirect("/login")
    users = run("""SELECT u.username, u.is_admin,
                   (SELECT COUNT(*) FROM jobs j WHERE j.owner=u.username AND j.status='done') jobs,
                   (SELECT COALESCE(SUM(j.pages*j.copies),0) FROM jobs j WHERE j.owner=u.username AND j.status='done') pages
                   FROM users u ORDER BY u.username""")
    now = time.time()
    printers = [{"label": p["label"], "online": now - p["last_seen"] < ONLINE_SECONDS}
                for p in run("SELECT * FROM printers ORDER BY label")]
    recent = run("SELECT owner,filename,status FROM jobs ORDER BY created DESC LIMIT 20")
    return render_template_string(ADMIN_HTML, users=users, printers=printers, recent=recent, permanent=USE_PG,
                                  flash=request.args.get("m"))

@app.route("/admin/add", methods=["POST"])
def admin_add():
    if not admin_required():
        abort(403)
    u = request.form["u"].strip().lower()
    p = request.form["p"]
    if not u or len(p) < 4 or u == ADMIN_USER:
        return redirect("/admin?m=Username/password sahi nahi hai (password kam se kam 4 akshar).")
    run("""INSERT INTO users(username,pw,is_admin,created) VALUES(?,?,0,?)
           ON CONFLICT(username) DO UPDATE SET pw=excluded.pw WHERE users.is_admin=0""",
        (u, generate_password_hash(p), time.time()), write=True)
    return redirect("/admin?m=Save ho gaya: " + u)

@app.route("/admin/delete/<username>", methods=["POST"])
def admin_delete(username):
    if not admin_required():
        abort(403)
    run("DELETE FROM users WHERE username=? AND is_admin=0", (username.lower(),), write=True)
    return redirect("/admin?m=User hata diya.")

# ---------------- agent API (office PC) ----------------
@app.route("/agent/heartbeat", methods=["POST"])
def agent_heartbeat():
    if not agent_ok():
        abort(403)
    d = request.get_json(force=True)
    agent = str(d.get("agent", "PC"))[:40]
    for name in d.get("printers", []):
        run("""INSERT INTO printers(id,label,last_seen) VALUES(?,?,?)
               ON CONFLICT(id) DO UPDATE SET last_seen=excluded.last_seen""",
            (f"{agent}|{name}", f"{name} ({agent})", time.time()), write=True)
    return jsonify({"ok": True})

@app.route("/agent/claim", methods=["POST"])
def agent_claim():
    if not agent_ok():
        abort(403)
    d = request.get_json(force=True)
    pid = f"{d.get('agent')}|{d.get('printer')}"
    now = time.time()
    c = db()
    try:
        if USE_PG:
            cur = _cursor(c)
            cur.execute("UPDATE jobs SET status='waiting' WHERE status='printing' AND claimed<%s", (now - STUCK_SECONDS,))
            cur.execute("""UPDATE jobs SET status='printing', claimed=%s
                           WHERE id=(SELECT id FROM jobs WHERE printer_id=%s AND status='waiting'
                                     ORDER BY created LIMIT 1 FOR UPDATE SKIP LOCKED)
                           RETURNING id, copies, color, settings, path""", (now, pid))
            job = cur.fetchone()
            c.commit()
        else:
            c.execute("BEGIN IMMEDIATE")
            c.execute("UPDATE jobs SET status='waiting' WHERE status='printing' AND claimed<?", (now - STUCK_SECONDS,))
            job = c.execute("""SELECT * FROM jobs WHERE printer_id=? AND status='waiting'
                               ORDER BY created LIMIT 1""", (pid,)).fetchone()
            if job:
                c.execute("UPDATE jobs SET status='printing', claimed=? WHERE id=?", (now, job["id"]))
            c.commit()
    finally:
        c.close()
    if not job:
        return jsonify({"job": None})
    if not job["path"] or not os.path.exists(job["path"]):     # server restart mein file ud gayi
        run("UPDATE jobs SET status='error', error=?, finished=? WHERE id=?",
            ("File server restart mein hat gayi, dobara bhejo", time.time(), job["id"]), write=True)
        return jsonify({"job": None})
    return jsonify({"job": {"id": job["id"], "copies": job["copies"], "color": bool(job["color"]),
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
    return jsonify({"ok": True})

@app.route("/healthz")
def healthz():
    return "ok"
