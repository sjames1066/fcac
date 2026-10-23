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
import sys
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from fcac_fonts import INTER

log = logging.getLogger("fcac")


def work_area() -> tuple[int, int, int, int]:
    """(left, top, width, height) of the usable desktop on Windows, in the units Chromium's window flags use."""
    try:
        import ctypes
        from ctypes import wintypes

        rect = wintypes.RECT()
        if ctypes.windll.user32.SystemParametersInfoW(48, 0, ctypes.byref(rect), 0):  # SPI_GETWORKAREA
            return rect.left, rect.top, rect.right - rect.left, rect.bottom - rect.top
    except Exception:
        pass
    return 0, 0, 1920, 1040


def centered_geometry(width: int = 1040, height: int = 800) -> tuple[int, int, int, int]:
    """Window size (shrunk to fit small screens) and the position that puts it in the middle of the screen."""
    left, top, area_w, area_h = work_area()
    width, height = min(width, area_w - 40), min(height, area_h - 40)
    return left + (area_w - width) // 2, top + (area_h - height) // 2, width, height


def _browser_candidates() -> list[Path]:
    roots = [os.environ.get(k) for k in ("ProgramFiles(x86)", "ProgramFiles", "LOCALAPPDATA")]
    parts = [("Microsoft", "Edge", "Application", "msedge.exe"), ("Google", "Chrome", "Application", "chrome.exe")]
    return [Path(root, *part) for part in parts for root in roots if root]


def raise_window(title: str = "FCAC", wait: float = 8.0) -> None:
    """Windows: bring the FCAC window to the front of everything. It is found by its title once it has appeared, and
    Windows' rules against stealing focus are got around by a brief 'always on top'. Runs in the background."""
    if not sys.platform.startswith("win"):
        return

    def work() -> None:
        try:
            import ctypes
            from ctypes import wintypes

            user32 = ctypes.windll.user32
            user32.SetWindowPos.argtypes = [wintypes.HWND, wintypes.HWND, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                                            ctypes.c_int, wintypes.UINT]
            user32.IsWindowVisible.argtypes = [wintypes.HWND]
            user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
            found: list = []
            enum_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

            def visit(hwnd, _lparam):
                if user32.IsWindowVisible(hwnd):
                    buf = ctypes.create_unicode_buffer(256)
                    user32.GetWindowTextW(hwnd, buf, 256)
                    if buf.value == title:
                        found.append(hwnd)
                return True

            deadline = time.time() + wait
            while time.time() < deadline and not found:
                user32.EnumWindows(enum_type(visit), 0)
                if not found:
                    time.sleep(0.25)
            if not found:
                return
            hwnd = found[0]
            user32.ShowWindow(hwnd, 9)  # SW_RESTORE: also un-minimizes
            user32.keybd_event(0x12, 0, 0, 0)  # a tap of Alt lets this process take the foreground
            user32.keybd_event(0x12, 0, 2, 0)
            user32.SetForegroundWindow(hwnd)
            flags = 0x0001 | 0x0002 | 0x0040  # SWP_NOSIZE | SWP_NOMOVE | SWP_SHOWWINDOW
            user32.SetWindowPos(hwnd, wintypes.HWND(-1), 0, 0, 0, 0, flags)  # HWND_TOPMOST...
            user32.SetWindowPos(hwnd, wintypes.HWND(-2), 0, 0, 0, 0, flags)  # ...then back to normal
            user32.SetForegroundWindow(hwnd)
        except Exception:
            log.exception("Could not bring the window to the front")

    threading.Thread(target=work, daemon=True).start()


class UIServer:
    def __init__(self, backend, profile_dir: Path) -> None:
        self.backend = backend
        self.profile_dir = profile_dir
        self.token = secrets.token_hex(16)
        self.httpd: ThreadingHTTPServer | None = None
        self.window: subprocess.Popen | None = None
        self.last_poll = 0.0
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
                parsed = urlparse(self.path)
                path = parsed.path
                try:
                    if path == "/":
                        self._send(200, server.page().encode("utf-8"), "text/html; charset=utf-8")
                    elif path == "/api/state":
                        server.last_poll = time.monotonic()
                        self._json(200, server.backend.ui_snapshot())
                    elif path.startswith("/logo/"):
                        data, mime = server.backend.ui_logo(path[6:].split(".")[0].upper())
                        self._send(200, data, mime)
                    elif path == "/icon.png":
                        self._send(200, server.backend.ui_icon(), "image/png")
                    elif path == "/studio/logo":  # the pack's own logo, for the preview
                        q = parse_qs(parsed.query)
                        data = server.backend.studio_logo(q.get("icao", [""])[0], q.get("voice", [""])[0])
                        if data is None:
                            self._json(404, {"error": "not found"})
                        else:
                            self._send(200, data, "image/png")
                    elif path == "/studio/audio":  # a processed clip, for the play button
                        q = parse_qs(parsed.query)
                        data = server.backend.studio_audio(q.get("icao", [""])[0], q.get("voice", [""])[0], q.get("slot", [""])[0])
                        if data is None:
                            self._json(404, {"error": "not found"})
                        else:
                            self._send(200, data, "audio/ogg")
                    else:
                        self._json(404, {"error": "not found"})
                except Exception:
                    log.exception("UI request failed: %s", path)
                    self._json(500, {"error": "internal error"})

            def do_POST(self) -> None:  # noqa: N802
                if not self._allowed():
                    return
                parsed = urlparse(self.path)
                path = parsed.path
                try:
                    if path == "/studio/upload":  # raw audio bytes; details in the query string
                        q = parse_qs(parsed.query)
                        size = int(self.headers.get("Content-Length") or 0)
                        if size <= 0 or size > 60 * 1024 * 1024:
                            self.close_connection = True
                            self._json(413, {"error": "That file is empty or too large (60 MB limit)."})
                            return
                        body = self.rfile.read(size)
                        self._json(200, server.backend.studio_upload(
                            q.get("icao", [""])[0], q.get("voice", [""])[0], q.get("slot", [""])[0],
                            q.get("filename", ["take.wav"])[0], body))
                        return
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
                raise_window()  # already open (maybe behind other windows): bring it forward
                return
            url = self.url()
            for exe in _browser_candidates():
                if exe.is_file():
                    try:
                        self.profile_dir.mkdir(parents=True, exist_ok=True)
                        x, y, w, h = centered_geometry()
                        self.window = subprocess.Popen(
                            [str(exe), f"--app={url}", f"--user-data-dir={self.profile_dir}", f"--window-size={w},{h}",
                             f"--window-position={x},{y}",
                             "--no-first-run", "--no-default-browser-check", "--hide-crash-restore-bubble",
                             "--use-fake-ui-for-media-stream"],   # no permission pop-up for the microphone (this window only shows FCAC itself)
                            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                        )
                        raise_window()
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
     radial-gradient(800px 480px at 100% 0%,rgba(255,146,24,.14),transparent 60%),var(--bg);background-attachment:fixed;
     color:var(--text);font:400 14px/1.5 'Inter',system-ui,'Segoe UI',sans-serif;-webkit-font-smoothing:antialiased;
     font-feature-settings:'cv11','ss01'}
.wrap{max-width:860px;margin:0 auto;padding:26px 28px 18px;min-height:100vh;display:flex;flex-direction:column}
.verbar{margin-top:auto;padding-top:26px;text-align:center;font-size:12px;font-weight:500;color:var(--faint);letter-spacing:.04em}
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
.hero .company{font-size:15px;font-weight:600;color:var(--text);margin-bottom:2px}
.hero .company .ids{font-weight:500;color:var(--muted);letter-spacing:.02em}
.hero .company .nm + .ids{margin-left:12px;padding-left:12px;border-left:1px solid var(--line2)}
.debug{margin:-12px 0 22px;padding:12px 16px;border:1px solid rgba(255,93,93,.45);border-radius:14px;background:rgba(255,93,93,.07);
       color:#ff6b6b;font:12px/1.55 Consolas,'Cascadia Mono',monospace;white-space:pre-wrap;word-break:break-word;max-height:220px;overflow:auto}
.debug::before{content:"DEBUG - hidden in the release build";display:block;font:700 10px 'Inter',sans-serif;letter-spacing:.09em;text-transform:uppercase;color:#ff4d4d;margin-bottom:6px}
.hero .side .credit a{color:var(--muted);text-decoration:none}
.hero .side .desc{font-style:italic;max-width:300px}
.voicerow{display:flex;align-items:center;gap:10px}
select.voice{font:600 13px 'Inter',sans-serif;color:var(--text);background:var(--surface2);border:1px solid var(--line2);border-radius:11px;padding:9px 12px;cursor:pointer;color-scheme:dark}
select.voice:disabled{opacity:.45}
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
.facts{display:grid;gap:10px}
.facts div{background:var(--surface2);border:1px solid var(--line);border-radius:12px;padding:11px 14px;display:flex;flex-direction:column;gap:2px}
.facts b{font-weight:600}
.facts span{font:500 12px ui-monospace,Consolas,monospace;color:#d9b8ff;word-break:break-all}
.facts i{font-style:normal;color:var(--muted);font-size:12.5px}
.setting{margin-bottom:18px}
.setting .label{margin-bottom:8px}
.radios{display:grid;gap:8px}
.radio{padding:11px 14px;border-radius:12px;border:1px solid var(--line2);background:var(--surface2);cursor:pointer;display:flex;flex-direction:column;gap:1px}
.radio b{font-weight:600}
.radio span{color:var(--muted);font-size:12.5px}
.radio.sel{border-color:transparent;background:linear-gradient(var(--surface2),var(--surface2)) padding-box,var(--grad) border-box;border:1px solid transparent;box-shadow:0 0 0 1px rgba(206,58,128,.35)}
.modal{position:fixed;inset:0;background:rgba(5,3,14,.72);backdrop-filter:blur(5px);display:none;align-items:center;justify-content:center;z-index:5}
.modal.show{display:flex}
#m-settings .row{position:sticky;bottom:-26px;background:var(--surface);padding:12px 0 2px}
.dialog{max-height:92vh;overflow-y:auto;background:var(--surface);border:1px solid var(--line2);border-radius:20px;padding:26px;width:440px;max-width:92vw;
        box-shadow:0 30px 80px rgba(0,0,0,.6)}
.dialog h3{margin:0 0 4px;font-size:18px;letter-spacing:-.02em}
.dialog p{margin:0 0 16px;color:var(--muted)}
.opts{display:grid;grid-template-columns:1fr 1fr;gap:10px;margin-bottom:16px}
.opt{padding:14px;border-radius:13px;border:1px solid var(--line2);background:var(--surface2);cursor:pointer;font-weight:600;text-align:center}
.opt.sel{border-color:transparent;background:var(--grad);color:#fff}
.check{display:flex;gap:9px;align-items:center;color:var(--muted);margin-bottom:18px;cursor:pointer;user-select:none}
.row{display:flex;gap:10px;justify-content:flex-end}
.tabs{display:flex;gap:6px;margin:0 0 18px}
.tabs button{background:transparent;border-color:transparent;color:var(--muted);padding:8px 16px;border-radius:999px}
.tabs button.on{background:var(--surface2);border-color:var(--line2);color:var(--text)}
.sd-grid{display:grid;grid-template-columns:1fr 1fr;gap:10px}
.sd-grid label{display:flex;flex-direction:column;gap:4px;color:var(--faint);font-size:12px}
.sd-grid label.wide{grid-column:1/-1}
.sd-grid input{width:100%;box-sizing:border-box}
.sd-meta{color:var(--muted);font-size:12.5px;margin-top:12px;line-height:1.5}
.prog{height:8px;border-radius:99px;background:var(--surface2);overflow:hidden;margin:6px 0 4px}
.prog i{display:block;height:100%;background:var(--grad)}
.logorow{display:flex;align-items:center;gap:14px;margin-top:16px;flex-wrap:wrap}
.logorow > div{flex:1;min-width:200px}
.logoimg{width:56px;height:56px;border-radius:14px;object-fit:cover;background:var(--surface2)}
details.grp{margin:14px 0}
details.grp summary{cursor:pointer;font-weight:600;font-size:14px;padding:6px 2px;color:var(--text)}
details.grp.sub{margin:6px 0 6px 16px;padding-left:10px;border-left:2px solid var(--line,#ddd)}
details.grp.sub summary{font-weight:500;font-size:13.5px}
details.grp summary span{color:var(--faint);font-weight:500;font-size:12.5px;margin-left:8px}
.slot{display:grid;grid-template-columns:1fr auto;gap:2px 12px;align-items:center;padding:9px 12px;margin:6px 0;border:1px solid var(--line);border-radius:12px;background:var(--surface2)}
.slot.done{border-color:rgba(80,200,140,.35)}
.slot.skipped{border-color:rgba(240,200,60,.6);background:rgba(240,200,60,.09)}
.slot .nm2 em.sk{color:#f0c83c}
.slot .nm2{font-weight:600}
.slot .nm2 em{font-style:normal;color:var(--err);margin-left:6px;font-size:11px}
.slot .sc{grid-column:1;color:var(--faint);font-size:12.5px}
.slot .st{grid-column:1;color:var(--ok,#7ee0a8);font-size:12px}
.slot .act{grid-column:2;grid-row:1/4;display:flex;gap:6px;align-items:center}
.slot .act button{padding:6px 11px}
.slot .act button.sq{width:32px;height:32px;padding:0;display:inline-flex;align-items:center;justify-content:center;border-radius:8px}
.slot .sc.custom{white-space:pre-wrap}
.slot .sc.editable{cursor:text;border-radius:6px}
.slot .sc.editable:hover{background:rgba(255,255,255,.05)}
.slot.reading .sc.editable{cursor:default}
textarea.sc-edit{width:100%;box-sizing:border-box;resize:none;overflow:hidden;font:500 13px 'Inter',sans-serif;color:var(--text);background:var(--bg);border:1px solid var(--magenta);border-radius:8px;padding:7px 9px;outline:none}
.slot .act button.rec{background:rgba(255,107,129,.16);border-color:rgba(255,107,129,.5)}
.slot .act button.live{background:var(--err);color:#fff;border-color:transparent;min-width:112px;font-variant-numeric:tabular-nums}
#view-studio{overflow-anchor:none}
.addline{display:flex;gap:8px;margin:8px 0}
.addline input{flex:1}
.chiprow{display:flex;gap:6px;flex-wrap:wrap;margin:6px 0}
.sdbar{position:sticky;bottom:0;background:linear-gradient(transparent,var(--bg,#0d0a1f) 30%);padding:18px 0 4px;display:flex;gap:10px;justify-content:flex-end;flex-wrap:wrap}
.meter{height:14px;border-radius:99px;background:var(--surface2);overflow:hidden;position:relative;margin:8px 0 4px}
.meter i{position:absolute;left:0;top:0;bottom:0;width:0;background:linear-gradient(90deg,#4cd08a 0 70%,#f0c040 70% 90%,#ff6b81 90%);transition:width .06s}
.meter b{position:absolute;top:0;bottom:0;width:2px;background:#fff;left:0}
.slot.reading{border-color:var(--magenta);background:linear-gradient(var(--surface2),var(--surface2)) padding-box}
.slot.reading .sc{font-size:20px;line-height:1.35;color:var(--text);padding:6px 0}
.slot .sc.custom::before{content:"script · ";color:var(--magenta);font-size:12px}
#m-mic select{width:100%}
#view-studio>.section-head:not(:first-child){margin-top:30px}
.prog.wait i{width:40%!important;animation:slide 1.1s ease-in-out infinite alternate}
@keyframes slide{from{margin-left:0}to{margin-left:60%}}
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

  <nav class="tabs"><button id="tab-main" class="on">Dashboard</button><button id="tab-studio">Pack Studio</button></nav>
  <div id="view-main">
  <section class="card hero" id="hero"></section>
  <pre id="debug" class="debug" style="display:none"></pre>

  <div class="section-head"><h2>Airline packs</h2><span id="packsub"></span></div>
  <div class="packs" id="packs"></div>

  <footer>
    <button class="ghost" data-open="airlines">Your recordings</button>
    <button class="ghost" data-open="fenix">Fenix packs folder</button>
    <button class="ghost" id="aliases-btn">Airline aliases</button>
    <button class="ghost" data-open="log">View log</button>
    <button class="ghost" id="settings">Settings</button>
    <button class="ghost" id="refresh">Refresh packs</button>
    <button class="ghost" id="forget">Forget terminal choices</button>
  </footer>
  </div>
  <div id="view-studio" style="display:none"></div>
  <div class="support">Enjoying FCAC? <button class="kofi" data-open="kofi"><span>&#9829;</span> Buy me a Ko-fi</button><div class="small" id="lib" style="margin-top:14px"></div></div>
  <div class="verbar" id="ver"></div>
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
  <input type="text" id="n-input" placeholder="e.g. captainsmith123" autocomplete="off" spellcheck="false">
  <div class="row" style="margin-top:18px"><button class="ghost" id="n-cancel">Cancel</button><button class="primary" id="n-ok">Save</button></div>
</div>
</div>

<div class="modal" id="m-welcome"><div class="dialog" style="width:520px">
  <h3>Welcome to FCAC</h3>
  <p>Before you start, here is what FCAC changes on this PC:</p>
  <div class="facts">
    <div><b>Your Fenix airline packs</b><span id="w-fenix"></span><i>FCAC replaces the welcome-aboard, arrival and refuelling announcements in these folders with the recording for your destination. Nothing else is touched.</i></div>
    <div><b>Your originals are kept</b><span id="w-orig"></span><i>The first time FCAC changes a pack it saves a copy of the original files. Restore them at any time from the tray icon menu &rarr; Restore original announcements.</i></div>
    <div><b>Your recordings</b><span id="w-rec"></span><i>Airline packs you install, and your own recordings, are stored here.</i></div>
  </div>
  <p class="small" style="margin:14px 0 18px">FCAC keeps running in the system tray (near the clock) and shows a notification each time it updates your announcements. Close this window whenever you like and open it again by double-clicking the tray icon.</p>
  <div class="row"><button class="primary" id="w-ok">Got it</button></div>
</div></div>

<div class="modal" id="m-aliases"><div class="dialog" style="width:520px">
  <h3>Airline aliases</h3>
  <p>Use another airline's recordings, e.g. BA Shuttle (SHT) with the BAW pack.</p>
  <div class="chips" id="alias-list"></div>
  <div class="addrow">
    <input type="text" id="a-alias" maxlength="3" placeholder="Airline" autocomplete="off" spellcheck="false">
    <span class="small">uses</span>
    <input type="text" id="a-source" maxlength="3" placeholder="Pack" list="a-sources" autocomplete="off" spellcheck="false">
    <datalist id="a-sources"></datalist>
    <button class="primary" id="a-add">Add alias</button>
  </div>
  <div class="small" style="margin:12px 0 18px">Fenix only uses an airline pack when its folder matches the livery's airline code, so FCAC builds a pack folder for the alias from the other airline's files.</div>
  <div class="row"><button class="primary" id="a-done">Done</button></div>
</div></div>

<div class="modal" id="m-settings"><div class="dialog" style="width:500px">
  <h3>Settings</h3><p>How FCAC behaves. Changes apply when you press Save.</p>
  <div class="setting"><div class="label">SimBrief username</div>
    <input type="text" id="s-name" placeholder="e.g. captainsmith123" autocomplete="off" spellcheck="false"></div>
  <div class="setting"><div class="label">When I close this window</div>
    <div class="radios" id="s-close">
      <div class="radio" data-v="tray"><b>Keep running in the tray</b><span>FCAC keeps your announcements up to date in the background</span></div>
      <div class="radio" data-v="quit"><b>Quit FCAC</b><span>Closing the window exits the app completely</span></div>
    </div></div>
  <div class="setting"><div class="label">When I minimize this window</div>
    <div class="radios" id="s-min">
      <div class="radio" data-v="no"><b>Minimize normally</b><span>It stays in the taskbar</span></div>
      <div class="radio" data-v="yes"><b>Hide it in the tray</b><span>It leaves the taskbar; double-click the tray icon to bring it back</span></div>
    </div></div>
  <div class="setting"><div class="label">Local time in the landing announcement</div>
    <div class="radios" id="s-time">
      <div class="radio" data-v="yes"><b>Say the local time</b><span>"&hellip;where the local time is four thirty-five", read from the sim once you have landed</span></div>
      <div class="radio" data-v="no"><b>Leave it out</b><span>The landing announcement goes straight on without the time</span></div>
    </div></div>
  <div class="setting"><div class="label">Extra announcements (nut allergy, Wi-Fi down...)</div>
    <div class="radios" id="s-extra">
      <div class="radio" data-v="0"><b>Never</b><span>The welcome announcement is always the same</span></div>
      <div class="radio" data-v="25"><b>Now and then</b><span>About one flight in four gets a random bolt-on from your extra folder</span></div>
      <div class="radio" data-v="50"><b>Often</b><span>About one flight in two</span></div>
    </div></div>
  <div class="setting"><div class="label">Start FCAC automatically</div>
    <div class="radios" id="s-start">
      <div class="radio" data-v="off"><b>No</b><span>I'll start it myself</span></div>
      <div class="radio" data-v="msfs"><b>With MSFS</b><span>The sim launches FCAC every time it starts (through its EXE.xml)</span></div>
      <div class="radio" data-v="windows"><b>With Windows</b><span>Starts when you sign in to your PC</span></div>
    </div>
    <label class="check" id="s-quit-row" style="margin:12px 0 0"><input type="checkbox" id="s-quit"> Close FCAC when MSFS closes</label>
  </div>
  <div class="row"><button class="ghost" id="s-cancel">Cancel</button><button class="primary" id="s-save">Save</button></div>
</div></div>

<div class="modal" id="m-mic"><div class="dialog">
  <h3>Audio settings</h3><p>Pick your microphone, speak normally and keep the bar in the green.</p>
  <div class="label small">Input device</div><select id="mic-dev" class="voice" style="width:100%;margin:6px 0 14px"></select>
  <div class="label small">Input boost <span id="mic-gv"></span></div><input type="range" id="mic-gain" min="-12" max="18" step="1" style="width:100%">
  <div class="meter"><i id="mic-bar"></i><b id="mic-peak"></b></div><div class="small" id="mic-msg">Starting the microphone...</div>
  <div class="label small" style="margin-top:14px">Playback device (for the play and test buttons)</div><select id="out-dev" class="voice" style="width:100%;margin:6px 0 14px"></select>
  <div class="label small">Playback volume <span id="out-gv"></span></div><input type="range" id="out-vol" min="0" max="100" step="5" style="width:100%">
  <div class="small" style="margin-top:6px">Windows lists FCAC in its volume mixer under the browser that shows this window (Microsoft Edge or Chrome), and only while a sound is playing.</div>
  <div class="row" style="margin-top:16px;justify-content:space-between"><button id="mic-test">Test: record 3 s and play back</button><button class="primary" id="mic-done">Done</button></div>
</div></div>
<div class="modal" id="m-up"><div class="dialog" style="width:560px">
  <h3>Upgrade to the new names and folders</h3><p>These clips use old names or folders. Renaming them lets them use the new announcement types, tags and modes. Only your Studio copy changes now. When you press "Save changes to my library", the old-named files there are replaced by these.</p>
  <div id="up-list" style="max-height:48vh;overflow:auto;margin-bottom:14px"></div>
  <div class="small" style="margin-bottom:14px">The takeoff and landing versions share one new name and are told apart by flight-phase tags (PreFlight = before takeoff, Descent/Approach = before landing). That is our reading of Fenix's guide - test it in the sim.</div>
  <div class="row"><button id="up-cancel">Not now</button><button class="primary" id="up-ok">Rename them</button></div>
</div></div>
<div class="modal" id="m-abandon"><div class="dialog">
  <h3>Abandon this pack?</h3><p id="ab-text"></p>
  <div class="row"><button id="ab-cancel">Keep it</button><button class="primary" id="ab-ok" style="background:var(--err);border-color:transparent">Delete the pack</button></div>
</div></div>
<div class="modal" id="m-quiet"><div class="dialog">
  <h3 id="q-t">No sound picked up</h3><p id="q-s"></p>
  <div class="row"><button id="q-close">Close</button><button class="primary" id="q-open">Open Audio settings</button></div>
</div></div>
<div class="modal" id="m-job"><div class="dialog"><h3 id="job-t">Working...</h3><p id="job-s">Please wait - this can take a moment.</p><div class="prog"><i id="job-bar"></i></div></div></div>
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
function confirmDiscard(p){
  $("#ab-text").textContent="This deletes all the recordings and details of "+p.icao+" - "+p.voice+" in Pack Studio. It cannot be undone. Your Airlines library is not touched.";
  $("#m-abandon").classList.add("show");
  $("#ab-cancel").onclick=()=>$("#m-abandon").classList.remove("show");
  $("#ab-ok").onclick=async()=>{$("#m-abandon").classList.remove("show");const r=await studio("discard",{icao:p.icao,voice:p.voice});if(r.ok)toast("Pack discarded")};
}
function toast(t,ms){const x=$("#toast");x.textContent=t;x.classList.add("show");clearTimeout(toast.t);toast.t=setTimeout(()=>x.classList.remove("show"),ms||3200)}
function logo(icao,cls){const i=el("img",cls);i.alt=icao;i.src="/logo/"+icao+".png?t="+TOKEN+"&v="+(snap&&snap.logo_v||0);return i}

function renderDebug(){
  const d=$("#debug"); if(!snap.debug||!snap.debug.length){d.style.display="none";return}
  const text=snap.debug.join("\n"); if(d.dataset.t===text) return; d.dataset.t=text;
  d.style.display="block"; d.textContent=text; d.scrollTop=d.scrollHeight;
}
let heroSig="";
function renderHero(){
  const sig=JSON.stringify([snap.setup_needed,snap.company,snap.voices,snap.flight,snap.message,snap.updated,snap.busy,snap.logo_v]);
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
    const ids=[f.number,f.callsign].filter(Boolean).join(" / ");
    if(snap.company||ids){const c=el("div","company"); if(snap.company) c.append(el("span","nm",snap.company)); if(ids) c.append(el("span","ids",ids)); mid.append(c)}
    const r=el("div","route"); r.append(document.createTextNode(f.airline||"???"));
    r.append(el("span","arrow","→")); r.append(document.createTextNode(f.dest)); mid.append(r);
    if(f.name) mid.append(el("div","dest-name",f.name));
  }else mid.append(el("div","route","No flight yet"));
  const m=el("div","msg"); m.textContent=snap.message||""; mid.append(m);
  h.append(mid);
  const side=el("div","side");
  const b=el("button","primary","Update now"); b.disabled=snap.busy; b.onclick=()=>api("update");
  const v=snap.voices;
  if(v){
    {
      const row=el("div","voicerow"); const sel=el("select","voice"); sel.title="Voice artist";
      v.options.forEach(o=>{const op=el("option",null,o.name); op.value=o.id; sel.append(op)});
      if(v.options.length>1){const rnd=el("option",null,"Random voice"); rnd.value="random"; sel.append(rnd)}
      sel.value=v.options.some(o=>o.id===v.selected)||v.selected==="random"&&v.options.length>1?v.selected:v.options[0].id; sel.disabled=snap.busy;
      sel.onchange=()=>api("voice",{voice:sel.value});
      row.append(el("span","small","Voice")); row.append(sel); row.append(b); side.append(row);
    }
    const cur=v.options.find(o=>o.id===(v.selected==="random"||v.options.length===1?(v.selected==="random"?v.used:v.options[0].id):v.selected))||(v.options.length===1?v.options[0]:null);
    if(v.selected==="random"&&cur) side.append(el("span","small","This flight: "+cur.name));
    if(cur&&(cur.artist||cur.accent||cur.description)){
      const line=el("span","small credit"); line.title=cur.description||"";
      const who=[cur.artist?"voiced by "+cur.artist:"",cur.accent].filter(Boolean).join(" · ");
      line.append(document.createTextNode(who.charAt(0).toUpperCase()+who.slice(1)));
      if(cur.credit_url){const a=el("a",null," ↗"); a.href=cur.credit_url; a.target="_blank"; a.rel="noopener"; line.append(a)}
      if(cur.donate_url){const a=el("a",null," · ♥ Support"); a.href=cur.donate_url; a.target="_blank"; a.rel="noopener"; line.append(a)}
      side.append(line);
      if(cur.description) side.append(el("span","small desc",cur.description));
    }
  } else side.append(b);
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
  if(p.voices&&p.voices.length) info.append(el("div","meta",(p.voices.length>1?p.voices.length+" voices: ":"Voice: ")+p.voices.join(", ")));
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
let welcomeShown=false;
function renderWelcome(){
  const w=snap.welcome, m=$("#m-welcome");
  if(!w){m.classList.remove("show");return}
  if(welcomeShown) return; welcomeShown=true;
  $("#w-fenix").textContent=w.fenix||"(found automatically once you've installed an airline pack)";
  $("#w-orig").textContent=w.originals; $("#w-rec").textContent=w.recordings;
  m.classList.add("show");
}
$("#w-ok").onclick=async()=>{await api("welcomed");$("#m-welcome").classList.remove("show");snap.welcome=null};
$("#aliases-btn").onclick=()=>{$("#m-aliases").classList.add("show");setTimeout(()=>$("#a-alias").focus(),50)};
$("#a-done").onclick=()=>$("#m-aliases").classList.remove("show");
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
let cfg={on_close:"tray",autostart:"off",min:"no",time:"yes",extra:"25"};
function pickRadio(id,val){document.querySelectorAll("#"+id+" .radio").forEach(r=>r.classList.toggle("sel",r.dataset.v===val))}
document.querySelectorAll(".radios").forEach(g=>g.querySelectorAll(".radio").forEach(r=>r.onclick=()=>{
  cfg[g.id==="s-close"?"on_close":g.id==="s-min"?"min":g.id==="s-time"?"time":g.id==="s-extra"?"extra":"autostart"]=r.dataset.v; pickRadio(g.id,r.dataset.v); $("#s-quit-row").style.display=cfg.autostart==="msfs"?"flex":"none"}));
function openSettings(){
  const s=snap.settings||{}; cfg={on_close:s.on_close||"tray",autostart:s.autostart||"off",min:s.minimize_to_tray?"yes":"no",time:s.local_time===false?"no":"yes",extra:String(s.extra_chance===undefined?25:s.extra_chance)};
  $("#s-name").value=snap.username||""; $("#s-quit").checked=s.quit_with_msfs!==false;
  pickRadio("s-close",cfg.on_close); pickRadio("s-start",cfg.autostart); pickRadio("s-min",cfg.min); pickRadio("s-time",cfg.time); pickRadio("s-extra",cfg.extra);
  $("#s-quit-row").style.display=cfg.autostart==="msfs"?"flex":"none";
  $("#m-settings").classList.add("show");
}
$("#settings").onclick=openSettings;
$("#s-cancel").onclick=()=>$("#m-settings").classList.remove("show");
$("#s-save").onclick=async()=>{
  const name=$("#s-name").value.trim();
  if(name&&name!==(snap.username||"")){const r=await api("username",{name}); if(r.error)return}
  const r=await api("settings",{on_close:cfg.on_close,autostart:cfg.autostart,minimize_to_tray:cfg.min==="yes",local_time:cfg.time==="yes",extra_chance:parseInt(cfg.extra,10),quit_with_msfs:$("#s-quit").checked});
  if(!r.error){$("#m-settings").classList.remove("show");toast("Settings saved")}
};
let minSent=false;
setInterval(()=>{ // a minimized Chromium window reports its position as far off screen
  const away=window.screenX<=-30000||window.screenY<=-30000;
  if(away&&!minSent&&snap&&snap.settings&&snap.settings.minimize_to_tray){minSent=true;api("minimized")}
  if(!away)minSent=false;
},500);
window.addEventListener("pagehide",()=>{try{fetch("/api/closing",{method:"POST",keepalive:true,headers:{"X-Token":TOKEN,"Content-Type":"application/json"},body:"{}"})}catch(e){}});
$("#n-cancel").onclick=()=>$("#m-name").classList.remove("show");
$("#n-ok").onclick=async()=>{const v=$("#n-input").value.trim(); if(!v)return; $("#m-name").classList.remove("show"); await api("username",{name:v}); toast("Saved - fetching your flight")};
$("#n-input").addEventListener("keydown",e=>{if(e.key==="Enter")$("#n-ok").click()});
$("#auto").onclick=()=>api("auto",{on:!snap.auto});
$("#refresh").onclick=async()=>{await api("refresh");toast("Refreshing pack list")};
$("#forget").onclick=async()=>{await api("forget");toast("Remembered choices cleared")};
document.querySelectorAll("[data-open]").forEach(b=>b.onclick=()=>api("open",{what:b.dataset.open}));

// ---------------------------------------------------------------- Pack Studio
let view="main", studioSig="", recState=null, openGroups=new Set(["Welcome aboard"]), playing=null;
function setView(v){view=v;$("#view-main").style.display=v==="main"?"":"none";$("#view-studio").style.display=v==="studio"?"":"none";
  $("#tab-main").classList.toggle("on",v==="main");$("#tab-studio").classList.toggle("on",v==="studio");if(v==="studio"){studioSig="";renderStudio()}}
$("#tab-main").onclick=()=>setView("main"); $("#tab-studio").onclick=()=>setView("studio");

function field(label,key,cur,ph,wide){const l=el("label",wide?"wide":"",label);const i=el("input");i.type="text";i.value=cur[key]||"";i.placeholder=ph||"";i.dataset.k=key;l.append(i);return l}
function pid(c){return {icao:c.icao,voice:c.voice}}
async function studio(name,data){const r=await api("studio_"+name,data);await refreshNow();return r}
async function refreshNow(){try{const r=await fetch("/api/state",{headers:{"X-Token":TOKEN}});snap=await r.json();studioSig="";renderStudio()}catch(e){}}

function wavBlob(chunks,rate){
  let n=0;chunks.forEach(c=>n+=c.length);const buf=new ArrayBuffer(44+n*2),v=new DataView(buf);
  const w=(o,s)=>{for(let i=0;i<s.length;i++)v.setUint8(o+i,s.charCodeAt(i))};
  w(0,"RIFF");v.setUint32(4,36+n*2,true);w(8,"WAVEfmt ");v.setUint32(16,16,true);v.setUint16(20,1,true);v.setUint16(22,1,true);
  v.setUint32(24,rate,true);v.setUint32(28,rate*2,true);v.setUint16(32,2,true);v.setUint16(34,16,true);w(36,"data");v.setUint32(40,n*2,true);
  let o=44;chunks.forEach(c=>{for(let i=0;i<c.length;i++){const x=Math.max(-1,Math.min(1,c[i]));v.setInt16(o,x<0?x*0x8000:x*0x7fff,true);o+=2}});
  return new Blob([buf],{type:"audio/wav"});
}
// ---- microphone settings (kept in this window's browser storage) ----
function micPrefs(){try{return Object.assign({device:"",gain:0,out:"",vol:100},JSON.parse(localStorage.getItem("fcacMic")||"{}"))}catch(e){return{device:"",gain:0}}}
function micSave(p){try{localStorage.setItem("fcacMic",JSON.stringify(p))}catch(e){}}
async function micStream(){
  const p=micPrefs(), a={echoCancellation:false,noiseSuppression:false,autoGainControl:false,channelCount:1};
  if(p.device)a.deviceId={exact:p.device};
  try{return await navigator.mediaDevices.getUserMedia({audio:a})}
  catch(e){if(p.device){micSave({...p,device:""});return await navigator.mediaDevices.getUserMedia({audio:{...a,deviceId:undefined}})}throw e}
}
let micCtx=null, micTimer=null, micStreamNow=null;
function newAudio(url){const a=new Audio(url),p=micPrefs();a.volume=Math.max(0,Math.min(1,p.vol/100));if(p.out&&a.setSinkId)a.setSinkId(p.out).catch(()=>{});return a}
async function micStop(){clearInterval(micTimer);micTimer=null;if(micStreamNow){micStreamNow.getTracks().forEach(t=>t.stop());micStreamNow=null}if(micCtx){try{await micCtx.close()}catch(e){}micCtx=null}}
async function micStart(){
  await micStop();const msg=$("#mic-msg"),p=micPrefs();
  let err=null;
  try{micStreamNow=await micStream()}catch(e){err=e}
  let all=[];try{all=await navigator.mediaDevices.enumerateDevices()}catch(e){}
  const devs=all.filter(d=>d.kind==="audioinput"),sel=$("#mic-dev");sel.textContent="";
  const cur=micStreamNow?micStreamNow.getAudioTracks()[0].getSettings().deviceId:"";
  devs.forEach((d,i)=>{const o=el("option",null,d.label||"Microphone "+(i+1));o.value=d.deviceId;sel.append(o)});
  if(!devs.length){const o=el("option",null,"No microphone found");o.value="";sel.append(o)}
  sel.value=p.device&&devs.some(d=>d.deviceId===p.device)?p.device:cur;
  const outs=all.filter(d=>d.kind==="audiooutput"),os=$("#out-dev");os.textContent="";
  outs.forEach((d,i)=>{const o=el("option",null,d.label||"Speakers "+(i+1));o.value=d.deviceId;os.append(o)});
  os.value=p.out&&outs.some(d=>d.deviceId===p.out)?p.out:(outs[0]?outs[0].deviceId:"");
  os.disabled=!("setSinkId" in HTMLMediaElement.prototype);$("#out-vol").value=p.vol;$("#out-gv").textContent=p.vol+"%";
  $("#mic-gain").value=p.gain;$("#mic-gv").textContent=(p.gain>0?"+":"")+p.gain+" dB";
  if(err){const n=err.name||"";
    msg.textContent=n==="NotAllowedError"||n==="SecurityError"?"The microphone is blocked. In Windows open Settings > Privacy & security > Microphone and turn on Microphone access and 'Let desktop apps access your microphone', then reopen this.":
      n==="NotFoundError"||n==="OverconstrainedError"?"No microphone was found. Plug one in (or enable it in Windows Sound settings > Input) and reopen this.":
      "Could not open the microphone ("+(n||"error")+(err.message?": "+err.message:"")+").";
    return}
  micCtx=new (window.AudioContext||window.webkitAudioContext)();const an=micCtx.createAnalyser();an.fftSize=1024;micCtx.createMediaStreamSource(micStreamNow).connect(an);
  const buf=new Float32Array(an.fftSize);let peak=0,hold=0,clip=0;
  micTimer=setInterval(()=>{an.getFloatTimeDomainData(buf);const g=Math.pow(10,micPrefs().gain/20);let m=0;for(const v of buf)m=Math.max(m,Math.abs(v)*g);
    peak=Math.max(m,peak*.9);hold=Math.max(m,hold*.995);if(m>=.99)clip=20;else if(clip)clip--;
    const db=m>0?20*Math.log10(m):-90,w=Math.max(0,Math.min(1,(db+60)/60));$("#mic-bar").style.width=(w*100)+"%";
    const hd=hold>0?20*Math.log10(hold):-90;$("#mic-peak").style.left=Math.max(0,Math.min(1,(hd+60)/60))*100+"%";
    msg.textContent=clip?"Too loud - it is clipping. Turn the boost down or move back.":hd<-30?"Very quiet - move closer or raise the boost.":hd<-18?"A bit quiet - fine, FCAC evens it out, but a little more is better.":hd>-3?"Close to clipping - back off a little.":"Good level.";
  },60);
}
function openMic(){$("#m-mic").classList.add("show");micStart()}
$("#mic-dev").onchange=()=>{micSave({...micPrefs(),device:$("#mic-dev").value});micStart()};
$("#out-dev").onchange=()=>micSave({...micPrefs(),out:$("#out-dev").value});
$("#out-vol").oninput=()=>{const v=+$("#out-vol").value;micSave({...micPrefs(),vol:v});$("#out-gv").textContent=v+"%"};
$("#mic-gain").oninput=()=>{const g=+$("#mic-gain").value;micSave({...micPrefs(),gain:g});$("#mic-gv").textContent=(g>0?"+":"")+g+" dB"};
$("#mic-done").onclick=async()=>{await micStop();$("#m-mic").classList.remove("show")};
$("#mic-test").onclick=async()=>{
  const b=$("#mic-test");if(b.disabled)return;b.disabled=true;
  try{const rec=await recordClip(3000,t=>b.textContent="Recording... "+Math.ceil(t)+" s");b.textContent="Playing back...";const url=URL.createObjectURL(rec);const a=newAudio(url);a.onended=()=>{b.disabled=false;b.textContent="Test: record 3 s and play back"};a.play()}
  catch(e){toast("Could not record - check the microphone");b.disabled=false;b.textContent="Test: record 3 s and play back"}
};
async function recordClip(ms,tick){ // records for ms from the chosen microphone with the chosen boost; returns a WAV blob
  const stream=await micStream(),ctx=new (window.AudioContext||window.webkitAudioContext)(),src=ctx.createMediaStreamSource(stream),node=ctx.createScriptProcessor(4096,1,1),chunks=[],g=Math.pow(10,micPrefs().gain/20);
  node.onaudioprocess=e=>chunks.push(Float32Array.from(e.inputBuffer.getChannelData(0),v=>v*g));src.connect(node);node.connect(ctx.destination);
  const t0=Date.now();await new Promise(r=>{const i=setInterval(()=>{const left=(ms-(Date.now()-t0))/1000;if(tick)tick(left);if(left<=0){clearInterval(i);r()}},100)});
  node.disconnect();src.disconnect();stream.getTracks().forEach(t=>t.stop());const rate=ctx.sampleRate;await ctx.close();return wavBlob(chunks,rate);
}
function micAlert(title,text){$("#q-t").textContent=title;$("#q-s").textContent=text;$("#m-quiet").classList.add("show")}
$("#q-close").onclick=()=>$("#m-quiet").classList.remove("show");
$("#q-open").onclick=()=>{$("#m-quiet").classList.remove("show");openMic()};
async function startRec(slot,btn){
  if(recState){return}
  let stream;
  try{stream=await micStream()}
  catch(e){micAlert("No microphone access","FCAC could not open a microphone. Allow microphone access when the window asks, check that one is plugged in and not blocked in Windows privacy settings, or upload a recording instead.");return}
  const ctx=new (window.AudioContext||window.webkitAudioContext)(), src=ctx.createMediaStreamSource(stream), node=ctx.createScriptProcessor(4096,1,1), chunks=[];
  const gain=Math.pow(10,micPrefs().gain/20);
  node.onaudioprocess=e=>chunks.push(Float32Array.from(e.inputBuffer.getChannelData(0),v=>v*gain));
  src.connect(node);node.connect(ctx.destination);
  recState={slot,btn,t0:Date.now(),stop:async()=>{
    node.disconnect();src.disconnect();stream.getTracks().forEach(t=>t.stop());const rate=ctx.sampleRate;await ctx.close();
    const st=recState;recState=null;clearInterval(st.timer);const rw=btn.closest(".slot");if(rw)rw.classList.remove("reading");
    let peak=0;for(const ch of chunks)for(let i=0;i<ch.length;i++){const v=Math.abs(ch[i]);if(v>peak)peak=v}
    if(!chunks.length||peak<0.003){ // nothing but silence: do not keep the take
      studioSig="";renderStudio();micAlert("No sound was picked up","The recording is silent. Check that the right microphone is selected, that it is not muted, and that the level meter moves when you speak - then record it again.");return}
    await upload(slot,wavBlob(chunks,rate),"take.wav");
    if(peak<0.03)micAlert("That recording is very quiet","It was saved, but the microphone level is low. Open Audio settings, raise the input boost or move closer, and re-record for the best result.");}};
  btn.textContent="■ Stop 0:00";btn.classList.add("live");const row=btn.closest(".slot");if(row){row.classList.add("reading");requestAnimationFrame(()=>row.scrollIntoView({block:"center"}))}
  recState.timer=setInterval(()=>{const s=Math.floor((Date.now()-recState.t0)/1000);btn.textContent="■ Stop "+Math.floor(s/60)+":"+String(s%60).padStart(2,"0")},250);
}
async function upload(slot,blob,name){
  const c=snap.studio.current; toast("Processing...");
  const q=new URLSearchParams({icao:c.icao,voice:c.voice,slot,filename:name,t:TOKEN});
  try{const r=await fetch("/studio/upload?"+q,{method:"POST",headers:{"X-Token":TOKEN},body:blob});const j=await r.json();
    if(j.error)toast(j.error,9000);else toast("Done - "+j.seconds+"s, "+j.lufs_in+" → "+j.lufs_out+" LUFS");}
  catch(e){toast("Upload failed - the recording was not saved. Try again or use Upload.",9000)}
  await refreshNow();
}
let player=null; // {id, audio}: the clip being played, paused or resumed
function syncPlayer(){
  document.querySelectorAll("#view-studio .pp").forEach(b=>{const on=player&&player.id===b.dataset.id,playingNow=on&&!player.audio.paused;
    b.textContent=playingNow?"⏸":"▶";b.title=playingNow?"Pause":"Play"});
  document.querySelectorAll("#view-studio .stp").forEach(b=>{b.style.display=player&&player.id===b.dataset.id?"":"none"});
}
function togglePlay(c,id){
  if(player&&player.id===id){const a=player.audio;if(a.paused)a.play().catch(()=>{});else a.pause();syncPlayer();return}
  stopPlay();
  const a=newAudio("/studio/audio?"+new URLSearchParams({icao:c.icao,voice:c.voice,slot:id,t:TOKEN,v:Date.now()}));
  player={id,audio:a};["play","pause"].forEach(e=>a.addEventListener(e,syncPlayer));
  a.addEventListener("ended",()=>{if(player&&player.audio===a)player=null;syncPlayer()});
  a.play().then(syncPlayer).catch(()=>{player=null;syncPlayer();toast("Could not play that")});syncPlayer();
}
function stopPlay(){if(player){const a=player.audio;player=null;a.pause();a.currentTime=0}syncPlayer()}
function slotRow(c,r){
  const d=el("div","slot"+(r.done?" done":"")+(r.skipped?" skipped":""));
  const nm=el("div","nm2",r.label); if(r.skipped)nm.append(el("em","sk","skipped")); else if(r.required&&!r.done)nm.append(el("em",null,"needed"));
  if(r.modes){const ms=el("select","voice");ms.style.cssText="margin-left:10px;padding:3px 8px;font-size:12px";ms.title="How Fenix plays it: Prerecorded = chosen in the cabin crew panel; Manual = the purser announces it (adds [Manual] to the file name)";
    r.modes.forEach(m=>{const o=el("option",null,m);o.value=m;ms.append(o)});ms.value=r.mode;ms.onchange=()=>studio("mode",{...pid(c),slot:r.id,mode:ms.value});nm.append(ms)}
  d.append(nm);
  const scl=el("div","sc editable"+(r.custom?" custom":""),r.script?(r.custom||r.music?r.script:"Say: "+r.script):"Click to add a script");
  scl.title="Click to edit this script. Enter saves it, Shift+Enter adds a line, Esc undoes.";
  scl.onclick=()=>{
    if(scl.querySelector("textarea")||recState)return;
    const ta=el("textarea","sc-edit");ta.value=r.script||"";ta.rows=1;scl.textContent="";scl.classList.remove("custom");scl.append(ta);
    const fit=()=>{ta.style.height="auto";ta.style.height=ta.scrollHeight+"px"};fit();ta.focus();ta.setSelectionRange(ta.value.length,ta.value.length);
    let timer=null,finished=false;const save=text=>api("studio_script",{...pid(c),slot:r.id,text});
    ta.oninput=()=>{fit();clearTimeout(timer);timer=setTimeout(()=>save(ta.value),250)};   // saved as you type
    const finish=async(undo)=>{if(finished)return;finished=true;clearTimeout(timer);
      const text=undo?(r.custom?r.script:""):ta.value;ta.blur();await save(text);await refreshNow()};
    ta.onkeydown=e=>{if(e.key==="Enter"&&!e.shiftKey){e.preventDefault();finish(false)}else if(e.key==="Escape"){e.preventDefault();finish(true)}};
    ta.onblur=()=>finish(false);
  };
  d.append(scl);
  if(r.variants&&r.variants.length)d.append(el("div","st","Also has: "+r.variants.join(", ")));
  if(r.done)d.append(el("div","st",r.imported?"From your library - unchanged":r.seconds+" s · "+r.lufs+" LUFS"+(r.music?" · unchanged":"")));
  const act=el("div","act");
  const rec=el("button","rec",r.done?"● Re-record":"● Record");rec.onclick=()=>{if(recState&&recState.slot===r.id){recState.stop()}else startRec(r.id,rec)};
  if(r.music)rec.style.display="none";
  const sc=el("button","sq");sc.title=r.custom?"Replace script (uploaded for this item)":"Upload Script";
  sc.innerHTML='<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M14 3H7a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2V8z"/><path d="M14 3v5h5"/><path d="M9 13h6M9 17h6"/></svg>';
  const sf2=el("input");sf2.type="file";sf2.accept=".txt,.md,text/plain";sf2.style.display="none";
  sf2.onchange=async()=>{const f=sf2.files[0];if(!f)return;if(f.size>20000){toast("That script is too long for one clip");return}await studio("script",{...pid(c),slot:r.id,text:await f.text()});toast("Script added to "+r.label)};
  sc.onclick=()=>sf2.click();
  const up=el("button",null,"Upload");const f=el("input");f.type="file";f.accept="audio/*,.wav,.mp3,.ogg,.m4a,.flac";f.style.display="none";
  f.onchange=()=>{if(f.files[0])upload(r.id,f.files[0],f.files[0].name)};up.onclick=()=>f.click();act.append(rec,up,sc,sf2,f);
  if(!r.done&&!r.music){const sk=el("button","ghost",r.skipped?"Unskip":"Skip");sk.title=r.skipped?"Put this back on the checklist":"Not recording this one: counts as done on the checklist";sk.onclick=()=>studio("skip",{...pid(c),slot:r.id,skip:!r.skipped});act.append(sk)}
  if(r.done){const p=el("button","pp","▶");p.dataset.id=r.id;p.title="Play";p.onclick=()=>togglePlay(c,r.id);const sp2=el("button","stp","⏹");sp2.dataset.id=r.id;sp2.title="Stop";sp2.style.display="none";sp2.onclick=stopPlay;const x=el("button","danger","✕");x.title="Remove";x.onclick=()=>studio("delete",{...pid(c),slot:r.id});act.append(p,sp2,x)}
  if(r.custom){const cs=el("button","sq ghost","✕");cs.title="Remove this item's script";cs.style.fontSize="11px";cs.onclick=()=>studio("script",{...pid(c),slot:r.id,text:""});act.insertBefore(cs,sf2.nextSibling)}
  d.append(act);return d;
}
function renderStudio(){
  if(view!=="studio"||!snap)return;
  const root=$("#view-studio"), s=snap.studio||{projects:[],current:null}, sig=JSON.stringify(s);
  if(sig===studioSig||recState||root.contains(document.activeElement)&&["INPUT","TEXTAREA"].includes(document.activeElement.tagName))return;
  studioSig=sig; root.textContent="";
  const c=s.current;
  if(!c){
    const card=el("section","card");card.append(el("h2",null,"Pack Studio"));
    const intro=el("p","small","Make an airline pack: record (or upload) each announcement, and FCAC gives it the cabin-PA sound and sets it to the loudness of the original Fenix announcements, so every pack sounds consistent. Then export a zip to share.");
    intro.style.fontSize="13.5px";intro.style.color="var(--muted)";card.append(intro);
    const g=el("div","sd-grid");const ic=el("label",null,"Airline code (3 letters)"),i1=el("input");i1.type="text";i1.maxLength=3;i1.placeholder="BAW";i1.style.textTransform="uppercase";ic.append(i1);
    const an=el("label",null,"Airline name"),i2=el("input");i2.type="text";i2.placeholder="British Airways";an.append(i2);
    const vn=el("label","wide","Name of this voice"),i3=el("input");i3.type="text";i3.placeholder="Emma";vn.append(i3);g.append(ic,an,vn);card.append(g);
    const row=el("div","row");row.style.marginTop="14px";const go=el("button","primary","Start a new pack");
    go.onclick=async()=>{const r=await studio("create",{icao:i1.value,airline:i2.value,voice:i3.value});if(r.ok)toast("Pack started")};row.append(go);card.append(row);root.append(card);
    if(s.library&&s.library.length){const h=el("div","section-head");h.append(el("h2",null,"Edit a pack from your library"),el("span",null,"change clips, add airports, fix a recording"));root.append(h);
      s.library.forEach(p=>{const b=el("button","ghost",p.icao+" · "+p.name+(p.artist?" · "+p.artist:""));b.style.margin="0 8px 8px 0";b.onclick=async()=>{const r=await studio("import",p);if(r.ok)toast("Opened for editing - your clips are unchanged until you replace them")};root.append(b)})}
    if(s.projects.length){const h=el("div","section-head");h.append(el("h2",null,"Your packs in progress"));root.append(h);
      s.projects.forEach(p=>{const w=el("span","chip2");w.style.margin="0 8px 8px 0";const b=el("button","ghost",p.icao+" · "+p.voice+(p.airline?" · "+p.airline:""));b.style.cssText="width:auto;height:auto;border-radius:10px;padding:6px 10px;color:var(--text)";b.onclick=()=>studio("open",p);
        const x=el("button",null,"✕");x.title="Abandon this pack";x.onclick=()=>confirmDiscard(p);
        w.append(b,x);root.append(w)})}
    return;
  }
  const top=el("div","section-head");const back=el("button","ghost","← All packs");back.onclick=()=>studio("close");
  top.append(back,el("h2",null,c.airline+" · "+c.voice+" ("+c.icao+")"));
  const sp=el("span");sp.style.cssText="margin-left:auto;display:flex;gap:8px";
  const mic=el("button",null,"🎧 Audio settings");mic.onclick=openMic;
  sp.append(mic);
  top.append(sp);root.append(top);
  const card=el("section","card");card.append(el("h2",null,"Voice details"));
  const g=el("div","sd-grid");
  g.append(field("Voice artist","artist",c,"Your name"),field("Accent","accent",c,"British"),field("Short description","description",c,"Calm and collected",true),
           field("Credit link (your site / socials)","credit_url",c,"https://..."),field("Support link (Ko-fi, Patreon...)","donate_url",c,"https://ko-fi.com/..."),field("Pack version","version",c,"1.0.0"));
  card.append(g);
  const lg=el("div","logorow"),im=el("img","logoimg");im.alt="Pack logo";
  im.src=c.logo?"/studio/logo?"+new URLSearchParams({icao:c.icao,voice:c.voice,t:TOKEN,v:Date.now()}):"/logo/"+c.icao+".png?t="+TOKEN+"&v="+(snap.logo_v||0);
  const lt=el("div",null);lt.append(el("div","nm2","Pack logo"),el("div","small",c.logo?"Shown in the pack list and the window header, and included in the exported zip.":"Optional. A png, jpg or webp - a square or wide logo on a transparent or light background works best."));
  const lf=el("input");lf.type="file";lf.accept="image/png,image/jpeg,image/webp";lf.style.display="none";
  const lb=el("button",null,c.logo?"Change logo":"Upload logo");lb.onclick=()=>lf.click();
  lf.onchange=async()=>{const f=lf.files[0];if(!f)return;if(f.size>5*1024*1024){toast("That image is too large (5 MB limit)",6000);return}
    const q=new URLSearchParams({icao:c.icao,voice:c.voice,slot:"@logo",filename:f.name,t:TOKEN});
    try{const r=await fetch("/studio/upload?"+q,{method:"POST",headers:{"X-Token":TOKEN},body:f});const j=await r.json();toast(j.error||"Logo saved",j.error?7000:3200)}catch(e){toast("Upload failed",7000)}
    await refreshNow()};
  const lr=el("button","ghost","Remove");lr.style.display=c.logo?"":"none";lr.onclick=async()=>{const r=await studio("logo_remove",pid(c));if(r.ok)toast("Logo removed")};
  lg.append(im,lt,lb,lr,lf);card.append(lg);
  const save=()=>{const d={...pid(c),effect:c.effect};g.querySelectorAll("input").forEach(i=>d[i.dataset.k]=i.value);return d};
  g.querySelectorAll("input").forEach(i=>i.onchange=async()=>{const r=await api("studio_details",save());if(r.ok)toast("Saved");studioSig=""});
  const ef=el("div","radios");ef.style.marginTop="14px";
  [[true,"Cabin-PA sound on","Band-limited speaker voice with light compression, like the original announcements. Recommended."],[false,"Leave my recording as it is","Only the loudness is matched."]].forEach(([v,t,sub])=>{
    const r=el("div","radio"+(c.effect===v?" sel":""));r.append(el("b",null,t),el("span",null,sub));r.onclick=async()=>{if(c.effect===v)return;toast("Reprocessing...");await studio("details",{...save(),effect:v});toast("Reprocessed with "+(v?"the PA sound":"no effect"))};ef.append(r)});
  card.append(ef);
  card.append(el("div","sd-meta",(c.target_pending?"Measuring the original announcements to set the loudness...":"Every clip is set to "+c.target+" LUFS, "+(c.target_count?"the average of "+c.target_count+" original announcements from "+c.target_source+".":"a standard level, because no original announcements were found.")+" Record in a quiet room, about a hand's width from the microphone. Your untouched recording is always kept, so you can switch the effect on or off any time.")));
  root.append(card);
  if(c.upgrades&&c.upgrades.length){const b=el("section","card");b.style.cssText="margin-top:16px;display:flex;gap:14px;align-items:center;justify-content:space-between";
    b.append(el("div",null,c.upgrades.length+" clip"+(c.upgrades.length>1?"s use":" uses")+" Fenix's old file names (like CrewSeatsLanding). Upgrade them to the new names to use the new features."));
    const ub=el("button","primary","Review upgrade");ub.onclick=()=>{const l=$("#up-list");l.textContent="";c.upgrades.forEach(u=>{const r=el("div","facts");const d=el("div");d.append(el("i",null,u.old),el("span",null,"→ "+u.new));r.append(d);l.append(r)});
      $("#up-ok").onclick=async()=>{$("#m-up").classList.remove("show");const r=await studio("upgrade",pid(c));if(r.ok)toast("Renamed "+r.renamed+" clip"+(r.renamed==1?"":"s"))};$("#m-up").classList.add("show")};
    b.append(ub);root.append(b)}
  const pr=el("div",null);pr.style.margin="16px 2px";pr.append(el("div","small",c.done+" of "+c.total+" recorded or skipped"+(c.missing?" · "+c.missing+" required still to do":" · all required clips done")));
  const bar=el("div","prog");const fill=el("i");fill.style.width=Math.round(100*c.done/c.total)+"%";bar.append(fill);pr.append(bar);root.append(pr);
  const groups=[];c.rows.forEach(r=>{let g=groups.find(x=>x.name===r.group);if(!g){g={name:r.group,rows:[]};groups.push(g)}g.rows.push(r)});
  ["Airports","Boarding music","Bolt-ons"].forEach(n=>{if(!groups.find(x=>x.name===n))groups.push({name:n,rows:[]})});
  const order=["Welcome aboard","Landing","Airports","Refuelling","Other announcements","Boarding music","Bolt-ons"];groups.sort((a,b)=>order.indexOf(a.name)-order.indexOf(b.name));
  groups.forEach(gr=>{
    const d=el("details","grp");d.open=openGroups.has(gr.name);d.ontoggle=()=>{d.open?openGroups.add(gr.name):openGroups.delete(gr.name)};
    const sm=el("summary",null,gr.name);sm.append(el("span",null,gr.rows.filter(r=>r.done||r.skipped).length+"/"+gr.rows.length));d.append(sm);
    if(gr.name==="Airports"){
      d.append(el("div","small","Add each airport and record just its name (for example “London Heathrow”). FCAC mixes that one clip into both the welcome aboard and the landing PA. Add a terminal variant like EGLL_T5 if you record one."));
      const line=el("div","addline"),i=el("input");i.type="text";i.placeholder="EGLL";i.style.textTransform="uppercase";const add=el("button",null,"Add airport");
      add.onclick=async()=>{if(!i.value.trim())return;const r=await studio("airport",{...pid(c),code:i.value});if(r.ok)openGroups.add("Airports");studioSig="";renderStudio()};
      i.addEventListener("keydown",e=>{if(e.key==="Enter")add.click()});line.append(i,add);d.append(line);
      if(c.airports.length){const cr=el("div","chiprow");c.airports.forEach(a=>{const b=el("button","ghost",a+" ✕");b.onclick=()=>studio("airport",{...pid(c),code:a,remove:true});cr.append(b)});d.append(cr)}
    }
    if(gr.name==="Boarding music"){
      d.append(el("div","small","Background music played while passengers board. Add a track name, then upload the audio - use only music you have the right to share. It is not recorded or processed, just converted to .ogg."));
      const line=el("div","addline"),i=el("input");i.type="text";i.placeholder="Departure lounge";const add=el("button",null,"Add track");
      add.onclick=async()=>{if(!i.value.trim())return;const r=await studio("music",{...pid(c),name:i.value});if(r.ok)openGroups.add("Boarding music");studioSig="";renderStudio()};
      i.addEventListener("keydown",e=>{if(e.key==="Enter")add.click()});line.append(i,add);d.append(line);
      if(c.music.length){const cr=el("div","chiprow");c.music.forEach(a=>{const b=el("button","ghost",a+" ✕");b.onclick=()=>studio("music",{...pid(c),name:a,remove:true});cr.append(b)});d.append(cr)}
    }
    if(gr.name==="Bolt-ons"){
      d.append(el("div","small","Short announcements FCAC sometimes adds to the welcome - e.g. “Nut allergy”, “Wi-Fi unavailable”, “Delayed by air traffic control”."));
      const line=el("div","addline"),i=el("input");i.type="text";i.placeholder="Nut allergy";const add=el("button",null,"Add bolt-on");
      add.onclick=async()=>{if(!i.value.trim())return;const r=await studio("extra",{...pid(c),name:i.value});if(r.ok)openGroups.add("Bolt-ons");studioSig="";renderStudio()};
      i.addEventListener("keydown",e=>{if(e.key==="Enter")add.click()});line.append(i,add);d.append(line);
      if(c.extras.length){const cr=el("div","chiprow");c.extras.forEach(a=>{const b=el("button","ghost",a.replace(/_/g," ")+" ✕");b.onclick=()=>studio("extra",{...pid(c),name:a,remove:true});cr.append(b)});d.append(cr)}
    }
    if(gr.rows.some(r=>r.part)){
      const parts=[];gr.rows.forEach(r=>{let p=parts.find(x=>x.name===r.part);if(!p){p={name:r.part,rows:[]};parts.push(p)}p.rows.push(r)});
      parts.forEach(pt=>{const key=gr.name+"/"+pt.name,sd=el("details","grp sub");sd.open=openGroups.has(key);sd.ontoggle=()=>{sd.open?openGroups.add(key):openGroups.delete(key)};
        const ss=el("summary",null,pt.name);ss.append(el("span",null,pt.rows.filter(r=>r.done||r.skipped).length+"/"+pt.rows.length));sd.append(ss);pt.rows.forEach(r=>sd.append(slotRow(c,r)));d.append(sd)});
    }else gr.rows.forEach(r=>d.append(slotRow(c,r)));
    root.append(d);
  });
  const bar2=el("div","sdbar");
  const dis=el("button","ghost danger","Abandon this pack");dis.title="Deletes this project's recordings and details. Your Airlines library is not touched.";
  dis.onclick=()=>confirmDiscard(c);
  const fol=el("button","ghost","Open project folder");fol.onclick=()=>api("studio_folder",pid(c));
  const use=el("button",null,c.origin?"Save changes to my library":"Use in FCAC now");use.onclick=async()=>{const r=await studio("install",pid(c));if(r.ok)toast(c.origin?"Saved to your library":"Added to your library as a voice for "+c.icao)};
  const ex=el("button","primary","Export pack zip");ex.onclick=async()=>{const r=await studio("export",pid(c));if(r.ok)toast("Exported - the folder is open")};
  bar2.append(dis,fol,use,ex);root.append(bar2);syncPlayer();
}

function renderJob(){
  const j=snap&&snap.studio&&snap.studio.job,m=$("#m-job");m.classList.toggle("show",!!j);if(!j)return;
  $("#job-t").textContent=j.label;const bar=$("#job-bar"),pr=bar.parentElement;
  pr.classList.toggle("wait",j.fraction==null);if(j.fraction!=null)bar.style.width=Math.max(4,Math.round(j.fraction*100))+"%";
}
$("#up-cancel").onclick=()=>$("#m-up").classList.remove("show");
async function tick(){
  try{
    const r=await fetch("/api/state",{headers:{"X-Token":TOKEN}}); snap=await r.json();
    $("#auto").classList.toggle("on",!!snap.auto); $("#ver").textContent="FCAC v"+(snap.version||""); $("#lib").textContent=snap.library?"Recordings folder: "+snap.library:"";
    renderHero(); renderDebug(); renderJob(); renderStudio(); renderPacks(); renderAliases(); renderWelcome(); renderPrompt();
    if(snap.setup_needed&&!snap.welcome&&!setupShown){setupShown=true;openName()}
  }catch(e){}
  setTimeout(tick,(snap&&(snap.busy||snap.prompt||(snap.studio&&snap.studio.job)))?400:1500);
}
tick();
</script>
</body>
</html>
"""
