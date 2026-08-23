"""The séance: a staged Oracle conversation → theatrical pull → reading → quest negotiation.

Heavily structured, lightly LLM. The stages are fixed (the ritual); the LLM only fills
warm specifics inside them (the voice). Every LLM touch has a template fallback, so the
whole ceremony runs offline on playa with nothing but the deck.

Seeker stages:  naming → listening → deepening → proposed → accepted
Tale stages:    tale_naming → tale_listening → tale_told
The Tale-Book (lore.py) makes the Turtle remember returning seekers across the burn.
"""
import datetime
import json
import random
import re
import time
import uuid

import os

from .deck import load_deck, card_payload, draw_spread, REPO
from .select import select_fallback, _tokens
from .weave import weave, SYSTEM, card_lore
from .geo import locate_spread, directions_lines, COMPASS_ROSE
from . import lore

WEATHER = json.load(open(os.path.join(REPO, "data", "weather.json"), encoding="utf-8"))
WEATHERS = {w["id"]: w for w in WEATHER["weathers"]}
STONES = WEATHER["stones"]
WEATHER_ASK = WEATHER["meta"]["ask"]

SESSIONS = {}
MAX_SESSIONS = 200  # two stations plus phones on the camp network: many séances at once

# LLM patience, set from measurement on the DGX Spark (qwen3:30b-a3b, 2026-08-07).
#
#   1 seeker,  warm:   full séance 10.5s   (weave+echoes 5.7, refine 2.1, seal 2.7)
#   6 seekers, warm:   slowest single call 24.4s, whole séance ~57s wall
#
# Ollama serialises on one GPU, so per-call latency scales with how many seekers are
# mid-séance. 25s looked generous against the solo number and would have tripped at a
# busy moment — the worst possible time, because that is when the most people are
# watching. These are guards against a genuinely hung model, not pacing controls: the
# fallback is a VISIBLE drop in quality, so let the model win whenever it is merely slow.
T_SHORT = float(os.environ.get("ORACLE_T_SHORT", "45"))   # one-liners: follow-up, echoes
T_LONG = float(os.environ.get("ORACLE_T_LONG", "60"))     # structured: refine, seal

NAME_ASKS = [
    "Ah. A traveler. Come closer — the shell is warm. First things first: what do they call you out here?",
    "Welcome, dusty one. Before any card moves, the Turtle takes names. What name do you carry tonight?",
    "Mm. The Tree said someone was coming. Sit. Tell me the name you go by in this city.",
]

STEM_ASKS = [
    "Mm. Then finish this, out loud, and only this:",
    "The Turtle believes you. Now finish this sentence — nothing more:",
    "Good. One sentence is enough, if it is true. Finish this:",
]

STONES_ASK = ("Words are hard tonight. No matter — the shell reads weight. "
              "Touch what you are carrying. Leave the rest in the dust.")

DRAWN_LINES = [
    "Enough. The Turtle has heard you. Watch — the Tree is choosing.",
    "The shell hums. Three cards rise for you: what to face, where you stand, what to reach for.",
    "Good. That is enough truth to pull on. The Tree is choosing your three.",
]

# Spoken instead of the usual line when a Shell card substitutes into a slot — roughly
# one séance in ten. The Turtle interrupting its own format is the whole point.
AXIS_LINE = ("The shell goes quiet. Mm. That is not a card from the Tree — that is the "
             "Tree's own spine. “{card}” has come up for you, and the Turtle does not "
             "choose when that happens. Sit with it.")

REFINE_ACKS = [
    "Mm. That changes the shape of it. The Tree bends — hear your quest again.",
    "Good. More truth makes a better quest. Listen.",
    "The Turtle chews on that. Slowly. Yes — the quest turns like this.",
]

DECISION_ASK = "Do you accept this quest? Or shall the Turtle hear more before it is sealed?"

ACCEPT_LINES = [
    "So be it. The quest is sealed. Move slow, bite things, and bring your proofs back to the shell.",
    "Sealed. The Tree will be watching, and trees see everything slowly. Go — and come back with the tale.",
]

TALE_NAME_ASKS = [
    "You came back. The shell felt your steps. First — the name you carry.",
    "A returner. Good. The Turtle keeps its ledger by name — what is yours?",
]

TALE_INVITES = [
    "Now. A turtle of the shell must stand beside you — the tale is told to a living creature, "
    "not a machine. Tell them the tale aloud, and let the shell listen too. Speak when ready.",
]

TALE_THANKS = [
    "So it happened, and now it is story. The shell keeps it in the Tale-Book. "
    "Turtle who witnessed: this one has earned the gift.",
    "That is a true tale — the Turtle can taste the dust in it. It joins the Tale-Book. "
    "Witness: give this one their gift.",
]

# Proof-of-quest tokens, one flavor per realm (rotated by card number so quests differ).
PROOFS = {
    "roots": [
        "Bring back the hardest true sentence spoken there — yours or a stranger's.",
        "Bring back the name of what you almost didn't face.",
        "Bring back one word for what you left behind in the dust there.",
        "Bring back the thing you understood there that you didn't before.",
    ],
    "trunk": [
        "Bring back the name of a stranger who stood beside you.",
        "Bring back one thing you only noticed because you stayed still.",
        "Bring back the story of who was there, and why they had come.",
        "Bring back a description of the ground you stood on — exactly as it was.",
    ],
    "branches": [
        "Bring back something given to you freely — a word, a bead, a taste, a promise.",
        "Bring back the wish you said out loud there.",
        "Bring back proof of one small brave thing: what it was, and how it felt.",
        "Bring back the name of the first person you told about it.",
    ],
}

LEAVES = [
    "Write one word on a scrap and leave it there, weighted with a stone.",
    "Leave behind something small you have been carrying — and mean it.",
    "Say the name of a habit out loud there, once, and walk away from it.",
]

VOW = ("When your three moves are made, return to the Terrible Turtle shell. Find a turtle. "
       "Tell the tale aloud, to their face — your proofs are the witnesses. Those who return and tell "
       "receive a gift from the shell — and while the shell still holds them, that gift "
       "is a deck of this very oracle.")
VOW_WHERE = "Camp placement posts in August — until then, ask any turtle where the shell is parked."
CHOSEN = "Meaning is not found. It is chosen. Bite down."

SLOT_TITLES = {"roots": "FACE", "trunk": "STAND", "branches": "REACH"}


def _new_id():
    return uuid.uuid4().hex[:12]


def _gc():
    if len(SESSIONS) <= MAX_SESSIONS:
        return
    for sid, _ in sorted(SESSIONS.items(), key=lambda kv: kv[1]["created"])[:-MAX_SESSIONS]:
        SESSIONS.pop(sid, None)


def _words(s):
    return len((s or "").split())


def _clean_line(s, max_words=40):
    """Sanitize an LLM one-liner: strip quotes/labels, keep it one short line."""
    s = (s or "").strip().strip('"').strip("'").strip()
    s = re.sub(r"^(question|follow-?up|oracle|turtle)\s*[:\-]\s*", "", s, flags=re.I).strip()
    s = s.splitlines()[0].strip() if s else ""
    if not s or _words(s) > max_words:
        return None
    return s


def _extract_name(text):
    # Filler words can stack ("um, hi there, I'm Wren") — strip repeatedly, not once,
    # or a second filler word left behind gets read as the name itself.
    t = (text or "").strip()
    filler = re.compile(r"^(hi|hey|hello|hiya|there|um|uh|er|well|ok|okay|so|yeah)[,!. ]+", re.I)
    while True:
        stripped = filler.sub("", t)
        if stripped == t:
            break
        t = stripped
    t = re.sub(r"^(i am|i'm|im|they call me|people call me|my name is|my name's|call me|it's|its|"
               r"the name is|name's|this is)\s+", "", t, flags=re.I)
    t = re.split(r"[,.!?;\n]| and | but ", t)[0].strip()
    name = " ".join(w.capitalize() for w in t.split()[:3])
    return (name or "Traveler")[:28]


def _time_context():
    now = datetime.datetime.now()
    h = now.hour + now.minute / 60
    if 5 <= h < 8:
        pod = "dawn — sunrise is near or happening; if the quest can, it ends facing the sun"
    elif 8 <= h < 12:
        pod = "morning — the city wakes slowly; heat is coming"
    elif 12 <= h < 17:
        pod = ("the hot afternoon — route through shade, ice at Arctica, misters; "
               "save the far playa for after dark")
    elif 17 <= h < 20:
        pod = "golden hour into sunset — the playa softens, art is close and kind"
    elif 20 <= h < 24:
        pod = "night — the city is fully lit; deep playa and sound camps are alive"
    else:
        pod = "deep night — the quiet hours; the strongest ending is sunrise, near 6:20am"
    return f"It is {now.strftime('%A, %I:%M %p').replace(' 0', ' ')} in Black Rock City: {pod}."


def _company(shares):
    t = " ".join(shares).lower()
    if re.search(r"\b(my partner|my wife|my husband|my boyfriend|my girlfriend|my friend|"
                 r"my friends|my crew|my campmates|both of us|the two of us|we came|we keep|"
                 r"we are|we're)\b", t):
        return ("The seeker is clearly here WITH someone (they speak as 'we'). Write the quest "
                "for them together — shared moves, and one done apart, reunited with something to tell.")
    return ""


def _context(sess):
    parts = [_time_context()]
    w = WEATHERS.get(sess.get("weather"))
    if w:
        parts.append(f'The seeker named their inner weather "{w["name"]}". '
                     f'REGISTER: {w["register"]} QUEST TILT: {w["quest_tilt"]}')
    if sess.get("stones"):
        names = [s["name"] for s in STONES if s["id"] in sess["stones"]]
        parts.append("When words were hard, they touched what they carry, instead of speaking: "
                     + ", ".join(names) + ". These are weight felt, not words said — never write "
                     "'you said' or quote a stone's name back to them as if they spoke it.")
    if sess.get("ground", 0) >= 0.5:
        parts.append("IMPORTANT — the seeker is far from shore tonight (altered, exhausted, or "
                     "unmoored). Keep the reading SHORT (60-90 words), warm, concrete. The quest "
                     "stays small-radius, physical, gentle. Grounding is the gift; no mysteries.")
    c = _company(sess["shares"])
    if c:
        parts.append(c)
    if sess.get("axis_slot"):
        parts.append("THE AXIS HAS SPOKEN: one of the three is not a Tree card but a Shell "
                     "card — the World Turtle's own axis, which surfaces for roughly one "
                     "seeker in ten. Name that this is rare, once, without ceremony or "
                     "flattery, and let it carry more weight in the reading than the "
                     "other two.")
    if sess.get("prior_line"):
        parts.append(sess["prior_line"])
    return " ".join(parts)


def _ground_signals(sess, text, meta):
    """Passive groundedness inference: weather + latency + speech shape. 0..1-ish."""
    g = WEATHERS.get(sess.get("weather"), {}).get("grounding", 0.0)
    meta = meta or {}
    try:
        if float(meta.get("ms", 0)) > 25000:
            g += 0.2
        secs = float(meta.get("audio_secs", 0))
        words = len((text or "").split())
        if secs > 1 and meta.get("input") == "voice":
            rate = words / secs
            if rate < 1.2 or rate > 4.5:
                g += 0.3
    except (TypeError, ValueError):
        pass
    sess["ground"] = max(sess.get("ground", 0.0), g)


def start(mode="seek"):
    """Open a séance (mode 'seek') or a tale-telling (mode 'tale')."""
    _gc()
    sid = _new_id()
    tale = (mode == "tale")
    SESSIONS[sid] = {
        "id": sid, "stage": "tale_naming" if tale else "naming",
        "name": None, "prior_line": None,
        "shares": [], "weather": None, "stones": [], "ground": 0.0, "stem_tried": False,
        "picks": None, "located": None, "reading": None, "adventure": None,
        "axis_slot": None,
        "quest": None, "echoes": None, "created": time.time(),
    }
    say = random.choice(TALE_NAME_ASKS if tale else NAME_ASKS)
    return {"session": sid, "stage": SESSIONS[sid]["stage"], "say": say, "expects": "name"}


def _followup_llm(shares, llm):
    prompt = (
        "A seeker at your shell just shared this about their burn:\n"
        + "\n".join(f"- {s}" for s in shares)
        + "\n\nAsk ONE short, warm follow-up question (under 25 words) in the Turtle's voice — wry, "
        "specific to their words, inviting one level deeper. It must be a question. "
        "Return the question only, no quotes, no preamble."
    )
    return _clean_line(llm.generate(prompt, system=SYSTEM, timeout=T_SHORT), max_words=32)


def _seeker_words(sess):
    """Return only words the seeker actually supplied, without UI stems or stone labels."""
    stem = WEATHERS.get(sess.get("weather"), {}).get("stem", "").strip()
    spoken = []
    for share in sess.get("shares", []):
        text = (share or "").strip()
        if text.startswith("I am carrying:"):
            continue
        if stem and text.startswith(stem):
            text = text[len(stem):].strip()
        if text:
            spoken.append(text)
    return spoken


def _quote_tokens(text):
    return re.findall(r"[\w’'-]+", text or "", flags=re.UNICODE)


def _quote_windows(spoken):
    """Make up to three distinct, natural 3-8-word quote candidates per answer."""
    windows = []
    for answer in spoken:
        words = _quote_tokens(answer)
        if len(words) < 3:
            continue
        width = min(7, len(words))
        starts = (0, max(0, (len(words) - width) // 2), max(0, len(words) - width))
        for start in starts:
            phrase = " ".join(words[start:start + width])
            if phrase not in windows:
                windows.append(phrase)
    return windows


def _valid_echo(line, spoken):
    quotes = re.findall(r"“([^”]+)”", line or "")
    if len(quotes) != 1 or not 3 <= len(_quote_tokens(quotes[0])) <= 8:
        return False
    phrase = " ".join(w.casefold() for w in _quote_tokens(quotes[0]))
    return any(phrase in " ".join(w.casefold() for w in _quote_tokens(source))
               for source in spoken)


def _echoes_llm(sess, llm):
    picks = sess["picks"]
    cl = card_lore()
    spoken = _seeker_words(sess)
    if not _quote_windows(spoken):
        return None
    lines = "\n".join(
        f'{r}: {picks[r]["name"]} — essence: {cl.get(picks[r]["id"], {}).get("essence", "")}; '
        f'bridge: {cl.get(picks[r]["id"], {}).get("bridge", "")}'
        for r in ("roots", "trunk", "branches"))
    prompt = (
        "SEEKER'S ACTUAL WORDS (the only source you may quote from):\n"
        + "\n".join(f"- {s}" for s in spoken)
        + f"\n\nCARD NOTES (for meaning only — NEVER quote these):\n{lines}\n\n"
        "For each card, write ONE line (under 22 words) the Turtle speaks as that card turns over. "
        "Each line quotes exactly ONE phrase of 3-8 words copied verbatim from SEEKER'S WORDS inside "
        "curly quotation marks — never words from CARD NOTES — then ties that phrase to the card in plain "
        "speech. No card mechanics, no fortune-telling.\n"
        "Example shape: You said “yes to everyone” — and the tide kept none of it for you.\n"
        'Return JSON only: {"roots": "...", "trunk": "...", "branches": "..."}'
    )
    resp = llm.generate(prompt, system=SYSTEM, as_json=True, timeout=T_SHORT)
    if not resp:
        return None
    try:
        out = json.loads(resp)
    except Exception:
        return None
    if isinstance(out, dict) and all(out.get(r) for r in ("roots", "trunk", "branches")):
        # structural guarantee: every echo must carry a quoted seeker phrase, else that
        # card's echo falls back to the deterministic quote-builder
        fb = _echoes_fallback(sess)
        result = {}
        for r in ("roots", "trunk", "branches"):
            line = _clean_line(out[r], 22)
            result[r] = line if (line and _valid_echo(line, spoken)) else fb[r]
        return result
    return None


def _echoes_fallback(sess):
    spoken = _seeker_words(sess)
    windows = _quote_windows(spoken)
    out = {}
    used = set()
    for realm in ("roots", "trunk", "branches"):
        c = sess["picks"][realm]
        kw = _tokens(" ".join(c.get("keywords", [])) + " " + c.get("reading", ""))
        ranked = sorted(enumerate(windows), key=lambda x: (-len(_tokens(x[1]) & kw), x[0]))
        frag = next((phrase for _, phrase in ranked if phrase not in used), "")
        if frag:
            used.add(frag)
        essence = card_lore().get(c["id"], {}).get("essence") or c.get("reading", "")
        essence_words = essence.split()
        bite = " ".join(essence_words[:10]).rstrip(" ,;:—-")
        if len(essence_words) > 10:
            bite += "…"
        elif bite and bite[-1] not in ".!?":
            bite += "."
        out[realm] = (f"You said “{frag}” — {bite}"
                      if frag else f"{c['name']} rose. {bite}")
    return out


def _draw(sess, llm):
    """THE PLAYA PULLS: pure chance, one card per realm. The AI's craft is the binding,
    not the choosing — meaning is made, not matched."""
    _, _, by_realm = load_deck()
    told = " ".join(_seeker_words(sess)) or "The seeker could not put it into words."
    picks, axis_slot = draw_spread(by_realm)
    sel_mode = "playa"
    located = locate_spread(picks)
    sess.update(picks=picks, located=located, axis_slot=axis_slot)
    out, weave_mode = weave(told, picks, llm, located, context=_context(sess))
    echoes = (_echoes_llm(sess, llm) if llm and llm.available() else None) or _echoes_fallback(sess)
    sess.update(reading=out["reading"], adventure=out["adventure"],
                echoes=echoes, stage="proposed")
    say = random.choice(DRAWN_LINES)
    if axis_slot:
        say = AXIS_LINE.format(card=picks[axis_slot]["name"])
    return {
        "session": sess["id"], "stage": "proposed",
        "say": say,
        "cards": {r: card_payload(picks[r], located[r]) for r in ("roots", "trunk", "branches")},
        "echoes": echoes,
        # which slot, if any, the Turtle's own axis spoke into — the kiosk marks it
        "axis_slot": axis_slot,
        "reading": out["reading"], "adventure": out["adventure"],
        "map": COMPASS_ROSE, "directions": directions_lines(picks, located),
        "ask": DECISION_ASK, "expects": "decision",
        "modes": {"select": sel_mode, "weave": weave_mode},
    }


def _refine_llm(sess, llm):
    picks, located = sess["picks"], sess["located"]
    spoken = _seeker_words(sess)
    earlier, newest = spoken[:-1], (spoken[-1] if spoken else "")
    lines = []
    for realm in ("roots", "trunk", "branches"):
        c, loc = picks[realm], located.get(realm, {})
        lines.append(f'{SLOT_TITLES[realm]} — {c["name"]}: dare="{c["turtle_dare"]}" '
                     f'real_2026="{c["real_2026"]["name"]}" where="{loc.get("directions", "")}"')
    prompt = (
        "The seeker has heard their reading and wants the quest tuned before accepting.\n"
        f"What they shared earlier:\n" + "\n".join(f"- {s}" for s in earlier)
        + f'\n\nWhat they JUST added — the new truth the rewritten quest MUST visibly use:\n"{newest}"\n'
        + f"\nCONTEXT: {_context(sess)}\n"
        + f"\nThe drawn cards (KEEP these, do not swap):\n" + "\n".join(lines)
        + f"\n\nThe current quest:\n{sess['adventure']}\n\n"
        "Rewrite the quest around that new truth — same three cards, same three real places, but the "
        "tasks should now put what they just confessed at the center (if they said they secretly sing, "
        "the quest makes them sing). At least ONE of the three moves must be REPLACED, not reworded — "
        "put the new truth in its own words, don't just gesture at it. If the new truth is something "
        "they are keeping secret, the REACH move should be telling one person. Keep the arc: FACE alone "
        "with a hard truth, STAND as presence at a place, REACH involving another human; keep one "
        "leave-something-behind. Concrete, doable, with directions, and as detailed as the quest you are "
        "replacing — this is a rewrite, not a summary. Keep it fit for speech: 75-110 words, one short "
        "opening, then exactly three compact moves introduced as First, Second, Third. No headings or "
        "bullets. Also write one short acknowledgement line (under 20 words) the Turtle says first, "
        "naming the new truth.\n"
        'Return JSON only: {"say": "...", "adventure": "..."}'
    )
    resp = llm.generate(prompt, system=SYSTEM, as_json=True, timeout=T_LONG)
    if not resp:
        return None
    try:
        out = json.loads(resp)
    except Exception:
        return None
    if isinstance(out, dict) and out.get("adventure"):
        adventure = out["adventure"].strip()
        # Reject a summary or a ramble: this whole passage is spoken while the seeker waits.
        if not 60 <= _words(adventure) <= 140:
            return None
        return {"say": _clean_line(out.get("say"), 30) or random.choice(REFINE_ACKS),
                "adventure": adventure}
    return None


def _refine_fallback(sess):
    """No LLM: re-score the realms against the fuller share; the Tree may reconsider a card."""
    _, _, by_realm = load_deck()
    told = " ".join(_seeker_words(sess)) or "The seeker could not put it into words."
    picks = select_fallback(told, by_realm)
    # select_fallback only knows the three Tree realms, so a re-score would quietly swap
    # out an axis card the seeker has already been shown. The Turtle does not take that
    # back — once the spine has spoken, it stays on the table.
    axis_slot = sess.get("axis_slot")
    if axis_slot and sess.get("picks"):
        picks[axis_slot] = sess["picks"][axis_slot]
    located = locate_spread(picks)
    out = weave(told, picks, None, located, context=_context(sess))[0]
    sess.update(picks=picks, located=located, reading=out["reading"])
    sess["echoes"] = _echoes_fallback(sess)
    return {"say": random.choice(REFINE_ACKS), "adventure": out["adventure"],
            "reading": out["reading"]}


def _name_step(sess, text, tale):
    """The seeker gives their name; the Turtle checks its ledger."""
    sess["name"] = _extract_name(text)
    name = sess["name"]
    prior_q, prior_t = lore.last_quest(name), lore.last_tale(name)
    if tale:
        recall = (f"The ledger shows your quest: “{prior_q['title']}.” " if prior_q else "")
        sess["stage"] = "tale_listening"
        return {"session": sess["id"], "stage": "tale_listening",
                "say": f"{name}. {recall}{random.choice(TALE_INVITES)}",
                "expects": "tale"}
    sess["stage"] = "weather"
    tiles = [{"id": w["id"], "name": w["name"], "tile": f"/tiles/{w['id']}.jpg"}
             for w in WEATHER["weathers"]]
    if prior_q:
        sess["prior_line"] = (
            f"This seeker has quested with the Turtle before. Their last quest: “{prior_q['title']}”."
            + (f' The tale they told of it: "{prior_t["tale"][:300]}"' if prior_t else "")
            + " Build tonight on top of that — acknowledge it once, never repeat it.")
        say = (f"{name}. The Turtle remembers you — you carried “{prior_q['title']}.” "
               + ("Your tale is in the book. " if prior_t else "The book still waits for that tale. ")
               + WEATHER_ASK)
    else:
        say = f"{name}. Good — a name the dust can hold. {WEATHER_ASK}"
    return {"session": sess["id"], "stage": "weather", "say": say,
            "weathers": tiles, "expects": "weather"}


def _tale_step(sess, text, llm):
    """The tale, told aloud to a human turtle, recorded by the shell."""
    prior_q = lore.last_quest(sess["name"])
    lore.append({"type": "tale", "name": sess["name"], "tale": text,
                 "quest_title": (prior_q or {}).get("title", "")})
    sess["stage"] = "tale_told"
    say = None
    if llm and llm.available():
        say = _clean_line(llm.generate(
            f'A seeker named {sess["name"]} returned to the shell and told this tale of their quest'
            + (f' “{prior_q["title"]}”' if prior_q else "") + f':\n"{text}"\n\n'
            "In the Turtle's voice, honor the tale in TWO sentences (under 40 words): first name one "
            "specific detail from the tale itself, then address the human turtle who witnessed it, "
            "telling THEM to hand this seeker their gift. Return the lines only.",
            system=SYSTEM, timeout=T_SHORT), max_words=50)
    return {"session": sess["id"], "stage": "tale_told",
            "say": say or random.choice(TALE_THANKS),
            "gift": True, "expects": "done"}


def hear(sid, body, llm=None):
    """The seeker speaks or taps. Routes on the session's stage; returns the next event."""
    sess = SESSIONS.get(sid)
    if not sess:
        return {"error": "no such séance — touch the shell to begin again", "stage": "gone"}
    body = body if isinstance(body, dict) else {"text": body}
    text = (body.get("text") or "").strip()
    meta = body.get("meta") or {}
    if sess["stage"] == "weather":
        w = WEATHERS.get((body.get("weather") or "").strip())
        if not w:
            return {"session": sid, "stage": "weather",
                    "say": "Touch one of the six skies, traveler.",
                    "weathers": [{"id": x["id"], "name": x["name"], "tile": f"/tiles/{x['id']}.jpg"}
                                 for x in WEATHER["weathers"]],
                    "expects": "weather"}
        sess["weather"] = w["id"]
        sess["ground"] = max(sess["ground"], w.get("grounding", 0.0))
        sess["stage"] = "stem"
        return {"session": sid, "stage": "stem",
                "say": f'{w["name"]}. {random.choice(STEM_ASKS)}',
                "stem": w["stem"], "expects": "stem"}
    if sess["stage"] == "stones":
        valid = {x["id"] for x in STONES}
        sess["stones"] = [s for s in (body.get("stones") or []) if s in valid]
        names = [x["name"] for x in STONES if x["id"] in sess["stones"]]
        sess["shares"].append("I am carrying: "
                              + (", ".join(names) if names else "nothing I can name") + ".")
        return _draw(sess, llm)
    if not text:
        return {"session": sid, "stage": sess["stage"],
                "say": "The Turtle heard only wind. Try again, slower.",
                "expects": "share"}
    if sess["stage"] == "naming":
        return _name_step(sess, text, tale=False)
    if sess["stage"] == "tale_naming":
        return _name_step(sess, text, tale=True)
    if sess["stage"] == "tale_listening":
        return _tale_step(sess, text, llm)
    if sess["stage"] == "tale_told":
        return {"session": sid, "stage": "tale_told", "gift": True,
                "say": "The tale is kept. Go get your gift, and let the next traveler in.",
                "expects": "done"}
    if sess["stage"] == "stem":
        _ground_signals(sess, text, meta)
        stem = WEATHERS.get(sess.get("weather"), {}).get("stem", "")
        sess["shares"].append(f"{stem} {text}" if stem else text)
        # thin answer → the stones rescue: recognition when words won't come
        if len(text.split()) < 4 and not sess["stem_tried"]:
            sess["stem_tried"] = True
            sess["stage"] = "stones"
            return {"session": sid, "stage": "stones", "say": STONES_ASK,
                    "stones": STONES, "expects": "stones"}
        return _draw(sess, llm)
    if sess["stage"] == "proposed":
        sess["shares"].append(text)
        ref = (_refine_llm(sess, llm) if llm and llm.available() else None)
        event = {"session": sid, "stage": "proposed", "map": COMPASS_ROSE,
                 "ask": DECISION_ASK, "expects": "decision"}
        if ref:
            sess["adventure"] = ref["adventure"]
            event.update(say=ref["say"], adventure=ref["adventure"], reading=sess["reading"],
                         modes={"refine": "llm"})
        else:
            fb = _refine_fallback(sess)
            sess["adventure"] = fb["adventure"]
            event.update(say=fb["say"], adventure=fb["adventure"], reading=fb["reading"],
                         modes={"refine": "fallback"})
        picks, located = sess["picks"], sess["located"]
        event["cards"] = {r: card_payload(picks[r], located[r]) for r in ("roots", "trunk", "branches")}
        event["echoes"] = sess["echoes"]
        event["directions"] = directions_lines(picks, located)
        return event
    if sess["stage"] == "accepted":
        return {"session": sid, "stage": "accepted", "quest": sess["quest"],
                "say": "The quest is already sealed, traveler. Go live it — the shell will wait.",
                "expects": "done"}
    return {"error": "the Turtle is confused", "stage": sess["stage"]}


def _seal_llm(sess, llm):
    """Personalize the three sealed moves (task/where/proof + one leave) from the final quest."""
    picks, located = sess["picks"], sess["located"]
    lines = []
    for realm in ("roots", "trunk", "branches"):
        c, loc = picks[realm], located.get(realm, {})
        lines.append(f'{SLOT_TITLES[realm]}: card="{c["name"]}" at="{c["real_2026"]["name"]}" '
                     f'where="{loc.get("directions", "")}"')
    prompt = (
        "Seal this quest into exactly three moves, in order FACE, STAND, REACH.\n"
        f"The seeker's words:\n" + "\n".join(f"- {s}" for s in sess["shares"])
        + f"\n\nThe accepted quest:\n{sess['adventure']}\n\nThe cards:\n" + "\n".join(lines)
        + "\n\nFor each move give: task (1-2 concrete sentences drawn from the quest; EVERY task must "
        "pair a physical action with an open interior door — 'stay until…', 'leave when you have…', "
        "'ask until someone…' — specific on the outside, open on the inside, since that openness is "
        "where the seeker finds themselves), where (short, from the card's where), proof (ONE specific "
        "thing to bring back to the shell, personal to their words). EXACTLY ONE move also gets leave: "
        "one small thing left behind there. FACE is done alone with a hard truth; STAND is presence at "
        "a place; REACH involves another human. Nothing risky, nothing without consent.\n"
        'Return JSON only: {"moves": [{"task":"","where":"","proof":"","leave":""}, {...}, {...}]}'
    )
    resp = llm.generate(prompt, system=SYSTEM, as_json=True, timeout=T_LONG)
    if not resp:
        return None
    try:
        moves = json.loads(resp).get("moves")
    except Exception:
        return None
    if not (isinstance(moves, list) and len(moves) == 3
            and all(isinstance(m, dict) and m.get("task") for m in moves)):
        return None
    return moves


def accept(sid, llm=None):
    """Seal the quest: three moves with places + proofs (+ one sacrifice), the vow, the map."""
    sess = SESSIONS.get(sid)
    if not sess:
        return {"error": "no such séance — touch the shell to begin again", "stage": "gone"}
    if sess["stage"] != "proposed" and not sess["quest"]:
        return {"error": "no quest to accept yet", "stage": sess["stage"]}
    seal_mode = None
    if not sess["quest"]:
        picks, located = sess["picks"], sess["located"]
        r, t, b = picks["roots"], picks["trunk"], picks["branches"]
        sealed = (_seal_llm(sess, llm) if llm and llm.available() else None)
        seal_mode = "llm" if sealed else "fallback"
        moves = []
        leave_at = random.randrange(3)
        realms = ("roots", "trunk", "branches")
        for i, realm in enumerate(realms):
            c, loc = picks[realm], located.get(realm, {})
            where = loc.get("directions", "") or "Somewhere out there — ask Playa Info."
            if sealed:
                m = sealed[i]
                # Real BRC geo wins over whatever the model invented; its guess only
                # rides along as a suffix, and only when it actually adds something.
                m_where = (m.get("where") or "").strip()
                merged_where = (f"{where} — {m_where}"
                                 if m_where and m_where.lower() != where.lower() else where)
                moves.append({
                    "slot": SLOT_TITLES[realm], "card": c["name"],
                    "task": m["task"].strip(),
                    "where": merged_where,
                    "at": c["real_2026"]["name"],
                    "proof": (m.get("proof") or PROOFS[realm][(c.get("number", 1) - 1) % 4]).strip(),
                    "leave": (m.get("leave") or "").strip(),
                })
            else:
                moves.append({
                    "slot": SLOT_TITLES[realm], "card": c["name"],
                    "task": c["turtle_dare"],
                    "where": where, "at": c["real_2026"]["name"],
                    "proof": PROOFS[realm][(c.get("number", 1) - 1) % 4],
                    "leave": LEAVES[c.get("number", 1) % len(LEAVES)] if i == leave_at else "",
                })
        # THE SACRIFICE demands exactly one move leave something behind — the model isn't
        # trustworthy on the count, so enforce it here rather than in the prompt alone.
        leave_idx = next((i for i, mv in enumerate(moves) if mv.get("leave")), None)
        if leave_idx is None:
            leave_idx = leave_at
            c = picks[realms[leave_idx]]
            moves[leave_idx]["leave"] = LEAVES[c.get("number", 1) % len(LEAVES)]
        else:
            for i, mv in enumerate(moves):
                if i != leave_idx:
                    mv["leave"] = ""
        sess["quest"] = {
            "title": f'The Quest of {b["name"]}',
            "for": sess.get("name") or "Traveler",
            "charge": (f'Face “{r["name"]}.” Stand in “{t["name"]}.” Reach for “{b["name"]}.” '
                       "Three moves, made slow — then home to the shell."),
            "adventure": sess["adventure"],
            "moves": moves,
            "vow": VOW, "vow_where": VOW_WHERE, "chosen": CHOSEN,
            "map": COMPASS_ROSE,
        }
        sess["stage"] = "accepted"
        lore.append({"type": "quest", "name": sess["quest"]["for"],
                     "title": sess["quest"]["title"], "shares": sess["shares"],
                     "cards": [picks[r]["id"] for r in ("roots", "trunk", "branches")],
                     "quest": sess["quest"]})
    event = {"session": sid, "stage": "accepted", "say": random.choice(ACCEPT_LINES),
             "quest": sess["quest"], "expects": "done"}
    if seal_mode:
        event["modes"] = {"seal": seal_mode}
    return event


def snapshot(sid):
    """The raw picks/located/payload for the printer."""
    sess = SESSIONS.get(sid)
    if not sess or not sess["picks"]:
        return None
    return sess
