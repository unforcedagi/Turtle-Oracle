#!/usr/bin/env python3
"""Focused offline checks for the optional Kokoro engine and /api/speak contract."""
import json
import os
import sys
import threading
from urllib.error import HTTPError
from urllib.request import Request, urlopen

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))

from oracle import server
from oracle.voice import KokoroVoice, VoiceUnavailable


class FakePipeline:
    def __init__(self):
        self.calls = []

    def __call__(self, text, **kwargs):
        self.calls.append((text, kwargs))
        yield (text, "phonemes", [0.1, 0.2])


def unit_checks():
    engine = KokoroVoice(backend="kokoro", voice="bm_george", speed=.86, cache_size=2)
    engine._pipeline = FakePipeline()
    engine._voice_source = "bm_george"
    engine._encode_wav = lambda chunks: b"RIFF-fake-wav-" + bytes([len(chunks[0])])
    first = engine.synthesize("  Slow   is smooth. ")
    second = engine.synthesize("Slow is smooth.")
    assert first == second
    assert len(engine._pipeline.calls) == 1, "normalized duplicate must hit the line cache"
    assert engine._pipeline.calls[0][1]["voice"] == "bm_george"
    assert engine._pipeline.calls[0][1]["speed"] == .86

    disabled = KokoroVoice(backend="browser")
    try:
        disabled.synthesize("No server voice.")
        raise AssertionError("disabled engine synthesized")
    except VoiceUnavailable:
        pass
    for bad in ("", "x" * 601):
        try:
            engine.synthesize(bad)
            raise AssertionError("invalid speech text accepted")
        except ValueError:
            pass


class FakeVoice:
    voice = "bm_george"

    def status(self):
        return {"backend": "kokoro", "ready": True, "voice": self.voice, "device": "cpu"}

    def synthesize(self, text):
        if text == "fail":
            raise VoiceUnavailable("test outage")
        if len(text) > 600:
            raise ValueError("speech line is too long")
        return b"RIFF-test-audio"


def post(base, text):
    req = Request(base + "/api/speak", method="POST",
                  headers={"Content-Type": "application/json"},
                  data=json.dumps({"text": text}).encode())
    return urlopen(req, timeout=3)


def endpoint_checks():
    original = server.VOICE_SINGLETON
    server.VOICE_SINGLETON = FakeVoice()
    httpd = server.OracleServer(("127.0.0.1", 0), server.Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{httpd.server_port}"
    try:
        with post(base, "The Turtle speaks.") as response:
            assert response.status == 200
            assert response.headers.get_content_type() == "audio/wav"
            assert response.headers["Cache-Control"] == "no-store"
            assert response.read() == b"RIFF-test-audio"
        for text, code in (("", 400), ("x" * 601, 400), ("fail", 503)):
            try:
                post(base, text)
                raise AssertionError(f"{text[:8]!r} should return {code}")
            except HTTPError as exc:
                assert exc.code == code
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=2)
        server.VOICE_SINGLETON = original


def main():
    unit_checks()
    endpoint_checks()
    print("kokoro voice: cache, validation, audio contract, and graceful failure passed")


if __name__ == "__main__":
    main()
