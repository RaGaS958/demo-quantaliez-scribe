"""
Quantaliez Voice Scribe: demo server.

    python app.py            -> http://127.0.0.1:8000  (demo HMIS; launch the scribe from a patient)

Runs fully offline on the built-in sample consultations. Add GROQ_API_KEY to .env for live
microphone / audio-upload transcription and live clinical understanding.
"""

from __future__ import annotations

import time
from pathlib import Path
from urllib.parse import urlparse

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from config import settings
from scribe import store
from scribe.models import ClinicalDraft, FacilityContext, PatientContext, PractitionerContext
from scribe.pipeline import generate_draft, rebuild, render_note
from scribe.stt import ALLOWED_EXT, STTError, transcribe
from scribe.understand import UnderstandError

WEB = Path(__file__).parent / "web"

app = FastAPI(title="Quantaliez Voice Scribe (demo)", version="0.1.0")
app.mount("/static", StaticFiles(directory=WEB), name="static")


# ---------------------------------------------------------------------------
# Pages
# ---------------------------------------------------------------------------


@app.get("/", include_in_schema=False)
def hmis_page():
    return FileResponse(WEB / "hmis.html")


@app.get("/scribe", include_in_schema=False)
def scribe_page():
    return FileResponse(WEB / "scribe.html")


@app.get("/report/{record_id}", include_in_schema=False)
def report_page(record_id: str):
    return FileResponse(WEB / "report.html")


# ---------------------------------------------------------------------------
# Config / demo data
# ---------------------------------------------------------------------------


@app.get("/api/config")
def get_config():
    data = store.demo_data()
    live = settings.llm_enabled and settings.stt_enabled
    return {
        "mode": "live" if live else "offline",
        "stt": {"enabled": settings.stt_enabled, "model": settings.stt_model, "host": urlparse(settings.stt_base_url).netloc},
        "llm": {"enabled": settings.llm_enabled, "model": settings.llm_model, "host": urlparse(settings.llm_base_url).netloc},
        "livekit_configured": bool(settings.livekit_url),
        "upload_formats": sorted(ALLOWED_EXT),
        "max_upload_mb": settings.stt_max_upload_mb,
        "practitioner": store.practitioner(),
        "facility": store.facility(),
        "patients": store.all_patients(),
        "samples": [{"id": s["id"], "title": s["title"], "subtitle": s["subtitle"], "segments": s["segments"]} for s in data["samples"]],
    }


# ---------------------------------------------------------------------------
# Sessions
# ---------------------------------------------------------------------------


class NewSession(BaseModel):
    patient_id: str | None = None
    patient: PatientContext | None = None


def _session(sid: str) -> store.Session:
    s = store.SESSIONS.get(sid)
    if not s:
        raise HTTPException(404, "Unknown session")
    return s


def _public(s: store.Session) -> dict:
    return {
        "id": s.id,
        "status": s.status,
        "patient": s.patient.model_dump(),
        "practitioner": s.practitioner.model_dump(),
        "facility": s.facility.model_dump(),
        "segments": [x.model_dump() for x in s.segments],
        "record_id": s.record_id,
        "sample_id": s.sample_id,
    }


@app.post("/api/sessions")
def new_session(body: NewSession):
    if body.patient:
        patient = body.patient
    else:
        p = store.get_patient(body.patient_id or "P001") or store.all_patients()[0]
        patient = PatientContext.model_validate(p)
    s = store.create_session(
        patient,
        PractitionerContext.model_validate(store.practitioner()),
        FacilityContext.model_validate(store.facility()),
    )
    return _public(s)


@app.get("/api/sessions/{sid}")
def get_session(sid: str):
    return _public(_session(sid))


class SegmentsIn(BaseModel):
    segments: list[dict]
    source: str = "typed"
    sample_id: str | None = None


@app.post("/api/sessions/{sid}/segments")
def post_segments(sid: str, body: SegmentsIn):
    s = _session(sid)
    if s.status == "approved":
        raise HTTPException(409, "Record already approved")
    if body.sample_id:
        s.sample_id = body.sample_id
    added = store.add_segments(s, body.segments, body.source)
    return {"added": [x.model_dump() for x in added], "count": len(s.segments)}


@app.post("/api/sessions/{sid}/audio")
async def post_audio(sid: str, file: UploadFile = File(...), mode: str = Form("segment")):
    s = _session(sid)
    if s.status == "approved":
        raise HTTPException(409, "Record already approved")
    name = file.filename or "audio.webm"
    ext = Path(name).suffix.lower()
    if ext and ext not in ALLOWED_EXT:
        raise HTTPException(415, f"Unsupported audio format {ext}. Use WAV, MP3, FLAC, M4A, OGG or WEBM.")
    audio = await file.read()
    try:
        result = transcribe(audio, name, segment_mode=(mode == "segment"))
    except STTError as exc:
        store.audit(sid, "transcription.failed", detail=str(exc))
        raise HTTPException(503, str(exc))
    source = "mic" if mode == "segment" else "upload"
    added = store.add_segments(s, result["segments"], source)
    s.timings["transcribe_s"] = round(s.timings.get("transcribe_s", 0) + result["latency_s"], 2)
    if added:
        store.audit(sid, "transcription.completed", actor=f"ASR {result['model']}",
                    detail=f"{len(added)} segment(s) from {source}, {result['latency_s']} s, language={result.get('language')}")
    return {"added": [x.model_dump() for x in added], "latency_s": result["latency_s"], "language": result.get("language")}


@app.post("/api/sessions/{sid}/draft")
def make_draft(sid: str):
    s = _session(sid)
    if s.status == "approved":
        raise HTTPException(409, "Record already approved")
    if not s.segments:
        raise HTTPException(400, "Nothing captured yet")
    store.audit(sid, "consultation.ended", actor=s.practitioner.name, detail=f"{len(s.segments)} transcript segment(s) captured")
    try:
        out = generate_draft(s)
    except UnderstandError as exc:
        store.audit(sid, "draft.failed", detail=str(exc))
        raise HTTPException(503, str(exc))
    v = out["validation"]
    store.audit(sid, "draft.generated", actor=out["engine"],
                detail=f"{sum(v['resource_counts'].values())} FHIR resources; {len(v['errors'])} error(s), {len(v['warnings'])} warning(s)")
    return out


class DraftEdit(BaseModel):
    draft: dict


def _flatten(obj, prefix=""):
    if isinstance(obj, dict):
        for k, val in obj.items():
            if k in ("coding", "turns"):
                continue
            yield from _flatten(val, f"{prefix}.{k}" if prefix else k)
    elif isinstance(obj, list):
        if not obj:
            yield prefix, None
        for i, val in enumerate(obj):
            yield from _flatten(val, f"{prefix}[{i}]")
    else:
        yield prefix, obj


@app.put("/api/sessions/{sid}/draft")
def edit_draft(sid: str, body: DraftEdit):
    s = _session(sid)
    if s.status == "approved":
        raise HTTPException(409, "Record already approved; amendments need a new version")
    if s.draft is None:
        raise HTTPException(400, "No draft yet")
    before = dict(_flatten(s.draft.model_dump()))
    new = ClinicalDraft.model_validate(body.draft)
    new.turns = s.draft.turns
    after = dict(_flatten(new.model_dump()))
    changed = sorted(k for k in set(before) | set(after) if before.get(k) != after.get(k))
    s.draft = new
    if changed:
        s.edits += 1
        shown = ", ".join(changed[:6]) + (" ..." if len(changed) > 6 else "")
        store.audit(sid, "draft.edited", actor=s.practitioner.name, detail=f"Changed {shown}")
    out = rebuild(s)
    out["changed"] = changed
    return out


class Approve(BaseModel):
    approver: str | None = None


@app.post("/api/sessions/{sid}/approve")
def approve(sid: str, body: Approve):
    s = _session(sid)
    if s.draft is None:
        raise HTTPException(400, "No draft to approve")
    if s.status == "approved":
        raise HTTPException(409, "Already approved")
    pre = rebuild(s)
    if not pre["validation"]["valid"]:
        raise HTTPException(422, {"message": "Fix validation errors before approval", "errors": pre["validation"]["errors"]})

    s.approved_at = store.now()
    s.status = "approved"
    final = rebuild(s)
    if not final["validation"]["valid"]:  # should not happen; roll back
        s.approved_at, s.status = None, "draft"
        raise HTTPException(500, {"message": "Final bundle failed validation", "errors": final["validation"]["errors"]})

    approver = body.approver or s.practitioner.name
    store.audit(sid, "record.approved", actor=approver, detail=f"OP Consult Record approved after {s.edits} edit(s)")
    record = store.save_to_hmis(s, final["bundle"], render_note(s.draft))
    s.record_id = record["record_id"]
    store.audit(sid, "record.saved_to_hmis", actor="HMIS adapter", detail=f"Record {record['record_id']} returned to host HMIS")
    store.audit(sid, "abdm.care_context_ready", actor="HIP adapter",
                detail="Care context ready to link; data shared only on patient consent")
    final["record"] = {k: record[k] for k in ("record_id", "saved_at", "abdm")}
    final["status"] = "approved"
    return final


@app.get("/api/sessions/{sid}/bundle")
def download_bundle(sid: str):
    s = _session(sid)
    if s.draft is None:
        raise HTTPException(400, "No draft yet")
    out = rebuild(s)
    return JSONResponse(out["bundle"], headers={"Content-Disposition": f'attachment; filename="{s.id}-op-consult-bundle.json"'})


# ---------------------------------------------------------------------------
# Audit + HMIS
# ---------------------------------------------------------------------------


@app.get("/api/audit")
def get_audit(session: str | None = None):
    return {"events": store.audit_for(session)}


@app.get("/api/hmis/records")
def get_records(patient_id: str | None = None):
    recs = store.hmis_records(patient_id)
    return {"records": [{k: r[k] for k in ("record_id", "saved_at", "patient_id", "patient_name", "practitioner", "document_type", "note", "abdm")} for r in recs]}


class NewPatient(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    gender: str = "unknown"
    age: int | None = Field(default=None, ge=0, le=130)
    mobile: str | None = None
    abha_number: str | None = None
    abha_address: str | None = None
    mrn: str | None = None
    reason: str | None = None


@app.post("/api/hmis/patients")
def register_patient(body: NewPatient):
    if body.gender not in ("male", "female", "other", "unknown"):
        raise HTTPException(422, "gender must be male, female, other or unknown")
    p = store.add_patient(body.model_dump())
    store.audit(None, "patient.registered", actor="HMIS", detail=f"{p['name']} ({p['mrn']}) registered, token #{p['token']}")
    return p


@app.delete("/api/hmis/patients/{patient_id}")
def delete_patient(patient_id: str):
    if not store.remove_patient(patient_id):
        raise HTTPException(404, "Only patients you registered can be removed")
    return {"ok": True}


@app.post("/api/demo/reset")
def reset_demo():
    """Clear demo HMIS records and the audit log (handy before presenting)."""
    removed = store.reset_runtime()
    return {"ok": True, "removed_records": removed}


def _record(record_id: str) -> dict:
    for r in store.hmis_records():
        if r["record_id"] == record_id:
            return r
    raise HTTPException(404, "Unknown record")


@app.get("/api/hmis/records/{record_id}")
def get_record(record_id: str):
    return _record(record_id)


@app.get("/api/hmis/records/{record_id}/bundle")
def get_record_bundle(record_id: str):
    r = _record(record_id)
    return JSONResponse(r["fhir_bundle"], headers={"Content-Disposition": f'attachment; filename="{record_id}-fhir-bundle.json"'})


# ---------------------------------------------------------------------------
# Optional LiveKit ambient capture channel (voice_agent/ambient_scribe.py posts here)
# ---------------------------------------------------------------------------

AMBIENT: list[dict] = []
AMBIENT_STATE = {"last_seen": None, "room": None}


class AmbientIn(BaseModel):
    text: str
    speaker: str | None = None
    room: str | None = None


@app.post("/api/ambient/segments")
def ambient_in(body: AmbientIn):
    AMBIENT_STATE["last_seen"] = time.time()
    AMBIENT_STATE["room"] = body.room
    if body.text.strip():
        AMBIENT.append({"id": len(AMBIENT) + 1, "text": body.text.strip(), "speaker": body.speaker})
    return {"ok": True, "count": len(AMBIENT)}


@app.post("/api/ambient/heartbeat")
def ambient_heartbeat(room: str | None = None):
    AMBIENT_STATE["last_seen"] = time.time()
    AMBIENT_STATE["room"] = room
    return {"ok": True}


@app.get("/api/ambient/segments")
def ambient_out(after: int = 0):
    last = AMBIENT_STATE["last_seen"]
    return {
        "connected": bool(last and time.time() - last < 30),
        "room": AMBIENT_STATE["room"],
        "segments": [a for a in AMBIENT if a["id"] > after],
        "last_id": len(AMBIENT),
    }


import subprocess
import sys

livekit_process = None

@app.post("/api/ambient/start")
def ambient_start():
    global livekit_process
    if livekit_process is None or livekit_process.poll() is not None:
        AMBIENT.clear()
        livekit_process = subprocess.Popen(
            [sys.executable, "-m", "voice_agent.ambient_scribe", "console"]
        )
    return {"ok": True}

@app.post("/api/ambient/stop")
def ambient_stop():
    global livekit_process
    if livekit_process is not None and livekit_process.poll() is None:
        livekit_process.terminate()
        try:
            livekit_process.wait(timeout=3)
        except subprocess.TimeoutExpired:
            livekit_process.kill()
        livekit_process = None
    return {"ok": True}

@app.get("/api/health")
def health():
    return {"ok": True, "mode": "live" if (settings.llm_enabled and settings.stt_enabled) else "offline"}


if __name__ == "__main__":
    import uvicorn

    mode = "LIVE (Groq / OpenAI-compatible)" if settings.llm_enabled else "OFFLINE DEMO (built-in samples)"
    print(f"\n  Quantaliez Voice Scribe: {mode}")
    print(f"  Open http://{settings.host}:{settings.port}  (demo HMIS)  or  /scribe  (review workspace)\n")
    uvicorn.run(app, host=settings.host, port=settings.port, log_level="warning")
