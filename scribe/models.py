"""
Data models for one consultation.

ClinicalDraft is the single structured object the doctor reviews. The LLM fills it,
the doctor edits it, and the FHIR builder turns it into an ABDM OP Consult Record.
Validators are deliberately lenient: LLM output is normalised, never trusted blindly.
"""

from __future__ import annotations

import re
from typing import Annotated, Any

from pydantic import BaseModel, BeforeValidator, Field


# ---------------------------------------------------------------------------
# Lenient coercion helpers (LLMs return "3 days" where we want 3, objects where we want strings)
# ---------------------------------------------------------------------------


def _to_int(v: Any) -> int | None:
    if v is None or v == "":
        return None
    if isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return int(v)
    m = re.search(r"\d+", str(v))
    return int(m.group()) if m else None


def _to_float(v: Any) -> float | None:
    if v is None or v == "":
        return None
    if isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return float(v)
    m = re.search(r"\d+(?:\.\d+)?", str(v))
    return float(m.group()) if m else None


def _to_str(v: Any) -> str | None:
    if v is None:
        return None
    if isinstance(v, dict):
        v = v.get("text") or v.get("name") or v.get("value") or ""
    s = str(v).strip()
    return s or None


def _to_str_list(v: Any) -> list[str]:
    if v is None:
        return []
    if isinstance(v, (str, dict)):
        v = [v]
    out = []
    for item in v:
        s = _to_str(item)
        if s:
            out.append(s)
    return out


def _to_list(v: Any) -> list:
    if v is None:
        return []
    if isinstance(v, (str, dict)):
        return [v]
    return list(v)


LInt = Annotated[int | None, BeforeValidator(_to_int)]
LFloat = Annotated[float | None, BeforeValidator(_to_float)]
LStr = Annotated[str | None, BeforeValidator(_to_str)]
LStrList = Annotated[list[str], BeforeValidator(_to_str_list)]


# ---------------------------------------------------------------------------
# Consultation context (comes from the host HMIS at launch)
# ---------------------------------------------------------------------------


class PatientContext(BaseModel):
    id: str = "P-DEMO"
    name: str = "Demo Patient"
    gender: str = "unknown"  # male | female | other | unknown (FHIR administrative gender)
    age: int | None = None
    abha_number: str | None = None
    abha_address: str | None = None
    mrn: str | None = None
    mobile: str | None = None


class PractitionerContext(BaseModel):
    id: str = "DR-DEMO"
    name: str = "Dr. Kavita Rao"
    hpr_id: str | None = "71-0000-0000-0001"
    qualification: str | None = "MBBS, MD (Medicine)"
    registration: str | None = None


class FacilityContext(BaseModel):
    id: str = "FAC-DEMO"
    name: str = "Demo Urban PHC, Lucknow"
    hfr_id: str | None = "IN0910000001"
    address: str | None = None
    phone: str | None = None


# ---------------------------------------------------------------------------
# Transcript
# ---------------------------------------------------------------------------


class Segment(BaseModel):
    """One captured utterance, before understanding (speaker may be unknown)."""

    id: int = 0
    text: str
    speaker: str | None = None
    start: float | None = None
    end: float | None = None
    source: str = "mic"  # mic | upload | sample | livekit | typed
    english: str | None = None  # samples carry a reference translation


class Turn(BaseModel):
    """A speaker-attributed turn after understanding."""

    speaker: str = "Unknown"
    text: str
    english: LStr = None


# ---------------------------------------------------------------------------
# Clinical draft
# ---------------------------------------------------------------------------


class Coding(BaseModel):
    system: str
    code: str
    display: str


class Complaint(BaseModel):
    text: str
    duration: LStr = None
    details: LStr = None
    coding: Coding | None = None


class Diagnosis(BaseModel):
    text: str
    status: str = "provisional"  # provisional | confirmed
    coding: Coding | None = None


class Vitals(BaseModel):
    temperature_f: LFloat = None
    pulse_bpm: LInt = None
    bp_systolic: LInt = None
    bp_diastolic: LInt = None
    spo2_pct: LInt = None
    resp_rate: LInt = None
    weight_kg: LFloat = None


class Result(BaseModel):
    """A test result mentioned in the consultation (e.g. a report the patient brought)."""

    text: str
    value: LStr = None
    unit: LStr = None
    coding: Coding | None = None


class Medication(BaseModel):
    name: str
    strength: LStr = None
    form: LStr = "tablet"
    route: LStr = "oral"
    frequency_code: LStr = None  # OD | BD | TDS | QID | HS | SOS (Indian prescription shorthand)
    times_per_day: LInt = None
    duration_days: LInt = None
    timing: LStr = None  # e.g. "After food"
    prn: bool = False
    instructions: LStr = None


class Investigation(BaseModel):
    text: str
    condition: LStr = None  # e.g. "If fever persists"
    coding: Coding | None = None


class FollowUp(BaseModel):
    in_days: LInt = None
    text: LStr = None


def _objs(key: str):
    """Allow a list of plain strings where a list of objects is expected."""

    def conv(v: Any) -> list:
        return [{key: x} if isinstance(x, str) else x for x in _to_list(v)]

    return BeforeValidator(conv)


class ClinicalDraft(BaseModel):
    turns: list[Turn] = Field(default_factory=list)
    languages: LStr = None
    capture_mode: LStr = "Live conversation"
    chief_complaints: Annotated[list[Complaint], _objs("text")] = Field(default_factory=list)
    history: LStrList = Field(default_factory=list)
    allergies: LStrList = Field(default_factory=list)
    no_known_allergies: bool = False
    vitals: Vitals = Field(default_factory=Vitals)
    examination: LStrList = Field(default_factory=list)
    results: Annotated[list[Result], _objs("text")] = Field(default_factory=list)
    assessment: Annotated[list[Diagnosis], _objs("text")] = Field(default_factory=list)
    medications: Annotated[list[Medication], _objs("name")] = Field(default_factory=list)
    investigations: Annotated[list[Investigation], _objs("text")] = Field(default_factory=list)
    advice: LStrList = Field(default_factory=list)
    follow_up: FollowUp = Field(default_factory=FollowUp)
    summary: LStr = None
    needs_review: LStrList = Field(default_factory=list)
