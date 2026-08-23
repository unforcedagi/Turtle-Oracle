"""Optional local Kokoro voice for the Turtle.

The core oracle stays dependency-free.  Kokoro and its audio stack are imported only when
ORACLE_TTS_BACKEND=kokoro, and every failure is surfaced as VoiceUnavailable so the kiosk can
fall back to the tablet's browser voice.
"""
from collections import OrderedDict
from io import BytesIO
import os
import threading
import time


SAMPLE_RATE = 24_000


class VoiceUnavailable(RuntimeError):
    pass


class KokoroVoice:
    def __init__(self, backend=None, voice=None, speed=None, device=None, cache_size=None):
        self.backend = (backend or os.environ.get("ORACLE_TTS_BACKEND", "browser")).lower()
        self.voice = voice or os.environ.get("ORACLE_TTS_VOICE", "bm_george")
        self.lang = self.voice[0] if self.voice and self.voice[0] in "ab" else "b"
        try:
            self.speed = float(speed if speed is not None else
                               os.environ.get("ORACLE_TTS_SPEED", "0.86"))
        except (TypeError, ValueError):
            self.speed = 0.86
        self.device = device if device is not None else os.environ.get("ORACLE_TTS_DEVICE", "cpu")
        self.model_dir = os.environ.get("ORACLE_TTS_MODEL_DIR", "").strip()
        try:
            self.cache_size = max(0, int(cache_size if cache_size is not None else
                                         os.environ.get("ORACLE_TTS_CACHE_LINES", "192")))
        except (TypeError, ValueError):
            self.cache_size = 192
        self._pipeline = None
        self._voice_source = self.voice
        self._cache = OrderedDict()
        self._lock = threading.RLock()
        self._retry_at = 0.0
        self.last_error = None

    @property
    def enabled(self):
        return self.backend == "kokoro"

    def status(self):
        return {
            "backend": "kokoro" if self.enabled else "browser",
            "ready": bool(self._pipeline) if self.enabled else False,
            "voice": self.voice if self.enabled else None,
            "device": self.device if self.enabled else None,
            "local_model": bool(self.model_dir) if self.enabled else False,
        }

    def _load(self):
        if not self.enabled:
            raise VoiceUnavailable("server voice is disabled")
        if self._pipeline is not None:
            return self._pipeline
        if time.monotonic() < self._retry_at:
            raise VoiceUnavailable("server voice is cooling down after a load failure")
        try:
            from kokoro import KModel, KPipeline
            kwargs = {"lang_code": self.lang, "repo_id": "hexgrad/Kokoro-82M"}
            if self.model_dir:
                config = os.path.join(self.model_dir, "config.json")
                weights = os.path.join(self.model_dir, "kokoro-v1_0.pth")
                self._voice_source = os.path.join(self.model_dir, "voices", self.voice + ".pt")
                for path in (config, weights, self._voice_source):
                    if not os.path.isfile(path):
                        raise FileNotFoundError(path)
                model = KModel(config=config, model=weights)
                if self.device:
                    model = model.to(self.device)
                kwargs["model"] = model.eval()
            elif self.device:
                kwargs["device"] = self.device
            self._pipeline = KPipeline(**kwargs)
            self.last_error = None
            return self._pipeline
        except Exception as exc:
            self.last_error = f"{type(exc).__name__}: {exc}"
            self._retry_at = time.monotonic() + 30
            raise VoiceUnavailable("Kokoro could not load") from exc

    @staticmethod
    def _clean(text):
        return " ".join((text or "").split())

    @staticmethod
    def _encode_wav(chunks):
        import numpy as np
        import soundfile as sf

        audio = np.concatenate(chunks) if len(chunks) > 1 else chunks[0]
        out = BytesIO()
        sf.write(out, audio, SAMPLE_RATE, format="WAV", subtype="PCM_16")
        return out.getvalue()

    def synthesize(self, text):
        text = self._clean(text)
        if not text:
            raise ValueError("no text")
        if len(text) > 600:
            raise ValueError("speech line is too long")
        key = (self.voice, round(self.speed, 3), text)
        with self._lock:
            cached = self._cache.pop(key, None)
            if cached is not None:
                self._cache[key] = cached
                return cached
            pipeline = self._load()
            try:
                chunks = []
                for result in pipeline(text, voice=self._voice_source, speed=self.speed,
                                       split_pattern=r"\n+"):
                    audio = result.audio if hasattr(result, "audio") else result[2]
                    if audio is not None and len(audio):
                        chunks.append(audio)
                if not chunks:
                    raise RuntimeError("Kokoro returned no audio")
                wav = self._encode_wav(chunks)
            except VoiceUnavailable:
                raise
            except Exception as exc:
                self.last_error = f"{type(exc).__name__}: {exc}"
                raise VoiceUnavailable("Kokoro could not synthesize this line") from exc
            if self.cache_size:
                self._cache[key] = wav
                while len(self._cache) > self.cache_size:
                    self._cache.popitem(last=False)
            return wav

    def warm(self):
        if self.enabled:
            self.synthesize("The Turtle wakes.")
