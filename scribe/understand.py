"""
Clinical understanding: transcript -> speaker-attributed turns + structured ClinicalDraft.

Uses an OpenAI-compatible chat endpoint. Demo: Groq-hosted gpt-oss-120b (open-weight).
Production: the same model (or another open-source LLM) served with vLLM inside NHA cloud.

The LLM only proposes content. It never writes FHIR, and nothing is saved until the
clinician approves.
"""

from __future__ import annotations

import json
import re
import time
from functools import lru_cache

from openai import APIConnectionError, AuthenticationError, BadRequestError, OpenAI
from pydantic import ValidationError

from config import settings
from scribe.models import ClinicalDraft, PatientContext, Segment

SYSTEM_PROMPT = """You are the clinical understanding engine of an ambient AI scribe for Indian OPD consultations.
You receive the raw transcript of one consultation. It may be in Hindi, English, Hinglish (Hindi in Roman script),
Devanagari, or another Indian language, and speaker labels may be missing or wrong.

Return ONLY one JSON object with exactly these keys:
{
  "turns": [{"speaker": "Doctor|Patient|Attendant", "text": "<original words>", "english": "<faithful English translation, or null if already English>"}],
  "languages": "<e.g. 'Hindi + English'>",
  "capture_mode": "Live conversation|Dictation",
  "chief_complaints": [{"text": "<clinical English, e.g. 'Fever', 'Dry cough'>", "duration": "<as spoken, e.g. '3 days'>", "details": "<qualifiers/negatives, or null>"}],
  "history": ["<relevant past history / current long-term medicines>"],
  "allergies": ["<allergy>"],
  "no_known_allergies": <true only if explicitly denied>,
  "vitals": {"temperature_f": null, "pulse_bpm": null, "bp_systolic": null, "bp_diastolic": null, "spo2_pct": null, "resp_rate": null, "weight_kg": null},
  "examination": ["<examination finding stated by the doctor>"],
  "results": [{"text": "<test name>", "value": "<value>", "unit": "<unit>"}],
  "assessment": [{"text": "<working diagnosis>", "status": "provisional|confirmed"}],
  "medications": [{"name": "<generic name>", "strength": "<e.g. '650 mg'>", "form": "tablet|syrup|capsule|...", "route": "oral|...",
                   "frequency_code": "OD|BD|TDS|QID|HS|SOS", "times_per_day": <int|null>, "duration_days": <int|null>,
                   "timing": "<e.g. 'After food', or null>", "prn": <bool>, "instructions": "<or null>"}],
  "investigations": [{"text": "<test name, e.g. 'CBC'>", "condition": "<e.g. 'If fever persists', or null>"}],
  "advice": ["<non-drug advice>"],
  "follow_up": {"in_days": <int|null>, "text": "<e.g. 'Review if fever persists'>"},
  "summary": "<one-line clinical summary>",
  "needs_review": ["<anything ambiguous, inaudible, assumed or inferred that the doctor must check>"]
}

Rules:
1. Extract ONLY what was said. Never invent symptoms, findings, doses, durations or diagnoses. Use null or [] when not stated.
2. Infer speaker roles from content: the doctor asks questions, examines, dictates and prescribes. One speaker throughout = "Dictation".
3. Keep turns[].text in the original words and script; do not correct the language. A transcript segment may hold
   several speakers: split it into separate turns. Merge fragments of the same turn. Drop filler noise and ASR artefacts.
4. Indian prescription shorthand: "din mein ek baar"/once daily = OD, "do baar"/twice = BD, "teen baar"/thrice = TDS,
   four times = QID, at bedtime = HS, when required = SOS (prn=true). A strength said as a bare number ("Paracetamol 650") is in mg.
5. assessment: use the doctor's stated diagnosis. If none is stated, write the most cautious syndromic description the
   transcript supports, mark it "provisional", and add a needs_review note saying it was not stated by the doctor.
6. Vitals only if a number was spoken. Temperature in Fahrenheit; convert Celsius if needed and say so in needs_review.
7. Medicines: generic names in English. Never change a dose. If a dose, frequency or duration is unclear, add a needs_review note.
8. Output valid JSON only: no markdown, no comments."""


class UnderstandError(RuntimeError):
    pass


@lru_cache(maxsize=1)
def _client() -> OpenAI:
    return OpenAI(api_key=settings.llm_api_key or "not-set", base_url=settings.llm_base_url, timeout=settings.llm_timeout_s, max_retries=3)


def _format_transcript(segments: list[Segment], patient: PatientContext) -> str:
    lines = [
        f"Patient context from HMIS: {patient.name}, {patient.gender}, age {patient.age or 'unknown'}.",
        "Transcript (speaker labels may be missing or wrong):",
    ]
    for s in segments:
        who = f"{s.speaker}: " if s.speaker else ""
        lines.append(f"[{s.id}] {who}{s.text}")
    return "\n".join(lines)


def _extract_json(text: str) -> dict:
    text = (text or "").strip()
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text)
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        m = re.search(r"\{.*\}", text, re.S)
        if not m:
            raise
        return json.loads(m.group())


def understand(segments: list[Segment], patient: PatientContext) -> tuple[ClinicalDraft, dict]:
    """Run the LLM and return (draft, meta). Raises UnderstandError on failure."""
    if not settings.llm_enabled:
        raise UnderstandError("Clinical understanding needs GROQ_API_KEY (or LLM_API_KEY). Offline demo mode is active.")
    if not segments:
        raise UnderstandError("Nothing has been captured yet.")

    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": _format_transcript(segments, patient)},
    ]
    base: dict = {"model": settings.llm_model, "messages": messages, "temperature": 0, "max_tokens": 5000}
    attempts = []
    with_extras = dict(base, response_format={"type": "json_object"})
    if settings.llm_reasoning_effort:
        # sent as a raw body field so it works with any openai SDK version
        with_extras["extra_body"] = {"reasoning_effort": settings.llm_reasoning_effort}
    attempts.append(with_extras)
    attempts.append(base)  # fallback for endpoints without JSON mode / reasoning_effort

    t0 = time.perf_counter()
    last_exc: Exception | None = None
    for kwargs in attempts:
        try:
            resp = _client().chat.completions.create(**kwargs)
            content = resp.choices[0].message.content
            data = _extract_json(content)
            draft = ClinicalDraft.model_validate(data)
            usage = resp.usage.model_dump() if getattr(resp, "usage", None) else {}
            return draft, {
                "engine": f"{settings.llm_model} (live)",
                "latency_s": round(time.perf_counter() - t0, 2),
                "usage": usage,
            }
        except (APIConnectionError, AuthenticationError) as exc:  # retrying will not help
            last_exc = exc
            break
        except (BadRequestError, ValueError, ValidationError) as exc:  # retry with the plainer request
            last_exc = exc
            continue
        except Exception as exc:  # rate limit, server error, ...
            last_exc = exc
            break
    raise UnderstandError(f"Clinical understanding failed: {last_exc}")
