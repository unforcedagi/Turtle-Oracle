#!/usr/bin/env python3
"""Focused regression checks for spoken reading and echo quality."""
import json

from oracle.deck import load_deck
from oracle.session import (_echoes_fallback, _echoes_llm, _refine_llm,
                            _seeker_words, _valid_echo)
from oracle.weave import SYSTEM, weave_fallback, weave_llm


def check(label, condition):
    print(("  ok   " if condition else "  FAIL ") + label)
    return not condition


class EchoLLM:
    def available(self):
        return True

    def generate(self, *args, **kwargs):
        return json.dumps({
            "roots": "You said “words I invented here” — no.",
            "trunk": "You said “building something honest with my friends” — the trunk can hold that.",
            "branches": "The future is bright.",
        })


class WeaveLLM:
    def __init__(self):
        self.prompt = self.system = ""

    def generate(self, prompt, system=None, **kwargs):
        self.prompt, self.system = prompt, system
        return json.dumps({"reading": "A short reading.", "adventure": "First. Second. Third."})


class RefineLLM:
    def __init__(self, words):
        self.words = words
        self.prompt = ""

    def generate(self, prompt, **kwargs):
        self.prompt = prompt
        return json.dumps({"say": "That changes it.", "adventure": "word " * self.words})


def main():
    _, _, realms = load_deck()
    picks = {realm: realms[realm][0] for realm in ("roots", "trunk", "branches")}
    sess = {
        "weather": "starry_calm",
        "shares": [
            "I want to keep… building something honest with my friends even when I fear they will leave me behind",
            "I am carrying: Grief, Too Many People.",
        ],
        "picks": picks,
    }
    fails = 0
    spoken = _seeker_words(sess)
    fails += check("UI stem removed from quote source", spoken == [
        "building something honest with my friends even when I fear they will leave me behind"])
    fallback = _echoes_fallback(sess)
    quotes = [line.split("“", 1)[1].split("”", 1)[0] for line in fallback.values()]
    fails += check("fallback chooses three distinct seeker phrases", len(set(quotes)) == 3)
    fails += check("every fallback quote is verbatim and 3-8 words",
                   all(_valid_echo(line, spoken) for line in fallback.values()))
    fails += check("fallback echoes stay short enough to speak on a card turn",
                   all(len(line.split()) <= 22 for line in fallback.values()))

    mixed = _echoes_llm(sess, EchoLLM())
    fails += check("valid model echo survives", mixed["trunk"].startswith(
        "You said “building something honest with my friends”"))
    fails += check("invented and unquoted model echoes fall back",
                   mixed["roots"] == fallback["roots"] and mixed["branches"] == fallback["branches"])
    stones_only = dict(sess, shares=["I am carrying: Grief, Too Many People."])
    fails += check("stone taps are never fabricated into spoken quotes",
                   _echoes_llm(stones_only, EchoLLM()) is None
                   and all("“" not in line for line in _echoes_fallback(stones_only).values()))

    located = {realm: {"directions": f"{i + 2}:00 & Esplanade"}
               for i, realm in enumerate(("roots", "trunk", "branches"))}
    out = weave_fallback("I keep caring for everyone else because I am afraid they will leave", picks, located)
    reading_words = len(out["reading"].split())
    quest_words = len(out["adventure"].split())
    fails += check("fallback reading fits a spoken-length budget", 70 <= reading_words <= 125)
    fails += check("fallback quest is compact and orally sequenced",
                   quest_words <= 135 and all(x in out["adventure"] for x in ("First.", "Second.", "Third.")))

    llm = WeaveLLM()
    weave_llm("I am afraid to ask for help", picks, llm, located, "night")
    fails += check("runtime prompt carries spoken word budgets",
                   "90-120 words" in llm.prompt and "75-110 words" in llm.prompt)
    fails += check("system voice bans prose that reads poorly aloud",
                   "spoken aloud" in SYSTEM and "no semicolons" in SYSTEM)

    refine_sess = dict(sess, shares=sess["shares"] + ["I secretly want to sing"],
                       located=located, adventure=out["adventure"])
    short_refine = RefineLLM(20)
    fails += check("refine rejects a stub quest", _refine_llm(refine_sess, short_refine) is None)
    fails += check("refine prompt preserves the spoken three-move shape",
                   "75-110 words" in short_refine.prompt and "First, Second, Third" in short_refine.prompt)
    fails += check("refine prompt excludes UI stems and synthetic stone labels",
                   "I want to keep…" not in short_refine.prompt and "I am carrying:" not in short_refine.prompt)

    print("\nALL PASS" if not fails else f"\n{fails} FAILED")
    return 1 if fails else 0


if __name__ == "__main__":
    raise SystemExit(main())
