"""
Office Print - Cloud server (Render par chalta hai)
Env variables (Render mein set karo):
  ADMIN_USER, ADMIN_PASS   -> admin ka login
  AGENT_KEY                -> office PC wale agent ka secret key (lamba random)
  SECRET_KEY               -> session ke liye lamba random text
  USERS (optional)         -> "rahul:pass1,amit:pass2"  (har restart par ye users ban jaate hain)
  DATA_DIR (optional)      -> persistent disk ka path, jaise /data
"""
import os, re, time, uuid, sqlite3, hmac
from datetime import timedelta

from flask import Flask, request, session, redirect, jsonify, render_template_string, send_file, abort
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
    try:
        c.execute("ALTER TABLE jobs ADD COLUMN settings TEXT")
    except sqlite3.OperationalError:
        pass
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


PAPERS = {"A3": "A3", "A4": "A4", "A5": "A5", "letter": "letter", "legal": "legal"}
PAPER_PT = {"A3": (842, 1191), "A4": (595, 842), "A5": (420, 595), "letter": (612, 792), "legal": (612, 1008)}
ORIENTS = {"auto", "portrait", "landscape"}
SCALES = {"shrink", "fit", "noscale"}
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

def process_pdf(src, dst, sel, orient, nup, paper):
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
            if orient != "auto" and (ww > hh) != (orient == "landscape"):
                p.rotate(90)
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
<label>File (PDF / JPG / PNG)</label><input type="file" id="file" accept=".pdf,.jpg,.jpeg,.png,image/*,application/pdf"></div>

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
  <div class="row"><label>Scale</label><select id="scale" onchange="refresh()"><option value="shrink">Shrink to fit</option><option value="fit">Fit to page</option><option value="noscale">Actual size</option></select></div>
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
let pdfDoc=null,imgURL=null,imgDim=null,nPages=0,cur=0,renderTok=0;
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
    const s=o.scale=="noscale"?1:(o.scale=="fit"?fitS:Math.min(1,fitS));
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
    const L=computeSheet({nup:nup,orient:$("orient").value,scale:$("scale").value,paper:$("paper").value},sizes,first);
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
function cancelFile(){$("file").value="";resetPreview();$("dlg").style.display="none"}

async function loadFile(){
  const f=$("file").files[0]; resetPreview();
  if(!f){$("dlg").style.display="none";return}
  $("dlg").style.display="grid"; $("pmsg").textContent="Loading preview...";
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
  const f=$("file").files[0]; if(!f){show("Choose a file first.","err");return}
  const sel=getSel(); if(sel.err){show(sel.err,"err");return}
  const fd=new FormData(); fd.append("file",f); fd.append("printer",$("printer").value);
  fd.append("copies",$("copies").value); fd.append("color",$("color").value);
  fd.append("paper",$("paper").value); fd.append("orient",$("orient").value);
  fd.append("scale",$("scale").value); fd.append("sides",$("sides").value);
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
        nup = int(request.form.get("nup", 1))
    except ValueError:
        return jsonify({"error": "Print settings sahi nahi hain."}), 400
    color = request.form.get("color") == "1"
    paper = request.form.get("paper", "A4")
    orient = request.form.get("orient", "auto")
    scale = request.form.get("scale", "shrink")
    sides = request.form.get("sides", "single")
    mode = request.form.get("mode", "all")
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
            image_to_pdf(path, pdf)
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
        token = process_pdf(path, out, sel, orient, nup, paper)
        for j in junk[:-1]:
            if os.path.exists(j):
                os.remove(j)
        path = out
    except Exception:
        for j in junk:
            if os.path.exists(j):
                os.remove(j)
        return jsonify({"error": "File kharab hai ya khul nahi rahi."}), 400

    settings = build_settings(copies, color, paper, token, "shrink" if nup > 1 else scale, sides)
    ahead = ahead_count(pid)
    run("""INSERT INTO jobs(id,user,filename,printer_id,copies,color,pages,status,created,path,settings)
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
