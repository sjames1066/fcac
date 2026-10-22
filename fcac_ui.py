"""
FCAC window: a small local web page shown in its own app-style window (Edge or Chrome in --app mode).

fcac.py runs the server inside the tray process. The page only ever talks to 127.0.0.1, every request must carry a
random token, and nothing here needs an extra package. The backend object (see fcac.py, UIMixin) supplies:

    ui_snapshot() -> dict        everything the page shows
    ui_action(name, data) -> dict   install / uninstall / update / ...
    ui_logo(icao) -> (bytes, mime)
    ui_icon() -> bytes           the app icon as PNG
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import secrets
import subprocess
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from fcac_fonts import INTER

log = logging.getLogger("fcac")


def _browser_candidates() -> list[Path]:
    roots = [os.environ.get(k) for k in ("ProgramFiles(x86)", "ProgramFiles", "LOCALAPPDATA")]
    parts = [("Microsoft", "Edge", "Application", "msedge.exe"), ("Google", "Chrome", "Application", "chrome.exe")]
    return [Path(root, *part) for part in parts for root in roots if root]


class UIServer:
    def __init__(self, backend, profile_dir: Path) -> None:
        self.backend = backend
        self.profile_dir = profile_dir
        self.token = secrets.token_hex(16)
        self.httpd: ThreadingHTTPServer | None = None
        self.window: subprocess.Popen | None = None
        self._lock = threading.Lock()

    # server -------------------------------------------------------------
    def start(self) -> None:
        server = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *args) -> None:  # keep the log file quiet
                pass

            def _host_ok(self) -> bool:
                port = server.httpd.server_address[1]
                return self.headers.get("Host", "") in (f"127.0.0.1:{port}", f"localhost:{port}")

            def _token(self) -> str:
                return self.headers.get("X-Token") or parse_qs(urlparse(self.path).query).get("t", [""])[0]

            def _send(self, code: int, body: bytes, mime: str = "application/json") -> None:
                etag = None
                if mime.startswith("image/"):  # revalidate every time, so a new logo.png shows up straight away
                    etag = '"' + hashlib.md5(body).hexdigest() + '"'
                    if self.headers.get("If-None-Match") == etag:
                        code, body = 304, b""
                self.send_response(code)
                if code != 304:
                    self.send_header("Content-Type", mime)
                if etag:
                    self.send_header("ETag", etag)
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-cache" if etag else "no-store")
                self.send_header("X-Content-Type-Options", "nosniff")
                self.end_headers()
                self.wfile.write(body)

            def _json(self, code: int, data) -> None:
                self._send(code, json.dumps(data).encode("utf-8"))

            def _allowed(self) -> bool:
                if self._host_ok() and secrets.compare_digest(self._token(), server.token):
                    return True
                self._json(403, {"error": "forbidden"})
                return False

            def do_GET(self) -> None:  # noqa: N802
                if not self._allowed():
                    return
                path = urlparse(self.path).path
                try:
                    if path == "/":
                        self._send(200, server.page().encode("utf-8"), "text/html; charset=utf-8")
                    elif path == "/api/state":
                        self._json(200, server.backend.ui_snapshot())
                    elif path.startswith("/logo/"):
                        data, mime = server.backend.ui_logo(path[6:].split(".")[0].upper())
                        self._send(200, data, mime)
                    elif path == "/icon.png":
                        self._send(200, server.backend.ui_icon(), "image/png")
                    else:
                        self._json(404, {"error": "not found"})
                except Exception:
                    log.exception("UI request failed: %s", path)
                    self._json(500, {"error": "internal error"})

            def do_POST(self) -> None:  # noqa: N802
                if not self._allowed():
                    return
                path = urlparse(self.path).path
                try:
                    length = min(int(self.headers.get("Content-Length") or 0), 65536)
                    data = json.loads(self.rfile.read(length) or b"{}")
                    if not path.startswith("/api/") or not isinstance(data, dict):
                        self._json(404, {"error": "not found"})
                        return
                    self._json(200, server.backend.ui_action(path[5:], data))
                except Exception:
                    log.exception("UI action failed: %s", path)
                    self._json(500, {"error": "internal error"})

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.httpd.daemon_threads = True
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def url(self) -> str:
        return f"http://127.0.0.1:{self.httpd.server_address[1]}/?t={self.token}"

    # window -------------------------------------------------------------
    def open_window(self) -> None:
        """Open the FCAC window (or leave the already open one alone)."""
        with self._lock:
            if self.httpd is None:
                self.start()
            if self.window is not None and self.window.poll() is None:
                return
            url = self.url()
            for exe in _browser_candidates():
                if exe.is_file():
                    try:
                        self.profile_dir.mkdir(parents=True, exist_ok=True)
                        self.window = subprocess.Popen(
                            [str(exe), f"--app={url}", f"--user-data-dir={self.profile_dir}", "--window-size=1040,780",
                             "--no-first-run", "--no-default-browser-check"],
                            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                        )
                        return
                    except OSError:
                        log.exception("Could not start %s", exe)
            webbrowser.open(url)  # no Edge/Chrome found: the default browser still works

    def page(self) -> str:
        faces = "\n".join(
            f"@font-face{{font-family:'Inter';font-weight:{w};font-style:normal;font-display:swap;"
            f"src:url(data:font/woff;base64,{INTER[name]}) format('woff');}}"
            for name, w in (("Regular", 400), ("Medium", 500), ("SemiBold", 600), ("Bold", 700))
        )
        return PAGE.replace("/*FONTS*/", faces).replace("__TOKEN__", self.token)


PAGE = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>FCAC</title>
<link rel="icon" href="/icon.png?t=__TOKEN__">
<style>
/*FONTS*/
:root{
  --bg:#0d0a1a; --surface:#16122a; --surface2:#1e1938; --line:rgba(255,255,255,.08); --line2:rgba(255,255,255,.14);
  --text:#f5f3fc; --muted:#a09bbd; --faint:#6f6a8c;
  --purple:#6b2fd6; --magenta:#ce3a80; --orange:#ff9218;
  --grad:linear-gradient(120deg,#6b2fd6 0%,#ce3a80 55%,#ff9218 100%);
  --ok:#3ddc97; --warn:#ffb454; --err:#ff6b81;
}
*{box-sizing:border-box}
html,body{margin:0;height:100%}
body{background:radial-gradient(900px 500px at 8% -10%,rgba(107,47,214,.28),transparent 60%),
     radial-gradient(800px 480px at 100% 0%,rgba(255,146,24,.14),transparent 60%),var(--bg);
     color:var(--text);font:400 14px/1.5 'Inter',system-ui,'Segoe UI',sans-serif;-webkit-font-smoothing:antialiased;
     font-feature-settings:'cv11','ss01'}
.wrap{max-width:860px;margin:0 auto;padding:26px 28px 40px}
header{display:flex;align-items:center;gap:14px;margin-bottom:22px}
header img{width:46px;height:46px;border-radius:12px;box-shadow:0 6px 20px rgba(107,47,214,.45)}
header h1{font-size:21px;font-weight:700;margin:0;letter-spacing:-.02em}
header p{margin:0;color:var(--muted);font-size:12.5px}
.spacer{flex:1}
.switch{display:flex;align-items:center;gap:10px;color:var(--muted);font-size:13px;cursor:pointer;user-select:none}
.track{width:40px;height:23px;border-radius:99px;background:var(--surface2);border:1px solid var(--line2);position:relative;transition:.2s}
.track::after{content:"";position:absolute;top:2px;left:2px;width:17px;height:17px;border-radius:50%;background:#cfc9ea;transition:.2s}
.switch.on .track{background:var(--grad);border-color:transparent}
.switch.on .track::after{left:19px;background:#fff}
.card{background:linear-gradient(180deg,rgba(255,255,255,.035),rgba(255,255,255,.015));border:1px solid var(--line);
      border-radius:18px;padding:20px 22px;backdrop-filter:blur(6px)}
.label{font-size:11px;font-weight:600;letter-spacing:.09em;text-transform:uppercase;color:var(--faint);margin:0 0 12px}
.hero{display:flex;align-items:center;gap:20px;margin-bottom:26px}
.hero .logo{width:68px;height:68px;border-radius:16px;flex:none;object-fit:cover;background:var(--surface2)}
.route{font-size:34px;font-weight:700;letter-spacing:-.03em;line-height:1.1}
.route .arrow{background:var(--grad);-webkit-background-clip:text;background-clip:text;color:transparent;margin:0 6px}
.dest-name{color:var(--muted);margin-top:2px}
.msg{margin-top:10px;font-size:13px;color:var(--muted)}
.msg b{color:var(--text);font-weight:600}
.hero .side{margin-left:auto;display:flex;flex-direction:column;align-items:flex-end;gap:8px;text-align:right}
.small{font-size:12px;color:var(--faint)}
button{font:600 13px 'Inter',sans-serif;color:var(--text);background:var(--surface2);border:1px solid var(--line2);
       border-radius:11px;padding:9px 16px;cursor:pointer;transition:.15s}
button:hover{border-color:rgba(255,255,255,.3);background:#262047}
button:disabled{opacity:.45;cursor:default}
button.primary{background:var(--grad);border-color:transparent;color:#fff;box-shadow:0 6px 18px rgba(206,58,128,.3)}
button.primary:hover{filter:brightness(1.1)}
button.ghost{background:transparent}
button.danger:hover{border-color:var(--err);color:var(--err)}
input[type=text]{font:500 14px 'Inter',sans-serif;color:var(--text);background:var(--bg);border:1px solid var(--line2);
       border-radius:11px;padding:10px 13px;width:100%;outline:none}
input[type=text]:focus{border-color:var(--magenta)}
.section-head{display:flex;align-items:baseline;gap:10px;margin:0 2px 12px}
.section-head h2{margin:0;font-size:15px;font-weight:600}
.section-head span{color:var(--faint);font-size:12.5px}
.packs{display:grid;grid-template-columns:1fr;gap:12px}
.pack{padding:16px 20px}
.pack .top{display:flex;align-items:center;gap:16px}
.pack .logo{width:56px;height:56px;border-radius:14px;flex:none;object-fit:cover;background:var(--surface2)}
.pack .name{font-weight:600;font-size:15px;letter-spacing:-.01em}
.pack .meta{color:var(--muted);font-size:12.5px}
.chip{display:inline-block;font-size:11px;font-weight:600;padding:2px 9px;border-radius:99px;margin-left:8px;vertical-align:1px}
.chip.ok{background:rgba(61,220,151,.14);color:var(--ok)}
.chip.up{background:rgba(255,180,84,.15);color:var(--warn)}
.pack .act{margin-left:auto;display:flex;gap:8px;align-items:center}
.pack .live{width:340px}
.bar{height:8px;border-radius:99px;background:rgba(255,255,255,.08);overflow:hidden}
.bar i{display:block;height:100%;width:0;border-radius:99px;background:var(--grad);transition:width .35s ease}
.bar.indet i{width:38%!important;animation:slide 1.1s ease-in-out infinite}
@keyframes slide{0%{margin-left:-40%}100%{margin-left:102%}}
.prog{display:flex;justify-content:space-between;font-size:12px;color:var(--muted);margin-bottom:7px}
.prog b{color:var(--text);font-weight:600}
.err{color:var(--err);font-size:12.5px}
.empty{color:var(--muted);padding:26px;text-align:center;border:1px dashed var(--line2);border-radius:16px}
footer{margin-top:28px;display:flex;gap:6px;flex-wrap:wrap;align-items:center;color:var(--faint);font-size:12.5px}
button.kofi{margin-left:6px;background:rgba(255,146,24,.12);border-color:rgba(255,146,24,.45);color:#ffb454!important;font-weight:600!important}
button.kofi span{color:#ff6b81;margin-right:4px}
button.kofi:hover{background:rgba(255,146,24,.22)}
  .support{margin-top:30px;text-align:center;color:var(--faint);font-size:12.5px}
footer button{padding:7px 12px;font-weight:500;font-size:12.5px;color:var(--muted)}
.chips{display:flex;flex-wrap:wrap;gap:8px;margin-bottom:14px;min-height:34px}
.chip2{display:inline-flex;align-items:center;gap:8px;padding:6px 8px 6px 14px;border-radius:99px;background:var(--surface2);border:1px solid var(--line2);font-weight:600}
.chip2 .to{color:var(--faint);font-weight:500}
.chip2 button{padding:0;width:22px;height:22px;border-radius:50%;line-height:1;font-size:15px;background:transparent;border-color:transparent;color:var(--muted)}
.chip2 button:hover{background:rgba(255,107,129,.18);color:var(--err)}
.addrow{display:flex;align-items:center;gap:10px}
.addrow input{width:96px;text-transform:uppercase;text-align:center;letter-spacing:.06em;font-weight:600}
.modal{position:fixed;inset:0;background:rgba(5,3,14,.72);backdrop-filter:blur(5px);display:none;align-items:center;justify-content:center;z-index:5}
.modal.show{display:flex}
.dialog{background:var(--surface);border:1px solid var(--line2);border-radius:20px;padding:26px;width:440px;max-width:92vw;
        box-shadow:0 30px 80px rgba(0,0,0,.6)}
.dialog h3{margin:0 0 4px;font-size:18px;letter-spacing:-.02em}
.dialog p{margin:0 0 16px;color:var(--muted)}
.opts{display:grid;grid-template-columns:1fr 1fr;gap:10px;margin-bottom:16px}
.opt{padding:14px;border-radius:13px;border:1px solid var(--line2);background:var(--surface2);cursor:pointer;font-weight:600;text-align:center}
.opt.sel{border-color:transparent;background:var(--grad);color:#fff}
.check{display:flex;gap:9px;align-items:center;color:var(--muted);margin-bottom:18px;cursor:pointer;user-select:none}
.row{display:flex;gap:10px;justify-content:flex-end}
.toast{position:fixed;bottom:22px;left:50%;transform:translateX(-50%);background:var(--surface2);border:1px solid var(--line2);
       padding:10px 16px;border-radius:12px;opacity:0;transition:.25s;pointer-events:none}
.toast.show{opacity:1}
</style>
</head>
<body>
<div class="wrap">
  <header>
    <img src="/icon.png?t=__TOKEN__" alt="">
    <div><h1>FCAC</h1><p>Fenix Cabin Announcements Configurator</p></div>
    <div class="spacer"></div>
    <div class="switch" id="auto"><span>Auto-update</span><div class="track"></div></div>
  </header>

  <section class="card hero" id="hero"></section>

  <div class="section-head"><h2>Airline packs</h2><span id="packsub"></span></div>
  <div class="packs" id="packs"></div>

  <div class="section-head" style="margin-top:30px"><h2>Airline aliases</h2><span>Use another airline's recordings, e.g. BA Shuttle (SHT) with the BAW pack</span></div>
  <section class="card" id="aliases">
    <div class="chips" id="alias-list"></div>
    <div class="addrow">
      <input type="text" id="a-alias" maxlength="3" placeholder="Airline" autocomplete="off" spellcheck="false">
      <span class="small">uses</span>
      <input type="text" id="a-source" maxlength="3" placeholder="Pack" list="a-sources" autocomplete="off" spellcheck="false">
      <datalist id="a-sources"></datalist>
      <button class="primary" id="a-add">Add alias</button>
    </div>
    <div class="small" style="margin-top:10px">Fenix only uses an airline pack when its folder matches the livery's airline code, so FCAC builds a pack folder for the alias from the other airline's files.</div>
  </section>

  <footer>
    <button class="ghost" data-open="airlines">Your recordings</button>
    <button class="ghost" data-open="fenix">Fenix packs folder</button>
    <button class="ghost" data-open="log">View log</button>
    <button class="ghost" id="settings">SimBrief username</button>
    <button class="ghost" id="refresh">Refresh packs</button>
    <button class="ghost" id="forget">Forget terminal choices</button>
  </footer>
  <div class="support">Enjoying FCAC? <button class="kofi" data-open="kofi"><span>&#9829;</span> Buy me a Ko-fi</button><div class="small" id="ver" style="margin-top:14px"></div><div class="small" id="lib" style="margin-top:4px"></div></div>
</div>

<div class="modal" id="m-choice"><div class="dialog">
  <h3 id="c-title"></h3><p id="c-text"></p>
  <div class="opts" id="c-opts"></div>
  <label class="check"><input type="checkbox" id="c-remember"> Remember my choice for this airport</label>
  <div class="row"><button class="primary" id="c-ok">Use this one</button></div>
</div></div>

<div class="modal" id="m-name"><div class="dialog">
  <h3>SimBrief username</h3>
  <p>The name you log in to SimBrief with (not the numeric ID). FCAC reads your latest flight plan from it.</p>
  <input type="text" id="n-input" placeholder="e.g. sjames1066" autocomplete="off" spellcheck="false">
  <div class="row" style="margin-top:18px"><button class="ghost" id="n-cancel">Cancel</button><button class="primary" id="n-ok">Save</button></div>
</div></div>

<div class="toast" id="toast"></div>

<script>
const TOKEN="__TOKEN__";
const $=s=>document.querySelector(s);
const el=(tag,cls,text)=>{const e=document.createElement(tag);if(cls)e.className=cls;if(text!==undefined)e.textContent=text;return e};
let snap=null, structSig="", promptId=null, selected=null, setupShown=false;

async function api(name,data){
  try{const r=await fetch("/api/"+name,{method:"POST",headers:{"X-Token":TOKEN,"Content-Type":"application/json"},body:JSON.stringify(data||{})});
      const j=await r.json(); if(j.error) toast(j.error); return j;}catch(e){toast("Lost contact with FCAC");return {}}
}
function toast(t){const x=$("#toast");x.textContent=t;x.classList.add("show");clearTimeout(toast.t);toast.t=setTimeout(()=>x.classList.remove("show"),3200)}
function logo(icao,cls){const i=el("img",cls);i.alt=icao;i.src="/logo/"+icao+".png?t="+TOKEN+"&v="+(snap&&snap.logo_v||0);return i}

let heroSig="";
function renderHero(){
  const sig=JSON.stringify([snap.setup_needed,snap.flight,snap.message,snap.updated,snap.busy,snap.logo_v]);
  if(sig===heroSig) return; heroSig=sig;
  const h=$("#hero"); h.replaceChildren();
  const f=snap.flight;
  if(snap.setup_needed){
    const box=el("div"); box.style.flex="1";
    box.append(el("p","label","Get started"));
    const t=el("div","route","Welcome aboard"); box.append(t);
    box.append(el("div","dest-name","Enter your SimBrief username and FCAC will fit your announcements to every flight."));
    const b=el("button","primary","Set SimBrief username"); b.style.marginTop="14px"; b.onclick=openName; box.append(b);
    h.append(box); return;
  }
  if(f&&f.airline){h.append(logo(f.airline,"logo"))}
  const mid=el("div"); mid.style.minWidth="0";
  mid.append(el("p","label","Your latest SimBrief flight"));
  if(f&&f.dest){
    const r=el("div","route"); r.append(document.createTextNode(f.airline||"???"));
    r.append(el("span","arrow","→")); r.append(document.createTextNode(f.dest)); mid.append(r);
    if(f.name) mid.append(el("div","dest-name",f.name));
  }else mid.append(el("div","route","No flight yet"));
  const m=el("div","msg"); m.textContent=snap.message||""; mid.append(m);
  h.append(mid);
  const side=el("div","side");
  const b=el("button","primary","Update now"); b.disabled=snap.busy; b.onclick=()=>api("update"); side.append(b);
  if(snap.updated) side.append(el("span","small","Last applied "+snap.updated));
  h.append(side);
}

function packCard(p){
  const c=el("div","card pack"); c.id="pack-"+p.icao;
  const top=el("div","top"); top.append(logo(p.icao,"logo"));
  const info=el("div"); const n=el("div","name",p.name);
  if(p.installed){const ch=el("span","chip "+(p.update?"up":"ok"),p.update?"Update available":"Installed");n.append(ch)}
  info.append(n);
  info.append(el("div","meta",[p.icao,p.version?"v"+p.version:"",p.size_mb?p.size_mb+" MB":""].filter(Boolean).join("  ·  ")));
  top.append(info);
  const act=el("div","act");
  if(!p.task){
    if(!p.installed||p.update){const b=el("button","primary",p.update?"Update":"Install");b.disabled=snap.busy;b.onclick=()=>api("install",{icao:p.icao});act.append(b)}
    if(p.installed){const b=el("button","danger","Uninstall");b.disabled=snap.busy;b.onclick=()=>{if(confirm("Uninstall the "+p.name+" pack?\nYour own recordings stay."))api("uninstall",{icao:p.icao})};act.append(b)}
  }
  if(p.task){
    const live=el("div","live");
    const pr=el("div","prog"); const a=el("span"); a.id="pt-"+p.icao; const b=el("b"); b.id="pp-"+p.icao; pr.append(a,b);
    const bar=el("div","bar"); bar.id="pb-"+p.icao; bar.append(el("i")); live.append(pr,bar); act.append(live);
  }
  top.append(act); c.append(top);
  return c;
}
function renderPacks(){
  const box=$("#packs"); const sig=JSON.stringify([snap.packs.map(p=>[p.icao,p.installed,p.update,!!p.task,p.name,p.version]),snap.busy,snap.catalog_error,snap.logo_v]);
  if(sig!==structSig){
    structSig=sig; box.replaceChildren();
    if(!snap.packs.length){
      const e=el("div","empty",snap.catalog_error||"No airline packs available yet."); e.style.gridColumn="1/-1"; box.append(e);
    }
    snap.packs.forEach(p=>box.append(packCard(p)));
  }
  snap.packs.forEach(p=>{
    if(!p.task) return;
    const t=$("#pt-"+p.icao),pp=$("#pp-"+p.icao),pb=$("#pb-"+p.icao); if(!t) return;
    t.textContent=p.task.text||"Working";
    const f=p.task.frac;
    pb.classList.toggle("indet",f===null||f===undefined);
    if(f!==null&&f!==undefined){pp.textContent=Math.round(f*100)+"%";pb.firstChild.style.width=(f*100)+"%"}else pp.textContent="";
  });
  const inst=snap.packs.filter(p=>p.installed).length;
  $("#packsub").textContent=snap.packs.length?inst+" of "+snap.packs.length+" installed":"";
  const errs=snap.packs.filter(p=>p.error);
}
let aliasSig="";
function renderAliases(){
  const sig=JSON.stringify([snap.aliases,snap.alias_sources]); if(sig===aliasSig) return; aliasSig=sig;
  const box=$("#alias-list"); box.replaceChildren();
  const entries=Object.entries(snap.aliases||{});
  if(!entries.length) box.append(el("span","small","No aliases yet."));
  entries.forEach(([a,s])=>{
    const c=el("span","chip2"); c.append(document.createTextNode(a)); c.append(el("span","to","\u2192")); c.append(document.createTextNode(s));
    const x=el("button",null,"\u00d7"); x.title="Remove"; x.onclick=()=>api("alias_remove",{alias:a}); c.append(x); box.append(c);
  });
  const dl=$("#a-sources"); dl.replaceChildren(); (snap.alias_sources||[]).forEach(v=>{const o=document.createElement("option");o.value=v;dl.append(o)});
}
$("#a-add").onclick=async()=>{
  const a=$("#a-alias").value.trim(), s=$("#a-source").value.trim(); if(!a||!s){toast("Enter both airline codes");return}
  const r=await api("alias_add",{alias:a,source:s}); if(!r.error){$("#a-alias").value="";$("#a-source").value="";toast(a.toUpperCase()+" now uses "+s.toUpperCase()+" recordings")}
};
["a-alias","a-source"].forEach(id=>$("#"+id).addEventListener("keydown",e=>{if(e.key==="Enter")$("#a-add").click()}));
function renderPrompt(){
  const p=snap.prompt, m=$("#m-choice");
  if(!p){m.classList.remove("show");promptId=null;return}
  if(promptId===p.id) return;
  promptId=p.id; selected=p.options[0];
  $("#c-title").textContent=p.title; $("#c-text").textContent=p.text; $("#c-remember").checked=false;
  const o=$("#c-opts"); o.replaceChildren();
  p.options.forEach(name=>{const d=el("div","opt"+(name===selected?" sel":""),name);
    d.onclick=()=>{selected=name;[...o.children].forEach(x=>x.classList.toggle("sel",x===d))}; o.append(d)});
  m.classList.add("show");
}
$("#c-ok").onclick=async()=>{await api("answer",{id:promptId,choice:selected,remember:$("#c-remember").checked});$("#m-choice").classList.remove("show");promptId="done:"+promptId};

function openName(){ $("#n-input").value=snap.username||""; $("#m-name").classList.add("show"); setTimeout(()=>$("#n-input").focus(),50) }
$("#settings").onclick=openName;
$("#n-cancel").onclick=()=>$("#m-name").classList.remove("show");
$("#n-ok").onclick=async()=>{const v=$("#n-input").value.trim(); if(!v)return; $("#m-name").classList.remove("show"); await api("username",{name:v}); toast("Saved - fetching your flight")};
$("#n-input").addEventListener("keydown",e=>{if(e.key==="Enter")$("#n-ok").click()});
$("#auto").onclick=()=>api("auto",{on:!snap.auto});
$("#refresh").onclick=async()=>{await api("refresh");toast("Refreshing pack list")};
$("#forget").onclick=async()=>{await api("forget");toast("Remembered choices cleared")};
document.querySelectorAll("[data-open]").forEach(b=>b.onclick=()=>api("open",{what:b.dataset.open}));

async function tick(){
  try{
    const r=await fetch("/api/state",{headers:{"X-Token":TOKEN}}); snap=await r.json();
    $("#auto").classList.toggle("on",!!snap.auto); $("#ver").textContent="FCAC v"+(snap.version||""); $("#lib").textContent=snap.library?"Recordings folder: "+snap.library:"";
    renderHero(); renderPacks(); renderAliases(); renderPrompt();
    if(snap.setup_needed&&!setupShown){setupShown=true;openName()}
  }catch(e){}
  setTimeout(tick,(snap&&(snap.busy||snap.prompt))?500:1500);
}
tick();
</script>
</body>
</html>
"""
