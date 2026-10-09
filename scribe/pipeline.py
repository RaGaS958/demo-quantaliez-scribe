"""
The five-step pipeline from the deck, wired together:

  1 Capture      -> segments arrive (mic, upload, LiveKit, sample)         [app.py / stt.py]
  2 Transcribe   -> Whisper (open-weight) via OpenAI-compatible API         [stt.py]
  3 Understand   -> open-weight LLM: speaker roles, translation, entities   [understand.py]
                    + terminology grounding (SNOMED CT / LOINC)             [terminology.py]
  4 Build        -> ABDM OP Consult Record, FHIR R4 document Bundle         [fhir_builder.py]
                    + structural validation                                  [validator.py]
  5 Clinician    -> edits and approves; only then saved to HMIS             [store.py]
"""

from __future__ import annotations

import time

from scribe.fhir_builder import build_bundle, medication_text
from scribe.models import ClinicalDraft, Turn
from scribe.store import Session, get_sample, now
from scribe.terminology import ground
from scribe.understand import UnderstandError, understand
from scribe.validator import validate_bundle


def cached_draft(session: Session) -> ClinicalDraft | None:
    """Offline demo: the pre-computed draft for a built-in sample consultation."""
    sample = get_sample(session.sample_id or "")
    if not sample:
        return None
    draft = ClinicalDraft.model_validate(sample["draft"])
    draft.turns = [Turn(speaker=s.speaker or "Unknown", text=s.text, english=s.english) for s in session.segments]
    return draft


def generate_draft(session: Session) -> dict:
    """Run understanding (live LLM, or cached for samples when offline / on failure)."""
    notice = None
    t0 = time.perf_counter()
    try:
        draft, meta = understand(session.segments, session.patient)
        engine = meta["engine"]
        llm_latency = meta["latency_s"]
    except UnderstandError as exc:
        draft = cached_draft(session)
        if draft is None:
            raise
        engine = "Offline demo (cached draft for built-in sample)"
        llm_latency = None
        notice = str(exc)
    session.timings["understand_s"] = llm_latency if llm_latency is not None else round(time.perf_counter() - t0, 2)

    session.draft = draft
    session.drafted_at = now()
    session.engine = engine
    session.status = "draft"
    out = rebuild(session)
    out["notice"] = notice
    return out


def rebuild(session: Session) -> dict:
    """Ground codes, build the bundle and validate. Runs after every doctor edit too."""
    assert session.draft is not None
    t0 = time.perf_counter()
    ground(session.draft)
    bundle = build_bundle(
        session.draft,
        session_id=session.id,
        patient=session.patient,
        practitioner=session.practitioner,
        facility=session.facility,
        started_at=session.created_at,
        drafted_at=session.drafted_at,
        approved_at=session.approved_at,
    )
    session.timings["build_ms"] = round((time.perf_counter() - t0) * 1000, 1)
    t1 = time.perf_counter()
    validation = validate_bundle(bundle)
    session.timings["validate_ms"] = round((time.perf_counter() - t1) * 1000, 1)
    return {
        "session_id": session.id,
        "status": session.status,
        "engine": session.engine,
        "draft": session.draft.model_dump(),
        "note": render_note(session.draft),
        "bundle": bundle,
        "validation": validation,
        "timings": session.timings,
    }


def render_note(d: ClinicalDraft) -> dict:
    """Output 1: the human-readable note (also stored in the HMIS record)."""
    v = d.vitals
    vitals = []
    if v.temperature_f is not None:
        vitals.append(f"Temp {v.temperature_f:g} °F")
    if v.pulse_bpm is not None:
        vitals.append(f"Pulse {v.pulse_bpm}/min")
    if v.bp_systolic and v.bp_diastolic:
        vitals.append(f"BP {v.bp_systolic}/{v.bp_diastolic} mmHg")
    if v.spo2_pct is not None:
        vitals.append(f"SpO2 {v.spo2_pct}%")
    if v.resp_rate is not None:
        vitals.append(f"RR {v.resp_rate}/min")
    if v.weight_kg is not None:
        vitals.append(f"Weight {v.weight_kg:g} kg")
    return {
        "Chief complaint": [c.text + (f" for {c.duration}" if c.duration else "") + (f" ({c.details})" if c.details else "") for c in d.chief_complaints],
        "History": d.history,
        "Allergies": d.allergies or (["No known allergies"] if d.no_known_allergies else []),
        "Vitals": vitals,
        "Examination": d.examination,
        "Results": [f"{r.text}: {r.value or ''} {r.unit or ''}".strip() for r in d.results],
        "Assessment": [a.text + (" (provisional)" if a.status != "confirmed" else "") for a in d.assessment],
        "Medication": [medication_text(m) for m in d.medications],
        "Investigations": [i.text + (f" ({i.condition})" if i.condition else "") for i in d.investigations],
        "Advice": d.advice,
        "Follow-up": [d.follow_up.text or f"Review in {d.follow_up.in_days} days"] if (d.follow_up.text or d.follow_up.in_days) else [],
        "Summary": [d.summary] if d.summary else [],
        "generated_at": now().isoformat(timespec="seconds"),
    }
