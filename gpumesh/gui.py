"""Local dashboard + tray for gpumesh.

``gpumesh gui`` serves one small page on this computer only and, when
available, a tray icon. No new runtime dependencies: the page is one
inline HTML string (no framework, no asset files) and the tray is optional
-- without ``pystray`` the dashboard alone is served, and everything the
tray menu can do is also a button on the page.

Security: the secret key is never rendered, logged, or embedded in any
page or URL. The dashboard binds to 127.0.0.1 only, refuses non-local
callers, and checks the ``Host``/``Origin`` headers on every request so a
web page open in the same browser cannot drive it. The tray passes the key
to its worker child through the child's environment, never its command line
(which is visible in process lists).

The page deliberately targets someone who has never used a terminal: one
plain sentence per idea, buttons that say what they do, and no jargon.
``tests/test_gui.py`` pins that promise by asserting that the words
"coordinator", "lease" and "subnet" never appear in the rendered page.
"""

from __future__ import annotations

import html
import os
import socket
import subprocess
import sys
import threading
import time
import urllib.parse
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

UI_PATH = "/ui"
JOIN_PATH = "/join"
CODE_PATH = "/code"
CREATE_PATH = "/create"
STOP_PATH = "/stop"
SCAN_PATH = "/scan"

DASHBOARD_DEFAULT_PORT = 8080
MESH_DEFAULT_PORT = 8000
FORM_MAX_BYTES = 4096

# Discovery binds a UDP port and then waits a second for beacons, so it is
# never done inline on a request: page loads stay instant and the answer is
# at most this stale.
NEARBY_CACHE_SECONDS = 10.0

TRAY_MENU_LABELS = ("Create mesh", "Join a mesh", "Open dashboard", "Quit")


def _esc(value: object) -> str:
    """HTML-escape one user-supplied value."""
    return html.escape("" if value is None else str(value), quote=True)


# The CPU descriptors Windows and Linux report are unreadable at a glance:
# "Intel64 Family 6 Model 154 Stepping 3, GenuineIntel" tells a person
# nothing they want to know. Collapse those to a brand, and leave short
# useful names ("RTX 4090", "M2 Pro") exactly as they are.
_DEVICE_NAME_NOISE = ("family", "stepping", "genuineintel", "authenticamd",
                      "model ", "cpu @", "processor @")
_DEVICE_NAME_MAX = 24


def _shorten_device_name(name: object) -> str:
    """A device name a person can read, or "" when there is nothing to add."""
    text = str(name or "").strip()
    if not text:
        return ""
    lowered = text.lower()
    if len(text) <= _DEVICE_NAME_MAX and not any(
            noise in lowered for noise in _DEVICE_NAME_NOISE):
        return text
    for brand in ("Intel", "AMD", "Apple"):
        if brand.lower() in lowered:
            return f"{brand} processor"
    return f"{text.split(',')[0].strip()[:_DEVICE_NAME_MAX]}…"


def _friendly_device(device: object, device_name: object) -> str:
    """Plain-language device label (no jargon)."""
    d = str(device or "cpu").strip().lower()
    name = _shorten_device_name(device_name)
    base = {"cuda": "Graphics card", "mps": "Apple chip"}.get(d, "Processor")
    if not name or name.lower() in (d, base.lower()):
        return base
    # "Intel processor" already reads as a device; bracketing it onto
    # "Processor" would only repeat the word.
    if base == "Processor" and name.lower().endswith("processor"):
        return name
    return f"{base} ({name})"


def _norm_machines(state: dict) -> list:
    """Machine rows from a state dict. Only these five keys are ever read,
    so anything else in the dict (notably any secret) cannot reach the page."""
    out = []
    raw = state.get("machines") if isinstance(state, dict) else None
    for entry in raw if isinstance(raw, list) else []:
        if not isinstance(entry, dict):
            continue
        try:
            score = float(entry.get("score", 0.0))
        except (TypeError, ValueError):
            score = 0.0
        out.append({
            "name": str(entry.get("name", "?")),
            "device": str(entry.get("device", "cpu")),
            "device_name": str(entry.get("device_name", "")),
            "score": score,
            "alive": bool(entry.get("alive", True)),
        })
    return out


def _status_text(machines: list, override: str = "") -> str:
    """The precise one-line state, e.g. "1 of 2 computers online"."""
    if override.strip():
        return override.strip()
    alive = sum(1 for m in machines if m["alive"])
    total = len(machines)
    if total == 0:
        return "Not sharing yet"
    if alive == total:
        s = "" if alive == 1 else "s"
        return f"{alive} computer{s} sharing"
    t = "" if total == 1 else "s"
    return f"{alive} of {total} computer{t} online"


def _headline(machines: list, unreachable: bool) -> str:
    """The big friendly sentence at the top of the page."""
    if unreachable:
        return "Can't reach your mesh right now"
    alive = sum(1 for m in machines if m["alive"])
    total = len(machines)
    if total == 0:
        return "Nothing is sharing yet"
    if total == 1:
        return "This computer is ready to help"
    if alive == total:
        return f"Your {total} computers are working as one"
    return f"{alive} of your {total} computers are ready"


def _speed_bar_css(score: float, top: float) -> str:
    """Width for the little relative speed bar, as a percentage."""
    if top <= 0 or score <= 0:
        return "0%"
    pct = max(6.0, min(100.0, (score / top) * 100.0))
    return f"{pct:.0f}%"


def build_dashboard_html(state: dict | None, *, pair: tuple = (),
                         nearby: list | None = None, notice: str = "",
                         sharing: bool = False, unreachable: bool = False,
                         dashboard_port: int = DASHBOARD_DEFAULT_PORT) -> str:
    """Render the dashboard page.

    ``state`` holds ``machines`` (rows as above) plus an optional ``status``
    string. ``pair`` is an optional ``(code, seconds_left)`` tuple minted
    for this computer's mesh; ``nearby`` lists ``(name, url)`` meshes found
    on the network for the join form. Every value is escaped.
    """
    machines = _norm_machines(state or {})
    override = (state or {}).get("status", "") if isinstance(state, dict) else ""
    status = _status_text(machines, override)
    headline = _headline(machines, unreachable)
    top_score = max((m["score"] for m in machines if m["alive"]), default=0.0)

    if machines:
        cards = []
        for m in machines:
            dot = "on" if m["alive"] else "off"
            word = "Online" if m["alive"] else "Offline"
            fastest = (m["alive"] and top_score > 0
                       and abs(m["score"] - top_score) < 1e-9)
            badge = '<span class="badge">Fastest here</span>' if fastest else ""
            bar = _speed_bar_css(m["score"], top_score)
            cards.append(
                f'<div class="card {dot}">'
                f'<div class="cardtop"><span class="dot {dot}"></span>'
                f'<span class="cstate {dot}">{word}</span></div>'
                f'<div class="cname">{_esc(m["name"])}</div>'
                f'<div class="cdev">{_esc(_friendly_device(m["device"], m["device_name"]))}</div>'
                f'<div class="speed"><span class="speednum">Speed {m["score"]:.1f}</span>'
                f'<span class="bartrack"><span class="barfill" '
                f'style="width:{bar}"></span></span></div>'
                f'{badge}'
                "</div>"
            )
        cards_html = "\n".join(cards)
    else:
        cards_html = (
            '<p class="empty">No computers found yet. Start sharing this one '
            "below, or join a mesh that is already running.</p>"
        )

    # ---- step 1: create ---------------------------------------------------
    if sharing:
        create_html = (
            '<p class="okline">This computer is sharing its speed.</p>'
            '<form method="post" action="' + STOP_PATH + '">'
            '<button class="btn ghost" type="submit">Stop sharing</button>'
            "</form>"
        )
    else:
        create_html = (
            '<p class="lead">Share this computer so the others can send it '
            "work. It keeps running in the background.</p>"
            '<form method="post" action="' + CREATE_PATH + '">'
            '<button class="btn primary" type="submit">'
            "Start sharing this computer</button></form>"
        )

    # ---- step 2: the code -------------------------------------------------
    if pair:
        code, left = pair
        pair_html = (
            f'<p class="code">{_esc(code)}</p>'
            f'<p class="lead">Type this on the other computer. It works once, '
            f"and only for the next {int(left)} seconds.</p>"
            '<div class="btnrow">'
            '<form method="post" action="' + CODE_PATH + '">'
            '<button class="btn" type="submit">New code</button></form>'
            f'<button class="btn ghost copy" type="button" '
            f'data-code="{_esc(code)}">Copy code</button>'
            "</div>"
        )
    elif sharing:
        pair_html = (
            '<p class="lead">A code appears here in a moment. '
            "This page updates on its own.</p>"
        )
    else:
        pair_html = (
            '<p class="lead">Create a mesh first, and the code for the other '
            "computers shows up here.</p>"
        )

    # ---- step 3: join -----------------------------------------------------
    options = "".join(
        f'<option value="{_esc(url)}">{_esc(name)} ({_esc(url)})</option>'
        for name, url in (nearby or [])
    )
    if nearby:
        picker = (
            '<label for="nearby">Mesh found near you</label>'
            f'<select id="nearby" name="nearby">{options}</select>'
            '<p class="lead tiny">Leave the address below empty to use the '
            "one picked here.</p>"
        )
    else:
        picker = (
            '<p class="lead tiny">No mesh found on this network yet. Ask the '
            "other computer for its address, or scan again.</p>"
            '<form method="post" action="' + SCAN_PATH + '">'
            '<button class="btn ghost" type="submit">Scan again</button>'
            "</form>"
        )
    join_html = (
        picker
        + '<form method="post" action="' + JOIN_PATH + '">'
        '<label for="host">Address of the other computer</label>'
        '<input id="host" name="host" autocomplete="off" spellcheck="false" '
        'placeholder="http://192.168.1.10:8000">'
        '<label for="code">6-digit code from that computer</label>'
        '<input id="code" name="code" inputmode="numeric" autocomplete="one-time-code" '
        'spellcheck="false" placeholder="123456" maxlength="9">'
        '<button class="btn primary" type="submit">Join this mesh</button>'
        "</form>"
    )

    notice_html = (f'<p class="notice">{_esc(notice)}</p>'
                   if notice.strip() else "")
    total = len(machines)

    return (
        "<!doctype html>\n"
        '<html lang="en">\n'
        "<head>\n"
        '<meta charset="utf-8">\n'
        '<meta name="viewport" content="width=device-width, initial-scale=1">\n'
        '<meta name="color-scheme" content="dark">\n'
        '<meta name="theme-color" content="#0d1117">\n'
        "<title>GPUMesh</title>\n"
        "<noscript>"
        '<meta http-equiv="refresh" content="5">'
        "</noscript>\n"
        "<style>\n"
        ":root{--bg:#0d1117;--panel:#161b22;--panel2:#1c2230;--line:#283040;"
        "--ink:#e6edf3;--dim:#9aa7b4;--accent:#3fb950;--accent2:#2ea043;"
        "--warn:#d29922;--off:#6e7681}\n"
        "*{box-sizing:border-box}\n"
        "body{margin:0;background:var(--bg);color:var(--ink);"
        "font-family:system-ui,-apple-system,'Segoe UI',Roboto,sans-serif;"
        "line-height:1.5;-webkit-text-size-adjust:100%}\n"
        ".wrap{max-width:820px;margin:0 auto;padding:24px 20px 48px}\n"
        "header{border-bottom:1px solid var(--line);padding-bottom:16px;"
        "margin-bottom:20px}\n"
        ".brand{display:flex;align-items:center;gap:10px}\n"
        ".logo{width:26px;height:26px;border-radius:8px;"
        "background:linear-gradient(135deg,var(--accent),#1f6feb)}\n"
        "h1{font-size:20px;margin:0;letter-spacing:-.01em}\n"
        ".tag{margin:6px 0 0;color:var(--dim);font-size:14px}\n"
        ".hero{background:linear-gradient(180deg,var(--panel2),var(--panel));"
        "border:1px solid var(--line);border-radius:14px;padding:20px;"
        "margin:0 0 20px}\n"
        ".hero h2{margin:0;font-size:clamp(20px,5vw,28px);line-height:1.25}\n"
        ".hero p{margin:8px 0 0;color:var(--dim);font-size:14px}\n"
        "h3{font-size:13px;text-transform:uppercase;letter-spacing:.08em;"
        "color:var(--dim);margin:24px 0 10px;font-weight:600}\n"
        ".cards{display:grid;gap:12px;"
        "grid-template-columns:repeat(auto-fit,minmax(min(100%,230px),1fr))}\n"
        ".card{background:var(--panel);border:1px solid var(--line);"
        "border-radius:12px;padding:14px}\n"
        ".card.off{opacity:.62}\n"
        ".cardtop{display:flex;align-items:center;gap:8px;margin-bottom:8px}\n"
        ".dot{width:9px;height:9px;border-radius:50%;flex:0 0 auto}\n"
        ".dot.on{background:var(--accent)}\n"
        ".dot.off{background:var(--off)}\n"
        ".cstate{font-size:12px;text-transform:uppercase;letter-spacing:.06em}\n"
        ".cstate.on{color:var(--accent)}\n"
        ".cstate.off{color:var(--off)}\n"
        ".cname{font-weight:700;font-size:16px;word-break:break-word}\n"
        ".cdev{color:var(--dim);font-size:13px;margin-top:2px;"
        "word-break:break-word}\n"
        ".speed{margin-top:10px}\n"
        ".speednum{font-size:12px;color:var(--dim)}\n"
        ".bartrack{display:block;height:6px;border-radius:99px;"
        "background:#0b0f16;margin-top:6px;overflow:hidden}\n"
        ".barfill{display:block;height:100%;border-radius:99px;"
        "background:linear-gradient(90deg,var(--accent2),var(--accent))}\n"
        ".badge{display:inline-block;margin-top:8px;font-size:11px;"
        "font-weight:700;letter-spacing:.04em;color:#06130a;"
        "background:var(--accent);border-radius:99px;padding:2px 8px}\n"
        ".grid2{display:grid;gap:12px;grid-template-columns:1fr}\n"
        "@media (min-width:700px){.grid2{grid-template-columns:1fr 1fr}}\n"
        ".panel{background:var(--panel);border:1px solid var(--line);"
        "border-radius:12px;padding:16px}\n"
        ".panel h4{margin:0 0 8px;font-size:15px}\n"
        ".lead{margin:0 0 12px;color:var(--dim);font-size:14px}\n"
        ".lead.tiny{font-size:13px}\n"
        ".okline{margin:0 0 12px;color:var(--accent);font-size:14px;"
        "font-weight:600}\n"
        ".code{margin:0 0 8px;font-size:clamp(30px,9vw,42px);font-weight:800;"
        "letter-spacing:.16em;font-variant-numeric:tabular-nums;"
        "background:var(--panel2);border:1px dashed var(--line);"
        "border-radius:10px;padding:10px 12px;text-align:center;"
        "word-break:break-all}\n"
        "label{display:block;font-size:13px;color:var(--dim);margin:10px 0 4px}\n"
        "input,select{display:block;width:100%;min-height:46px;padding:10px 12px;"
        "font-size:16px;color:var(--ink);background:#0b0f16;"
        "border:1px solid var(--line);border-radius:10px}\n"
        "input:focus,select:focus,button:focus{outline:2px solid #1f6feb;"
        "outline-offset:1px}\n"
        ".btn{display:inline-flex;align-items:center;justify-content:center;"
        "min-height:46px;padding:11px 18px;margin-top:12px;font-size:15px;"
        "font-weight:600;border-radius:10px;border:1px solid var(--line);"
        "background:var(--panel2);color:var(--ink);cursor:pointer}\n"
        ".btn.block{width:100%}\n"
        ".btn.primary{background:var(--accent2);border-color:var(--accent2);"
        "color:#04140a;width:100%}\n"
        ".btn.primary:hover{background:var(--accent)}\n"
        ".btn.ghost{background:transparent}\n"
        ".btnrow{display:flex;gap:10px;flex-wrap:wrap}\n"
        ".btnrow .btn{margin-top:0}\n"
        ".btnrow form{display:contents}\n"
        ".notice{margin:0 0 18px;padding:12px 14px;border-radius:10px;"
        "background:#2b2410;border:1px solid var(--warn);color:#f0d38a;"
        "font-size:14px}\n"
        ".empty{color:var(--dim)}\n"
        ".hint{color:var(--off);font-size:12px;margin-top:28px;"
        "border-top:1px solid var(--line);padding-top:14px}\n"
        "</style>\n"
        "</head>\n"
        "<body>\n"
        '<div class="wrap">\n'
        "<header>\n"
        '<div class="brand"><span class="logo"></span><h1>gpumesh</h1></div>\n'
        '<p class="tag">Make all your computers act like one.</p>\n'
        "</header>\n"
        f"{notice_html}"
        '<div class="hero">\n'
        f"<h2>{_esc(headline)}</h2>\n"
        f"<p>{_esc(status)}</p>\n"
        "</div>\n"
        f"<h3>Computers in this mesh ({total})</h3>\n"
        f'<div class="cards">\n{cards_html}\n</div>\n'
        "<h3>Share or join</h3>\n"
        '<div class="grid2">\n'
        f'<div class="panel"><h4>Step 1 &middot; Create mesh</h4>{create_html}</div>\n'
        f'<div class="panel"><h4>Step 2 &middot; Let another computer join</h4>{pair_html}</div>\n'
        "</div>\n"
        '<div class="panel" style="margin-top:12px">'
        f"<h4>Step 3 &middot; Join a mesh that already exists</h4>{join_html}</div>\n"
        '<p class="hint">This page stays on this computer. Your secret key is '
        "never shown here, and nothing leaves your network. It updates by "
        "itself every 5 seconds.</p>\n"
        "</div>\n"
        "<script defer>\n"
        "(function(){\n"
        "  var TYPING = ['INPUT','SELECT','TEXTAREA'];\n"
        "  setInterval(function(){\n"
        "    var el = document.activeElement;\n"
        "    if (el && TYPING.indexOf(el.tagName) !== -1) return;\n"
        "    location.reload();\n"
        "  }, 5000);\n"
        "  document.querySelectorAll('.copy').forEach(function(b){\n"
        "    b.addEventListener('click', function(){\n"
        "      var code = b.getAttribute('data-code') || '';\n"
        "      var done = function(){ b.textContent = 'Copied';\n"
        "        setTimeout(function(){ b.textContent = 'Copy code'; }, 1500); };\n"
        "      if (navigator.clipboard && navigator.clipboard.writeText) {\n"
        "        navigator.clipboard.writeText(code).then(done, done);\n"
        "      } else { done(); }\n"
        "    });\n"
        "  });\n"
        "})();\n"
        "</script>\n"
        "</body>\n"
        "</html>\n"
    )


def is_loopback_client(ip: object) -> bool:
    """True when ``ip`` is this machine only."""
    h = str(ip or "").strip().strip("[]").lower()
    if h.startswith("::ffff:"):
        # IPv4-mapped IPv6, e.g. ::ffff:127.0.0.1 on a dual-stack listener.
        h = h[len("::ffff:"):]
    return h in ("localhost", "::1", "0:0:0:0:0:0:0:1") or h.startswith("127.")


def _host_allowed(value: object) -> bool:
    """True when a ``Host`` header names this computer.

    Blocks DNS rebinding: a page on ``evil.test`` that resolves to 127.0.0.1
    reaches this socket, but it sends ``Host: evil.test``, so it is refused.
    """
    host = str(value or "").strip().lower()
    if not host:
        return False
    if host.startswith("["):
        host = host[1:].split("]", 1)[0]
    elif host.count(":") == 1:
        host = host.split(":", 1)[0]
    return is_loopback_client(host)


def _origin_allowed(value: object) -> bool:
    """True when a browser ``Origin`` is this dashboard.

    A cross-origin page can POST to 127.0.0.1 even though it cannot read the
    reply, and the side effects here are real (minting a code, joining a
    stranger's mesh and then running its work). A missing header means a
    non-browser client, which is allowed: the local check already passed.
    """
    if not value:
        return True
    try:
        parts = urllib.parse.urlsplit(str(value))
    except ValueError:
        return False
    return parts.scheme in ("http", "https") and is_loopback_client(parts.hostname)


def _mesh_client(url: str, token: str):
    from .worker import MeshClient

    return MeshClient(url, token)


def _snapshot(url: str, token: str) -> dict:
    """Best-effort machine rows from this computer's mesh. Token stays here.

    Returns ``{"machines": [...]}``, or ``{"unreachable": True}`` when the
    saved mesh cannot be reached at all -- which is a different thing to a
    mesh with nothing joined, and is reported differently on the page.
    """
    try:
        resp = _mesh_client(url, token).call("GET", "/api/workers", timeout=2.0) or {}
    except Exception:
        return {"unreachable": True}
    rows = resp.get("workers", []) if isinstance(resp, dict) else []
    machines = [
        {
            "name": w.get("hostname", w.get("id", "?")),
            "device": w.get("device", "cpu"),
            "device_name": w.get("device_name", ""),
            "score": w.get("score", 0.0),
            "alive": w.get("alive", True),
        }
        for w in rows if isinstance(w, dict)
    ]
    return {"machines": machines}


_NEARBY_LOCK = threading.Lock()
_NEARBY_CACHE: dict = {"at": 0.0, "rows": [], "busy": False}


def _nearby_scan() -> list:
    """Probe the network once. Never raises."""
    try:
        from .discovery import Listener

        listener = Listener()
        try:
            listener.start()
        except OSError:
            return []
        try:
            time.sleep(1.0)
            peers = listener.peers()
        finally:
            try:
                listener.stop()
            except Exception:
                pass
        out = []
        for p in peers:
            try:
                ip = (p.addr[0] if getattr(p, "addr", None) else "").strip()
                if not ip:
                    continue
                out.append((getattr(p, "hostname", "?"),
                            f"http://{ip}:{getattr(p, 'api_port', 8000)}"))
            except Exception:
                continue
        return out
    except Exception:
        return []


def _nearby(force: bool = False) -> list:
    """Cached ``(name, url)`` meshes heard on the network.

    Never blocks a page load: the probe runs on a daemon thread and the
    caller gets the previous answer until it lands.
    """
    with _NEARBY_LOCK:
        fresh = (time.time() - _NEARBY_CACHE["at"]) < NEARBY_CACHE_SECONDS
        if (fresh or _NEARBY_CACHE["busy"]) and not force:
            return list(_NEARBY_CACHE["rows"])
        _NEARBY_CACHE["busy"] = True

    def _refresh() -> None:
        rows = _nearby_scan()
        with _NEARBY_LOCK:
            _NEARBY_CACHE.update(at=time.time(), rows=rows, busy=False)

    threading.Thread(target=_refresh, daemon=True).start()
    return list(_NEARBY_CACHE["rows"])


def _saved() -> tuple:
    """Saved (url, token), or ("", "")."""
    try:
        from . import connection_manager

        saved = connection_manager.load_connection() or {}
        return saved.get("url") or "", saved.get("token") or ""
    except Exception:
        return "", ""


def _mint_pair(url: str, token: str) -> dict:
    """Fresh single-use pairing code for this computer's mesh, or {}."""
    try:
        resp = _mesh_client(url, token).call("POST", "/api/pair/request", {})
    except Exception:
        return {}
    if not isinstance(resp, dict) or not resp.get("code"):
        return {}
    try:
        ttl = int(resp.get("expires_in", 300))
    except (TypeError, ValueError):
        ttl = 300
    return {"code": str(resp["code"]), "at": time.time(), "ttl": ttl}


def _claim(host: str, code: str) -> str:
    """Trade a pairing code for the mesh key. "" on any failure."""
    try:
        resp = _mesh_client(host, "").call(
            "POST", "/api/pair/claim",
            {"code": code, "hostname": socket.gethostname()},
        )
    except Exception:
        return ""
    return resp.get("token", "") if isinstance(resp, dict) else ""


class DashboardHandler(BaseHTTPRequestHandler):
    """Stdlib page server. Binds to 127.0.0.1 only (see serve_dashboard)."""

    def _notice_page(self, notice: str) -> None:
        url, token = _saved()
        snap = _snapshot(url, token) if (url and token) else {}
        # "Sharing" means a mesh on this computer actually answers. A saved
        # connection whose mesh is gone is reported as unreachable instead
        # of as sharing, because those lead to different next steps: one
        # wants a code, the other wants the Start button.
        unreachable = bool(snap.get("unreachable"))
        machines = snap.get("machines") or []
        sharing = bool(url and token) and not unreachable
        pair = self._pair_for(url, token) if sharing else ()
        # Discovery runs only when there is nothing to show; on a live mesh
        # the list is already the answer and a UDP probe would be noise.
        neighbours = [] if machines else _nearby()
        self._send_html(build_dashboard_html(
            {"machines": machines}, pair=pair, nearby=neighbours,
            notice=notice, sharing=sharing, unreachable=unreachable,
        ))

    def _pair_for(self, url: str, token: str) -> tuple:
        """One cached code per dashboard process, re-minted on click.

        Minting per page load would burn single-use codes on every 5s
        refresh; never caching would strand the owner with no code after one
        use. A used code simply fails on the joiner side, and the button
        mints a fresh one. Returns ``(code, seconds_left)`` or ``()``.
        """
        if not (url and token):
            return ()
        cached = getattr(self.server, "gpumesh_pair", None)
        if not isinstance(cached, dict) or not cached.get("code") or \
                self._seconds_left(cached) <= 0:
            # Mint when there is no code to show *and* when the cached one has
            # expired. Caching a used-up code and never replacing it left the
            # panel saying "a code appears here in a moment" forever, which is
            # the worst possible answer for the person waiting on it.
            cached = _mint_pair(url, token)
            self.server.gpumesh_pair = cached
        left = self._seconds_left(cached)
        return (cached["code"], left) if cached.get("code") and left > 0 else ()

    @staticmethod
    def _seconds_left(cached: object) -> float:
        """Seconds a cached pairing code stays valid. 0 for anything unusable."""
        if not isinstance(cached, dict) or not cached.get("code"):
            return 0.0
        try:
            return (cached["at"] + cached["ttl"]) - time.time()
        except (KeyError, TypeError, ValueError):
            return 0.0

    def _send_html(self, body: str) -> None:
        data = body.encode()
        try:
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(data)
        except Exception:
            pass

    def _send_plain(self, status: int, body: bytes) -> None:
        try:
            self.send_response(status)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)
        except Exception:
            pass

    def _redirect(self, location: str) -> None:
        try:
            self.send_response(302)
            self.send_header("Location", location)
            self.send_header("Content-Length", "0")
            self.end_headers()
        except Exception:
            pass

    def _same_origin(self) -> bool:
        """Host/Origin guard. False when the caller must be refused."""
        if not _host_allowed(self.headers.get("Host")):
            self._send_plain(403, b"wrong Host header")
            return False
        if not _origin_allowed(self.headers.get("Origin")):
            self._send_plain(403, b"refused: cross-site request")
            return False
        return True

    def _local_only(self) -> bool:
        try:
            ip = self.client_address[0]
        except Exception:
            ip = ""
        if is_loopback_client(ip):
            return True
        self._send_plain(403, b"dashboard is available on this computer only")
        return False

    def _form(self) -> dict:
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except (TypeError, ValueError):
            length = 0
        if length <= 0 or length > FORM_MAX_BYTES:
            return {}
        try:
            raw = self.rfile.read(length).decode("utf-8", "replace")
        except Exception:
            return {}
        return {k: v[0] for k, v in
                urllib.parse.parse_qs(raw, keep_blank_values=True).items() if v}

    def _controller(self):
        return getattr(self.server, "gpumesh_controller", None)

    def do_GET(self) -> None:
        if not self._local_only() or not self._same_origin():
            return
        route = self.path.split("?", 1)[0].rstrip("/") or "/"
        if route == "/":
            self._redirect(UI_PATH)
            return
        if route != UI_PATH:
            self._send_plain(404, b"not found")
            return
        notice = ""
        query = [*self.path.split("?", 1), ""][1]
        if "joined=" in query:
            notice = "Joined. This computer is part of the mesh now."
        elif "created=" in query:
            notice = ("Starting the mesh on this computer. This takes a few "
                      "seconds; the page will catch up on its own.")
        elif "stopped=" in query:
            notice = "Stopped sharing on this computer."
        self._notice_page(notice)

    def do_POST(self) -> None:
        if not self._local_only() or not self._same_origin():
            return
        route = self.path.split("?", 1)[0].rstrip("/") or "/"

        if route == SCAN_PATH:
            _nearby(force=True)
            self._notice_page("Scanning the network for meshes...")
            return

        if route == CODE_PATH:
            self.server.gpumesh_pair = _mint_pair(*_saved())
            self._notice_page("Fresh code. It works once, for five minutes.")
            return

        if route == CREATE_PATH:
            controller = self._controller()
            started = None
            if controller is not None:
                try:
                    started = controller.create_mesh()
                except Exception as exc:
                    self._notice_page(f"Could not start sharing: {exc}")
                    return
            if started is None and controller is not None:
                self._notice_page(
                    "Could not start sharing. Close any other gpumesh "
                    "window, then try again.")
                return
            self._redirect(f"{UI_PATH}?created=1")
            return

        if route == STOP_PATH:
            controller = self._controller()
            if controller is not None:
                try:
                    controller.quit()
                except Exception:
                    pass
            self.server.gpumesh_pair = {}
            self._redirect(f"{UI_PATH}?stopped=1")
            return

        if route != JOIN_PATH:
            self._send_plain(404, b"not found")
            return

        form = self._form()
        # The typed address wins over a nearby pick, so typing something
        # always does what it looks like it should.
        host = (form.get("host") or "").strip() or (form.get("nearby") or "").strip()
        code = (form.get("code") or "").strip()
        if not host.startswith(("http://", "https://")):
            self._notice_page(
                "That address needs to start with http:// or https://, "
                "for example http://192.168.1.10:8000")
            return
        if not code:
            self._notice_page("Type the 6-digit code shown on the other computer.")
            return
        token = _claim(host, code)
        if not token:
            self._notice_page(
                "Code not accepted. It may be wrong, already used, or expired. "
                "Make a fresh one on the other computer and try again.")
            return
        try:
            from . import connection_manager

            connection_manager.save_connection(host, token)
        except Exception as exc:
            self._notice_page(f"Could not save the connection: {exc}")
            return
        controller = self._controller()
        if controller is not None:
            controller.join_mesh(host, token)
        self._redirect(f"{UI_PATH}?joined=1")

    def log_message(self, fmt: str, *args: object) -> None:
        pass


def serve_dashboard(port: int = DASHBOARD_DEFAULT_PORT,
                    mesh_port: int = MESH_DEFAULT_PORT) -> ThreadingHTTPServer:
    """Bind the page server to 127.0.0.1 only. Never 0.0.0.0.

    Threaded on purpose: a page load can wait on another computer for up to
    two seconds, and a single-threaded server would queue every other
    request behind it. The controller lives on the server so the children it
    starts are owned by the dashboard and stopped with it.
    """
    httpd = ThreadingHTTPServer(("127.0.0.1", int(port)), DashboardHandler)
    httpd.daemon_threads = True
    httpd.gpumesh_pair = {}
    httpd.gpumesh_mesh_port = int(mesh_port)
    httpd.gpumesh_controller = TrayController(
        f"http://127.0.0.1:{httpd.server_address[1]}{UI_PATH}", int(mesh_port))
    return httpd


def tray_available() -> bool:
    """True when the optional tray backend imports."""
    try:
        import pystray  # type: ignore[import-not-found]  # noqa: F401

        return True
    except Exception:
        return False


class TrayController:
    """Owns the tray icon and the serve/join child processes.

    Uses only existing CLI entry points
    (``python -m gpumesh serve`` / ``python -m gpumesh join``) via
    :mod:`subprocess`. Importing this module never requires ``pystray``;
    only :meth:`run` touches it, and it falls back to dashboard-only.
    """

    def __init__(self, dashboard_url: str =
                 f"http://127.0.0.1:{DASHBOARD_DEFAULT_PORT}/ui",
                 mesh_port: int = MESH_DEFAULT_PORT) -> None:
        self.dashboard_url = dashboard_url
        self.mesh_port = int(mesh_port)
        self._serve_proc: subprocess.Popen | None = None
        self._join_proc: subprocess.Popen | None = None
        self._icon: object = None

    def _running(self, proc: subprocess.Popen | None) -> bool:
        return proc is not None and proc.poll() is None

    def create_mesh(self, port: int | None = None) -> subprocess.Popen | None:
        """Start ``gpumesh serve`` as a child. Reuses a live one.

        Binds the LAN, not loopback: a loopback mesh answers no other
        computer, which would make "create then join" silently impossible.
        The exposure banner prints in the child's console.
        """
        if self._running(self._serve_proc):
            return self._serve_proc
        target = int(port if port is not None else self.mesh_port)
        proc = subprocess.Popen(  # noqa: S603  - fixed argv, no shell
            [sys.executable, "-m", "gpumesh", "serve",
             "--host", "0.0.0.0", "--port", str(target)]
        )
        self._serve_proc = proc
        return proc

    def join_mesh(self, url: str = "", token: str = "") -> subprocess.Popen | None:
        """Start ``gpumesh join`` as a child.

        With no address/key nothing starts (the page explains how to join
        instead). The key travels in the child's environment, never its
        command line, so it does not show up in process lists.
        """
        if not url or not token:
            return None
        if self._running(self._join_proc):
            return self._join_proc
        env = dict(os.environ)
        env["GPUMESH_TOKEN"] = token  # `join --token` reads this by default
        proc = subprocess.Popen(  # noqa: S603  - fixed argv, no shell
            [sys.executable, "-m", "gpumesh", "join", url], env=env
        )
        self._join_proc = proc
        return proc

    def open_dashboard(self) -> bool:
        """Open the local page in a browser. Best-effort."""
        try:
            return bool(webbrowser.open(self.dashboard_url))
        except Exception:
            return False

    def quit(self) -> None:
        """Stop children and the icon."""
        for proc in (self._serve_proc, self._join_proc):
            try:
                if self._running(proc):
                    proc.terminate()  # type: ignore[union-attr]
            except Exception:
                pass
        self._serve_proc = None
        self._join_proc = None
        icon, self._icon = self._icon, None
        if icon is not None:
            try:
                icon.stop()  # type: ignore[attr-defined]
            except Exception:
                pass

    def run(self) -> bool:
        """Show the tray. False when the backend is missing (dashboard-only)."""
        try:
            from pystray import Icon, Menu, MenuItem
        except Exception:
            return False

        try:
            from PIL import Image, ImageDraw  # type: ignore[import-not-found]

            img = Image.new("RGB", (32, 32), (13, 17, 23))
            ImageDraw.Draw(img).ellipse([6, 6, 26, 26], fill=(63, 185, 80))
        except Exception:
            return False
        menu = Menu(
            MenuItem(TRAY_MENU_LABELS[0], lambda: self.create_mesh()),
            MenuItem(TRAY_MENU_LABELS[1], lambda: self.open_dashboard()),
            MenuItem(TRAY_MENU_LABELS[2], lambda: self.open_dashboard()),
            MenuItem(TRAY_MENU_LABELS[3], lambda: self.quit()),
        )
        try:
            self._icon = Icon("gpumesh", img, "GPUMesh", menu)
            self._icon.run()  # type: ignore[attr-defined]
            return True
        except Exception:
            self._icon = None
            return False


def _opt_int(args: object, name: str, default: int) -> int:
    try:
        return int(getattr(args, name, default))
    except (TypeError, ValueError):
        return default


def cmd_gui(args: object) -> None:
    """Run the local dashboard, with a tray icon when possible.

    Never prints or logs any key; the page itself is served from
    127.0.0.1 only. The tray and the page share one controller, so a mesh
    started from either one is stopped by either one.
    """
    port = _opt_int(args, "port", DASHBOARD_DEFAULT_PORT)
    mesh_port = _opt_int(args, "mesh_port", MESH_DEFAULT_PORT)
    no_tray = bool(getattr(args, "no_tray", False))
    no_browser = bool(getattr(args, "no_browser", False))

    try:
        httpd = serve_dashboard(port, mesh_port)
    except OSError as exc:
        print(f"[ERROR] Dashboard port {port} is busy: {exc}")
        raise SystemExit(1) from None

    controller = getattr(httpd, "gpumesh_controller", TrayController(mesh_port=mesh_port))
    url = f"http://127.0.0.1:{httpd.server_address[1]}{UI_PATH}"
    print(f"[OK] Dashboard on {url}")
    print("   This page is on this computer only.")

    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()

    if not no_browser:
        try:
            webbrowser.open(url)
        except Exception:
            pass

    try:
        if not no_tray and tray_available():
            controller.run()
        else:
            if not no_tray:
                print("   Tray icon not available -- the page does everything.")
            print("   Ctrl+C to stop.")
            try:
                while True:
                    time.sleep(1.0)
            except KeyboardInterrupt:
                pass
    finally:
        try:
            httpd.shutdown()
        except Exception:
            pass
        try:
            httpd.server_close()
        except Exception:
            pass
        controller.quit()
