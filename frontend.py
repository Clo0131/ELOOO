#!/usr/bin/env python3
"""
CoolStock - front end (screens + web server)
============================================
Run:    python frontend.py            then open  http://localhost:8000
Option: python frontend.py --lan      lets other devices on your Wi-Fi/LAN connect
        python frontend.py --port 9000 --no-browser

Keep this file, logic.py, .env and the assets folder in the same folder.
This file holds the HTML/CSS/JavaScript screens, the URL routes and the HTTP server.
All of the real work (database, rules, algorithms) is in logic.py.

Deploy on Vercel: api/index.py is the function and vercel.json sends every URL to it;
public/font/nunito.woff2 is served as a static file there. The data lives in Supabase
(PostgreSQL): the connection string comes from DATABASE_URL, which is read from .env
locally and from the project's environment variables on Vercel.
"""
import json
import base64
import os
import sys
import threading
import traceback
import webbrowser
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from logic import *            # noqa: F401,F403  (handlers, db helpers, constants)
from logic import _t

PUBLIC, OWNER, OP, OE = "public", {"owner"}, {"owner", "purchasing"}, {"owner", "employee"}
ROUTES = [
    ("POST", r"/api/movements", h_move, None), ("POST", r"/api/reorder", h_reorder, OP),
    ("POST", r"/api/login", h_login, PUBLIC), ("POST", r"/api/register", h_register, PUBLIC), ("GET", r"/api/login-info", h_login_info, PUBLIC), ("GET", r"/api/shops", h_shops, PUBLIC),
    ("POST", r"/api/logout", h_logout, None), ("GET", r"/api/me", h_me, None), ("POST", r"/api/password", h_password, None),
    ("PUT", r"/api/settings", h_settings_put, OWNER),
    ("GET", r"/api/dashboard", h_dashboard, None),
    ("GET", r"/api/products", h_products, None), ("POST", r"/api/products", h_product_create, OWNER),
    ("GET", r"/api/products/(\d+)/detail", h_product_detail, None),
    ("PUT", r"/api/products/(\d+)", h_product_update, OWNER), ("DELETE", r"/api/products/(\d+)", h_product_archive, OWNER),
    ("POST", r"/api/promo", h_promo, OWNER),
    ("GET", r"/api/lowstock", h_lowstock, None), ("GET", r"/api/deadstock", h_deadstock, OWNER),
    ("GET", r"/api/substitutes", h_substitutes, None),
    ("GET", r"/api/sales", h_sales, OE), ("POST", r"/api/sales", h_sale_create, OE),
    ("GET", r"/api/sales/(\d+)", h_sale_get, OE), ("POST", r"/api/sales/(\d+)/void", h_sale_void, OWNER),
    ("GET", r"/api/reports", h_reports, OWNER), ("GET", r"/api/alerts", h_alerts, OWNER), ("GET", r"/api/audit", h_audit, OWNER),
    ("GET", r"/api/users", h_users, OWNER), ("POST", r"/api/users", h_user_save, OWNER), ("PUT", r"/api/users/(\d+)", h_user_save, OWNER),
    ("POST", r"/api/users/(\d+)/(approve|reject)", h_user_approve, OWNER),
]


# ================================================================ http server
class Handler(BaseHTTPRequestHandler):
    server_version = "CoolStock"

    def log_message(self, *a):
        pass

    def reply(self, code, data, ctype="application/json", headers=None):
        if not isinstance(data, bytes):
            data = json.dumps(data).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype + ("; charset=utf-8" if ctype.startswith("text") or ctype == "application/json" else ""))
        self.send_header("Content-Length", str(len(data)))
        response_headers = headers or {}
        self.send_header("Cache-Control", next((v for k, v in response_headers.items() if k.lower() == "cache-control"), "no-store"))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        for k, v in response_headers.items():
            if k.lower() != "cache-control":
                self.send_header(k, v)
        self.end_headers()
        self.wfile.write(data)

    def redirect(self, to):
        self.send_response(302)
        self.send_header("Location", to)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def handle_any(self, method):
        url = urlparse(self.path)
        path = url.path
        # A rewrite to this function can preserve the original URL or expose the
        # function path followed by the original URL. Normalize both forms.
        for prefix in ("/api/index", "/api/index.py"):
            if path == prefix or path.startswith(prefix + "/"):
                path = path[len(prefix):] or "/"
                break
        if method == "GET" and path in ("/logo.png", "/font/nunito.woff2"):
            if path == "/logo.png":
                return self.reply(200, LOGO_PNG, "image/png", {"Cache-Control": "public, max-age=86400"})
            fp = os.path.join(os.path.dirname(os.path.abspath(__file__)), "assets", "nunito.woff2")
            try:
                with open(fp, "rb") as f:
                    data = f.read()
            except OSError:
                return self.reply(404, b"Font not found", "text/plain")
            return self.reply(200, data, "font/woff2", {"Cache-Control": "public, max-age=86400"})
        ck = SimpleCookie(self.headers.get("Cookie", ""))
        sid = ck["sid"].value if "sid" in ck else None
        c = db()
        _t.shop = None
        try:
            user = session_user(c, sid)
            _t.shop = user["shop"] if user else None
            if method == "GET":
                if path in ("/", "/index.html"):
                    return self.reply(200, (APP_HTML if user else LOGIN_HTML).replace("__APP__", APP_NAME).encode(), "text/html")
                if path == "/login":
                    return self.redirect("/") if user else self.reply(200, LOGIN_HTML.replace("__APP__", APP_NAME).encode(), "text/html")
                m = re.fullmatch(r"/invoice/(\d+)", path)
                if m:
                    if not user or user["role"] not in ("owner", "employee"):
                        return self.redirect("/")
                    s = c.execute("SELECT \"user\" FROM sales WHERE id=? AND shop_id=?", (int(m.group(1)), user["shop"])).fetchone()
                    if not s:
                        return self.reply(404, b"Invoice not found", "text/plain")
                    if user["role"] == "employee" and s["user"] != user["username"]:
                        return self.reply(403, b"Not your sale", "text/plain")
                    return self.reply(200, invoice_html(c, int(m.group(1))).encode(), "text/html")
            for m_, pattern, fn, roles in ROUTES:
                mt = re.fullmatch(pattern, path)
                if m_ != method or not mt:
                    continue
                if roles != PUBLIC:
                    if not user:
                        return self.reply(401, {"error": "Please sign in."})
                    if roles and user["role"] not in roles:
                        return self.reply(403, {"error": "Your role is not allowed to do this."})
                if method != "GET" and self.headers.get("X-Requested-With") != "fetch":
                    return self.reply(403, {"error": "Blocked: missing request header."})
                body = {}
                n = int(self.headers.get("Content-Length") or 0)
                if n:
                    try:
                        body = json.loads(self.rfile.read(n))
                        if not isinstance(body, dict):
                            raise ValueError
                    except ValueError:
                        return self.reply(400, {"error": "Invalid JSON"})
                ctx = dict(user or {}, ip=self.client_address[0])
                write = method != "GET"
                try:
                    if write:
                        c.execute("BEGIN")
                    data = fn(c, ctx, parse_qs(url.query), body, *mt.groups())
                    if write:
                        c.execute("COMMIT")
                except BaseException:
                    if c.in_transaction:
                        c.execute("ROLLBACK")
                    raise
                if isinstance(data, Resp):
                    return self.reply(data.code, data.data, data.ctype, data.headers)
                if user and user["role"] == "employee":
                    data = redact(data)
                return self.reply(200, data)
            self.reply(404, {"error": "Not found"})
        except ApiError as e:
            self.reply(e.code, {"error": e.msg})
        except Exception as e:
            traceback.print_exc()
            self.reply(500, {"error": f"Server error: {e}"})
        finally:
            c.close()

    def do_GET(self): self.handle_any("GET")
    def do_POST(self): self.handle_any("POST")
    def do_PUT(self): self.handle_any("PUT")
    def do_DELETE(self): self.handle_any("DELETE")


LOGO_PNG = base64.b64decode(
"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")

LOGIN_HTML = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<link rel="icon" type="image/png" href="/logo.png">
<title>Sign in - __APP__</title>
<style>
@font-face{font-family:"Nunito";font-style:normal;font-weight:400 800;font-display:swap;src:url("/font/nunito.woff2") format("woff2")}
*{box-sizing:border-box}
:root{--panel:#f4f7fc;--ink:#262250;--mut:#666c93;--line:rgba(51,45,86,.15);--pri:#3368A0;--pri2:#2a5688;--action:#3368A0;--ok:#33b992;--bad:#e9617c;--ice:#e2e9f4;--edge:rgba(255,255,255,.62);--btn2:linear-gradient(180deg,#ffffff 0%,#e4ecf7 100%);--btn2-sh:0 12px 22px -14px rgba(51,45,86,.45),inset 0 2px 2px rgba(255,255,255,.95),inset 0 -5px 9px rgba(51,104,160,.2);--ease-out:cubic-bezier(.25,.1,.25,1);
--sh-in:inset 0 4px 9px rgba(51,45,86,.2),inset 0 -2px 3px rgba(255,255,255,.9);
--clay:linear-gradient(155deg,#ffffff 0%,#f4f7fc 52%,#e6ecf6 100%);
--btn:linear-gradient(160deg,#3d7bb8 0%,#3368A0 34%,#332D56 78%,#421454 100%);
--btn-sh:0 14px 26px -12px rgba(51,104,160,.8),inset 0 3px 3px rgba(255,255,255,.6),inset 0 -6px 10px rgba(51,104,160,.32);
--font-text:"Nunito","SF Pro Rounded",ui-rounded,-apple-system,BlinkMacSystemFont,"Segoe UI Variable Text","Segoe UI",system-ui,sans-serif;--font-display:"Nunito","SF Pro Rounded",ui-rounded,-apple-system,BlinkMacSystemFont,"Segoe UI Variable Display","Segoe UI",system-ui,sans-serif;color-scheme:light}
@media(prefers-color-scheme:dark){:root{--panel:#1f1c3d;--ink:#eef1fb;--mut:#a9b0d0;--line:rgba(215,205,255,.14);--pri:#6fa4dd;--pri2:#5b95cf;--action:#6fa4dd;--ok:#5fe0b6;--bad:#ff8fa5;--ice:#2a2850;--edge:rgba(255,255,255,.09);--sh-in:inset 0 4px 10px rgba(0,0,0,.45),inset 0 -2px 3px rgba(255,255,255,.06);--clay:linear-gradient(155deg,#292650 0%,#1f1c3d 55%,#191635 100%);--btn:linear-gradient(160deg,#4a88c6 0%,#3368A0 34%,#332D56 78%,#421454 100%);--btn-sh:0 14px 26px -12px rgba(0,0,0,.75),inset 0 3px 3px rgba(255,255,255,.18),inset 0 -6px 10px rgba(0,0,0,.35);--btn2:linear-gradient(180deg,#302d58 0%,#25224a 100%);--btn2-sh:0 12px 22px -14px rgba(0,0,0,.6),inset 0 2px 2px rgba(255,255,255,.1),inset 0 -5px 9px rgba(0,0,0,.28);color-scheme:dark}}
body{margin:0;min-height:100vh;padding:24px;display:grid;place-items:center;font:15px/1.5 var(--font-text);background:radial-gradient(760px 560px at 8% -6%,#e6d4f1 0%,transparent 58%),radial-gradient(680px 560px at 100% 4%,#cfe1f4 0%,transparent 55%),radial-gradient(820px 640px at 50% 112%,#d5dbf1 0%,transparent 60%),linear-gradient(160deg,#eef2f9 0%,#ecf0f9 48%,#e6ecf6 100%);background-attachment:fixed;color:var(--ink);-webkit-font-smoothing:antialiased;-moz-osx-font-smoothing:grayscale;letter-spacing:-.006em;font-optical-sizing:auto}
@media(prefers-color-scheme:dark){body{background:radial-gradient(760px 560px at 8% -6%,#4a1c60 0%,transparent 58%),radial-gradient(680px 560px at 100% 4%,#1f4570 0%,transparent 55%),radial-gradient(820px 640px at 50% 112%,#2a2655 0%,transparent 60%),linear-gradient(160deg,#17143a 0%,#100e28 100%);background-attachment:fixed}}
::selection{background:color-mix(in srgb,var(--pri) 24%,transparent)}
.brand{display:flex;align-items:center;gap:14px;margin-bottom:26px}
.mark{width:52px;height:52px;border-radius:18px;display:grid;place-items:center;flex:none;color:#fff;background:var(--btn);box-shadow:var(--btn-sh)}
.mark svg{width:26px;height:26px}
.logo{font-family:var(--font-display);font-size:32px;font-weight:800;letter-spacing:-.04em;line-height:1.05}
.sub{color:var(--mut);margin:3px 0 0;font-size:14px;line-height:1.4}
label{display:block;font-size:12px;color:var(--mut);margin:14px 0 6px;font-weight:600}
input,select{width:100%;padding:13px 16px;border:0;border-radius:18px;font:inherit;font-size:14px;background:var(--ice);color:var(--ink);box-shadow:var(--sh-in)}
input:focus,select:focus{outline:0;box-shadow:var(--sh-in),0 0 0 4px color-mix(in srgb,var(--pri) 18%,transparent)}
::placeholder{color:var(--mut);opacity:1}
button{width:100%;margin-top:26px;padding:15px;border:0;border-radius:980px;background:var(--btn);color:#fff;font:inherit;font-size:15px;font-weight:800;letter-spacing:-.005em;cursor:pointer;box-shadow:var(--btn-sh);transition:transform .15s var(--ease-out),box-shadow .15s var(--ease-out),filter .15s var(--ease-out)}
button:hover{transform:translateY(-2px);filter:brightness(1.04)}button:active{transform:translateY(2px);box-shadow:inset 0 4px 9px rgba(50,30,130,.35)}button:disabled{opacity:.5;cursor:not-allowed;transform:none;filter:none}
.err{background:color-mix(in srgb,var(--bad) 12%,var(--panel));color:var(--bad);border:0;border-radius:18px;padding:12px 15px;margin-top:14px;display:none;font-size:13.5px;box-shadow:var(--sh-in)}
.sw{margin-top:20px;text-align:center;color:var(--mut);font-size:14px}.sw a{color:var(--pri);cursor:pointer;text-decoration:none}.sw a:hover{text-decoration:underline;text-underline-offset:2px}
.hint{font-size:12px;color:var(--mut);margin-top:5px}.hide{display:none}
.roles{display:grid;gap:10px;margin-top:4px}.role{display:flex;gap:11px;align-items:flex-start;border:0;border-radius:20px;padding:13px 15px;cursor:pointer;margin:0;font-size:14px;color:var(--ink);background:var(--btn2);box-shadow:var(--btn2-sh);transition:background .15s var(--ease-out),box-shadow .15s var(--ease-out),transform .15s var(--ease-out)}
.role input{width:auto;margin-top:3px;box-shadow:none}.role b{display:block;font-weight:600}.role small{color:var(--mut);font-size:12px}.role:has(input:checked){background:color-mix(in srgb,var(--pri) 14%,var(--panel));box-shadow:inset 0 3px 9px rgba(51,45,86,.22),0 0 0 3px color-mix(in srgb,var(--pri) 30%,transparent)}
.okmsg{background:color-mix(in srgb,var(--ok) 12%,var(--panel));color:var(--ok);border:0;border-radius:18px;padding:12px 15px;font-size:13.5px;margin-bottom:8px}.welcome{background:color-mix(in srgb,var(--pri) 9%,var(--panel));border:0;border-radius:18px;padding:12px 15px;font-size:13px;margin-bottom:6px}
.card{width:min(452px,92vw);background:var(--clay);border:0;border-radius:40px;box-shadow:0 34px 70px -24px rgba(51,45,86,.45),0 10px 24px -14px rgba(51,45,86,.25),inset 0 3px 3px rgba(255,255,255,.95),inset 0 -10px 20px rgba(51,104,160,.16);padding:40px 36px 36px}
@media(prefers-color-scheme:dark){.card{box-shadow:0 34px 70px -24px rgba(0,0,0,.8),inset 0 3px 3px rgba(255,255,255,.08),inset 0 -10px 20px rgba(0,0,0,.3)}}
input[type=number]{-moz-appearance:textfield}input[type=number]::-webkit-inner-spin-button{-webkit-appearance:none}
select{-webkit-appearance:none;appearance:none;padding-right:34px;background-image:url("data:image/svg+xml;utf8,<svg xmlns='http://www.w3.org/2000/svg' width='12' height='12' viewBox='0 0 12 12'><path d='M2.5 4.5 6 8l3.5-3.5' fill='none' stroke='%238e8e93' stroke-width='1.8' stroke-linecap='round' stroke-linejoin='round'/></svg>");background-repeat:no-repeat;background-position:right 13px center;background-size:12px}
@media(prefers-reduced-motion:reduce){*,*::before,*::after{animation-duration:.01ms!important;transition-duration:.01ms!important}}
@media(max-width:480px){body{padding:16px}.card{width:100%;padding:26px 22px 22px}}
.brand{display:block;text-align:center}.brand .lg{width:250px;max-width:80%;margin:0 auto 4px}.brand .sub{text-align:center;margin-top:0}
@media(prefers-color-scheme:dark){.brand .lg{background:#f4f7fc;border-radius:24px;padding:10px 14px;box-sizing:border-box}}
</style></head><body>
<form class="card" id="f" autocomplete="on">
  <div class="brand"><img class="lg" src="/logo.png" alt="__APP__"><div class="sub" id="shop">Aircon Inventory, Sales &amp; Purchasing</div></div>
  <div id="welcome" class="welcome hide">No accounts yet. Create the first account to get started.</div>
  <div id="ok" class="okmsg hide" role="status" aria-live="polite"></div>
  <div id="rn" class="hide"><label for="n">Full name</label><input id="n" name="name" autocomplete="name"></div>
  <label for="u">Username</label><input id="u" name="username" autofocus required autocomplete="username">
  <label for="p">Password</label><input id="p" name="password" type="password" required autocomplete="current-password">
  <div id="rc" class="hide"><label for="c">Confirm password</label><input id="c" name="confirm" type="password" autocomplete="new-password">
    <label style="margin-top:12px">Your role</label>
    <div class="roles" id="roles">
      <label class="role"><input type="radio" name="role" value="owner" checked><span><b>Shop Owner</b><small>Creates your own shop. You add the stock and manage everything.</small></span></label>
      <label class="role"><input type="radio" name="role" value="employee"><span><b>Sales staff</b><small>Works for a shop owner: sells and searches stock</small></span></label>
      <label class="role"><input type="radio" name="role" value="purchasing"><span><b>Purchasing</b><small>Works for a shop owner: restock planning</small></span></label>
    </div>
    <div id="rs"><label for="sn">Shop name</label><input id="sn" name="shop_name" placeholder="e.g. Santos Aircon Center"><div class="hint">Your shop starts empty. You add the products after signing in.</div></div>
    <div id="ro" class="hide"><label for="ow">Which shop owner do you work for?</label><select id="ow"></select>
      <div class="hint" id="owhint">The owner has to approve your account before you can sign in. You will then see that shop's stock.</div></div>
  </div>
  <div class="err" id="err" role="alert" aria-live="polite"></div>
  <button id="go">Sign in</button>
  <div class="sw" id="sw"></div>
</form>
<script>
const $=s=>document.querySelector(s);
let REG=false,INFO={users:1},SHOPS=[];
const role=()=>{const r=document.querySelector('#roles input:checked');return r?r.value:''};
function roleUI(){
  const own=role()==='owner';
  $('#rs').classList.toggle('hide',!own);$('#ro').classList.toggle('hide',own);
}
function mode(r){
  REG=r;$('#rn').classList.toggle('hide',!r);$('#rc').classList.toggle('hide',!r);$('#go').textContent=r?'Create account':'Sign in';
  $('#p').autocomplete=r?'new-password':'current-password';$('#err').style.display='none';
  $('#welcome').classList.toggle('hide',!(r&&INFO.users===0));
  $('#sw').innerHTML=INFO.users>0?(r?'Already have an account? <a href="#" data-m="in">Sign in</a>':"Don't have an account? <a href='#' data-m='up'>Create one</a>"):'';
  roleUI();
}
$('#sw').onclick=e=>{const a=e.target.closest('a[data-m]');if(a){e.preventDefault();$('#ok').classList.add('hide');mode(a.dataset.m==='up')}};
$('#roles').onchange=roleUI;
Promise.all([fetch('/api/login-info').then(r=>r.json()),fetch('/api/shops').then(r=>r.json())]).then(([d,sh])=>{
  INFO=d;SHOPS=sh;
  const sel=$('#ow');
  sel.innerHTML=sh.length?sh.map(x=>`<option value="${x.id}">${(x.shop||'Unnamed shop').replace(/</g,'&lt;')} - owner: ${x.owner.replace(/</g,'&lt;')}</option>`).join(''):'<option value="">No shop owners yet</option>';
  if(!sh.length){$('#owhint').textContent='There are no shop owners yet. The shop owner has to create an account first.'}
  mode(d.users===0);
}).catch(()=>mode(false));
$('#f').onsubmit=async e=>{
  e.preventDefault();const b=$('#go');b.disabled=true;$('#err').style.display='none';
  try{
    const body={username:$('#u').value,password:$('#p').value};
    if(REG){
      body.name=$('#n').value;body.confirm=$('#c').value;body.role=role();
      if(body.role==='owner')body.shop_name=$('#sn').value;else body.owner_id=$('#ow').value;
    }
    const r=await fetch(REG?'/api/register':'/api/login',{method:'POST',headers:{'Content-Type':'application/json','X-Requested-With':'fetch'},body:JSON.stringify(body)});
    const d=await r.json();
    if(!r.ok)throw new Error(d.error||'Something went wrong');
    if(d.pending){
      const m=$('#ok');m.textContent='Request sent. '+d.owner+' has to approve your account. You can sign in after that.';m.classList.remove('hide');
      mode(false);$('#p').value='';b.disabled=false;return;
    }
    location.href='/';
  }catch(x){$('#err').textContent=x.message;$('#err').style.display='block';b.disabled=false}
};
</script>
</body></html>
"""
APP_HTML = r"""<!doctype html>
<html lang="en" data-theme="light"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<link rel="icon" type="image/png" href="/logo.png">
<title>__APP__</title>
<style>
@font-face{font-family:"Nunito";font-style:normal;font-weight:400 800;font-display:swap;src:url("/font/nunito.woff2") format("woff2")}
:root{--bg:#ecf0f9;--panel:#f4f7fc;--ink:#262250;--mut:#666c93;--line:rgba(51,45,86,.15);--pri:#3368A0;--pri2:#2a5688;--action:#3368A0;--ok:#33b992;--warn:#e59b3c;--bad:#e9617c;
--ice:#e2e9f4;--hov:#e9eff8;--c1:#3368A0;--c2:#0fa8a8;--c3:#eda54b;--c4:#8a4fa3;--side:#eef2f9;--sideink:#262250;--r:30px;--ease-out:cubic-bezier(.25,.1,.25,1);
--edge:rgba(255,255,255,.62);
--sh:0 26px 50px -22px rgba(51,45,86,.4),0 10px 20px -12px rgba(51,45,86,.2),inset 0 3px 3px rgba(255,255,255,.95),inset 0 -8px 16px rgba(51,104,160,.16);
--sh-sm:0 14px 26px -14px rgba(51,45,86,.32),inset 0 2px 2px rgba(255,255,255,.92),inset 0 -4px 8px rgba(51,104,160,.12);
--sh-in:inset 0 4px 9px rgba(51,45,86,.2),inset 0 -2px 3px rgba(255,255,255,.9);
--clay:linear-gradient(155deg,#ffffff 0%,#f4f7fc 52%,#e6ecf6 100%);
--clay-side:linear-gradient(165deg,#fbfcff 0%,#eef2fa 55%,#e1e8f4 100%);
--btn:linear-gradient(160deg,#3d7bb8 0%,#3368A0 34%,#332D56 78%,#421454 100%);
--btn2:linear-gradient(180deg,#ffffff 0%,#e4ecf7 100%);
--btn-sh:0 14px 26px -12px rgba(51,104,160,.8),inset 0 3px 3px rgba(255,255,255,.6),inset 0 -6px 10px rgba(51,104,160,.32);
--btn2-sh:0 12px 22px -14px rgba(51,45,86,.45),inset 0 2px 2px rgba(255,255,255,.95),inset 0 -5px 9px rgba(51,104,160,.2);
--btn-danger:linear-gradient(180deg,#ffaec0 0%,#ee6a86 100%);
--btn-danger-sh:0 14px 26px -12px rgba(238,106,134,.75),inset 0 3px 3px rgba(255,255,255,.55),inset 0 -6px 10px rgba(150,40,65,.3);
--font-text:"Nunito","SF Pro Rounded",ui-rounded,-apple-system,BlinkMacSystemFont,"Segoe UI Variable Text","Segoe UI",system-ui,sans-serif;
--font-display:"Nunito","SF Pro Rounded",ui-rounded,-apple-system,BlinkMacSystemFont,"Segoe UI Variable Display","Segoe UI",system-ui,sans-serif;color-scheme:light}
[data-theme=dark]{--bg:#141230;--panel:#1f1c3d;--ink:#eef1fb;--mut:#a9b0d0;--line:rgba(215,205,255,.14);--pri:#6fa4dd;--pri2:#5b95cf;--action:#6fa4dd;--ok:#5fe0b6;--warn:#f3b969;--bad:#ff8fa5;
--ice:#2a2850;--hov:#2b2955;--c1:#6fa4dd;--c2:#3fd0cf;--c3:#f3b969;--c4:#c08bdc;--side:#1b1838;--sideink:#eef1fb;
--edge:rgba(255,255,255,.08);
--sh:0 26px 50px -22px rgba(0,0,0,.75),0 10px 22px -12px rgba(0,0,0,.5),inset 0 3px 3px rgba(255,255,255,.07),inset 0 -8px 16px rgba(0,0,0,.28);
--sh-sm:0 14px 26px -14px rgba(0,0,0,.6),inset 0 2px 2px rgba(255,255,255,.08),inset 0 -4px 8px rgba(0,0,0,.25);
--sh-in:inset 0 4px 10px rgba(0,0,0,.45),inset 0 -2px 3px rgba(255,255,255,.06);
--clay:linear-gradient(155deg,#292650 0%,#1f1c3d 55%,#191635 100%);
--clay-side:linear-gradient(165deg,#25224a 0%,#1a1738 100%);
--btn:linear-gradient(160deg,#4a88c6 0%,#3368A0 34%,#332D56 78%,#421454 100%);
--btn2:linear-gradient(180deg,#302d58 0%,#25224a 100%);
--btn-sh:0 14px 26px -12px rgba(0,0,0,.75),inset 0 3px 3px rgba(255,255,255,.18),inset 0 -6px 10px rgba(0,0,0,.35);
--btn2-sh:0 12px 22px -14px rgba(0,0,0,.6),inset 0 2px 2px rgba(255,255,255,.1),inset 0 -5px 9px rgba(0,0,0,.28);
--btn-danger:linear-gradient(180deg,#ffaec0 0%,#ee6a86 100%);
--btn-danger-sh:0 14px 26px -12px rgba(0,0,0,.65),inset 0 3px 3px rgba(255,255,255,.2),inset 0 -6px 10px rgba(120,35,55,.35);
color-scheme:dark}
*{box-sizing:border-box}
::selection{background:color-mix(in srgb,var(--pri) 24%,transparent)}
body{margin:0;font:14px/1.5 var(--font-text);background:radial-gradient(760px 560px at 6% -6%,#e6d4f1 0%,transparent 58%),radial-gradient(680px 560px at 100% 4%,#cfe1f4 0%,transparent 55%),radial-gradient(820px 640px at 52% 112%,#d5dbf1 0%,transparent 60%),linear-gradient(160deg,#eef2f9 0%,#ecf0f9 48%,#e6ecf6 100%);background-attachment:fixed;color:var(--ink);-webkit-font-smoothing:antialiased;-moz-osx-font-smoothing:grayscale;letter-spacing:-.006em;caret-color:var(--pri);font-optical-sizing:auto;font-synthesis:none;text-rendering:optimizeLegibility}
[data-theme=dark] body{background:radial-gradient(760px 560px at 6% -6%,#4a1c60 0%,transparent 58%),radial-gradient(680px 560px at 100% 4%,#1f4570 0%,transparent 55%),radial-gradient(820px 640px at 52% 112%,#2a2655 0%,transparent 60%),linear-gradient(160deg,#17143a 0%,#100e28 100%);background-attachment:fixed}
.app{display:block;min-height:100vh;padding:12px 0 12px 12px}
#side{background:var(--clay-side);color:var(--sideink);position:sticky;top:12px;height:calc(100vh - 24px);width:260px;overflow:hidden;padding:18px 14px;border-radius:34px;box-shadow:var(--sh);display:flex;flex-direction:column;border:0}
.logo{display:flex;align-items:center;gap:12px;padding:2px 8px 20px}
.mark{width:38px;height:38px;border-radius:15px;display:grid;place-items:center;flex:none;color:#fff;background:var(--btn);box-shadow:var(--btn-sh)}
.mark svg{width:21px;height:21px}
.logo .wordmark{font-family:var(--font-display);font-size:19px;font-weight:800;letter-spacing:-.035em;line-height:1.15}
.logo small{display:block;font-size:12px;font-weight:400;color:var(--mut);margin-top:1px;letter-spacing:0;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;max-width:160px}
#nav{flex:1;overflow-y:auto;min-height:0;margin-bottom:10px;padding-right:2px}
#nav h6{margin:20px 12px 6px;font-size:10.5px;color:var(--mut);font-weight:700;text-transform:uppercase;letter-spacing:.09em}
#nav a{display:flex;align-items:center;gap:11px;padding:8px 12px;margin:2px 0;color:var(--sideink);text-decoration:none;cursor:pointer;border-radius:17px;font-size:13.5px;font-weight:600;letter-spacing:-.004em;transition:background .15s var(--ease-out),color .15s var(--ease-out),box-shadow .15s var(--ease-out),transform .15s var(--ease-out)}
#nav a:hover{background:color-mix(in srgb,var(--pri) 11%,transparent)}
#nav a.on{background:var(--btn);color:#fff;box-shadow:var(--btn-sh)}
#nav a:active{transform:scale(.99)}
#nav a:focus-visible{outline:2px solid var(--pri);outline-offset:2px}
.ni{width:18px;height:18px;display:grid;place-items:center;flex:none;opacity:.62}
.ni svg{width:16px;height:16px}
#nav a.on .ni{opacity:1}
.main{min-width:0;padding:0 16px}
.top{display:flex;gap:10px;align-items:center;padding:10px 24px;position:sticky;top:0;z-index:20;background:var(--clay);-webkit-backdrop-filter:none;backdrop-filter:none;border:0;border-radius:26px;box-shadow:var(--sh-sm);margin-top:12px}
.gs{position:relative;flex:1;min-width:0;max-width:460px}
.gsi{position:absolute;left:14px;top:50%;transform:translateY(-50%);color:var(--mut);pointer-events:none;display:grid}
.gs input{width:100%;background:var(--ice);border:0;border-radius:16px;box-shadow:var(--sh-in);padding:10px 14px 10px 38px;font-size:13.5px}
.gs input:focus{background:var(--panel);box-shadow:var(--sh-in),0 0 0 4px color-mix(in srgb,var(--pri) 18%,transparent)}
#gsr{position:absolute;top:100%;left:0;right:0;background:var(--clay);border:0;border-radius:24px;box-shadow:var(--sh);margin-top:8px;display:none;z-index:30;overflow:hidden}
#gsr a{display:flex;justify-content:space-between;gap:10px;padding:11px 16px;cursor:pointer;border-bottom:1px solid var(--line);text-decoration:none;color:var(--ink)}#gsr a:hover{background:var(--hov)}
.grow{flex:1}#burger{display:none}
.ib{background:var(--btn2);border:0;border-radius:980px;padding:9px 16px;cursor:pointer;color:var(--ink);font:inherit;font-size:13px;font-weight:600;white-space:nowrap;box-shadow:var(--btn2-sh);transition:transform .15s var(--ease-out),box-shadow .15s var(--ease-out)}.ib:hover{transform:translateY(-2px)}.ib:active{transform:translateY(1px);box-shadow:var(--sh-in)}
.ib b{background:var(--bad);color:#fff;border-radius:980px;padding:0 6px;font-size:11px;margin-left:5px;font-weight:700;box-shadow:inset 0 1px 1px rgba(255,255,255,.4)}
.me{padding:12px;border:0;border-radius:24px;margin-top:auto;background:var(--clay);box-shadow:var(--sh-sm)}
.me-info b{display:block;font-size:13px;font-weight:600;line-height:1.3;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.me-info small{display:block;font-size:12px;color:var(--mut);margin-top:1px}
.me-actions{display:flex;gap:6px;margin-top:10px}.me-actions .ib{flex:1;padding:6px 8px;font-size:12px;box-shadow:var(--btn2-sh);background:var(--btn2)}
.page{padding:28px 24px 64px;max-width:1440px;margin:0 auto}
.ph{display:flex;justify-content:space-between;gap:16px;flex-wrap:wrap;align-items:center;margin-bottom:28px}
.ph h2{margin:0;font-family:var(--font-display);font-size:34px;font-weight:800;line-height:1.05;letter-spacing:-.04em;text-wrap:balance}.mut{color:var(--mut)}.ph p{margin:7px 0 0;font-size:15px;line-height:1.4;letter-spacing:-.01em}.pa{display:flex;gap:10px;flex-wrap:wrap}
.kpis{display:grid;grid-template-columns:repeat(auto-fit,minmax(190px,1fr));gap:16px;margin-bottom:20px}
.kpis.k4{grid-template-columns:repeat(4,minmax(0,1fr))}
.kpis.k3{grid-template-columns:repeat(3,minmax(0,1fr))}
.kpi{padding:20px 22px;background:var(--clay);border:0;border-radius:30px;box-shadow:var(--sh)}
.kpi .l{color:var(--mut);font-size:12px;font-weight:600;letter-spacing:.01em}.kpi .v{font-family:var(--font-display);font-size:32px;font-weight:800;line-height:1.08;margin-top:5px;letter-spacing:-.035em}.kpi .s{font-size:12.5px;color:var(--mut);margin-top:4px;line-height:1.4}
.panel{background:var(--clay);border:0;border-radius:32px;box-shadow:var(--sh);padding:22px 24px;margin-bottom:20px;overflow-x:auto}
.panel h3{margin:0 0 16px;font-family:var(--font-display);font-size:17px;font-weight:600;line-height:1.3;letter-spacing:-.015em;text-wrap:balance}.g2{display:grid;grid-template-columns:1fr 1fr;gap:24px}.g3{display:grid;grid-template-columns:2fr 1fr;gap:24px}
.panel .tw{margin:0 -24px}.panel .tw:last-child{margin-bottom:-22px}
.phead{display:flex;align-items:center;justify-content:space-between;gap:12px;margin-bottom:16px}
.phead h3{margin:0}
.plink{background:none;border:0;padding:2px 0;font:inherit;font-size:12.5px;font-weight:600;color:var(--pri);cursor:pointer;letter-spacing:-.005em}
.plink:hover{text-decoration:underline;text-underline-offset:2px}
.invsum{padding:20px 24px}
.invsum .phead{margin-bottom:14px}
.invstats{display:grid;grid-template-columns:repeat(3,minmax(0,1fr))}
.invstats>div{padding:0 24px;border-left:1px solid var(--line)}
.invstats>div:first-child{padding-left:0;border-left:0}
.invstats>div:last-child{padding-right:0}
.invstats .l{display:block;color:var(--mut);font-size:12px;font-weight:600;letter-spacing:.01em}
.invstats b{display:block;font-family:var(--font-display);font-size:26px;font-weight:700;letter-spacing:-.03em;line-height:1.15;margin-top:4px;font-variant-numeric:tabular-nums}
.invstats .s{display:block;color:var(--mut);font-size:12px;margin-top:3px}
.psplit{margin-top:20px;padding-top:18px;border-top:1px solid var(--line)}
.psplit .phead{margin-bottom:14px}
table{width:100%;border-collapse:collapse}th,td{padding:11px 16px;text-align:left;border-bottom:1px solid color-mix(in srgb,var(--line) 75%,transparent);white-space:nowrap;vertical-align:middle}
th{font-size:10.5px;color:var(--mut);font-weight:700;text-transform:uppercase;letter-spacing:.08em;background:color-mix(in srgb,var(--ice) 70%,transparent)}thead th:first-child{border-radius:14px 0 0 14px}thead th:last-child{border-radius:0 14px 14px 0}th.srt{cursor:pointer;user-select:none}th.srt:hover{color:var(--pri)}th.srt:focus-visible{outline:2px solid var(--pri);outline-offset:-2px}
tbody tr:last-child td{border-bottom:0}
td{font-size:13.5px}
td.r,th.r{text-align:right}tbody tr:hover td{background:var(--hov)}tbody tr:focus-within td{background:var(--hov)}td.wrap{white-space:normal;min-width:160px}td.act{white-space:nowrap;min-width:100px}
.tact{display:flex;gap:16px;align-items:center;white-space:nowrap}
.tact a,.tact button{background:none;border:0;padding:3px 0;font:inherit;font-size:13px;font-weight:600;color:var(--pri);cursor:pointer;text-decoration:none;white-space:nowrap}
.tact a:hover,.tact button:hover{text-decoration:underline;text-underline-offset:2px}
.tact .danger{color:var(--bad)}
.si{margin-left:4px;vertical-align:-1px;opacity:.75}
.kpi .v,td.r,th.r,.tot,.stats b,.hb b,.chart text{font-variant-numeric:tabular-nums}
.sub{color:var(--mut);font-size:12px;line-height:1.4}.empty,.load{padding:40px 28px;text-align:center;color:var(--mut);font-size:14px}
.badge{display:inline-block;padding:4px 11px;border-radius:980px;font-size:11.5px;font-weight:700;letter-spacing:.005em;white-space:nowrap;box-shadow:inset 0 1px 1px rgba(255,255,255,.35)}
.OK,.IN,.RECEIVED,.PAID,.RELEASED,.low-sev{background:color-mix(in srgb,var(--ok) 15%,transparent);color:var(--ok)}
.LOW,.ADJUST,.PARTIAL,.ORDERED,.ACTIVE,.A,.med{background:color-mix(in srgb,var(--warn) 16%,transparent);color:var(--warn)}
.OUT,.VOID,.CANCELLED,.high,.overdue{background:color-mix(in srgb,var(--bad) 14%,transparent);color:var(--bad)}
.SALE,.DRAFT,.B,.C{background:color-mix(in srgb,var(--pri) 13%,transparent);color:var(--pri)}
.neg{color:var(--bad)}.pos{color:var(--ok)}.warn{color:var(--warn)}.promo{color:var(--bad)}
.b{background:var(--btn);color:#fff;border:0;border-radius:980px;padding:11px 20px;cursor:pointer;font:inherit;font-size:13px;font-weight:700;letter-spacing:-.005em;white-space:nowrap;text-decoration:none;display:inline-block;box-shadow:var(--btn-sh);transition:transform .15s var(--ease-out),box-shadow .15s var(--ease-out),filter .15s var(--ease-out)}
.b:hover{transform:translateY(-1px);filter:brightness(1.04)}.b:active{transform:translateY(1px);box-shadow:inset 0 2px 6px rgba(40,60,140,.35)}.b.g{background:var(--btn2);border:0;color:var(--ink);box-shadow:var(--btn2-sh)}.b.g:hover{background:var(--btn2);filter:brightness(1.02)}
.b.d{background:var(--btn-danger);color:#fff;box-shadow:var(--btn-danger-sh)}.b.sm{padding:6px 12px;font-size:12px;margin:0}.b:disabled{opacity:.5;cursor:not-allowed;transform:none;filter:none}
.b:focus-visible,.ib:focus-visible,.chip:focus-visible,.x:focus-visible,.tact button:focus-visible,.tact a:focus-visible{outline:2px solid var(--pri);outline-offset:2px}
input,select,textarea{padding:10px 14px;border:0;border-radius:16px;font:inherit;font-size:13.5px;background:var(--ice);color:var(--ink);box-shadow:var(--sh-in)}
input:focus,select:focus,textarea:focus{outline:0;box-shadow:var(--sh-in),0 0 0 4px color-mix(in srgb,var(--pri) 18%,transparent)}
::placeholder{color:var(--mut);opacity:1}
code{font:12px ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;background:color-mix(in srgb,var(--pri) 9%,transparent);border:0;border-radius:10px;padding:1px 6px;color:var(--ink)}
.bar{display:flex;gap:10px;margin-bottom:18px;flex-wrap:wrap;align-items:center}.bar input[type=search]{flex:1;min-width:220px}
.chip{background:var(--btn2);border:0;border-radius:980px;padding:9px 16px;cursor:pointer;font:inherit;font-size:13px;font-weight:600;color:var(--ink);box-shadow:var(--btn2-sh);transition:transform .15s var(--ease-out),box-shadow .15s var(--ease-out),filter .15s var(--ease-out)}
.chip:hover{transform:translateY(-1px);filter:brightness(1.02)}
.chip.on{background:var(--btn);color:#fff;border-color:transparent;box-shadow:var(--btn-sh)}
dialog{border:0;border-radius:38px;padding:0;width:min(500px,94vw);background:var(--clay);color:var(--ink);box-shadow:0 40px 90px -32px rgba(51,45,86,.5),inset 0 2px 2px rgba(255,255,255,.9)}[data-theme=dark] dialog{box-shadow:0 40px 90px -32px rgba(0,0,0,.8),inset 0 2px 2px rgba(255,255,255,.08)}
dialog.wide{width:min(920px,96vw)}dialog::backdrop{background:rgba(40,50,80,.32);-webkit-backdrop-filter:blur(8px);backdrop-filter:blur(8px)}
dialog[open]{animation:dlg-in .18s var(--ease-out)}dialog[open]::backdrop{animation:bd-in .18s ease}
@keyframes dlg-in{from{opacity:0;transform:translateY(8px) scale(.985)}}@keyframes bd-in{from{opacity:0}}
.mh{display:flex;justify-content:space-between;align-items:center;padding:22px 28px 12px}.mh h3{margin:0;font-family:var(--font-display);font-size:21px;font-weight:700;letter-spacing:-.025em}
.x{background:var(--btn2);border:0;border-radius:50%;width:32px;height:32px;display:grid;place-items:center;cursor:pointer;color:var(--mut);box-shadow:var(--btn2-sh);transition:transform .15s var(--ease-out),box-shadow .15s var(--ease-out)}.x:hover{transform:translateY(-1px);color:var(--ink)}.x:active{transform:translateY(1px);box-shadow:var(--sh-in)}
.mb{padding:4px 28px;max-height:72vh;overflow:auto}
.acts{display:flex;justify-content:flex-end;gap:10px;padding:18px 28px 26px}
.acts.sale-actions{justify-content:space-between;align-items:center}
.mactions{display:flex;gap:8px;flex-wrap:wrap;margin:18px 0 4px;padding-top:18px;border-top:1px solid var(--line)}
label{display:block;font-size:12px;color:var(--mut);margin:12px 0 6px;font-weight:600}.mb input,.mb select,.mb textarea{width:100%}
.r2{display:grid;grid-template-columns:1fr 1fr;gap:12px}.r3{display:grid;grid-template-columns:repeat(3,1fr);gap:12px}
.note{background:color-mix(in srgb,var(--pri) 7%,var(--panel));border:0;border-radius:24px;padding:14px 17px;margin-bottom:16px;font-size:13.5px;line-height:1.45;box-shadow:var(--sh-sm)}
.note.bad{background:color-mix(in srgb,var(--bad) 9%,var(--panel));border-color:color-mix(in srgb,var(--bad) 18%,var(--edge))}
.sg{display:grid;grid-template-columns:repeat(auto-fill,minmax(240px,1fr));gap:14px}.card{background:var(--clay);border:0;border-radius:26px;box-shadow:var(--sh-sm);padding:18px}
.card b{display:block}
.stats{display:grid;grid-template-columns:repeat(auto-fill,minmax(130px,1fr));gap:16px 22px;margin:16px 0;padding:16px 0;border-top:1px solid var(--line);border-bottom:1px solid var(--line)}
.stats div{padding:0;background:none;border-radius:0}
.stats small{display:block;color:var(--mut);font-size:12px}.stats b{font-size:17px;font-weight:700}
.posg{display:grid;grid-template-columns:1.25fr 1fr;gap:20px;align-items:start}.cartp{position:sticky;top:74px}
.pi{display:grid;grid-template-columns:minmax(0,1fr) 130px 88px;gap:12px;align-items:center;padding:12px 2px;border-bottom:1px solid var(--line)}
.pi>div:nth-child(2){text-align:right;white-space:nowrap;font-variant-numeric:tabular-nums}
.pi>div:last-child{justify-self:end}
.pi:last-child{border:0}.pi.dis{opacity:.55}#plist{margin-top:10px;max-height:66vh;overflow:auto}
.cl{display:grid;grid-template-columns:1fr 60px 104px 26px;gap:6px;align-items:center;padding:10px 0;border-bottom:1px solid var(--line)}
.cl input{padding:5px 7px;width:100%;text-align:right}.tot{margin-top:12px}.tot div{display:flex;justify-content:space-between;padding:4px 0}
.tot .big{font-size:21px;font-weight:700;letter-spacing:-.02em;border-top:1px solid var(--line);margin-top:6px;padding-top:10px}
.ln{display:grid;grid-template-columns:1fr 80px 110px 30px;gap:6px;margin:5px 0}
.cartp input,.cartp select{width:100%}
.chart{width:100%;height:auto}.chart text{fill:var(--mut);font-size:11px}.chart .gl{stroke:var(--line);stroke-dasharray:2 4;opacity:.8}
.chart .ln1{stroke:var(--c1)}.chart .ln2{stroke:var(--c2)}.chart path{fill:none;stroke-width:2.8;stroke-linejoin:round;stroke-linecap:round}.chart .ar{stroke:none;opacity:1;pointer-events:none}
.chart circle.d1{fill:var(--c1)}.chart circle.d2{fill:var(--c2)}.chart circle.d1,.chart circle.d2{stroke:var(--panel);stroke-width:2}.lgd{display:flex;gap:16px;font-size:12px;color:var(--mut);margin-bottom:10px}.lgd i{display:inline-block;width:10px;height:10px;border-radius:50%;margin-right:6px;vertical-align:middle;box-shadow:inset 0 1px 1px rgba(255,255,255,.5)}
.hbs{display:flex;flex-direction:column;gap:8px}.hb{display:grid;grid-template-columns:minmax(120px,160px) 1fr 90px;gap:10px;align-items:center;font-size:12.5px}
.hl{overflow:hidden;text-overflow:ellipsis;white-space:nowrap}.hbar{background:color-mix(in srgb,var(--ice) 85%,var(--panel));height:7px;border-radius:980px;overflow:hidden;box-shadow:var(--sh-in)}
.hbar i{display:block;height:100%;background:linear-gradient(90deg,var(--c1),color-mix(in srgb,var(--c1) 65%,#fff));border-radius:980px;box-shadow:inset 0 1px 1px rgba(255,255,255,.4)}
.hbar i.c2{background:linear-gradient(90deg,var(--c2),color-mix(in srgb,var(--c2) 65%,#fff))}.hbar i.c3{background:linear-gradient(90deg,var(--c3),color-mix(in srgb,var(--c3) 65%,#fff))}.hb b{text-align:right;font-weight:600}
.don{display:flex;gap:18px;align-items:center;flex-wrap:wrap}.don svg{width:120px;height:120px;filter:drop-shadow(0 8px 14px rgba(51,104,160,.18))}.don circle{fill:none}.don circle.seg{stroke-linecap:round;transition:stroke-width .12s}.don circle.seg:hover{stroke-width:22}.dc{font-size:22px;font-weight:700;fill:var(--ink)!important}
.leg div{margin:4px 0;font-size:13px}.leg i{display:inline-block;width:9px;height:9px;border-radius:50%;margin-right:6px;box-shadow:inset 0 1px 1px rgba(255,255,255,.5)}
.al{display:flex;gap:12px;align-items:flex-start;padding:16px 18px;border-radius:26px;border:0;background:var(--clay);box-shadow:var(--sh-sm);margin-bottom:12px;cursor:pointer;transition:background .15s var(--ease-out),box-shadow .15s var(--ease-out)}
.al .dot{width:8px;height:8px;border-radius:50%;margin-top:6px;flex:none;background:var(--mut);box-shadow:0 0 0 4px color-mix(in srgb,var(--mut) 12%,transparent)}
.al.high .dot{background:var(--bad);box-shadow:0 0 0 4px color-mix(in srgb,var(--bad) 12%,transparent)}.al.med .dot{background:var(--warn);box-shadow:0 0 0 4px color-mix(in srgb,var(--warn) 12%,transparent)}.al.low .dot{background:var(--ok);box-shadow:0 0 0 4px color-mix(in srgb,var(--ok) 12%,transparent)}
.al:hover{background:color-mix(in srgb,var(--pri) 4%,var(--panel));box-shadow:var(--sh)}.al .albody{flex:1;min-width:0}.al .alhead{display:flex;align-items:center;gap:8px;flex-wrap:wrap}.al b{display:block;font-weight:600}.al .chev{margin-left:auto;align-self:center;color:var(--mut);flex:none}.al:focus-visible{outline:2px solid var(--pri);outline-offset:2px}
.badge.type{background:rgba(120,120,128,.10);color:var(--mut);font-weight:500;box-shadow:inset 0 1px 1px rgba(255,255,255,.35)}
#toast{position:fixed;bottom:24px;left:50%;transform:translate(-50%,8px);background:var(--ink);color:var(--panel);padding:12px 20px;border-radius:22px;z-index:99;max-width:min(420px,calc(100vw - 32px));font-size:13.5px;box-shadow:0 20px 44px -18px rgba(51,45,86,.6),inset 0 1px 1px rgba(255,255,255,.16);opacity:0;visibility:hidden;pointer-events:none;transition:opacity .18s var(--ease-out),transform .18s var(--ease-out),visibility .18s}#toast.show{opacity:1;visibility:visible;transform:translate(-50%,0)}#toast.err{background:var(--bad);color:#fff}
#banner .note{margin:12px 32px 0}
@media(max-width:1360px){.g3{grid-template-columns:1fr}}
@media(max-width:1180px){.g2,.posg{grid-template-columns:1fr}.cartp{position:static}.kpis.k4,.kpis.k3{grid-template-columns:repeat(2,minmax(0,1fr))}}
@media(max-width:820px){.app{grid-template-columns:1fr;padding:10px}.main{padding:0}.top{margin-top:0;border-radius:18px;padding:10px 16px;gap:8px}.page{padding:22px 16px 60px}.kpis,.kpis.k4,.kpis.k3{grid-template-columns:repeat(2,minmax(0,1fr))}.ph h2{font-size:29px}}
@media(max-width:560px){.r2,.r3{grid-template-columns:1fr}.page{padding:20px 14px 56px}.ph{margin-bottom:22px}.ph h2{font-size:27px}.kpi{padding:17px 18px}.kpi .v{font-size:27px}.cl{grid-template-columns:1fr 52px 90px 26px}.ln{grid-template-columns:1fr 70px 96px 26px}.top .gs input{padding:10px 12px 10px 36px;font-size:13px}.invstats{grid-template-columns:1fr}.invstats>div{padding:12px 0;border-left:0;border-top:1px solid var(--line)}.invstats>div:first-child{padding-top:0;border-top:0}.invstats>div:last-child{padding-bottom:0}th,td{padding:9px 8px}th{font-size:9.5px}td{font-size:12.5px}td.wrap{min-width:100px}}
@media print{#side,.top,.pa,.bar{display:none!important}.app{display:block}}
/* ---- form control polish ---- */
input[type=number]{-moz-appearance:textfield;appearance:textfield}
input[type=number]::-webkit-inner-spin-button,input[type=number]::-webkit-outer-spin-button{-webkit-appearance:none;margin:0}
select{-webkit-appearance:none;-moz-appearance:none;appearance:none;padding-right:34px;cursor:pointer;background-image:url("data:image/svg+xml;utf8,<svg xmlns='http://www.w3.org/2000/svg' width='12' height='12' viewBox='0 0 12 12'><path d='M2.5 4.5 6 8l3.5-3.5' fill='none' stroke='%238e8e93' stroke-width='1.8' stroke-linecap='round' stroke-linejoin='round'/></svg>");background-repeat:no-repeat;background-position:right 13px center;background-size:12px}
input[type=search]::-webkit-search-cancel-button{-webkit-appearance:none;height:14px;width:14px;border-radius:50%;cursor:pointer;background:rgba(142,142,147,.55) url("data:image/svg+xml;utf8,<svg xmlns='http://www.w3.org/2000/svg' width='14' height='14' viewBox='0 0 14 14'><path d='M4.5 4.5l5 5m0-5l-5 5' stroke='white' stroke-width='1.6' stroke-linecap='round'/></svg>")}
input[type=date]{min-height:38px}
input[type=date]::-webkit-calendar-picker-indicator{opacity:.55;cursor:pointer;border-radius:6px}
input[type=checkbox],input[type=radio]{accent-color:var(--pri)}
/* ---- number stepper + scrollbars ---- */
.numw{position:relative;display:inline-block;vertical-align:middle}.mb .numw,.r2 .numw,.r3 .numw{display:block}
.numw input{padding-right:36px}
.stp{position:absolute;right:7px;top:50%;transform:translateY(-50%);display:flex;flex-direction:column;gap:2px}
.stp button{width:22px;height:13px;border:0;padding:0;border-radius:7px;cursor:pointer;background:color-mix(in srgb,var(--pri) 15%,#fff);display:grid;place-items:center;color:var(--mut);transition:background .12s,color .12s}
.stp button:hover{background:var(--pri);color:#fff}[data-theme=dark] .stp button{background:color-mix(in srgb,var(--pri) 22%,#25224a)}.stp button:active{transform:scale(.92)}
.stp svg{width:9px;height:9px;fill:none;stroke:currentColor;stroke-width:2;stroke-linecap:round;stroke-linejoin:round}
::-webkit-scrollbar{width:12px;height:12px}
::-webkit-scrollbar-track{background:transparent}
::-webkit-scrollbar-thumb{background:rgba(120,120,128,.38);border:3px solid transparent;background-clip:content-box;border-radius:999px}
::-webkit-scrollbar-thumb:hover{background:rgba(120,120,128,.6);background-clip:content-box;border:3px solid transparent}
::-webkit-scrollbar-corner{background:transparent}
@supports not selector(::-webkit-scrollbar){*{scrollbar-width:thin;scrollbar-color:rgba(120,120,128,.5) transparent}}
@media(max-width:560px){.top{flex-wrap:wrap;row-gap:8px}.top .gs{order:3;flex:1 0 100%;max-width:none}.top .grow{display:none}.top .ib{padding:6px 9px}}
@media(prefers-reduced-motion:reduce){*,*::before,*::after{animation-duration:.01ms!important;transition-duration:.01ms!important;scroll-behavior:auto!important}}
/* ---- chart hover ---- */
.chart{cursor:crosshair;touch-action:pan-y;-webkit-user-select:none;user-select:none}
.chart .xh{stroke:var(--mut);stroke-width:1;stroke-dasharray:4 4;opacity:.7;visibility:hidden;pointer-events:none}
.chart circle.hv{stroke:var(--panel);stroke-width:2.5;visibility:hidden;pointer-events:none}
.chart circle.hv1{fill:var(--c1)}.chart circle.hv2{fill:var(--c2)}
.don circle.seg{cursor:pointer;transition:stroke-width .12s}.don circle.seg:hover{stroke-width:27}
.leg div{cursor:default}
.hb{padding:3px 6px;margin:-3px -6px;border-radius:10px;cursor:default;transition:background .12s}.hb:hover{background:var(--hov)}
#tip{position:fixed;z-index:120;left:0;top:0;display:none;pointer-events:none;min-width:150px;max-width:280px;padding:12px 15px;border-radius:22px;font-size:12.5px;line-height:1.5;color:var(--ink);background:var(--clay);border:0;box-shadow:var(--sh)}
#tip .th{font-weight:600;margin-bottom:4px;display:flex;align-items:center;gap:7px}
#tip .tr{display:flex;justify-content:space-between;gap:18px;color:var(--mut)}#tip .tr b{color:var(--ink);font-weight:600;white-space:nowrap}
#tip i{display:inline-block;width:9px;height:9px;border-radius:50%;flex:none}#tip .tr i{margin-right:6px}

/* ---- logo ---- */
.lg{display:block;width:100%;height:auto}
.logo{display:block;text-align:center;padding:4px 6px 14px}.logo .lg{width:196px;max-width:100%;margin:0 auto}
.logo small{max-width:none;margin-top:4px}
[data-theme=dark] .logo .lg{background:#f4f7fc;border-radius:22px;padding:8px 10px;box-sizing:border-box}
/* ---- hidden sidebar: slides out when the mouse nears the left edge ---- */
#side{position:fixed;top:12px;left:12px;z-index:60;transform:translateX(calc(-100% - 40px));opacity:0;visibility:hidden;transition:transform .28s var(--ease-out),opacity .22s var(--ease-out),visibility 0s linear .28s;will-change:transform}
#side.open,#side:focus-within{transform:none;opacity:1;visibility:visible;transition:transform .28s var(--ease-out),opacity .22s var(--ease-out),visibility 0s}
#hz{position:fixed;left:0;top:0;bottom:0;width:18px;z-index:55}
#edge{position:fixed;left:5px;top:50%;width:5px;height:72px;margin-top:-36px;border-radius:5px;z-index:54;pointer-events:none;background:linear-gradient(180deg,#3368A0,#421454);opacity:.5;transition:opacity .2s,transform .2s}
body.sideopen #edge{opacity:0;transform:translateX(-8px)}
#burger{display:inline-block}
@media(hover:hover) and (pointer:fine){#burger{display:none}}
@media print{#hz,#edge{display:none!important}}

/* ---- logo ---- */
.lg{display:block;width:100%;height:auto}
.logo{display:block;text-align:center;padding:4px 6px 14px}.logo .lg{width:196px;max-width:100%;margin:0 auto}
.logo small{max-width:none;margin-top:4px}
[data-theme=dark] .logo .lg{background:#f4f7fc;border-radius:22px;padding:8px 10px;box-sizing:border-box}
/* ---- hidden sidebar: slides out when the mouse nears the left edge ---- */
#side{position:fixed;top:12px;left:12px;z-index:60;transform:translateX(calc(-100% - 40px));opacity:0;visibility:hidden;transition:transform .28s var(--ease-out),opacity .22s var(--ease-out),visibility 0s linear .28s;will-change:transform}
#side.open,#side:focus-within{transform:none;opacity:1;visibility:visible;transition:transform .28s var(--ease-out),opacity .22s var(--ease-out),visibility 0s}
#hz{position:fixed;left:0;top:0;bottom:0;width:18px;z-index:55}
#edge{position:fixed;left:5px;top:50%;width:5px;height:72px;margin-top:-36px;border-radius:5px;z-index:54;pointer-events:none;background:linear-gradient(180deg,#3368A0,#421454);opacity:.5;transition:opacity .2s,transform .2s}
body.sideopen #edge{opacity:0;transform:translateX(-8px)}
#burger{display:inline-block}
@media(hover:hover) and (pointer:fine){#burger{display:none}}
@media print{#hz,#edge{display:none!important}}
</style></head><body>
<div id="hz" aria-hidden="true"></div><div id="edge" aria-hidden="true"></div>
<div class="app">
  <aside id="side">
    <div class="logo"><img class="lg" src="/logo.png" alt="__APP__"><small id="shop"></small></div>
    <nav id="nav"></nav>
    <div class="me" id="me"></div>
  </aside>
  <div class="main">
    <header class="top">
      <button class="ib" id="burger" data-act="burger">Menu</button>
      <div class="gs"><span class="gsi" aria-hidden="true"><svg viewBox="0 0 16 16" width="15" height="15" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round"><circle cx="7" cy="7" r="4.2"/><path d="m10.2 10.2 3 3"/></svg></span><input id="gs" type="search" aria-label="Search products" placeholder="Quick search products (Ctrl+K)" autocomplete="off"><div id="gsr"></div></div>
      <span class="grow"></span>
      <button class="ib" id="bell" data-act="go" data-id="alerts" style="display:none" title="Risk Center">Alerts <b id="belln">0</b></button>
      <button class="ib" id="themeBtn" data-act="theme" title="Switch to dark mode" aria-label="Switch to dark mode">Dark mode</button>
    </header>
    <div id="banner"></div>
    <main id="view" class="page"></main>
  </div>
</div>
<dialog id="dlg"><form id="dform"></form></dialog>
<div id="toast"></div>
<div id="tip"></div>
<script>
'use strict';
const $=(s,r=document)=>r.querySelector(s), $$=(s,r=document)=>[...r.querySelectorAll(s)];
const esc=s=>String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const peso=n=>'₱'+Number(n||0).toLocaleString('en-PH',{maximumFractionDigits:2});
const num=n=>Number(n||0).toLocaleString('en-PH');
const PH_TZ='Asia/Manila';
const parseDT=s=>{let t=String(s||'').trim();if(!t)return null;if(/^\d{4}-\d{2}-\d{2}$/.test(t))t+='T00:00:00Z';else{t=t.replace(' ','T');if(!/(?:Z|[+-]\d{2}:?\d{2})$/i.test(t))t+='Z'}const d=new Date(t);return isNaN(d)?null:d};
const localDT=(d,o)=>d.toLocaleString('en-GB',{timeZone:PH_TZ,...o}).replace(/\b(am|pm)\b/i,x=>x.toUpperCase());
const dtm=s=>{const d=parseDT(s);return d?localDT(d,{day:'2-digit',month:'short',year:'numeric',hour:'numeric',minute:'2-digit',hour12:true}):String(s||'')};
const d10=s=>{const d=parseDT(s);return d?d.toLocaleDateString('en-GB',{timeZone:PH_TZ,day:'2-digit',month:'short',year:'numeric'}):String(s||'')};
const dts=s=>{const d=parseDT(s);return d?localDT(d,{day:'2-digit',month:'short',hour:'numeric',minute:'2-digit',hour12:true}):String(s||'')};
const npl=(n,w)=>`${num(n)} ${w}${Number(n)===1?'':'s'}`;
const xIcon='<svg viewBox="0 0 16 16" width="14" height="14" aria-hidden="true"><path d="M4 4l8 8M12 4l-8 8" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round"/></svg>';
const chevIcon='<svg class="chev" viewBox="0 0 16 16" width="14" height="14" aria-hidden="true"><path d="M6 3.5 10.5 8 6 12.5" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"/></svg>';
const sortIcon=d=>`<svg class="si" viewBox="0 0 10 10" width="9" height="9" aria-hidden="true"><path d="${d>0?'M2 6.5 5 3.5l3 3':'M2 3.5 5 6.5l3-3'}" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"/></svg>`;
const kfmt=v=>v>=1e6?(v/1e6).toFixed(v%1e6?1:0)+'M':v>=1e3?(v/1e3).toFixed(v%1e3?1:0)+'k':String(v);
let ME=null; const ACT={},CHG={},V={},TBL={}; let _tid=0,PC={};
const can=(...r)=>r.includes(ME.role);
const cacheP=l=>{l.forEach(p=>PC[p.id]=p)};

async function api(path,method='GET',body){
  const o={method,headers:{'X-Requested-With':'fetch'}};
  if(method!=='GET'){o.headers['Content-Type']='application/json';o.body=JSON.stringify(body||{})}
  const r=await fetch('/api'+path,o);
  if(r.status===401){location.href='/';throw new Error('Signed out')}
  let d={};try{d=await r.json()}catch(e){}
  if(!r.ok)throw new Error(d.error||'Something went wrong');
  return d;
}
function toast(m,err){const t=$('#toast');t.textContent=m;t.className=err?'show err':'show';clearTimeout(t._t);t._t=setTimeout(()=>t.classList.remove('show'),4200)}
async function run(fn){try{return await fn()}catch(e){toast(e.message,true)}}
function closeModal(){const d=$('#dlg');if(d.open)d.close()}
function modal(title,html,onSubmit,okText,wide,actionsHtml){
  const f=$('#dform'); $('#dlg').className=wide?'wide':'';
  f.innerHTML=`<div class="mh"><h3>${title}</h3><button type="button" class="x" data-act="close" aria-label="Close">${xIcon}</button></div><div class="mb">${html}</div><div class="acts ${actionsHtml?'sale-actions':''}">${actionsHtml||(onSubmit?`<button type="button" class="b g" data-act="close">Cancel</button><button type="submit" class="b">${okText||'Save'}</button>`:`<button type="button" class="b g" data-act="close">Close</button>`)}</div>`;
  f.onsubmit=ev=>{ev.preventDefault();if(!onSubmit)return;const o=Object.fromEntries(new FormData(f));run(async()=>{const r=await onSubmit(o,f);if(r!==false)closeModal()})};
  if(!$('#dlg').open)$('#dlg').showModal();
}
/* ---------- tiny UI kit ---------- */
const head=(t,sub,act)=>`<div class="ph"><div><h2>${t}</h2>${sub?`<p class="mut">${sub}</p>`:''}</div><div class="pa">${act||''}</div></div>`;
const kpi=(l,v,s,c)=>`<div class="kpi"><div class="l">${l}</div><div class="v ${c||''}">${v}</div>${s?`<div class="s">${s}</div>`:''}</div>`;
const badge=(s,t)=>`<span class="badge ${esc(s)}">${esc(t||s)}</span>`;
const stTxt={OK:'In stock',LOW:'Low stock',OUT:'Out of stock'};
const pbadge=p=>badge(p.status,stTxt[p.status]);
const pname=p=>`<b>${esc(p.brand)}</b> ${esc(p.model)}<div class="sub">${esc(p.sku)} · ${p.hp}HP · ${esc(p.type)}</div>`;
const priceCell=p=>p.promo_price?`<s class="mut">${peso(p.price)}</s> <b class="promo">${peso(p.promo_price)}</b>`:peso(p.price);
const coverCell=p=>p.days_cover==null?'<span class="mut">no sales yet</span>':`<span class="${p.days_cover<p.lead_days?'neg':p.days_cover<p.lead_days*2?'warn':''}">${Math.round(p.days_cover)} days</span>`;
const words=q=>String(q||'').toLowerCase().replace(/hp/g,' ').split(/\s+/).filter(Boolean);
const hay=p=>`${p.brand} ${p.model} ${p.sku} ${p.type} ${p.hp} ${p.location||''}`.toLowerCase();
const sel=(name,opts,cur,extra)=>`<select name="${name}" ${extra||''}>${opts.map(o=>{const [v,t]=Array.isArray(o)?o:[o,o];return `<option value="${esc(v)}" ${String(v)===String(cur)?'selected':''}>${esc(t)}</option>`}).join('')}</select>`;

function dtable(cols,rows,o={}){
  const id='t'+(++_tid);TBL[id]={cols,rows,sort:o.sort??null,dir:o.dir??1,empty:o.empty||'Nothing to show.'};
  return `<div class="tw" id="${id}">${tblHTML(id)}</div>`;
}
function tblHTML(id){
  const t=TBL[id];let rows=t.rows.slice();
  if(t.sort!=null){const g=t.cols[t.sort].s;if(g)rows.sort((a,b)=>{const x=g(a),y=g(b);return (x>y?1:x<y?-1:0)*t.dir})}
  if(!rows.length)return `<div class="empty">${t.empty}</div>`;
  return `<table><thead><tr>${t.cols.map((c,i)=>`<th data-ts="${id}:${i}" class="${c.cls||''} ${c.s?'srt':''}"${c.s?` tabindex="0" aria-sort="${t.sort===i?(t.dir>0?'ascending':'descending'):'none'}"`:''}>${c.h}${t.sort===i?sortIcon(t.dir):''}</th>`).join('')}</tr></thead><tbody>${rows.map(r=>`<tr>${t.cols.map(c=>`<td class="${c.cls||''}">${c.f(r)}</td>`).join('')}</tr>`).join('')}</tbody></table>`;
}
const TIP=$('#tip'),LC={};let _lc=0,_hc=null;
const trow=(l,v,c)=>`<div class="tr"><span>${c?`<i style="background:${c}"></i>`:''}${esc(l)}</span><b>${v}</b></div>`;
const fdate=l=>{const d=parseDT(l);return d?d.toLocaleDateString('en-PH',{timeZone:PH_TZ,weekday:'short',month:'short',day:'numeric',year:'numeric'}):l};

function lineChart(labels,series,counts,opts={}){
  const vw=innerWidth;
  let W;
  if(vw<=820)W=Math.max(300,vw-100);
  else if(vw<=1360)W=Math.max(400,vw-460);
  else W=opts.full?Math.min(1340,vw-412):Math.min(860,Math.max(560,vw-820));
  const H=vw<=560?280:300,L=vw<=560?36:48,R=vw<=560?10:14,T=18,B=vw<=560?26:30,iw=W-L-R,ih=H-T-B,id='lc'+(++_lc);
  let mx=Math.max(1,...series.flatMap(s=>s.vals));const p=Math.pow(10,Math.floor(Math.log10(mx))),f=mx/p;mx=(f<=1?1:f<=2?2:f<=5?5:10)*p;
  const x=i=>L+(labels.length>1?i*iw/(labels.length-1):iw/2),y=v=>T+ih-(v/mx)*ih;
  LC[id]={labels,series,counts:counts||null,W,xs:labels.map((_,i)=>x(i)),y};
  let g='';for(let k=0;k<=4;k++){const v=mx*k/4;g+=`<line class="gl" x1="${L}" x2="${W-R}" y1="${y(v)}" y2="${y(v)}"/><text x="${L-6}" y="${y(v)+4}" text-anchor="end">${kfmt(v)}</text>`}
  const st=Math.ceil(labels.length/(vw<=560?5:8)),idx=[];for(let i=0;i<labels.length;i+=st)idx.push(i);
  const last=labels.length-1,gap=vw<=560?40:58;
  if(idx[idx.length-1]!==last){if(x(last)-x(idx[idx.length-1])<gap)idx.pop();idx.push(last)}
  labels.forEach((l,i)=>{if(!idx.includes(i))return;const anchor=i===0?'start':i===last?'end':'middle',lx=i===0?L:i===last?W-R:x(i);g+=`<text x="${lx}" y="${H-8}" text-anchor="${anchor}">${l.slice(5)}</text>`});
  const grads=series.map((s,si)=>`<linearGradient id="${id}g${si+1}" x1="0" y1="0" x2="0" y2="1"><stop offset="0%" style="stop-color:var(--c${si+1});stop-opacity:.30"/><stop offset="100%" style="stop-color:var(--c${si+1});stop-opacity:0"/></linearGradient>`).join('');
  const paths=series.map((s,si)=>{const d=s.vals.map((v,i)=>`${i?'L':'M'}${x(i).toFixed(1)},${y(v).toFixed(1)}`).join('');
    const area=si===0?`<path class="ar" style="fill:url(#${id}g1)" d="${d}L${x(labels.length-1)},${y(0)}L${x(0)},${y(0)}Z"/>`:'';
    const dots=labels.length>40?'':s.vals.map((v,i)=>`<circle class="d${si+1}" cx="${x(i)}" cy="${y(v)}" r="3.2"/>`).join('');
    return `${area}<path class="ln${si+1}" d="${d}"/>${dots}`}).join('');
  const hov=`<line class="xh" x1="0" x2="0" y1="${T}" y2="${T+ih}"/>`+series.map((s,si)=>`<circle class="hv hv${si+1}" r="5.5" cx="0" cy="0"/>`).join('');
  return `<div class="lgd">${series.map((s,i)=>`<span><i style="background:var(--c${i+1})"></i>${s.name}</span>`).join('')}</div><svg class="chart" id="${id}" data-lc="${id}" viewBox="0 0 ${W} ${H}" role="img" aria-label="${esc(series.map(s=>s.name).join(' and '))} chart"><defs>${grads}</defs>${g}${paths}${hov}</svg>`;
}
function hbars(items,cls,vl){
  const mx=Math.max(1,...items.map(i=>i.value)),tot=items.reduce((a,i)=>a+i.value,0)||1;
  return `<div class="hbs">${items.map(i=>{
    const tip=esc(`<div class="th">${esc(i.title||i.label)}</div>`+trow(vl||'Value',i.text)+trow('Share',Math.round(i.value/tot*100)+'%')+(i.extra||''));
    return `<div class="hb" data-tip="${tip}"><span class="hl">${esc(i.label)}</span><div class="hbar"><i class="${cls||''}" style="width:${Math.max(2,i.value/mx*100)}%"></i></div><b>${i.text}</b></div>`}).join('')}</div>`}
function donut(parts,center,unit){const tot=parts.reduce((a,p)=>a+p.value,0)||1,R=54,C=2*Math.PI*R;let off=0;
  const tip=p=>esc(`<div class="th"><i style="background:${p.color}"></i>${esc(p.label)}</div>`+trow(unit||'Count',p.value)+trow('Share',Math.round(p.value/tot*100)+'%')+(p.extra||''));
  const segs=parts.filter(p=>p.value>0).map(p=>{const len=p.value/tot*C;const s=`<circle class="seg" r="${R}" cx="70" cy="70" style="stroke:${p.color}" stroke-width="22" stroke-dasharray="${len} ${C-len}" stroke-dashoffset="${-off}" transform="rotate(-90 70 70)" data-tip="${tip(p)}"/>`;off+=len;return s}).join('');
  return `<div class="don"><svg viewBox="0 0 140 140" role="img" aria-label="${esc(parts.map(p=>p.label+': '+p.value).join(', '))}">${segs}<text x="70" y="76" text-anchor="middle" class="dc">${center}</text></svg><div class="leg">${parts.map(p=>`<div data-tip="${tip(p)}"><i style="background:${p.color}"></i>${esc(p.label)} <b>${p.value}</b></div>`).join('')}</div></div>`}

/* ---------- chart hover tooltip ---------- */
function tipShow(html,cx,cy,mid){
  if(TIP._h!==html){TIP.innerHTML=html;TIP._h=html}
  TIP.style.display='block';
  const w=TIP.offsetWidth,h=TIP.offsetHeight;
  let x=cx+16,y=mid?cy-h/2:cy+16;
  if(x+w>innerWidth-8)x=cx-w-16;
  x=Math.max(8,x);y=Math.max(8,Math.min(y,innerHeight-h-8));
  TIP.style.left=x+'px';TIP.style.top=y+'px';
}
function tipHide(){
  if(_hc){const xh=$('.xh',_hc);if(xh)xh.style.visibility='hidden';$$('.hv',_hc).forEach(d=>d.style.visibility='hidden');_hc=null}
  TIP.style.display='none';
}
function tipMove(e){
  const t=e.target;if(!t||!t.closest)return;
  const lc=t.closest('svg[data-lc]');
  if(_hc&&_hc!==lc)tipHide();
  if(lc){
    const c=LC[lc.dataset.lc];if(!c||!c.xs.length)return;
    const r=lc.getBoundingClientRect(),k=c.W/r.width,n=c.xs.length;
    const step=n>1?(c.xs[n-1]-c.xs[0])/(n-1):1;
    const i=n>1?Math.max(0,Math.min(n-1,Math.round(((e.clientX-r.left)*k-c.xs[0])/step))):0;
    const px=c.xs[i];_hc=lc;
    const xh=$('.xh',lc);xh.setAttribute('x1',px);xh.setAttribute('x2',px);xh.style.visibility='visible';
    $$('.hv',lc).forEach((d,si)=>{d.setAttribute('cx',px);d.setAttribute('cy',c.y(c.series[si].vals[i]));d.style.visibility='visible'});
    const top=Math.min(...c.series.map(s=>c.y(s.vals[i])));
    tipShow(`<div class="th">${esc(fdate(c.labels[i]))}</div>`+c.series.map((s,si)=>trow(s.name,peso(s.vals[i]),`var(--c${si+1})`)).join('')+(c.counts?trow('Invoices',num(c.counts[i])):''),r.left+px/k,r.top+top/k,true);
    return;
  }
  const el=t.closest('[data-tip]');
  if(el)tipShow(el.dataset.tip,e.clientX,e.clientY);else TIP.style.display='none';
}
document.addEventListener('pointermove',tipMove);
document.addEventListener('pointerdown',tipMove);
document.documentElement.addEventListener('mouseleave',tipHide);
window.addEventListener('scroll',tipHide,{passive:true});

/* ---------- navigation ---------- */
const ALL=['owner','employee','purchasing'],O=['owner'],OE=['owner','employee'],OP=['owner','purchasing'];
const NAV=[['#','Overview'],['dash','Dashboard',ALL],['#','Sell'],['pos','New Sale',OE],['sales','Sales History',OE],['sub','Substitute Finder',ALL],
 ['#','Stock'],['inv','Inventory',ALL],['low','Restock Radar',ALL],['dead','Dead Stock Recovery',O],
 ['#','Buy'],['reorder','Reorder Simulator',OP],
 ['#','Control'],['rep','Reports',O],['alerts','Risk Center',O],['audit','Audit Trail',O],['users','Users & Settings',O]];
const NAVI={
 dash:'<svg viewBox="0 0 20 20" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"><rect x="3" y="3" width="6" height="6" rx="1.5"/><rect x="11" y="3" width="6" height="6" rx="1.5"/><rect x="3" y="11" width="6" height="6" rx="1.5"/><rect x="11" y="11" width="6" height="6" rx="1.5"/></svg>',
 pos:'<svg viewBox="0 0 20 20" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"><path d="M3 3.5h2l1.7 9.1a1.4 1.4 0 0 0 1.4 1.1h6.1a1.4 1.4 0 0 0 1.4-1.1L17 7H5.6"/><circle cx="8.4" cy="16.4" r="1.1"/><circle cx="14.6" cy="16.4" r="1.1"/></svg>',
 sales:'<svg viewBox="0 0 20 20" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"><path d="M6 2.8h8a1 1 0 0 1 1 1v13.4l-2.2-1.5-2.3 1.5-2.3-1.5L6 17.2V3.8a1 1 0 0 1 1-1Z"/><path d="M8 7h4M8 10.2h4"/></svg>',
 sub:'<svg viewBox="0 0 20 20" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"><path d="M3.5 7h11.8l-3.2-3.2M16.5 13H4.7l3.2 3.2"/></svg>',
 inv:'<svg viewBox="0 0 20 20" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"><path d="m10 2.8 6.2 3.4v7.6L10 17.2l-6.2-3.4V6.2L10 2.8Z"/><path d="m3.8 6.2 6.2 3.4 6.2-3.4M10 9.6v7.6"/></svg>',
 low:'<svg viewBox="0 0 20 20" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"><path d="M3.8 15.2a6.7 6.7 0 1 1 12.4 0"/><path d="m10 15 3.1-4.3"/><circle cx="10" cy="15.1" r="1"/></svg>',
 dead:'<svg viewBox="0 0 20 20" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"><path d="M3.4 4.2h6.2l6.9 6.9a1.4 1.4 0 0 1 0 2l-3.4 3.4a1.4 1.4 0 0 1-2 0L4.2 9.6V4.2Z"/><circle cx="6.8" cy="6.8" r="1"/></svg>',
 reorder:'<svg viewBox="0 0 20 20" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"><path d="M3.5 6.2h13M3.5 13.8h13"/><circle cx="8" cy="6.2" r="1.8"/><circle cx="13" cy="13.8" r="1.8"/></svg>',
 rep:'<svg viewBox="0 0 20 20" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"><path d="M3.5 16.5h13"/><path d="M6 16.5v-5M10 16.5V6M14 16.5V9.5"/></svg>',
 alerts:'<svg viewBox="0 0 20 20" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"><path d="M10 3.2 17 16H3L10 3.2Z"/><path d="M10 7.8v3.8"/><circle cx="10" cy="14" r=".9" fill="currentColor" stroke="none"/></svg>',
 audit:'<svg viewBox="0 0 20 20" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"><circle cx="10" cy="10" r="6.8"/><path d="M10 5.8V10l2.8 1.7"/></svg>',
 users:'<svg viewBox="0 0 20 20" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"><circle cx="8" cy="6.8" r="2.8"/><path d="M3.2 16.2c0-2.7 2.1-4.4 4.8-4.4s4.8 1.7 4.8 4.4"/><path d="M13.4 4.4a2.8 2.8 0 0 1 0 5.4M14.8 12.4c1.6.6 2.6 1.9 2.6 3.8"/></svg>'
};
let sideT=null;
function setSide(on){clearTimeout(sideT);const sd=$('#side');if(!sd)return;sd.classList.toggle('open',!!on);document.body.classList.toggle('sideopen',!!on)}
function go(t){if(location.hash==='#'+t)render();else location.hash='#'+t}
let renderVersion=0;
async function render(){
  const version=++renderVersion;
  const t=(location.hash||'#dash').slice(1);const n=NAV.find(x=>x[0]===t&&x[0]!=='#'&&x[2].includes(ME.role));const tab=n?t:'dash';
  $$('#nav a').forEach(a=>{const on=a.dataset.id===tab;a.classList.toggle('on',on);if(on)a.setAttribute('aria-current','page');else a.removeAttribute('aria-current')});(matchMedia('(hover:hover)').matches||setSide(false));
  tipHide();$('#view').innerHTML='<div class="load">Loading…</div>';window.scrollTo(0,0);
  try{await V[tab](version)}catch(e){if(version===renderVersion)$('#view').innerHTML=`<div class="panel"><b>Could not load this page.</b><p class="mut">${esc(e.message)}</p></div>`}
}
window.addEventListener('hashchange',render);
document.addEventListener('click',e=>{
  const th=e.target.closest('th[data-ts]');
  if(th){const [id,i]=th.dataset.ts.split(':');const t=TBL[id];if(!t||!t.cols[i].s)return;if(t.sort==+i)t.dir*=-1;else{t.sort=+i;t.dir=1}$('#'+id).innerHTML=tblHTML(id);return}
  const b=e.target.closest('[data-act]');
  if(b&&ACT[b.dataset.act]){e.preventDefault();run(()=>ACT[b.dataset.act](b.dataset.id,b,e))}
  if(!e.target.closest('.gs'))$('#gsr').style.display='none';
});
document.addEventListener('change',e=>{const el=e.target.closest('[data-chg]');if(el&&CHG[el.dataset.chg])run(()=>CHG[el.dataset.chg](el,e))});
document.addEventListener('keydown',e=>{
  if(e.key!=='Enter'&&e.key!==' ')return;
  const el=e.target.closest('th[data-ts],.al[data-act]');
  if(el){e.preventDefault();el.click()}
});
function syncTheme(){
  const dark=document.documentElement.dataset.theme==='dark',b=$('#themeBtn');if(!b)return;
  b.textContent=dark?'Light mode':'Dark mode';b.title=dark?'Switch to light mode':'Switch to dark mode';b.setAttribute('aria-label',b.title);
}
Object.assign(ACT,{
  go:id=>go(id),close:()=>closeModal(),burger:()=>setSide(!$('#side').classList.contains('open')),
  theme:()=>{const t=document.documentElement.dataset.theme==='dark'?'light':'dark';document.documentElement.dataset.theme=t;try{localStorage.setItem('cs-theme',t)}catch(e){}syncTheme()},
  logout:async()=>{await api('/logout','POST');location.href='/'},
  pw:()=>modal('Change my password',`<label>Current password</label><input name="old" type="password" required autocomplete="current-password"><label>New password (min 6 characters)</label><input name="new" type="password" minlength="6" required autocomplete="new-password">`,
    async o=>{await api('/password','POST',o);toast('Password changed');$('#banner').innerHTML=''},'Change password'),
});

/* ---------- global search ---------- */
let gst;
$('#gs').addEventListener('input',e=>{clearTimeout(gst);const q=e.target.value.trim();if(!q){$('#gsr').style.display='none';return}
  gst=setTimeout(()=>run(async()=>{const ps=await api('/products?limit=7&q='+encodeURIComponent(q));cacheP(ps);const b=$('#gsr');
    b.innerHTML=ps.length?ps.map(p=>`<a href="#" data-act="detail" data-id="${p.id}"><span>${pname(p)}</span><span style="text-align:right">${priceCell(p)}<div class="sub">${p.available} available</div></span></a>`).join(''):'<div class="empty">No match</div>';b.style.display='block'}),180)});
$('#gs').addEventListener('keydown',e=>{if(e.key==='Escape'){$('#gsr').style.display='none';e.target.blur()}});
document.addEventListener('keydown',e=>{if((e.ctrlKey||e.metaKey)&&e.key.toLowerCase()==='k'){e.preventDefault();$('#gs').focus();$('#gs').select()}});

/* ---------- product detail & forms ---------- */
ACT.detail=async id=>{
  $('#gsr').style.display='none';
  const d=await api('/products/'+id+'/detail'),p=d.product,cost=can('owner','purchasing');
  const mrg=cost&&p.price?`${Math.round((p.eff_price-p.cost)/p.eff_price*100)}%`:'';
  modal(esc(p.brand+' '+p.model),`<div class="sub" style="margin-bottom:6px">${esc(p.sku)} · ${p.hp}HP · ${esc(p.type)} · shelf ${esc(p.location||'-')} · warranty ${p.warranty_months} mo</div>
   <div class="stats"><div><small>On hand</small><b>${p.qty}</b></div><div><small>Available</small><b>${p.available}</b></div>
   <div><small>Sold (30 days)</small><b>${d.sold_30}</b></div><div><small>Avg / day</small><b>${p.per_day}</b></div><div><small>Days of cover</small><b>${p.days_cover==null?'-':Math.round(p.days_cover)}</b></div><div><small>Reorder point</small><b>${p.rop}</b></div>
   <div><small>Suggested order</small><b>${p.suggest_qty}</b></div><div><small>Price</small><b>${priceCell(p)}</b></div>${cost?`<div><small>Unit cost</small><b>${peso(p.cost)}</b></div><div><small>Margin</small><b>${mrg}</b></div>`:''}
   <div><small>Last sale</small><b style="font-size:13px">${d10(d.last_sale)||'never'}</b></div></div>
   <h3 style="font-size:14px;margin:14px 0 4px">Stock card (latest 25 movements)</h3>${dtable([
    {h:'When',f:r=>dtm(r.ts)},{h:'Type',f:r=>badge(r.kind)},{h:'Qty',cls:'r',f:r=>`<b class="${r.delta<0?'neg':'pos'}">${r.delta>0?'+':''}${r.delta}</b>`},{h:'Note',cls:'wrap',f:r=>esc(r.note)},{h:'By',f:r=>esc(r.user)}],d.movements)}
   <div class="mactions">${can('owner','purchasing')?`<button type="button" class="b sm" data-act="stockin" data-id="${p.id}">Stock in</button>`:''}${can('owner','employee')?`<button type="button" class="b sm g" data-act="stockout" data-id="${p.id}">Stock out</button>`:''}${can('owner')?`<button type="button" class="b sm g" data-act="edit" data-id="${p.id}">Edit product</button>`:''}${can('owner')&&p.qty===0?`<button type="button" class="b sm d" data-act="archive" data-id="${p.id}">Archive</button>`:''}</div>`,null,'',true);
};
ACT.add=()=>productForm();
ACT.edit=id=>productForm(PC[id]);
async function productForm(p){
  const v=k=>esc(p?(p[k]??''):'');
  modal(p?'Edit product':'Add product',`<div class="r2"><div><label>SKU</label><input name="sku" required value="${v('sku')}"></div><div><label>Brand</label><input name="brand" required value="${v('brand')}"></div></div>
   <label>Model</label><input name="model" required value="${v('model')}">
   <div class="r3"><div><label>Horsepower (HP)</label><input name="hp" type="number" step="0.25" min="0.25" required value="${v('hp')}"></div>
   <div><label>Type</label>${sel('type',['Window','Window Inverter','Split','Split Inverter','Floor Standing','Cassette','Portable'],p?p.type:'Split Inverter')}</div>
   <div><label>Shelf / location</label><input name="location" value="${v('location')}"></div></div>
   <div class="r3"><div><label>Unit cost (₱)</label><input name="cost" type="number" step="0.01" min="0" required value="${v('cost')}"></div>
   <div><label>Selling price (₱)</label><input name="price" type="number" step="0.01" min="0" required value="${v('price')}"></div>
   <div><label>Promo price (optional)</label><input name="promo_price" type="number" step="0.01" min="0" value="${v('promo_price')}"></div></div>
   <div class="r3"><div><label>Minimum quantity</label><input name="min_qty" type="number" min="0" required value="${p?p.min_qty:2}"></div>
   <div><label>Warranty (months)</label><input name="warranty_months" type="number" min="0" value="${p?p.warranty_months:12}"></div>
   ${p?'<div></div>':'<div><label>Opening stock</label><input name="qty" type="number" min="0" value="0"></div>'}</div>
   ${p?'<p class="sub">Quantity changes only through stock-in, sales, stock-out and counts so everything is traceable. Price and cost edits are written to the audit trail.</p>':''}`,
   async o=>{await api(p?'/products/'+p.id:'/products',p?'PUT':'POST',o);toast('Saved');drawInvRefresh()},'Save product');
}
ACT.archive=async id=>{const p=PC[id];if(!confirm(`Archive ${p.brand} ${p.model}? History is kept; it just disappears from lists.`))return;await api('/products/'+id,'DELETE');toast('Archived');closeModal();drawInvRefresh()};
ACT.stockin=id=>{const p=PC[id];modal('Stock in: '+esc(p.brand+' '+p.model),`<p class="sub">On hand ${p.qty} · available ${p.available}</p>
  <label>Quantity received</label><input name="qty" type="number" min="1" required autofocus><label>Supplier / delivery receipt no.</label><input name="note" required placeholder="Required for the audit trail">`,
  async o=>{await api('/movements','POST',{product_id:p.id,kind:'IN',...o});toast('Stock received');drawInvRefresh()},'Record stock in')};
ACT.stockout=id=>{const p=PC[id];modal('Stock out (not a sale): '+esc(p.brand+' '+p.model),`<div class="note">For customer sales use <b>New Sale</b>. Use this only for damage, display units, transfers or losses. Every stock-out is flagged in the Risk Center.</div>
  <p class="sub">On hand ${p.qty} · available ${p.available}</p><label>Quantity</label><input name="qty" type="number" min="1" max="${p.available}" required>
  <label>Reason</label>${sel('reason',ME.out_reasons,'')}<label>Details</label><input name="note" required placeholder="What happened?">`,
  async o=>{await api('/movements','POST',{product_id:p.id,kind:'OUT',...o});toast('Stock-out recorded');drawInvRefresh()},'Record stock-out')};
ACT.promoclear=async id=>{await api('/promo','POST',{items:[{product_id:+id,price:null}]});toast('Promo cleared');drawInvRefresh()};

/* ---------- dashboard ---------- */
V.dash=async(version=renderVersion)=>{
  const d=await api('/dashboard');if(version!==renderVersion)return;const o=can('owner'),p=ME.role==='purchasing',e=ME.role==='employee';cacheP(d.low);
  const k=[];
  if(o){
    k.push(kpi('Sales today',peso(d.today_revenue),npl(d.today_count,'invoice')));
    k.push(kpi('This month',peso(d.month_revenue),npl(d.month_count,'invoice')+' · profit '+peso(d.month_profit)));
    k.push(kpi('Restock needed',d.low_count,`${d.out_count} out of stock · ${d.low_count-d.out_count} low`,d.low_count?'neg':''));
    k.push(kpi('Risk alerts',d.alerts,d.alerts?'Needs attention':'All clear',d.alerts?'warn':''));
  }else{
    if(!p)k.push(kpi('My sales today',peso(d.my_today),npl(d.my_today_count,'invoice')));
    k.push(kpi('Units on hand',num(d.units),npl(d.models,'model')));
    k.push(kpi('Restock needed',d.low_count,`${d.out_count} out of stock · ${d.low_count-d.out_count} low`,d.low_count?'neg':''));
    k.push(kpi('Inventory value (cost)',peso(d.value),'at cost'));
  }
  const notes=(d.models?'':(o?`<div class="note"><b>Your shop has no stock yet.</b> Add your first product to start selling. <button class="b sm" data-act="add">Add product</button></div>`:`<div class="note"><b>No stock yet.</b> The shop owner needs to add products before anything can be sold.</div>`))
   +(o&&d.pending_users?`<div class="note bad"><b>${npl(d.pending_users,'staff request')}</b> waiting for your approval. <button class="b sm" data-act="go" data-id="users">Review</button></div>`:'');
  const ser=[{name:e?'My revenue':'Revenue',vals:d.series.revenue}];if(d.series.profit)ser.push({name:'Gross profit',vals:d.series.profit});
  const inv=o?`<div class="panel invsum"><div class="phead"><h3>Inventory overview</h3><button class="plink" data-act="go" data-id="inv">View inventory</button></div><div class="invstats">
    <div><span class="l">Units on hand</span><b>${num(d.units)}</b><span class="s">${npl(d.models,'model')}</span></div>
    <div><span class="l">Inventory value (cost)</span><b>${peso(d.value)}</b><span class="s">at cost</span></div>
    <div><span class="l">Dead-stock capital</span><b>${peso(d.dead_capital)}</b><span class="s">${npl(d.dead_count,'model')} idle 90+ days</span></div>
   </div></div>`:'';
  const salesLink=can('owner','employee')?`<button class="plink" data-act="go" data-id="sales">View sales</button>`:'';
  $('#view').innerHTML=head('Dashboard','Today at a glance.',can('owner','employee')?`<button class="b" data-act="go" data-id="pos">＋ New sale</button>`:`<button class="b" data-act="go" data-id="reorder">Plan restock</button>`)+
   `${notes}<div class="kpis ${k.length===4?'k4':'k3'}">${k.join('')}</div>${inv}
   <div class="g3"><div class="panel"><div class="phead"><h3>${e?'My sales':'Sales'} · last 14 days</h3>${salesLink}</div>${lineChart(d.series.labels,ser,d.series.count)}</div>
   <div class="panel"><div class="phead"><h3>Stock health</h3><button class="plink" data-act="go" data-id="inv">View inventory</button></div>${donut([{label:'In stock',value:d.ok_count,color:'var(--ok)'},{label:'Low',value:d.low_count-d.out_count,color:'var(--warn)'},{label:'Out',value:d.out_count,color:'var(--bad)'}],d.models,'Models')}
   <div class="psplit"><div class="phead"><h3>Best sellers · 30 days</h3></div>${d.top.length?hbars(d.top.map(t=>({label:t.label.split(' - ')[1]||t.label,title:t.label,value:t.units,text:npl(t.units,'unit'),extra:trow('Revenue',peso(t.revenue))})),'c2','Units sold'):'<p class="mut">No sales yet.</p>'}</div></div></div>
   <div class="g2"><div class="panel"><div class="phead"><h3>Needs restocking</h3><button class="plink" data-act="go" data-id="inv">View all</button></div>${dtable([{h:'Product',cls:'wrap',f:r=>pname(r)},{h:'Avail.',cls:'r',f:r=>r.available},{h:'Cover',f:r=>coverCell(r)},{h:'Status',f:r=>pbadge(r)}],d.low,{empty:'Everything is above its minimum.'})}</div>
   <div class="panel"><div class="phead"><h3>Recent activity</h3></div>${dtable([{h:'When',f:r=>dts(r.ts)},{h:'Item',cls:'wrap',f:r=>esc(r.label.split(' - ')[1]||r.label)},{h:'Type',f:r=>badge(r.kind)},{h:'Qty',cls:'r',f:r=>`<b class="${r.delta<0?'neg':'pos'}">${r.delta>0?'+':''}${r.delta}</b>`}],d.recent.slice(0,6),{empty:'No stock movements yet.'})}</div></div>`;
};

/* ---------- inventory ---------- */
let INV={q:'',st:'ALL',brand:'',type:''},INVL=[];
V.inv=async(version=renderVersion)=>{
  INVL=await api('/products');if(version!==renderVersion)return;cacheP(INVL);
  const brands=[...new Set(INVL.map(p=>p.brand))].sort(),types=[...new Set(INVL.map(p=>p.type))].sort();
  const chips=[['ALL','All'],['OK','In stock'],['LOW','Low'],['OUT','Out'],['SOON','Reorder soon'],['PROMO','On promo']];
  $('#view').innerHTML=head('Inventory','Search by brand, model, SKU, HP or shelf.',`${can('owner')?'<button class="b" data-act="add">+ Add product</button>':''}`)+
   `<div class="bar"><input type="search" id="iq" placeholder='Try "lg 1.5", "carrier window", "B1" …' value="${esc(INV.q)}">
   ${chips.map(c=>`<button class="chip ${INV.st===c[0]?'on':''}" data-act="invst" data-id="${c[0]}">${c[1]}</button>`).join('')}
   <select id="ib">${[['','All brands'],...brands].map(b=>{const [v,t]=Array.isArray(b)?b:[b,b];return `<option value="${esc(v)}" ${INV.brand===v?'selected':''}>${esc(t)}</option>`}).join('')}</select>
   <select id="it">${[['','All types'],...types].map(b=>{const [v,t]=Array.isArray(b)?b:[b,b];return `<option value="${esc(v)}" ${INV.type===v?'selected':''}>${esc(t)}</option>`}).join('')}</select></div><div class="panel" id="invt"></div>`;
  $('#iq').oninput=e=>{INV.q=e.target.value;drawInv()};$('#ib').onchange=e=>{INV.brand=e.target.value;drawInv()};$('#it').onchange=e=>{INV.type=e.target.value;drawInv()};
  drawInv();
};
ACT.invst=id=>{INV.st=id;$$('.chip').forEach(c=>c.classList.toggle('on',c.dataset.id===id));drawInv()};
async function drawInvRefresh(){if((location.hash||'#dash')==='#inv'){INVL=await api('/products');cacheP(INVL);drawInv()}else render()}
function drawInv(){
  const w=words(INV.q),cost=can('owner','purchasing');
  const rows=INVL.filter(p=>(INV.st==='ALL'||(INV.st==='SOON'?p.reorder_soon:INV.st==='PROMO'?!!p.promo_price:p.status===INV.st))&&(!INV.brand||p.brand===INV.brand)&&(!INV.type||p.type===INV.type)&&w.every(x=>hay(p).includes(x)));
  const cols=[{h:'Product',f:p=>pname(p).replace('</div>',` · shelf ${esc(p.location||'-')}</div>`),s:p=>p.brand+p.model},{h:'On hand',cls:'r',f:p=>p.qty,s:p=>p.qty},
   {h:'Available',cls:'r',f:p=>`<b>${p.available}</b>`,s:p=>p.available},{h:'Cover',f:coverCell,s:p=>p.days_cover==null?9999:p.days_cover}];
  if(cost)cols.push({h:'Cost',cls:'r',f:p=>peso(p.cost),s:p=>p.cost});
  cols.push({h:'Price',cls:'r',f:priceCell,s:p=>p.eff_price},{h:'Status',f:p=>pbadge(p)+(p.reorder_soon?' <span class="sub">reorder soon</span>':''),s:p=>p.status},
   {h:'',cls:'act',f:p=>`<div class="tact"><button data-act="detail" data-id="${p.id}">Details</button>${can('owner','purchasing')?`<button data-act="stockin" data-id="${p.id}">Stock in</button>`:''}</div>`});
  $('#invt').innerHTML=`<div class="sub" style="margin-bottom:8px">${rows.length} of ${npl(INVL.length,'model')} · click a column header to sort</div>`+dtable(cols,rows,{empty:INVL.length?'No matching products.':(can('owner')?'No products yet. Click "+ Add product" to add your first one.':'The shop owner has not added any stock yet.')});
}

/* ---------- point of sale ---------- */
let CART=[],PRE=null,POSP=[],POSQ='',POSF={customer:'',phone:'',payment:'Cash',note:''};
V.pos=async(version=renderVersion)=>{
  const ps=await api('/products');if(version!==renderVersion)return;POSP=ps;cacheP(ps);
  $('#view').innerHTML=head('New Sale','Add items to the cart, then print the invoice.')+
   `<div class="posg"><div class="panel"><input type="search" id="pq" style="width:100%" placeholder="Scan SKU or type brand, model, HP…" value="${esc(POSQ)}"><div id="plist"></div></div><div class="panel cartp" id="cartbox"></div></div>`;
  $('#pq').oninput=e=>{POSQ=e.target.value;drawPList()};
  $('#pq').onkeydown=e=>{if(e.key==='Enter'){const m=POSP.filter(p=>p.sku.toLowerCase()===POSQ.trim().toLowerCase()||(words(POSQ).length&&words(POSQ).every(w=>hay(p).includes(w))&&p.available>0));if(m.length===1){ACT.padd(m[0].id);POSQ='';e.target.value='';drawPList()}}};
  $('#pq').focus();drawPList();drawCart();
};
function drawPList(){
  const w=words(POSQ),rows=POSP.filter(p=>w.every(x=>hay(p).includes(x))).slice(0,60);
  $('#plist').innerHTML=rows.map(p=>`<div class="pi ${p.available<=0?'dis':''}"><div>${pname(p)}</div><div>${priceCell(p)}<div class="sub">${p.available>0?p.available+' available':'out of stock'}</div></div>
   <div>${p.available>0?`<button class="b sm" data-act="padd" data-id="${p.id}">Add</button>`:`<button class="b sm g" data-act="subfor" data-id="${p.id}">Alternatives</button>`}</div></div>`).join('')||'<div class="empty">No matching products.</div>';
}
ACT.padd=id=>{const p=PC[id],held=PRE&&PRE.product_id==p.id?PRE.qty:0,max=p.available+held,ex=CART.find(i=>i.id==p.id);
  if(ex){if(ex.qty<ex.max)ex.qty++;else return toast('No more units available.',true)}else{if(max<=0)return toast('Out of stock.',true);CART.push({id:p.id,label:p.brand+' '+p.model,sku:p.sku,list:p.eff_price,unit:p.eff_price,qty:1,max,cost:p.cost})}
  drawCart()};
ACT.subfor=id=>{SUBID=id;go('sub')};
ACT.prm=i=>{CART.splice(+i,1);drawCart()};
CHG.cartq=el=>{const it=CART[+el.dataset.id];it.qty=Math.max(1,Math.min(parseInt(el.value)||1,it.max));drawCart()};
CHG.cartp=el=>{CART[+el.dataset.id].unit=Math.max(0,parseFloat(el.value)||0);drawCart()};
CHG.posf=el=>{POSF[el.name]=el.value};
function drawCart(){
  const tot=CART.reduce((a,i)=>a+i.unit*i.qty,0),list=CART.reduce((a,i)=>a+i.list*i.qty,0),dep=0,lim=ME.max_emp_discount;
  const lines=CART.map((i,ix)=>{let w='';const off=i.list?1-i.unit/i.list:0;
    if(i.cost!==undefined&&i.unit<i.cost)w=`<div class="sub neg">Below cost${can('owner')?' (owner override)':' - owner must approve'}</div>`;
    else if(!can('owner')&&off>lim+.0005)w=`<div class="sub neg">Over ${Math.round(lim*100)}% discount - owner approval needed</div>`;
    else if(off>.0005)w=`<div class="sub warn">${(off*100).toFixed(1)}% off list</div>`;
    return `<div class="cl"><div><b>${esc(i.label)}</b><div class="sub">${esc(i.sku)} · list ${peso(i.list)}</div>${w}</div><input type="number" min="1" max="${i.max}" value="${i.qty}" data-chg="cartq" data-id="${ix}"><input type="number" min="0" step="0.01" value="${i.unit}" data-chg="cartp" data-id="${ix}"><button class="x" data-act="prm" data-id="${ix}">×</button></div>`}).join('');
  const profit=can('owner')&&CART.length?CART.reduce((a,i)=>a+(i.unit-i.cost)*i.qty,0):null;
  $('#cartbox').innerHTML=`<h3>Current sale</h3>
   <div class="r2"><div><label>Customer</label><input name="customer" data-chg="posf" value="${esc(POSF.customer)}" placeholder="Walk-in"></div><div><label>Phone</label><input name="phone" data-chg="posf" value="${esc(POSF.phone)}"></div></div>
   <label>Payment method</label>${sel('payment',ME.payments,POSF.payment,'data-chg="posf"')}
   <div style="margin-top:12px">${lines||'<div class="empty">Cart is empty. Add a product from the list.</div>'}</div>
   <div class="tot"><div><span class="mut">List total</span><span>${peso(list)}</span></div><div><span class="mut">Discount</span><span>${peso(list-tot)}</span></div>
   ${profit!==null?`<div><span class="mut">Est. profit</span><span class="${profit<0?'neg':'pos'}">${peso(profit)}</span></div>`:''}<div class="big"><span>Amount due</span><span>${peso(tot-dep)}</span></div></div>
   <button class="b" style="width:100%;margin-top:14px;padding:12px" data-act="pay" ${CART.length?'':'disabled'}>Complete sale</button>
   ${CART.length?`<button class="b g" style="width:100%;margin-top:8px" data-act="clearcart">Clear cart</button>`:''}`;
}
ACT.clearcart=()=>{CART=[];PRE=null;POSF.res='';drawCart()};
ACT.pay=async()=>{
  const r=await api('/sales','POST',{customer:POSF.customer,phone:POSF.phone,payment:POSF.payment,items:CART.map(i=>({product_id:i.id,qty:i.qty,price:i.unit}))});
  CART=[];PRE=null;POSF={customer:'',phone:'',payment:'Cash',note:''};
  modal('Sale complete',`<div class="note"><b>${esc(r.invoice_no)}</b> · total ${peso(r.total)}</div><p>Stock was deducted and the sale was added to the ledger.</p>`,null,null,false,
   `<a class="b" href="/invoice/${r.id}" target="_blank">Print invoice</a><button type="button" class="b g" data-act="close">Close</button>`);
  await V.pos();
};

/* ---------- sales history ---------- */
let SF={from:'',to:'',status:'',q:'',user:''},SALES=[];
V.sales=async(version=renderVersion)=>{
  const users=can('owner')?await api('/users'):[];
  if(version!==renderVersion)return;
  $('#view').innerHTML=head('Sales History',ME.role==='employee'?'Your invoices only.':'All invoices.')+
   `<div class="bar"><input type="search" id="sq" placeholder="Invoice no. or customer" value="${esc(SF.q)}"><label style="margin:0">From</label><input type="date" id="sfrom" value="${SF.from}"><label style="margin:0">To</label><input type="date" id="sto" value="${SF.to}">
   <select id="sst"><option value="">All statuses</option><option value="PAID" ${SF.status==='PAID'?'selected':''}>Paid</option><option value="VOID" ${SF.status==='VOID'?'selected':''}>Void</option></select>
   ${can('owner')?`<select id="su"><option value="">All cashiers</option>${users.map(u=>`<option value="${esc(u.username)}" ${SF.user===u.username?'selected':''}>${esc(u.name)}</option>`).join('')}</select>`:''}</div><div class="panel" id="sl"></div>`;
  const ch=()=>{SF={q:$('#sq').value,from:$('#sfrom').value,to:$('#sto').value,status:$('#sst').value,user:$('#su')?$('#su').value:''};run(loadSales)};
  ['#sq','#sfrom','#sto','#sst','#su'].forEach(s=>{const el=$(s);if(el)el.onchange=ch});$('#sq').oninput=()=>{clearTimeout(window._sq);window._sq=setTimeout(ch,250)};
  await loadSales(version);
};
async function loadSales(version=renderVersion){
  const qs=Object.entries(SF).filter(([k,v])=>v).map(([k,v])=>k+'='+encodeURIComponent(v)).join('&');const sales=await api('/sales?'+qs);if(version!==renderVersion)return;SALES=sales;
  const paid=SALES.filter(s=>s.status==='PAID'),cost=can('owner');
  const cols=[{h:'Invoice',f:s=>`<b>${esc(s.invoice_no)}</b>`,s:s=>s.invoice_no},{h:'Date',f:s=>dtm(s.ts),s:s=>s.ts},{h:'Customer',f:s=>esc(s.customer),s:s=>s.customer},{h:'Items',cls:'wrap',f:s=>esc(s.items)},{h:'Payment',f:s=>esc(s.payment)},{h:'Cashier',f:s=>esc(s.user),s:s=>s.user},
   {h:'Total',cls:'r',f:s=>peso(s.total),s:s=>s.total}];
  if(cost)cols.push({h:'Profit',cls:'r',f:s=>`<span class="${s.profit<0?'neg':''}">${peso(s.profit)}</span>`,s:s=>s.profit});
  cols.push({h:'Status',f:s=>badge(s.status)},{h:'',cls:'act',f:s=>`<div class="tact"><button data-act="sview" data-id="${s.id}">View</button><a href="/invoice/${s.id}" target="_blank">Print</a></div>`});
  $('#sl').innerHTML=`<div class="sub" style="margin-bottom:8px">${npl(SALES.length,'invoice')} · paid total <b>${peso(paid.reduce((a,s)=>a+s.total,0))}</b></div>`+dtable(cols,SALES,{sort:1,dir:-1,empty:'No sales match these filters.'});
}
ACT.sview=async id=>{const s=await api('/sales/'+id);modal(esc(s.invoice_no)+' '+badge(s.status),`<p class="sub">${dtm(s.ts)} · ${esc(s.customer)} ${esc(s.phone||'')} · ${esc(s.payment)} · cashier ${esc(s.user)}</p>
  ${dtable([{h:'Item',cls:'wrap',f:i=>esc(i.label)},{h:'Qty',cls:'r',f:i=>i.qty},{h:'Unit price',cls:'r',f:i=>peso(i.unit_price)},{h:'List',cls:'r',f:i=>peso(i.list_price)},{h:'Amount',cls:'r',f:i=>peso(i.qty*i.unit_price)}],s.items)}
  <div class="tot"><div><span>Total</span><b>${peso(s.total)}</b></div>${s.profit!==undefined?`<div><span>Profit</span><span>${peso(s.profit)}</span></div>`:''}</div>
  ${s.status==='VOID'?`<div class="note bad"><b>Voided</b> by ${esc(s.void_by)} on ${dtm(s.void_ts)}: ${esc(s.void_reason)}</div>`:''}
  <div class="mactions"><a class="b sm g" href="/invoice/${s.id}" target="_blank">Print invoice</a>${can('owner')&&s.status==='PAID'?`<button class="b sm d" data-act="svoid" data-id="${s.id}">Void sale</button>`:''}</div>`,null,'',true)};
ACT.svoid=id=>{const s=SALES.find(x=>x.id==id);modal('Void '+esc(s?s.invoice_no:'sale'),`<div class="note bad">Voiding returns the units to stock and cannot be undone. It is recorded against your name.</div><label>Reason</label><input name="reason" required autofocus placeholder="e.g. customer cancelled, wrong model">`,
  async o=>{await api('/sales/'+id+'/void','POST',o);toast('Sale voided, stock restored');closeModal();loadSales()},'Void sale')};

/* ---------- restock radar ---------- */
V.low=async(version=renderVersion)=>{
  const ps=await api('/lowstock');if(version!==renderVersion)return;cacheP(ps);
  $('#view').innerHTML=head('Restock Radar','Products below minimum stock, or likely to run out before a new order arrives.',can('owner','purchasing')?`<button class="b" data-act="go" data-id="reorder">Plan purchase</button>`:'')+
   `<div class="note"><b>How it works:</b> <i>Days of cover</i> = available ÷ average daily sales (last 90 days). <i>Reorder point</i> = sales during the delivery lead time + minimum quantity.</div>
   <div class="panel">${dtable([{h:'Product',f:pname,s:p=>p.brand+p.model},{h:'Avail.',cls:'r',f:p=>`<b>${p.available}</b>`,s:p=>p.available},{h:'Min',cls:'r',f:p=>p.min_qty},
    {h:'Sales/day',cls:'r',f:p=>p.per_day},{h:'Cover',f:coverCell,s:p=>p.days_cover==null?9999:p.days_cover},{h:'Reorder pt',cls:'r',f:p=>p.rop},{h:'Suggested qty',cls:'r',f:p=>`<b>${p.suggest_qty}</b>`,s:p=>p.suggest_qty},{h:'Lead time',f:p=>npl(p.lead_days,'day')},
    {h:'Status',f:p=>p.status!=='OK'?pbadge(p):badge('ORDERED','Reorder soon')}],ps,{empty:'Nothing needs restocking.'})}</div>`;
};

/* ---------- reorder simulator ---------- */
let LASTPLAN=[],RB={budget:150000,strategy:'urgent'};
V.reorder=async()=>{
  $('#view').innerHTML=head('Budget-Aware Reorder Simulator','Enter a budget to get a suggested purchase list based on stock cover, delivery lead time.')+
   `<div class="panel"><div class="bar" style="margin:0"><label style="margin:0">Budget (₱)</label><input id="bg" type="number" min="0" step="1000" value="${RB.budget}" style="width:150px">
   <label style="margin:0">Priority</label><select id="st"><option value="urgent" ${RB.strategy==='urgent'?'selected':''}>Most urgent first (avoid stock-outs)</option><option value="profit" ${RB.strategy==='profit'?'selected':''}>Highest profit potential</option><option value="balanced" ${RB.strategy==='balanced'?'selected':''}>Balanced</option></select>
   <button class="b" data-act="sim">Simulate</button></div></div><div id="simout"></div>`;
  await ACT.sim();
};
ACT.sim=async()=>{
  RB={budget:$('#bg').value,strategy:$('#st').value};const d=await api('/reorder','POST',RB);LASTPLAN=d.plan;
  $('#simout').innerHTML=`<div class="kpis">${kpi('Budget',peso(d.budget))}${kpi('Planned spend',peso(d.spent),npl(d.plan.length,'product'))}${kpi('Left over',peso(d.left))}${kpi('Expected revenue',peso(d.exp_revenue),'if all units sell')}${kpi('Expected profit',peso(d.exp_profit),'','pos')}</div>
   <div class="panel"><h3>Purchase plan</h3>${dtable([{h:'Product',f:pname},{h:'Why',cls:'wrap',f:p=>esc(p.why)},{h:'Avail.',cls:'r',f:p=>p.available},{h:'Target',cls:'r',f:p=>p.target},{h:'Buy',cls:'r',f:p=>`<b>${p.buy}</b>`},{h:'Unit cost',cls:'r',f:p=>peso(p.cost)},{h:'Line total',cls:'r',f:p=>peso(p.spend)},{h:'Coverage',f:p=>p.full?badge('OK','Fully covered'):badge('LOW','Partial (budget)')}],d.plan,{empty:'The budget is too small to buy any needed unit, or nothing needs restocking.'})}
   </div>
   ${d.skipped.length?`<div class="panel"><h3>Still needs restocking (not affordable)</h3>${dtable([{h:'Product',f:pname},{h:'Why',cls:'wrap',f:p=>esc(p.why)},{h:'Needed',cls:'r',f:p=>p.need},{h:'Unit cost',cls:'r',f:p=>peso(p.cost)}],d.skipped)}</div>`:''}`;
};

/* ---------- dead stock ---------- */
let DDAYS=60,DEAD=[];
V.dead=async(version=renderVersion)=>{
  $('#view').innerHTML=head('Dead Stock Recovery Planner','Slow-moving products, the money tied up in them, and suggested promo prices.')+
   `<div class="bar"><label style="margin:0">Show products unsold for at least</label><select id="dd">${[30,60,90,120,180].map(d=>`<option ${d==DDAYS?'selected':''}>${d}</option>`).join('')}</select> days</div><div id="dout"></div>`;
  $('#dd').onchange=e=>{DDAYS=+e.target.value;run(loadDead)};await loadDead(version);
};
async function loadDead(version=renderVersion){
  const d=await api('/deadstock?days='+DDAYS);if(version!==renderVersion)return;DEAD=d.items;
  $('#dout').innerHTML=`<div class="kpis">${kpi('Slow-moving models',d.items.length)}${kpi('Capital tied up (cost)',peso(d.tied_capital),'',d.tied_capital?'warn':'')}</div>
   <div class="panel">${dtable([{h:'Product',f:pname},{h:'Units',cls:'r',f:p=>p.qty},{h:'Idle',cls:'r',f:p=>`<b class="neg">${p.idle_days}d</b>`,s:p=>p.idle_days},{h:'Capital tied',cls:'r',f:p=>peso(p.tied_capital),s:p=>p.tied_capital},{h:'Price now',cls:'r',f:priceCell},
    {h:'Suggested',cls:'r',f:p=>`<b>${peso(p.suggested_price)}</b> <span class="sub">(-${p.suggested_discount}%)</span>`},{h:'Margin/unit',cls:'r',f:p=>peso(p.margin_after)},{h:'Action',cls:'wrap',f:p=>esc(p.action)},
    {h:'',f:p=>`<div class="tact">${p.promo_price?`<button data-act="promoclear2" data-id="${p.id}">Clear promo</button>`:`<button data-act="promoapply" data-id="${p.id}">Apply promo</button>`}</div>`}],d.items,{empty:'No slow-moving stock for this period.'})}
   ${d.items.length?`<div style="padding-top:14px;text-align:right"><button class="b" data-act="promoall">Apply all suggested promos</button></div>`:''}<p class="sub" style="margin-top:10px">Suggested prices never go below cost + 2%. Applied promos show on the New Sale screen automatically.</p></div>`;
}
ACT.promoapply=async id=>{const p=DEAD.find(x=>x.id==id);await api('/promo','POST',{items:[{product_id:p.id,price:p.suggested_price}]});toast('Promo applied: '+peso(p.suggested_price));loadDead()};
ACT.promoclear2=async id=>{await api('/promo','POST',{items:[{product_id:+id,price:null}]});toast('Promo cleared');loadDead()};
ACT.promoall=async()=>{if(!confirm(`Apply promo prices to ${npl(DEAD.filter(p=>!p.promo_price).length,'product')}?`))return;await api('/promo','POST',{items:DEAD.filter(p=>!p.promo_price).map(p=>({product_id:p.id,price:p.suggested_price}))});toast('Promos applied');loadDead()};

/* ---------- substitutes ---------- */
let SUBID=null;
V.sub=async(version=renderVersion)=>{
  const ps=await api('/products');if(version!==renderVersion)return;cacheP(ps);
  if(!ps.length){$('#view').innerHTML=head('Smart Substitute Finder','Find in-stock units with similar HP, type and price.')+'<div class="panel empty">There are no products yet.</div>';return}
  if(!SUBID||!PC[SUBID])SUBID=(ps.find(p=>p.status==='OUT')||ps[0]).id;
  $('#view').innerHTML=head('Smart Substitute Finder',"Find in-stock units with similar HP, type and price.")+
   `<div class="bar"><select id="sp" style="min-width:340px">${ps.map(p=>`<option value="${p.id}" ${p.id==SUBID?'selected':''}>${esc(p.brand+' '+p.model)} - ${p.status==='OUT'?'OUT OF STOCK':p.available+' available'}</option>`).join('')}</select></div><div id="sout"></div>`;
  $('#sp').onchange=e=>{SUBID=+e.target.value;run(loadSub)};await loadSub(version);
};
async function loadSub(version=renderVersion){
  const d=await api('/substitutes?product_id='+SUBID);if(version!==renderVersion)return;const t=d.target;
  $('#sout').innerHTML=`<p>Alternatives to <b>${esc(t.brand+' '+t.model)}</b> (${t.hp}HP, ${esc(t.type)}, ${peso(t.eff_price)}) ${pbadge(t)}</p>`+(d.substitutes.length?`<div class="sg">${d.substitutes.map((s,i)=>`<div class="card"><span class="sub">${i?'Option '+(i+1):'Best match'}</span><b>${esc(s.brand+' '+s.model)}</b><div style="font-size:18px;margin:4px 0">${priceCell(s)} <span class="sub ${s.price_diff>0?'neg':'pos'}">(${s.price_diff>=0?'+':''}${peso(s.price_diff)})</span></div><div class="sub">${s.available} available · ${esc(s.sku)} · shelf ${esc(s.location||'-')}</div><div class="sub">${s.reasons.join(' · ')}</div>${can('owner','employee')?`<button class="b sm" style="margin-top:8px" data-act="padd2" data-id="${s.id}">Sell this instead</button>`:''}</div>`).join('')}</div>`:'<p class="mut">No in-stock alternatives within 0.5HP.</p>');
}
ACT.padd2=id=>{cacheP([]);go('pos');setTimeout(()=>ACT.padd(id),400)};

/* ---------- reports ---------- */
let RDAYS=30;
V.rep=async(version=renderVersion)=>{
  const d=await api('/reports?days='+RDAYS);
  if(version!==renderVersion)return;
  const pays=d.payments.map((p,i)=>({label:p.payment,value:p.n,extra:trow('Revenue',peso(p.revenue)),color:['var(--c1)','var(--c2)','var(--c3)','var(--c4)','var(--ok)','var(--bad)'][i%6]}));
  $('#view').innerHTML=head('Reports & Analytics',`Last ${npl(d.days,'day')}.`,`<select id="rd">${[7,30,90,180,365].map(x=>`<option value="${x}" ${x==RDAYS?'selected':''}>Last ${x} days</option>`).join('')}</select>`)+
   `<div class="kpis">${kpi('Revenue',peso(d.revenue),npl(d.invoices,'invoice')+' · avg '+peso(d.avg_ticket))}${kpi('Gross profit',peso(d.profit),d.margin_pct+'% margin','pos')}${kpi('Units sold',num(d.units),'Discounts given '+peso(d.discounts))}
   ${kpi('Inventory value',peso(d.inventory_value),'at cost')}${kpi('Inventory turns',d.turns+'×',d.days_inventory?npl(d.days_inventory,'day')+' of inventory':'')}</div>
   <div class="panel"><h3>Revenue & profit per day</h3>${lineChart(d.series.labels,[{name:'Revenue',vals:d.series.revenue},{name:'Gross profit',vals:d.series.profit}],d.series.count,{full:true})}</div>
   <div class="g2"><div class="panel"><h3>Stock value by brand (cost)</h3>${hbars(d.brands.filter(b=>b.stock_value>0).map(b=>({label:b.brand,value:b.stock_value,text:peso(b.stock_value),extra:trow('Units on hand',num(b.units))})),'c3','Stock value')}</div>
   <div class="panel"><h3>Revenue by brand</h3>${hbars(d.brands.filter(b=>b.revenue>0).sort((a,b)=>b.revenue-a.revenue).map(b=>({label:b.brand,value:b.revenue,text:peso(b.revenue)})),'','Revenue')}</div></div>
   <div class="g2"><div class="panel"><h3>Payment methods (invoices)</h3>${donut(pays,d.invoices,'Invoices')}</div>
   <div class="panel"><h3>Cashier accountability</h3>${dtable([{h:'User',f:c=>esc(c.user)},{h:'Invoices',cls:'r',f:c=>c.invoices},{h:'Revenue',cls:'r',f:c=>peso(c.revenue)},{h:'Discounts',cls:'r',f:c=>peso(c.discounts)},{h:'Voids',cls:'r',f:c=>c.voids?`<b class="neg">${c.voids}</b>`:0}],d.cashiers)}</div></div>
   <div class="panel"><h3>Product performance & ABC class</h3><p class="sub">A = products making the first 80% of revenue, B = next 15%, C = the long tail. Keep A items always in stock; shrink C.</p>${dtable([{h:'Product',cls:'wrap',f:p=>esc(p.label),s:p=>p.label},{h:'ABC',f:p=>badge(p.abc),s:p=>p.abc},{h:'Units',cls:'r',f:p=>p.units,s:p=>p.units},{h:'Revenue',cls:'r',f:p=>peso(p.revenue),s:p=>p.revenue},{h:'Profit',cls:'r',f:p=>peso(p.profit),s:p=>p.profit}],d.products,{sort:3,dir:-1,empty:'No sales in this period.'})}</div>`;
  $('#rd').onchange=e=>{RDAYS=+e.target.value;run(V.rep)};
};

/* ---------- risk center ---------- */
V.alerts=async(version=renderVersion)=>{
  const a=await api('/alerts');if(version!==renderVersion)return;const ic={};$('#belln').textContent=a.length;
  $('#view').innerHTML=head('Risk Center','Problems found automatically in stock and sales records.')+
   `<div class="kpis">${kpi('High priority',a.filter(x=>x.severity==='high').length,'','neg')}${kpi('Medium',a.filter(x=>x.severity==='med').length,'','warn')}${kpi('Low',a.filter(x=>x.severity==='low').length)}</div>`+
   (a.length?a.map(x=>`<div class="al ${x.severity}" data-act="go" data-id="${x.tab}" role="link" tabindex="0"><span class="dot" aria-hidden="true"></span><div class="albody"><div class="alhead"><b>${esc(x.title)}</b><span class="badge type">${esc(x.type)}</span></div><span class="mut">${esc(x.detail)}</span>${x.ts?`<div class="sub">${dtm(x.ts)}</div>`:''}</div>${chevIcon}</div>`).join(''):'<div class="panel empty">All clear. Nothing needs your attention.</div>');
};

/* ---------- audit ---------- */
V.audit=async(version=renderVersion)=>{
  $('#view').innerHTML=head('Audit Trail','Record of changes to prices, stock, voids and users.')+`<div class="bar"><input type="search" id="aq" placeholder="Filter by action, user or detail"></div><div class="panel" id="al"></div>`;
  const load=async(pageVersion=renderVersion)=>{const search=$('#aq'),q=search?search.value:'',l=await api('/audit?q='+encodeURIComponent(q));if(pageVersion!==renderVersion)return;const out=$('#al');if(out)out.innerHTML=dtable([{h:'Date / time',f:x=>dtm(x.ts),s:x=>x.ts},{h:'User',f:x=>esc(x.user),s:x=>x.user},{h:'Action',f:x=>`<code>${esc(x.action)}</code>`,s:x=>x.action},{h:'Detail',cls:'wrap',f:x=>esc(x.detail)}],l,{sort:0,dir:-1})};
  $('#aq').oninput=()=>{clearTimeout(window._aq);window._aq=setTimeout(()=>run(()=>load()),250)};await load(version);
};

/* ---------- users & settings ---------- */
V.users=async(version=renderVersion)=>{
  const all=await api('/users');if(version!==renderVersion)return;const us=all.filter(u=>!u.pending),pend=all.filter(u=>u.pending),s=ME.settings;window._users=us;
  const pendHtml=pend.length?`<div class="note bad"><b>${npl(pend.length,'staff request')}</b> waiting for your approval. Approve only people you know. Once approved they can see this shop's stock.</div><div class="panel"><h3>Staff requests</h3>${dtable([{h:'Username',f:u=>`<b>${esc(u.username)}</b>`},{h:'Name',f:u=>esc(u.name)},{h:'Wants to be',f:u=>({owner:'Shop Owner',employee:'Sales staff',purchasing:'Purchasing'})[u.role]},{h:'Requested',f:u=>dtm(u.created_at)},{h:'',f:u=>`<div class="tact"><button data-act="uok" data-id="${u.id}">Approve</button><button class="danger" data-act="uno" data-id="${u.id}">Reject</button></div>`}],pend)}</div>`:'';
  $('#view').innerHTML=head('Users & Settings','Accounts, roles and shop details.',`<button class="b" data-act="uform">+ Add user</button>`)+
   `${pendHtml}<div class="panel"><h3>Accounts</h3>${dtable([{h:'Username',f:u=>`<b>${esc(u.username)}</b>`},{h:'Name',f:u=>esc(u.name)},{h:'Role',f:u=>({owner:'Shop Owner',employee:'Sales staff',purchasing:'Purchasing'})[u.role]},{h:'Active',f:u=>u.active?badge('OK','active'):badge('CANCELLED','disabled')},{h:'Created',f:u=>d10(u.created_at)},{h:'',f:u=>`<div class="tact"><button data-act="uform" data-id="${u.id}">Edit</button></div>`}],us)}</div>
   <div class="panel"><h3>Shop details (printed on invoices)</h3><form id="setf"><label>Shop name</label><input name="shop_name" value="${esc(s.shop_name)}" style="width:100%"><label>Address</label><input name="address" value="${esc(s.address)}" style="width:100%">
   <div class="r2"><div><label>Phone</label><input name="phone" value="${esc(s.phone)}" style="width:100%"></div><div><label>TIN</label><input name="tin" value="${esc(s.tin)}" style="width:100%"></div></div><label>Invoice footer</label><textarea name="footer" rows="2" style="width:100%">${esc(s.footer)}</textarea>
   <label><input type="checkbox" name="allow_signup" value="1" ${s.allow_signup!=='0'?'checked':''} style="width:auto"> Let staff ask to join my shop from the login page (I approve each request)</label>
   <p><button class="b" data-act="savesettings">Save shop details</button></p></form></div>`;
};
ACT.uok=async id=>{await api('/users/'+id+'/approve','POST');toast('Approved');V.users()};
ACT.uno=async id=>{if(!confirm('Reject and delete this request?'))return;await api('/users/'+id+'/reject','POST');toast('Request rejected');V.users()};
ACT.savesettings=async(id,b,e)=>{e.preventDefault();const o=Object.fromEntries(new FormData($('#setf')));o.allow_signup=$('#setf [name=allow_signup]').checked?'1':'0';await api('/settings','PUT',o);Object.assign(ME.settings,o);$('#shop').textContent=o.shop_name;toast('Saved')};
ACT.uform=id=>{const u=id?window._users.find(x=>x.id==id):null,v=k=>esc(u?u[k]:'');
  modal(u?'Edit user':'Add user',`${u?`<p class="sub">Username: <b>${esc(u.username)}</b></p>`:`<label>Username</label><input name="username" required pattern="[a-zA-Z0-9_.\\-]{3,30}">`}<label>Full name</label><input name="name" required value="${v('name')}">
   <label>Role</label>${sel('role',[['owner','Shop Owner (everything)'],['employee','Sales staff (sell, search stock)'],['purchasing','Purchasing (restock planning)']],u?u.role:'employee')}
   <label>${u?'New password (leave blank to keep)':'Password (min 6 characters)'}</label><input name="password" type="password" ${u?'':'required'} minlength="6" autocomplete="new-password">
   ${u?`<label><input type="checkbox" name="active" value="1" ${u.active?'checked':''} style="width:auto"> Account is active</label>`:''}`,
   async o=>{await api(u?'/users/'+u.id:'/users',u?'PUT':'POST',o);toast('Saved');V.users()})};

/* ---------- number steppers ---------- */
function stepper(){
  $$('input[type=number]').forEach(i=>{
    if(i.dataset.stp||i.closest('.cl,.ln'))return;i.dataset.stp='1';
    const w=document.createElement('span');w.className='numw';i.parentNode.insertBefore(w,i);w.appendChild(i);
    const up='<svg viewBox="0 0 10 10"><path d="M2 6.5 5 3.5l3 3"/></svg>',dn='<svg viewBox="0 0 10 10"><path d="M2 3.5 5 6.5l3-3"/></svg>';
    const p=document.createElement('span');p.className='stp';p.innerHTML=`<button type="button" tabindex="-1" aria-label="Increase">${up}</button><button type="button" tabindex="-1" aria-label="Decrease">${dn}</button>`;
    const go=d=>{if(i.disabled||i.readOnly)return;d>0?i.stepUp():i.stepDown();i.dispatchEvent(new Event('input',{bubbles:true}));i.dispatchEvent(new Event('change',{bubbles:true}))};
    p.children[0].onclick=()=>go(1);p.children[1].onclick=()=>go(-1);w.appendChild(p);
  });
}
new MutationObserver(stepper).observe(document.body,{childList:true,subtree:true});stepper();
/* ---------- boot ---------- */
(async()=>{
  
/* sidebar: hidden until the mouse nears the left edge */
document.addEventListener('mousemove',e=>{
  const sd=$('#side');if(!sd)return;const open=sd.classList.contains('open');
  if(e.clientX<=18){if(!open)setSide(true);else clearTimeout(sideT)}
  else if(open){const r=sd.getBoundingClientRect();
    if(e.clientX>r.right+24||e.clientY<r.top-24||e.clientY>r.bottom+24){if(!sideT&&!sd.contains(document.activeElement))sideT=setTimeout(()=>{sideT=null;setSide(false)},260)}
    else{clearTimeout(sideT);sideT=null}}
});
document.addEventListener('mouseleave',()=>{if($('#side')&&$('#side').classList.contains('open')&&matchMedia('(hover:hover)').matches){clearTimeout(sideT);sideT=setTimeout(()=>{sideT=null;setSide(false)},260)}});
document.addEventListener('click',e=>{const sd=$('#side');if(sd&&sd.classList.contains('open')&&!sd.contains(e.target)&&!e.target.closest('#burger'))setSide(false)});
document.addEventListener('keydown',e=>{if(e.key==='Escape')setSide(false)});
try{const t=localStorage.getItem('cs-theme');if(t)document.documentElement.dataset.theme=t}catch(e){}
  ME=await api('/me');$('#shop').textContent=ME.settings.shop_name;
  $('#nav').innerHTML=NAV.filter(n=>n[0]==='#'||n[2].includes(ME.role)).filter((n,i,a)=>n[0]!=='#'||(a[i+1]&&a[i+1][0]!=='#')).map(n=>n[0]==='#'?`<h6>${n[1]}</h6>`:`<a href="#${n[0]}" data-id="${n[0]}" data-nav="1"><span class="ni" aria-hidden="true">${NAVI[n[0]]||''}</span>${n[1]}</a>`).join('');
  $('#nav').addEventListener('click',e=>{if(e.target.closest('a'))(matchMedia('(hover:hover)').matches||setSide(false))});
  $('#me').innerHTML=`<div class="me-info"><b>${esc(ME.name)}</b><small>${({owner:'Shop Owner',employee:'Sales staff',purchasing:'Purchasing'})[ME.role]}</small></div><div class="me-actions"><button class="ib" data-act="pw">Password</button><button class="ib" data-act="logout">Sign out</button></div>`;
  syncTheme();
  if(can('owner')){$('#bell').style.display='';run(async()=>{const a=await api('/alerts');$('#belln').textContent=a.length})}
  if(!location.hash)history.replaceState(null,'','#dash');
  await render();
})();
</script></body></html>
"""


# Inline the logo as a data URI so the page never depends on a separate image request.
_LOGO_URI = "data:image/png;base64," + base64.b64encode(LOGO_PNG).decode()
LOGIN_HTML = LOGIN_HTML.replace('"/logo.png"', '"' + _LOGO_URI + '"')
APP_HTML = APP_HTML.replace('"/logo.png"', '"' + _LOGO_URI + '"')


def main():
    args = sys.argv[1:]
    port = int(os.environ.get("PORT", "8000"))
    if "--port" in args:
        port = int(args[args.index("--port") + 1])
    host = "0.0.0.0" if "--lan" in args else "127.0.0.1"
    init_db()
    server = ThreadingHTTPServer((host, port), Handler)
    url = f"http://localhost:{port}"
    print(f"\n  {APP_NAME} is running at {url}   (Ctrl+C to stop)")
    if host == "0.0.0.0":
        print("  LAN mode: other devices can connect using this computer's IP address and the same port.")
    print("  First time? Open the page and create your account. Shop owners start with an empty shop.\n")
    if "--no-browser" not in args:
        threading.Timer(0.8, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")


if __name__ == "__main__":
    main()
