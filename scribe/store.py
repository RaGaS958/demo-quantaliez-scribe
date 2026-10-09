"""
Session state, audit trail and a stand-in "host HMIS".

Demo only: sessions live in memory; the audit log and approved records are written as JSON
under ./runtime. Production uses PostgreSQL + encrypted object storage, as on the deck.
"""

from __future__ import annotations

import json
import secrets
import threading
from datetime import datetime, timedelta, timezone
from functools import lru_cache
from pathlib import Path

from pydantic import BaseModel, Field

from config import settings
from scribe.models import (
    ClinicalDraft,
    FacilityContext,
    PatientContext,
    PractitionerContext,
    Segment,
)

IST = timezone(timedelta(hours=5, minutes=30))
_RESOURCES = Path(__file__).parent / "resources"
_lock = threading.RLock()


def now() -> datetime:
    return datetime.now(IST)


def runtime_dir() -> Path:
    p = Path(settings.runtime_dir)
    (p / "hmis_records").mkdir(parents=True, exist_ok=True)
    return p


@lru_cache(maxsize=1)
def demo_data() -> dict:
    return json.loads((_RESOURCES / "demo_data.json").read_text(encoding="utf-8"))


def get_sample(sample_id: str) -> dict | None:
    return next((s for s in demo_data()["samples"] if s["id"] == sample_id), None)


# ---------------------------------------------------------------------------
# Doctor / clinic (from .env when set, else the demo values) and patients
# ---------------------------------------------------------------------------


def practitioner() -> dict:
    d = dict(demo_data()["practitioner"])
    if settings.doctor_name:
        d.update(id="DR-LOCAL", name=settings.doctor_name, hpr_id=settings.doctor_hpr_id or None,
                 qualification=settings.doctor_qualification or None)
    if settings.doctor_registration:
        d["registration"] = settings.doctor_registration
    return d


def facility() -> dict:
    d = dict(demo_data()["facility"])
    if settings.facility_name:
        d.update(id="FAC-LOCAL", name=settings.facility_name, hfr_id=settings.facility_hfr_id or None)
    if settings.facility_address:
        d["address"] = settings.facility_address
    if settings.facility_phone:
        d["phone"] = settings.facility_phone
    return d


def _patients_file() -> Path:
    return runtime_dir() / "patients.json"


def registered_patients() -> list[dict]:
    try:
        return json.loads(_patients_file().read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []


def all_patients() -> list[dict]:
    """Patients you registered (newest first), then the synthetic demo patients."""
    mine = [dict(p, demo=False) for p in reversed(registered_patients())]
    demo = [dict(p, demo=True) for p in demo_data()["patients"]]
    return mine + demo


def get_patient(patient_id: str) -> dict | None:
    return next((p for p in all_patients() if p["id"] == patient_id), None)


def add_patient(data: dict) -> dict:
    with _lock:
        pts = registered_patients()
        n = len(pts) + 1
        tokens = [p.get("token", 0) for p in all_patients()]
        patient = {
            "id": f"R{now().strftime('%y%m%d')}-{secrets.token_hex(2).upper()}",
            "name": data["name"].strip(),
            "gender": data.get("gender") or "unknown",
            "age": data.get("age"),
            "mobile": (data.get("mobile") or "").strip() or None,
            "abha_number": (data.get("abha_number") or "").strip() or None,
            "abha_address": (data.get("abha_address") or "").strip() or None,
            "mrn": (data.get("mrn") or "").strip() or f"UHID-{now().strftime('%Y')}-{n:04d}",
            "reason": (data.get("reason") or "").strip() or "OPD consultation",
            "token": max(tokens or [0]) + 1,
            "sample": None,
            "registered_at": now().isoformat(timespec="seconds"),
        }
        pts.append(patient)
        _patients_file().write_text(json.dumps(pts, ensure_ascii=False, indent=2), encoding="utf-8")
    return patient


def remove_patient(patient_id: str) -> bool:
    with _lock:
        pts = registered_patients()
        keep = [p for p in pts if p["id"] != patient_id]
        if len(keep) == len(pts):
            return False
        _patients_file().write_text(json.dumps(keep, ensure_ascii=False, indent=2), encoding="utf-8")
    return True


# ---------------------------------------------------------------------------
# Sessions
# ---------------------------------------------------------------------------


class Session(BaseModel):
    id: str
    created_at: datetime
    status: str = "capturing"  # capturing | draft | approved
    patient: PatientContext
    practitioner: PractitionerContext
    facility: FacilityContext
    sample_id: str | None = None
    segments: list[Segment] = Field(default_factory=list)
    draft: ClinicalDraft | None = None
    engine: str | None = None
    timings: dict = Field(default_factory=dict)
    record_id: str | None = None
    drafted_at: datetime | None = None
    approved_at: datetime | None = None
    edits: int = 0


SESSIONS: dict[str, Session] = {}


def create_session(patient: PatientContext, practitioner: PractitionerContext, facility: FacilityContext) -> Session:
    sid = "VS-" + now().strftime("%Y%m%d") + "-" + secrets.token_hex(3).upper()
    s = Session(id=sid, created_at=now(), patient=patient, practitioner=practitioner, facility=facility)
    with _lock:
        SESSIONS[sid] = s
    audit(sid, "session.started", actor=practitioner.name,
          detail=f"Voice Scribe launched from HMIS for patient {patient.name} ({patient.abha_address or patient.id})")
    return s


def add_segments(s: Session, items: list[dict], source: str) -> list[Segment]:
    added = []
    with _lock:
        for it in items:
            text = (it.get("text") or "").strip()
            if not text:
                continue
            seg = Segment(
                id=len(s.segments) + 1,
                text=text,
                speaker=it.get("speaker"),
                start=it.get("start"),
                end=it.get("end"),
                english=it.get("english"),
                source=source,
            )
            s.segments.append(seg)
            added.append(seg)
    return added


# ---------------------------------------------------------------------------
# Audit trail (slide 11: who accessed what, every transcription, edit, approval, API call)
# ---------------------------------------------------------------------------

AUDIT: list[dict] = []


def audit(session_id: str | None, event: str, *, actor: str = "system", detail: str = "") -> dict:
    entry = {
        "time": now().isoformat(timespec="seconds"),
        "session": session_id,
        "event": event,
        "actor": actor,
        "detail": detail,
    }
    with _lock:
        AUDIT.append(entry)
        with open(runtime_dir() / "audit.jsonl", "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    return entry


def audit_for(session_id: str | None = None, limit: int = 200) -> list[dict]:
    rows = [a for a in AUDIT if session_id is None or a["session"] == session_id]
    return rows[-limit:]


# ---------------------------------------------------------------------------
# Demo host HMIS (where approved records are "returned")
# ---------------------------------------------------------------------------


def save_to_hmis(s: Session, bundle: dict, note: dict) -> dict:
    record_id = "OPC-" + now().strftime("%Y%m%d-%H%M%S") + "-" + secrets.token_hex(2).upper()
    record = {
        "record_id": record_id,
        "saved_at": now().isoformat(timespec="seconds"),
        "session_id": s.id,
        "patient_id": s.patient.id,
        "patient_name": s.patient.name,
        "practitioner": s.practitioner.name,
        "facility": s.facility.name,
        "document_type": "OP Consult Record",
        "engine": s.engine,
        "edits": s.edits,
        "patient": s.patient.model_dump(),
        "practitioner_detail": s.practitioner.model_dump(),
        "facility_detail": s.facility.model_dump(),
        "draft": s.draft.model_dump() if s.draft else None,
        "note": note,
        "fhir_bundle": bundle,
        "abdm": {
            "hi_type": "OPConsultation",
            "care_context": f"{s.facility.hfr_id}/{s.patient.mrn or s.patient.id}/{s.id}",
            "status": "Ready to link: shared only on patient consent (HIP flow)",
        },
    }
    path = runtime_dir() / "hmis_records" / f"{record_id}.json"
    path.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
    return record


def reset_runtime() -> int:
    """Delete demo HMIS records and the audit log."""
    n = 0
    with _lock:
        for p in (runtime_dir() / "hmis_records").glob("OPC-*.json"):
            p.unlink(missing_ok=True)
            n += 1
        (runtime_dir() / "audit.jsonl").unlink(missing_ok=True)
        AUDIT.clear()
    return n


def hmis_records(patient_id: str | None = None) -> list[dict]:
    out = []
    for p in sorted((runtime_dir() / "hmis_records").glob("OPC-*.json"), reverse=True):
        try:
            rec = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if patient_id and rec.get("patient_id") != patient_id:
            continue
        out.append(rec)
    return out
