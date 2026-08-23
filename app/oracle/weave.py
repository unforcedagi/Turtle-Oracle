"""Weave the three cards into one reading + one adventure. LLM -> template fallback."""
import json
import os
import re

from .deck import REPO

# Same knob as session.py's T_LONG — the weave is the single most expensive call in the
# séance and was hardcoded to 120s, so tuning the others left this one untouched.
T_WEAVE = float(os.environ.get("ORACLE_T_LONG", "60"))

_LORE = None


def card_lore():
    """Per-card lore bundles (data/card_lore.json) — big-model judgment baked into data
    so the small runtime model inherits it. Tolerant of the file being absent."""
    global _LORE
    if _LORE is None:
        try:
            with open(os.path.join(REPO, "data", "card_lore.json"), encoding="utf-8") as f:
                _LORE = json.load(f)
        except Exception:
            _LORE = {}
    return _LORE

SYSTEM = (
    "You are the Terrible Turtle Oracle — the ancient World Turtle of Terrible Turtle camp at "
    "Burning Man 2026 (theme: Axis Mundi; the World Tree grows from your shell). "
    "Creed: 'Move Slow & Bite Things.'\n"
    "VOICE RULES — never break them:\n"
    "- Speak TO the seeker: 'you', present tense. Never talk about them in third person.\n"
    "- Short declarative sentences. Let the important lines land short and hard.\n"
    "- This is spoken aloud. Write for the ear: sentences under 18 words; clean pauses; no semicolons, "
    "parentheses, headings, bullets, or throat-clearing.\n"
    "- Warm, dry wit with a little bite. Never cruel. Never saccharine. No mystical fluff.\n"
    "- Concrete over abstract: name real things — dust, shade, ice, the trash fence, sunrise, bikes.\n"
    "- Metaphors come ONLY from: shells, slowness, teeth and biting, roots/trunk/branches, dust, "
    "weight, tides, the moon.\n"
    "- BANNED words and moves: journey, vibrant, tapestry, magical, cosmic, manifest, energy, vibes, "
    "unlock, delve, 'the universe', 'hush now', calling the seeker 'child' or 'little one'.\n"
    "- Never explain card mechanics or name the realms; speak what the cards mean for THIS seeker.\n"
    "- End strong. No trailing pleasantries, no 'may you…' blessings.\n"
    "EXAMPLE of the register (copy the cadence, never the phrases): "
    "'You built all year for other people. That is a fine way to disappear. Tonight nobody needs you — "
    "which is the door. Walk out past the Man to where the map runs out, and stay until you want one "
    "thing. Then bite it.'\n"
    "THE TERRIBLE TRUTH you stand on: you are called Terrible because you carry the oldest problem — "
    "we cannot have always been, and we cannot have come from nothing. Turtles all the way down, and "
    "nobody sees the bottom. There is a limit to what can be known in one life. So you NEVER pretend "
    "to know the future or the seeker's fate. The cards find nothing; they offer. Meaning is not found, "
    "it is chosen — so every reading ends by handing the seeker a choice to bite down on, not a prophecy.\n"
    "SAFETY COVENANT (absolute, silent — never lecture about it): never dare physical risk, substances, "
    "climbing on art, or anything done to another person without their consent; never involve Rangers or "
    "medics except as helpers; in a whiteout, shelter comes first — the quest waits."
)


def _line(label, c, loc):
    where = (loc or {}).get("directions", "")
    lo = card_lore().get(c["id"], {})
    extra = (f' essence="{lo["essence"]}" bridge="{lo["bridge"]}" seed="{lo["seed"]}"'
             if lo else "")
    return (f'{label} — {c["name"]} ({c["realm"]}): '
            f'reading="{c["reading"]}" shadow="{c.get("shadow", "")}" dare="{c["turtle_dare"]}" '
            f'real_2026="{c["real_2026"]["name"]}" where="{where}"{extra}')


def weave_llm(question, cards, llm, located=None, context=""):
    located = located or {}
    body = "\n".join([
        _line("WHAT TO FACE (root)", cards["roots"], located.get("roots")),
        _line("WHERE YOU STAND (trunk)", cards["trunk"], located.get("trunk")),
        _line("WHAT TO REACH FOR (branch)", cards["branches"], located.get("branches")),
    ])
    prompt = (
        f'A seeker shared: "{question}"\n\n'
        + (f"CONTEXT: {context}\n\n" if context else "")
        + f"Three cards rose along the World Tree:\n{body}\n\n"
        "THE BINDING — read this first: these cards were drawn BLIND, by the playa's own chance, "
        "not chosen to match. That is the craft: bind them to this seeker so tightly they look "
        "inevitable. For each card, take one EXACT word or phrase the seeker said and one image "
        "from the card (use its essence and bridge lines) and tie them into one thought. Never "
        "apologize for a card or call it random.\n\n"
        "Weave the three into ONE reading (90-120 words, or 60-85 when CONTEXT asks for grounding) "
        "spoken directly TO the seeker in the "
        "Turtle's voice, honoring the REGISTER in CONTEXT. Move as one connected thought about THEIR "
        "words: what to face -> how to stand -> what to reach for. Fold one card's shadow in as a plain "
        "warning, in your own words — never write the word 'shadow', never write 'the root/trunk/branch "
        "says', never label which card anything came from. The reading contains NO instructions and NO "
        "place names — it names what is true, not what to do or where to go; all doing belongs to the "
        "quest below. If CONTEXT says the seeker is here with a partner or friends, the reading should "
        "sound like it knows that — not describe someone facing this alone. End the reading by handing "
        "them a choice, not a prophecy.\n\n"
        "Then give ONE concrete quest at Burning Man in 75-110 words (use the cards' seed lines as raw "
        "material). It will also be spoken aloud: one short opening, then exactly three compact moves "
        "introduced as First, Second, Third. No headings or bullets. Build it on these rules:\n"
        "- THE CROSSING: find the thing the seeker confessed they avoid, don't do, or secretly want — "
        "the heart of the quest makes them do exactly that. Not visit it. Do it.\n"
        "- THE ARC: the FACE move is done alone and involves a hard truth; the STAND move is a presence "
        "practice at a real place; the REACH move must involve another human — tell, ask, give, or witness.\n"
        "- THE SACRIFICE: exactly one move has them leave something behind (a written word, an object, "
        "a habit named out loud) — left, not kept.\n"
        "- THE SHAPE: every move pairs a concrete anchor (a named place + a physical action) with an "
        "open interior door ('stay until…', 'leave when you have…', 'ask until someone…') — specific "
        "on the outside, open on the inside. That openness is where the seeker finds themselves.\n"
        "- Fit the quest to the hour given in CONTEXT (heat, dark, sunrise). If CONTEXT says they are here "
        "with a partner or friends, this is NOT optional: write the quest for them together, and it MUST "
        "include one move done apart and one moment reunited. If it is their first burn, keep it simple "
        "and kind.\n"
        "- Name the real 2026 places/art/camps and simple directions from the 'where' hints (Black Rock "
        "City is a clock + street grid, the Man at center, deep playa past it).\n\n"
        'Return JSON only: {"reading": "...", "adventure": "..."}'
    )
    resp = llm.generate(prompt, system=SYSTEM, as_json=True, timeout=T_WEAVE)
    if not resp:
        return None
    try:
        out = json.loads(resp)
    except Exception:
        return None
    if isinstance(out, dict) and out.get("reading") and out.get("adventure"):
        return {"reading": out["reading"].strip(), "adventure": out["adventure"].strip()}
    return None


def _opener():
    """Time-aware first words for the offline quest."""
    import datetime
    h = datetime.datetime.now().hour
    if 5 <= h < 12:
        return "Today, before the heat wins, an adventure in three moves."
    if 12 <= h < 17:
        return "This afternoon — move through shade and ice, save the far playa for dark. Three moves."
    if 17 <= h < 21:
        return "As the light goes gold, an adventure in three moves."
    if 21 <= h < 24 or h < 2:
        return "Tonight, an adventure in three moves."
    return "In the deep night, three moves — and if your legs hold, end it facing the sunrise."


def _first_sentence(text):
    text = (text or "").strip()
    parts = [p.strip() for p in re.split(r"(?<=[.!?])\s+", text) if p.strip()]
    return parts[0] if parts else text


def _short_words(text, limit):
    words = (text or "").split()
    if len(words) <= limit:
        return " ".join(words)
    return " ".join(words[:limit]).rstrip(" ,;:—-") + "…"


def weave_fallback(question, cards, located=None):
    located = located or {}
    r, t, b = cards["roots"], cards["trunk"], cards["branches"]
    warning = _first_sentence(t.get("shadow") or r.get("shadow") or b.get("shadow"))
    question_short = _short_words(question, 12).rstrip(".!?")
    question_quote = f"“{question_short}”" if question_short.endswith("…") else f"“{question_short}.”"
    reading = (
        f"You brought the Turtle this: {question_quote} Hear the answer as one. "
        f"{_first_sentence(r['reading'])} {_first_sentence(t['reading'])} "
        f"{_first_sentence(b['reading'])} Mind the teeth: {warning} "
        "Nothing here predicts you. Choose what you will face, what you will stand in, and what you "
        "will reach for. Then bite."
    )

    def move(ordinal, c, realm):
        where = located.get(realm, {}).get("directions", "").replace("&", "and")
        tail = f" The map says {where}." if where else ""
        return f"{ordinal}. {c['turtle_dare'].strip()}{tail}"

    adventure = (
        _opener() + " "
        + move("First", r, "roots") + " "
        + move("Second", t, "trunk") + " "
        + move("Third", b, "branches")
    )
    return {"reading": reading, "adventure": adventure}


def _lower_first(s):
    s = s.strip()
    return s[0].lower() + s[1:] if s else s


def weave(question, cards, llm=None, located=None, context=""):
    """Returns ({reading, adventure}, mode: 'llm'|'fallback')."""
    if llm and llm.available():
        out = weave_llm(question, cards, llm, located, context)
        if out:
            return out, "llm"
    return weave_fallback(question, cards, located), "fallback"
