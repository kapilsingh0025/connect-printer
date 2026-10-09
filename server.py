"""
Office Print - Cloud server (Render par chalta hai)
Env variables (Render mein set karo):
  ADMIN_USER, ADMIN_PASS   -> admin ka login
  AGENT_KEY                -> office PC wale agent ka secret key (lamba random)
  SECRET_KEY               -> session ke liye lamba random text
  USERS (optional)         -> "rahul:pass1,amit:pass2"  (har restart par ye users ban jaate hain)
  DATA_DIR (optional)      -> persistent disk ka path, jaise /data
"""
import os, time, uuid, sqlite3, hmac
from datetime import timedelta

from flask import Flask, request, session, redirect, jsonify, render_template_string, send_file, abort
from werkzeug.security import generate_password_hash, check_password_hash
from PIL import Image

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

ALLOWED = {".pdf", ".jpg", ".jpeg", ".png"}
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
def db():
    c = sqlite3.connect(DB_PATH, timeout=15)
    c.row_factory = sqlite3.Row
    return c

def run(sql, args=(), one=False, write=False):
    c = db()
    try:
        cur = c.execute(sql, args)
        if write:
            c.commit()
            return cur.rowcount
        rows = cur.fetchall()
        return (rows[0] if rows else None) if one else rows
    finally:
        c.close()

def init():
    c = db()
    c.executescript("""
    CREATE TABLE IF NOT EXISTS users(username TEXT PRIMARY KEY, pw TEXT, is_admin INTEGER DEFAULT 0, created REAL);
    CREATE TABLE IF NOT EXISTS printers(id TEXT PRIMARY KEY, label TEXT, last_seen REAL);
    CREATE TABLE IF NOT EXISTS jobs(id TEXT PRIMARY KEY, user TEXT, filename TEXT, printer_id TEXT,
        copies INTEGER, color INTEGER, pages INTEGER, status TEXT, error TEXT,
        created REAL, claimed REAL, finished REAL, path TEXT);
    """)
    c.execute("""INSERT INTO users(username,pw,is_admin,created) VALUES(?,?,1,?)
                 ON CONFLICT(username) DO UPDATE SET pw=excluded.pw, is_admin=1""",
              (ADMIN_USER, generate_password_hash(ADMIN_PASS), time.time()))
    for pair in os.environ.get("USERS", "").split(","):
        if ":" in pair:
            u, p = pair.split(":", 1)
            u = u.strip().lower()
            if u and p:
                c.execute("""INSERT INTO users(username,pw,is_admin,created) VALUES(?,?,0,?)
                             ON CONFLICT(username) DO UPDATE SET pw=excluded.pw WHERE is_admin=0""",
                          (u, generate_password_hash(p), time.time()))
    c.commit()
    c.close()

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

LOGIN_HTML = """<!doctype html><html lang="hi"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>Office Print - Login</title>""" + STYLE + """</head><body>
<form class="c" method="post" style="max-width:360px;margin-top:40px"><h2>🖨️ Office Print</h2>
<input name="u" placeholder="Username" autocapitalize="none" autocomplete="username" required>
<input name="p" type="password" placeholder="Password" autocomplete="current-password" required>
{% if err %}<div class="e">{{err}}</div>{% endif %}
<button>Login</button></form></body></html>"""

MAIN_HTML = """<!doctype html><html lang="hi"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>Office Print</title>""" + STYLE + """</head><body>
<div class="top"><b>👤 {{user}}</b><span>{% if admin %}<a href="/admin">Admin</a> · {% endif %}<a href="/logout">Logout</a></span></div>
<div class="c"><h2>🖨️ Print karo</h2>
<label>Printer</label><select id="printer"></select>
<label>File (PDF / JPG / PNG)</label><input type="file" id="file" accept=".pdf,.jpg,.jpeg,.png,image/*,application/pdf">
<label>Copies</label><input type="number" id="copies" value="1" min="1" max="50">
<label style="display:flex;gap:8px;align-items:center"><input type="checkbox" id="color" style="width:auto;margin:0"> Color print (warna Black &amp; White)</label>
<button id="btn" onclick="send()">Print karo</button>
<div class="msg" id="msg"></div></div>
<div class="c"><h3>Meri jobs</h3><div id="jobs">Abhi koi job nahi.</div></div>
<script>
const $=id=>document.getElementById(id);
const label={waiting:"⏳ Wait",printing:"🖨️ Print ho raha hai",done:"✅ Ho gaya",error:"❌ Error"};
function show(t,c){const m=$("msg");m.className="msg "+(c||"");m.textContent=t;m.style.display="block"}
async function loadPrinters(){
  const r=await (await fetch("/api/printers")).json(), s=$("printer"), cur=s.value;
  s.innerHTML="";
  r.forEach(p=>{const o=document.createElement("option");o.value=p.id;
    o.textContent=p.label+" — "+(!p.online?"PC band ❌":(p.queue>0?"busy ("+p.queue+" job aage)":"free ✅"));s.appendChild(o)});
  if(!r.length){const o=document.createElement("option");o.textContent="Koi printer online nahi";s.appendChild(o)}
  if(cur) s.value=cur;
}
async function loadJobs(){
  const r=await (await fetch("/api/jobs")).json(); if(!r.length) return;
  const box=$("jobs"); box.innerHTML="";
  r.forEach(j=>{const d=document.createElement("div");d.className="job";
    const a=document.createElement("span");a.textContent=j.file+" ("+j.printer+")";
    const b=document.createElement("span");b.textContent=label[j.status]+(j.status=="waiting"?" (aage "+j.ahead+")":"")+(j.status=="error"?": "+(j.error||""):"");
    d.appendChild(a);d.appendChild(b);box.appendChild(d)});
}
async function send(){
  const f=$("file").files[0]; if(!f){show("Pehle file chuno.","err");return}
  const fd=new FormData(); fd.append("file",f); fd.append("printer",$("printer").value);
  fd.append("copies",$("copies").value); fd.append("color",$("color").checked?"1":"0");
  $("btn").disabled=true; show("Bhej raha hoon...");
  try{
    const r=await fetch("/api/print",{method:"POST",body:fd}), d=await r.json();
    if(r.ok){show(d.message,d.warn?"":"ok"); $("file").value="";} else show(d.error||"Error aaya","err");
  }catch(e){show("Server se connect nahi hua.","err")}
  $("btn").disabled=false; loadPrinters(); loadJobs();
}
loadPrinters(); loadJobs(); setInterval(()=>{loadPrinters();loadJobs()},4000);
</script></body></html>"""

ADMIN_HTML = """<!doctype html><html lang="hi"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>Admin</title>""" + STYLE + """</head><body>
<div class="top"><a href="/">← Print page</a><a href="/logout">Logout</a></div>
{% if flash %}<div class="c" style="background:#d1fae5">{{flash}}</div>{% endif %}
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
{% for j in recent %}<tr><td>{{j.user}}</td><td>{{j.filename}}</td><td>{{j.status}}</td></tr>{% endfor %}
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
                  WHERE j.user=? ORDER BY j.created DESC LIMIT 8""", (u["username"],))
    return jsonify([{"file": r["filename"], "printer": r["label"] or "?", "status": r["status"],
                     "error": r["error"], "ahead": ahead_count(r["printer_id"], r["created"])} for r in rows])

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
    except ValueError:
        copies = 1

    jid = uuid.uuid4().hex[:10]
    path = os.path.join(UPLOAD_DIR, jid + ext)
    f.save(path)
    try:
        if ext != ".pdf":
            pdf = os.path.join(UPLOAD_DIR, jid + ".pdf")
            Image.open(path).convert("RGB").save(pdf, "PDF", resolution=150)
            os.remove(path)
            path = pdf
        pages = count_pages(path)
    except Exception:
        if os.path.exists(path):
            os.remove(path)
        return jsonify({"error": "File kharab hai ya khul nahi rahi."}), 400

    ahead = ahead_count(pid)
    run("""INSERT INTO jobs(id,user,filename,printer_id,copies,color,pages,status,created,path)
           VALUES(?,?,?,?,?,?,?,'waiting',?,?)""",
        (jid, u["username"], (f.filename or "file")[:60], pid, copies,
         1 if request.form.get("color") == "1" else 0, pages, time.time(), path), write=True)

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
                   (SELECT COUNT(*) FROM jobs j WHERE j.user=u.username AND j.status='done') jobs,
                   (SELECT COALESCE(SUM(j.pages*j.copies),0) FROM jobs j WHERE j.user=u.username AND j.status='done') pages
                   FROM users u ORDER BY u.username""")
    now = time.time()
    printers = [{"label": p["label"], "online": now - p["last_seen"] < ONLINE_SECONDS}
                for p in run("SELECT * FROM printers ORDER BY label")]
    recent = run("SELECT user,filename,status FROM jobs ORDER BY created DESC LIMIT 20")
    return render_template_string(ADMIN_HTML, users=users, printers=printers, recent=recent,
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
           ON CONFLICT(username) DO UPDATE SET pw=excluded.pw WHERE is_admin=0""",
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
    c = db()
    for name in d.get("printers", []):
        c.execute("""INSERT INTO printers(id,label,last_seen) VALUES(?,?,?)
                     ON CONFLICT(id) DO UPDATE SET last_seen=excluded.last_seen""",
                  (f"{agent}|{name}", f"{name} ({agent})", time.time()))
    c.commit()
    c.close()
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
    return jsonify({"job": {"id": job["id"], "copies": job["copies"], "color": bool(job["color"])}})

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
