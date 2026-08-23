#!/usr/bin/env python3
"""Séance-shaped Ollama bench. Prompts come from the real session/weave functions.

    PYTHONPATH=app python3 tools/bench_models.py --self-test
    PYTHONPATH=app python3 tools/bench_models.py --host http://127.0.0.1:11434 \
        --models qwen3:30b-a3b,mistral-small:24b,qwen3:32b,gemma3:27b

Does not change production. Default request options are the recommended
set in tools/MODEL_PASS.md; --prod-options replays llm.py as it ships.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "app"))

from oracle.session import _echoes_llm, _followup_llm, _seal_llm  # noqa: E402
from oracle.weave import SYSTEM, weave  # noqa: E402

BANNED = (
    "journey", "vibrant", "tapestry", "magical", "cosmic", "manifest",
    "energy", "vibes", "unlock", "delve", "the universe", "hush now",
)

# Recommended options (MODEL_PASS.md). Production is temperature 0.75 only.
REC_OPTS = {
    "followup": {"temperature": 0.75, "num_predict": 80, "num_ctx": 8192},
    "weave":    {"temperature": 0.5,  "num_predict": 700, "num_ctx": 8192},
    "echoes":   {"temperature": 0.5,  "num_predict": 180, "num_ctx": 8192},
    "seal":     {"temperature": 0.5,  "num_predict": 500, "num_ctx": 8192},
}
PROD_OPTS = {k: {"temperature": 0.75} for k in REC_OPTS}

SEEKERS = [
    {
        "id": "first-timer",
        "shares": [
            "first burn, came with my camp but I keep hovering at the edge of other people's plans",
            "I said yes to everyone all year and I am tired of disappearing into that",
        ],
        "context": "hour=dusk; first burn; arrived with camp-mates, not a partner.",
    },
    {
        "id": "couple",
        "shares": [
            "we got here last night, it's our third burn together",
            "we keep making a plan and then hiding in the group instead of actually doing it",
        ],
        "context": "hour=afternoon heat; with a partner; third burn.",
    },
    {
        "id": "returning-alone",
        "shares": [
            "I have been sober 90 days and I have not told anyone at camp",
            "I came to walk out past the Man and stay until I want one thing",
        ],
        "context": "hour=night; alone; returning burner.",
    },
]

# Frozen spread so models see the same cards. Locations are real 2026 names.
CARDS = {
    "roots": {
        "id": "roots-01", "name": "Mebuyan Pulse", "realm": "roots",
        "reading": "The descent isn't a punishment; it's a nursery.",
        "shadow": "Romanticizing the descent can keep you down there.",
        "turtle_dare": "Climb the spheres of Mebuyan Pulse after dark.",
        "real_2026": {"name": "Mebuyan Pulse"},
    },
    "trunk": {
        "id": "trunk-01", "name": "Move Slow & Bite Things", "realm": "trunk",
        "reading": "Patience without teeth is just waiting.",
        "shadow": "Moving slow forever is just never deciding.",
        "turtle_dare": "Stop circling — bite it today.",
        "real_2026": {"name": "Terrible Turtle Camp"},
    },
    "branches": {
        "id": "branches-01", "name": "Above and Below", "realm": "branches",
        "reading": "You can only reach as high as you're willing to be rooted.",
        "shadow": "All reach and no root blows away in the first wind.",
        "turtle_dare": "Climb Above and Below at sunrise.",
        "real_2026": {"name": "Above and Below"},
    },
}
LOCATED = {
    r: {"directions": f'{CARDS[r]["real_2026"]["name"]}, see the card'}
    for r in ("roots", "trunk", "branches")
}

CANNED = {
    "followup": "What are you still saying yes to, just so nobody asks you to stay?",
    "weave": json.dumps({
        "reading": (
            "You said yes to everyone all year. That is a fine way to disappear. "
            "The dark you fell into was tending you, and you keep hovering at the "
            "edge of other people's plans as if the edge were a home. It is not. "
            "Nobody needs you tonight — which is the door. Stand in the dust until "
            "you want one thing, then bite it. The slow yes and the sharp no are "
            "the same skill, and you have been practising only the first. Leave the "
            "map. Stay until the wanting is yours."
        ),
        "adventure": (
            "FACE: walk out past the Man alone and stay until you can name one want. "
            "STAND: sit in the Turtle shell until someone sits with you. "
            "REACH: tell one person the want. Leave the circling-habit there."
        ),
    }),
    "echoes": json.dumps({
        "roots": "You said 'hovering at the edge' — and the tide kept none of it.",
        "trunk": "You said 'yes to everyone' — and the jaw never closed.",
        "branches": "You said 'tired of disappearing' — so stop handing the map away.",
    }),
    "seal": json.dumps({"moves": [
        {"task": "Walk out past the Man alone and stay until you name one want.",
         "where": "deep playa past the Man", "proof": "the want written on tape", "leave": ""},
        {"task": "Sit in the shell until someone sits with you.",
         "where": "Terrible Turtle Camp", "proof": "their name", "leave": ""},
        {"task": "Tell one person the want. Leave the yes-habit behind.",
         "where": "Above and Below", "proof": "a witness", "leave": "the circling habit named out loud"},
    ]}),
}


class CaptureLLM:
    """Production generate() sink: records exact prompts, returns canned JSON."""

    def __init__(self):
        self.model = "capture"
        self.calls = []  # {stage, prompt, system, as_json}

    def available(self):
        return True

    def generate(self, prompt, system=None, timeout=90, as_json=False):
        stage = ("weave" if '"reading"' in (prompt or "") and "Three cards rose" in (prompt or "")
                 else "echoes" if "SEEKER'S WORDS" in (prompt or "")
                 else "seal" if "Seal this quest" in (prompt or "")
                 else "followup")
        self.calls.append({
            "stage": stage, "prompt": prompt, "system": system,
            "as_json": as_json, "timeout": timeout,
        })
        return CANNED[stage]


def capture_prompts(seeker):
    llm = CaptureLLM()
    _followup_llm(seeker["shares"], llm)
    sess = {
        "shares": seeker["shares"], "picks": CARDS, "located": LOCATED,
        "adventure": json.loads(CANNED["weave"])["adventure"],
        "ground": 0.0, "weather": None, "name": "Wren",
    }
    # weave() needs a real LLM-shaped object; CaptureLLM is enough.
    weave(" ".join(seeker["shares"]), CARDS, llm, LOCATED, context=seeker["context"])
    _echoes_llm(sess, llm)
    _seal_llm(sess, llm)
    return llm.calls


def stream_generate(host, model, prompt, system, options, think, timeout):
    """One streaming /api/generate. Returns dict with text, ttft, toks, wall, error."""
    opts = dict(options)
    fmt = opts.pop("_format_json", False)
    body = {
        "model": model,
        "prompt": prompt,
        "system": system or "",
        "stream": True,
        "keep_alive": -1,
        "options": opts,
    }
    if fmt:
        body["format"] = "json"
    if think is False:
        body["think"] = False
    data = json.dumps(body).encode()
    req = urllib.request.Request(
        host.rstrip("/") + "/api/generate", data=data,
        headers={"Content-Type": "application/json"},
    )
    t0 = time.monotonic()
    ttft = None
    chunks = []
    eval_count = prompt_eval = 0
    eval_ns = prompt_ns = 0
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            for raw in resp:
                line = raw.decode("utf-8", errors="replace").strip()
                if not line:
                    continue
                msg = json.loads(line)
                piece = msg.get("response") or ""
                if piece and ttft is None:
                    ttft = time.monotonic() - t0
                chunks.append(piece)
                if msg.get("done"):
                    eval_count = int(msg.get("eval_count") or 0)
                    eval_ns = int(msg.get("eval_duration") or 0)
                    prompt_eval = int(msg.get("prompt_eval_count") or 0)
                    prompt_ns = int(msg.get("prompt_eval_duration") or 0)
                    break
    except Exception as e:
        return {"text": None, "error": f"{type(e).__name__}: {e}",
                "ttft": ttft, "wall": time.monotonic() - t0,
                "eval_count": eval_count, "toks": 0}
    wall = time.monotonic() - t0
    toks = eval_count
    tps = (eval_count / (eval_ns / 1e9)) if eval_ns else 0.0
    return {
        "text": "".join(chunks).strip() or None,
        "error": None,
        "ttft": ttft if ttft is not None else wall,
        "wall": wall,
        "eval_count": eval_count,
        "prompt_eval_count": prompt_eval,
        "toks": tps,
        "prompt_ns": prompt_ns,
        "eval_ns": eval_ns,
    }


def score(stage, text, as_json):
    out = {"json_ok": None, "words": 0, "banned": [], "has_you": False}
    if not text:
        return out
    out["words"] = len(text.split())
    low = text.lower()
    out["banned"] = [w for w in BANNED if w in low]
    out["has_you"] = " you " in f" {low} " or low.startswith("you ")
    if as_json:
        try:
            json.loads(text)
            out["json_ok"] = True
        except Exception:
            out["json_ok"] = False
    return out


def no_think(model):
    return (model or "").lower().startswith(("qwen3", "deepseek", "gpt-oss", "magistral"))


def run_model(host, model, option_set, timeout):
    rows = []
    for seeker in SEEKERS:
        calls = capture_prompts(seeker)
        for call in calls:
            opts = dict(option_set[call["stage"]])
            if call["as_json"]:
                opts["_format_json"] = True
            result = stream_generate(
                host, model, call["prompt"], call["system"] or SYSTEM,
                opts, think=False if no_think(model) else None,
                timeout=timeout,
            )
            sc = score(call["stage"], result["text"], call["as_json"])
            rows.append({
                "model": model, "seeker": seeker["id"], "stage": call["stage"],
                **{k: result[k] for k in ("ttft", "wall", "toks", "eval_count", "error")},
                **sc,
            })
    return rows


def print_table(rows):
    hdr = f"{'model':<22} {'seeker':<16} {'stage':<9} {'ttft':>6} {'wall':>6} {'tok/s':>6} {'json':>5} {'words':>5} banned"
    print(hdr)
    print("-" * len(hdr))
    for r in rows:
        js = {True: "ok", False: "FAIL", None: "—"}.get(r["json_ok"], "—")
        print(f"{r['model']:<22} {r['seeker']:<16} {r['stage']:<9} "
              f"{(r['ttft'] or 0):6.2f} {(r['wall'] or 0):6.2f} {r['toks']:6.1f} "
              f"{js:>5} {r['words']:5d} {','.join(r['banned']) or '—'}"
              + (f"  ERR {r['error']}" if r["error"] else ""))
    # 2-seeker tax: 2 × mean weave wall (Ollama serialises)
    weaves = [r for r in rows if r["stage"] == "weave" and not r["error"]]
    if weaves:
        mean = sum(r["wall"] for r in weaves) / len(weaves)
        print(f"\n2-seeker tax (2 × mean weave wall): {2 * mean:.1f}s  "
              f"{'INSIDE' if 2 * mean < 60 else 'OUTSIDE'} the 60s T_LONG guard")


# --- self-test (fake Ollama, no GPU) -----------------------------------------

class FakeOllama(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_POST(self):
        n = int(self.headers.get("Content-Length", 0))
        req = json.loads(self.rfile.read(n))
        prompt = req.get("prompt") or ""
        if "Three cards rose" in prompt:
            text = CANNED["weave"]
        elif "SEEKER'S WORDS" in prompt:
            text = CANNED["echoes"]
        elif "Seal this quest" in prompt:
            text = CANNED["seal"]
        else:
            text = CANNED["followup"]
        self.send_response(200)
        self.send_header("Content-Type", "application/x-ndjson")
        self.end_headers()
        # one token, then done — enough to prove TTFT + parse
        first, rest = text[:8], text[8:]
        self.wfile.write((json.dumps({"response": first, "done": False}) + "\n").encode())
        self.wfile.flush()
        self.wfile.write((json.dumps({
            "response": rest, "done": True,
            "eval_count": max(1, len(text.split())),
            "eval_duration": 50_000_000,
            "prompt_eval_count": 10,
            "prompt_eval_duration": 10_000_000,
        }) + "\n").encode())


def self_test():
    fails = 0

    def check(name, got, want):
        nonlocal fails
        ok = got == want
        print(("  ok   " if ok else "  FAIL ") + name + ("" if ok else f"\n         got {got!r}\n         want {want!r}"))
        if not ok:
            fails += 1
        return ok

    calls = capture_prompts(SEEKERS[0])
    stages = [c["stage"] for c in calls]
    check("captures followup, weave, echoes, seal", stages,
          ["followup", "weave", "echoes", "seal"])
    check("weave prompt is the production binding",
          "Three cards rose along the World Tree" in calls[1]["prompt"], True)
    check("echoes demand a verbatim seeker quote",
          "SEEKER'S WORDS" in calls[2]["prompt"], True)
    check("seal asks for FACE/STAND/REACH JSON",
          "Seal this quest into exactly three moves" in calls[3]["prompt"], True)
    check("follow-up is not JSON", calls[0]["as_json"], False)
    check("weave is JSON", calls[1]["as_json"], True)

    srv = ThreadingHTTPServer(("127.0.0.1", 0), FakeOllama)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    host = f"http://127.0.0.1:{srv.server_address[1]}"
    rows = run_model(host, "fake", PROD_OPTS, timeout=5)
    check("12 rows (3 seekers × 4 calls)", len(rows), 12)
    check("all JSON stages parsed",
          all(r["json_ok"] is not False for r in rows), True)
    check("TTFT recorded", all(r["ttft"] is not None for r in rows), True)
    srv.shutdown()
    print(f"\n{'ALL PASS' if not fails else str(fails) + ' FAILED'}")
    return 1 if fails else 0


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--host", default=os.environ.get("OLLAMA_HOST", "http://127.0.0.1:11434"))
    ap.add_argument("--models", default="qwen3:30b-a3b")
    ap.add_argument("--prod-options", action="store_true",
                    help="send production options (temperature 0.75 only)")
    ap.add_argument("--timeout", type=float, default=90)
    ap.add_argument("--self-test", action="store_true")
    args = ap.parse_args()
    if args.self_test:
        return self_test()
    option_set = PROD_OPTS if args.prod_options else REC_OPTS
    all_rows = []
    for model in [m.strip() for m in args.models.split(",") if m.strip()]:
        print(f"\n=== {model}  options={'prod' if args.prod_options else 'recommended'} ===")
        rows = run_model(args.host, model, option_set, args.timeout)
        print_table(rows)
        all_rows.extend(rows)
    print("\nJSON summary:")
    print(json.dumps(all_rows, indent=2, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
