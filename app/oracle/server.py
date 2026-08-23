"""Zero-dependency local server for the oracle kiosk.

Run:  PYTHONPATH=app python3 -m oracle.server   (then open http://localhost:8777)

Stdlib only, so it runs on playa with no pip installs. Uses a local Ollama model if one is
running; otherwise the offline template weave. Single-threaded on purpose: one seeker at a time.
"""
import json
import os
import re
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from .deck import load_deck, REPO, card_payload
from .select import select_cards
from .weave import weave
from .llm import make_llm
from .geo import locate, locate_spread, directions_lines, COMPASS_ROSE
from . import printer
from . import session
from . import ears
from . import lore
from . import voice

WEB = os.path.join(REPO, "app", "web")
ART = os.path.join(REPO, "cards", "art")
PORT = int(os.environ.get("ORACLE_PORT", "8777"))
LLM_SINGLETON = make_llm()
VOICE_SINGLETON = voice.KokoroVoice()
ART_RE = re.compile(r"^(shell|roots|trunk|branches)-\d{2}\.png$")
WEBIMG_RE = re.compile(r"^((shell|roots|trunk|branches)-\d{2}|back)\.jpg$")

# Legacy single-question flow (/api/reading, app/web/index.html) only. The séance
# (/api/session/*) never touches this — its state is per-session, because two tablets
# plus phones on the camp network mean several seekers are mid-reading at once and a
# shared "last reading" prints one person's quest for another.
LAST = {}
LAST_LOCK = threading.Lock()

# Per-station printer binding: ESCPOS_HOST_1 / _2 pick a printer for kiosk=1 / kiosk=2.
def _printer_host(kiosk):
    return os.environ.get(f"ESCPOS_HOST_{kiosk}") if kiosk else None


# The fallback weave is good enough that a dead model reads as a working oracle — the
# séance completes, the reading is real, nobody notices. On playa that could go a whole
# night. Count which tier actually served each reading so a camp turtle can check.
TIERS = {"llm": 0, "fallback": 0}
TIERS_LOCK = threading.Lock()


def note_tier(mode):
    with TIERS_LOCK:
        if mode in TIERS:
            TIERS[mode] += 1


def build_reading(question):
    _, _, by_realm = load_deck()
    picks, sel_mode = select_cards(question, by_realm, LLM_SINGLETON)
    located = locate_spread(picks)
    reading, weave_mode = weave(question, picks, LLM_SINGLETON, located)
    payload = {
        "question": question,
        "cards": {r: card_payload(picks[r], located[r]) for r in ("roots", "trunk", "branches")},
        "reading": reading["reading"],
        "adventure": reading["adventure"],
        "map": COMPASS_ROSE,
        "directions": directions_lines(picks, located),
        "modes": {"select": sel_mode, "weave": weave_mode},
    }
    with LAST_LOCK:
        LAST.clear()
        LAST.update({"payload": payload, "picks": picks, "located": located})
    return payload


def receipt_for_session(sid):
    """Format the receipt for one séance, from that séance's own state."""
    sess = session.snapshot(sid)
    if not sess:
        return None
    payload = {
        "question": " / ".join(sess["shares"]),
        "reading": sess["reading"],
        "adventure": sess["adventure"],
        "name": sess.get("name"),
    }
    return printer.format_receipt(payload, sess["picks"], sess["located"],
                                  quest=sess.get("quest"))


REALM_ORDER = {"shell": 0, "roots": 1, "trunk": 2, "branches": 3}


def all_cards_payload():
    _, cards, _ = load_deck()
    ordered = sorted(cards, key=lambda c: (REALM_ORDER[c["realm"]], c["number"]))
    return [card_payload(c, locate(c)) for c in ordered]


DOWNLOADS = {
    "booklet.pdf": ("print/booklet.pdf", "application/pdf"),
    "proof.pdf": ("print/proof.pdf", "application/pdf"),
    "contact-sheet.png": ("cards/contact-sheet.png", "image/png"),
}


class Handler(BaseHTTPRequestHandler):
    # HTTP/1.0 (connection closes per request) + threading = robust; no keep-alive edge cases.
    timeout = 20  # cap a slow client's request read so a thread can't hang forever

    def _send(self, code, body, ctype="application/json", cache=None):
        if isinstance(body, (dict, list)):
            body = json.dumps(body).encode("utf-8")
        elif isinstance(body, str):
            body = body.encode("utf-8")
        try:
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            # never cache HTML/API (so fixes land on refresh); images/pdf may cache
            self.send_header("Cache-Control", cache or
                             ("no-store" if ctype.startswith("text/html") or
                              ctype == "application/json" else "public, max-age=86400"))
            self.end_headers()
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _serve_file(self, relpath, ctype):
        try:
            with open(os.path.join(REPO, relpath), "rb") as f:
                return self._send(200, f.read(), ctype)
        except FileNotFoundError:
            return self._send(404, {"error": f"{relpath} missing"})

    def do_GET(self):
        path = self.path.split("?", 1)[0]
        if path in ("/", "/index.html", "/site.html"):
            return self._serve_file("app/web/site.html", "text/html; charset=utf-8")
        if path in ("/oracle", "/oracle.html", "/app"):
            return self._serve_file("app/web/index.html", "text/html; charset=utf-8")
        if path in ("/kiosk", "/kiosk.html"):
            return self._serve_file("app/web/kiosk.html", "text/html; charset=utf-8")
        if path.startswith("/tiles/"):
            name = os.path.basename(path)
            if re.match(r"^[a-z_]+\.jpg$", name):
                return self._serve_file(f"cards/web/tiles/{name}", "image/jpeg")
            return self._send(404, {"error": "no such tile"})
        if path == "/avatar.jpg":
            rel = "cards/web/med/avatar.jpg" if os.path.exists(os.path.join(REPO, "cards/web/med/avatar.jpg")) \
                else "cards/back.png"
            return self._serve_file(rel, "image/jpeg" if rel.endswith(".jpg") else "image/png")
        if path == "/api/deck":
            data, cards, _ = load_deck()
            return self._send(200, {"title": data["deck"]["title"],
                                    "subtitle": data["deck"]["subtitle"],
                                    "count": len(cards),
                                    "llm": LLM_SINGLETON.available()})
        if path == "/api/cards":
            return self._send(200, all_cards_payload())
        if path == "/api/lore":
            return self._send(200, lore.counts())
        if path == "/api/health":
            with TIERS_LOCK:
                tiers = dict(TIERS)
            total = tiers["llm"] + tiers["fallback"]
            return self._send(200, {
                "llm_reachable": LLM_SINGLETON.available(),
                "backend": type(LLM_SINGLETON).__name__.replace("CloudLLM", "cloud")
                                                       .replace("LLM", "ollama"),
                "model": LLM_SINGLETON.model,
                "ears": ears.available(),
                "voice": VOICE_SINGLETON.status(),
                "readings": tiers,
                # the number to look at: if this is climbing, the Turtle has gone dumb
                # and is hiding it behind a very convincing template
                "fallback_pct": round(100.0 * tiers["fallback"] / total, 1) if total else None,
                "live_seances": len(session.SESSIONS),
                "printer": ("network" if os.environ.get("ESCPOS_HOST") or
                            os.environ.get("ESCPOS_HOST_1")
                            else "usb" if os.environ.get("ESCPOS_VENDOR_ID") else "preview-only"),
            })
        if path.startswith("/thumb/") or path.startswith("/med/"):
            sub = "thumb" if path.startswith("/thumb/") else "med"
            name = os.path.basename(path)
            if WEBIMG_RE.match(name):
                return self._serve_file(f"cards/web/{sub}/{name}", "image/jpeg")
            return self._send(404, {"error": "no such image"})
        if path == "/art/back.png":
            return self._serve_file("cards/back.png", "image/png")
        if path.startswith("/art/"):
            name = os.path.basename(path)
            if ART_RE.match(name):
                fp = os.path.join(ART, name)
                if os.path.exists(fp):
                    with open(fp, "rb") as f:
                        return self._send(200, f.read(), "image/png")
            return self._send(404, {"error": "no such card art"})
        if path.startswith("/download/"):
            name = os.path.basename(path)
            if name in DOWNLOADS:
                rel, ctype = DOWNLOADS[name]
                return self._serve_file(rel, ctype)
            return self._send(404, {"error": "no such download"})
        return self._send(404, {"error": "not found"})

    def do_POST(self):
        path = self.path.split("?", 1)[0]
        length = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(length) if length else b""
        if path == "/api/reading":
            try:
                q = (json.loads(raw or b"{}").get("question") or "").strip()
            except Exception:
                q = ""
            if not q:
                return self._send(400, {"error": "no question"})
            try:
                return self._send(200, build_reading(q))
            except Exception as e:
                return self._send(500, {"error": str(e)})
        if path == "/api/print":
            try:
                body = json.loads(raw or b"{}")
            except Exception:
                body = {}
            sid = (body.get("session") or "").strip()
            if sid:
                text = receipt_for_session(sid)
                if text is None:
                    return self._send(400, {"error": "no such séance to print"})
            else:
                # legacy /api/reading flow
                with LAST_LOCK:
                    if not LAST:
                        return self._send(400, {"error": "no reading to print yet"})
                    snap = dict(LAST)
                text = printer.format_receipt(snap["payload"], snap["picks"], snap["located"],
                                              quest=snap.get("quest"))
            result = printer.print_or_preview(text, host=_printer_host(body.get("kiosk")))
            result["receipt"] = text
            return self._send(200, result)
        if path == "/api/transcribe":
            if not raw:
                return self._send(400, {"error": "no audio"})
            ctype = (self.headers.get("Content-Type") or "").split(";")[0].strip()
            suffix = {"audio/webm": ".webm", "audio/mp4": ".mp4", "audio/mpeg": ".mp3",
                      "audio/ogg": ".ogg", "audio/wav": ".wav"}.get(ctype, ".webm")
            text = ears.transcribe(raw, suffix)
            if text is None:
                return self._send(501, {"error": "the Turtle has no ears on this machine"})
            return self._send(200, {"text": text})
        if path == "/api/speak":
            try:
                text = (json.loads(raw or b"{}").get("text") or "").strip()
            except Exception:
                text = ""
            if not text:
                return self._send(400, {"error": "no text"})
            try:
                wav = VOICE_SINGLETON.synthesize(text)
            except ValueError as exc:
                return self._send(400, {"error": str(exc)})
            except voice.VoiceUnavailable:
                return self._send(503, {"error": "the Turtle's deeper voice is unavailable"})
            # Readings contain the seeker's words; never let a browser or proxy retain them.
            return self._send(200, wav, "audio/wav", cache="no-store")
        if path.startswith("/api/session/"):
            try:
                body = json.loads(raw or b"{}")
            except Exception:
                body = {}
            action = path.rsplit("/", 1)[1]
            try:
                if action == "start":
                    mode = (body.get("mode") or "seek").strip()
                    return self._send(200, session.start(mode))
                sid = (body.get("session") or "").strip()
                if action == "say":
                    event = session.hear(sid, body, LLM_SINGLETON)
                    modes = event.get("modes") or {}
                    note_tier(modes.get("weave"))
                    note_tier(modes.get("refine"))
                    return self._send(200, event)
                if action == "accept":
                    # The sealed quest stays on the session; /api/print reads it back by
                    # session id. Nothing is staged in shared state.
                    result = session.accept(sid, LLM_SINGLETON)
                    note_tier((result.get("modes") or {}).get("seal"))
                    return self._send(200, result)
            except Exception as e:
                return self._send(500, {"error": str(e)})
            return self._send(404, {"error": "unknown séance action"})
        return self._send(404, {"error": "not found"})

    def log_message(self, *a):
        pass  # quiet


class OracleServer(ThreadingHTTPServer):
    request_queue_size = 128   # larger listen backlog so bursts of parallel connections aren't dropped
    daemon_threads = True
    allow_reuse_address = True


# keep_alive=-1 should keep the model resident forever, but a Spark reboot, an Ollama
# restart, or an eviction under memory pressure all silently cold the model — and the
# NEXT seeker eats a 60s timeout and gets served the (perfectly polished, entirely
# generic) offline template with nobody the wiser. One boot-time warm call doesn't catch
# any of those; a standing loop does.
WARM_INTERVAL = float(os.environ.get("ORACLE_WARM_INTERVAL", "240"))


def _warm_keeper():
    import time
    while True:
        try:
            if LLM_SINGLETON.available():
                LLM_SINGLETON.generate("wake", timeout=90)
        except Exception:
            pass
        time.sleep(WARM_INTERVAL)


def _warm_voice():
    try:
        VOICE_SINGLETON.warm()
    except voice.VoiceUnavailable:
        pass


def main():
    print(f"🐢  Terrible Turtle Oracle at http://localhost:{PORT}")
    print(f"    LLM (Ollama {LLM_SINGLETON.model}): {'available' if LLM_SINGLETON.available() else 'OFF — using offline weave'}")
    print(f"    Ears (whisper.cpp): {'available' if ears.available() else 'OFF — typed input only'}")
    print(f"    Voice: {VOICE_SINGLETON.status()['backend']} ({VOICE_SINGLETON.voice})")
    threading.Thread(target=_warm_keeper, daemon=True).start()
    threading.Thread(target=_warm_voice, daemon=True).start()
    OracleServer(("0.0.0.0", PORT), Handler).serve_forever()


if __name__ == "__main__":
    main()
