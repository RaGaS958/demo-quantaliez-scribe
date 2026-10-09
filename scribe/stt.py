"""
Speech-to-text through any OpenAI-compatible /audio/transcriptions endpoint.

Demo: Groq-hosted Whisper large-v3 (open-weight, multilingual incl. Hindi).
Production: the same call against an open-source ASR served inside NHA cloud
(e.g. vLLM serving Whisper, or an Indic ASR model behind the same API).
"""

from __future__ import annotations

import re
import time
from functools import lru_cache

from openai import OpenAI

from config import settings

# Whisper is known to "hear" these on silence or noise; drop them.
_HALLUCINATIONS = {
    "thank you.", "thank you", "thanks for watching!", "thanks for watching.",
    "you", "bye.", "धन्यवाद", "धन्यवाद।", "subtitles by the amara.org community",
    "please subscribe", "like and subscribe",
}

ALLOWED_EXT = {".wav", ".mp3", ".flac", ".m4a", ".ogg", ".webm", ".mp4", ".mpeg", ".mpga", ".opus"}


class STTError(RuntimeError):
    pass


@lru_cache(maxsize=1)
def _client() -> OpenAI:
    # max_retries: free tiers rate-limit per minute; the SDK backs off and honours Retry-After
    return OpenAI(api_key=settings.stt_api_key or "not-set", base_url=settings.stt_base_url, timeout=120, max_retries=4)


def _clean(text: str) -> str:
    return re.sub(r"\s+", " ", text or "").strip()


def transcribe(audio: bytes, filename: str, *, segment_mode: bool) -> dict:
    """
    Transcribe audio bytes.

    segment_mode=True  -> a short utterance from the live mic: returns one segment.
    segment_mode=False -> an uploaded file: returns Whisper's timed segments.
    """
    if not settings.stt_enabled:
        raise STTError("Speech-to-text needs GROQ_API_KEY (or STT_API_KEY) in .env. Offline demo mode is active.")
    if len(audio) > settings.stt_max_upload_mb * 1024 * 1024:
        raise STTError(f"Audio is larger than {settings.stt_max_upload_mb} MB.")

    kwargs: dict = {
        "model": settings.stt_model,
        "file": (filename, audio),
        "response_format": "verbose_json",
        "temperature": 0.0,
    }
    if settings.stt_language:
        kwargs["language"] = settings.stt_language
    if settings.stt_prompt:
        kwargs["prompt"] = settings.stt_prompt

    t0 = time.perf_counter()
    try:
        resp = _client().audio.transcriptions.create(**kwargs)
    except Exception as exc:  # network, auth, unsupported format
        raise STTError(f"Speech-to-text failed: {exc}") from exc
    elapsed = time.perf_counter() - t0

    data = resp.model_dump() if hasattr(resp, "model_dump") else dict(resp)
    raw_segments = data.get("segments") or []
    language = data.get("language")

    segments = []
    for s in raw_segments:
        text = _clean(s.get("text", ""))
        if not text or text.lower() in _HALLUCINATIONS:
            continue
        if (s.get("no_speech_prob") or 0) > 0.6 and (s.get("avg_logprob") or 0) < -0.8:
            continue
        segments.append({"text": text, "start": s.get("start"), "end": s.get("end")})

    if segment_mode or not segments:
        text = _clean(" ".join(x["text"] for x in segments)) if segments else _clean(data.get("text", ""))
        if not text or text.lower() in _HALLUCINATIONS:
            segments = []
        else:
            segments = [{"text": text, "start": None, "end": None}]

    return {
        "segments": segments,
        "language": language,
        "duration": data.get("duration"),
        "latency_s": round(elapsed, 2),
        "model": settings.stt_model,
    }
