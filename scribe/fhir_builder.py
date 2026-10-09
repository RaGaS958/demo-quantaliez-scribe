"""
FHIR builder: ClinicalDraft -> ABDM OP Consult Record (FHIR R4 document Bundle).

This step is plain deterministic code, not the LLM. The model proposes the content and this
module decides the structure, so the same draft always gives the same bundle and every field
can be traced back to the draft the doctor approved.

Profiles and section codes follow the NRCeS ABDM FHIR Implementation Guide
(https://nrces.in/ndhm/fhir/r4/). Confirm them against the IG version NHA mandates.
"""

from __future__ import annotations

import html
import uuid
from datetime import datetime, timedelta, timezone

from scribe.models import (
    ClinicalDraft,
    FacilityContext,
    Medication,
    PatientContext,
    PractitionerContext,
)
from scribe.terminology import LOINC, SNOMED

NRCES = "https://nrces.in/ndhm/fhir/r4/StructureDefinition/"
UCUM = "http://unitsofmeasure.org"
V2_0203 = "http://terminology.hl7.org/CodeSystem/v2-0203"
IST = timezone(timedelta(hours=5, minutes=30))

# OP Consult Record section slices and codes, as listed in the NRCeS OPConsultRecord profile
SECTIONS = {
    "chief_complaints": ("422843007", "Chief complaint section"),
    "allergies": ("722446000", "Allergy record"),
    "history": ("371529009", "History and physical report"),
    "examination": ("425044008", "Physical exam section"),
    "observations": ("404684003", "Clinical finding"),
    "investigations": ("721963009", "Order document"),
    "medications": ("721912009", "Medication summary document"),
    "follow_up": ("390906007", "Follow-up encounter"),
}

# Indian prescription shorthand -> (times per day, HL7 GTS abbreviation, label)
FREQUENCY = {
    "OD": (1, "QD", "once daily"),
    "BD": (2, "BID", "twice daily"),
    "TDS": (3, "TID", "three times daily"),
    "TID": (3, "TID", "three times daily"),
    "QID": (4, "QID", "four times daily"),
    "HS": (1, "HS", "at bedtime"),
    "SOS": (None, None, "when required"),
}

FHIR_GENDER = {"male", "female", "other", "unknown"}


def _uuid(session_id: str, key: str) -> str:
    """Stable ids: re-building after an edit keeps the same ids, so diffs stay readable."""
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"quantaliez-voice-scribe/{session_id}/{key}"))


def _ref(rid: str) -> dict:
    return {"reference": f"urn:uuid:{rid}"}


def _cc(text: str, coding=None) -> dict:
    out = {"text": text}
    if coding is not None:
        out["coding"] = [{"system": coding.system, "code": coding.code, "display": coding.display}]
    return out


def _snomed(code: str, display: str, text: str | None = None) -> dict:
    out = {"coding": [{"system": SNOMED, "code": code, "display": display}]}
    if text:
        out["text"] = text
    return out


def _narrative(lines: list[str]) -> dict:
    items = "".join(f"<li>{html.escape(x)}</li>" for x in lines if x)
    body = f"<ul>{items}</ul>" if items else "<p>None recorded</p>"
    return {"status": "generated", "div": f'<div xmlns="http://www.w3.org/1999/xhtml">{body}</div>'}


def medication_text(m: Medication) -> str:
    """Human-readable dosage line, also used as dosageInstruction.text."""
    parts = [m.name]
    if m.strength:
        parts.append(m.strength)
    head = " ".join(parts)
    freq = FREQUENCY.get((m.frequency_code or "").upper())
    bits = []
    if m.prn or (m.frequency_code or "").upper() == "SOS":
        bits.append("when required")
    elif freq:
        bits.append(freq[2])
    elif m.times_per_day:
        bits.append(f"{m.times_per_day} times daily")
    if m.duration_days:
        bits.append(f"for {m.duration_days} day{'s' if m.duration_days != 1 else ''}")
    line = head + (", " + " ".join(bits) if bits else "")
    if m.timing:
        line += f", {m.timing.lower()}"
    if m.instructions:
        line += f" ({m.instructions})"
    return line


def _dosage(m: Medication) -> dict:
    code = (m.frequency_code or "").upper()
    freq = FREQUENCY.get(code)
    dosage: dict = {"sequence": 1, "text": medication_text(m)}
    if m.timing or m.instructions:
        dosage["additionalInstruction"] = [{"text": t} for t in (m.timing, m.instructions) if t]
    repeat: dict = {}
    per_day = (freq[0] if freq else None) or m.times_per_day
    if per_day and not m.prn:
        repeat.update({"frequency": per_day, "period": 1, "periodUnit": "d"})
    if m.duration_days:
        repeat["boundsDuration"] = {"value": m.duration_days, "unit": "days", "system": UCUM, "code": "d"}
    timing: dict = {}
    if repeat:
        timing["repeat"] = repeat
    if freq and freq[1]:
        timing["code"] = {
            "coding": [{"system": "http://terminology.hl7.org/CodeSystem/v3-GTSAbbreviation", "code": freq[1]}],
            "text": code,
        }
    if timing:
        dosage["timing"] = timing
    if m.prn or code == "SOS":
        dosage["asNeededBoolean"] = True
    if (m.route or "").lower() == "oral":
        dosage["route"] = _snomed("26643006", "Oral route")
    elif m.route:
        dosage["route"] = {"text": m.route}
    return dosage


def _vital(oid, subject, enc, when, status, loinc, display, value, unit, ucum) -> dict:
    return {
        "resourceType": "Observation",
        "id": oid,
        "meta": {"profile": [NRCES + "Observation"]},
        "status": status,
        "category": [{"coding": [{"system": "http://terminology.hl7.org/CodeSystem/observation-category", "code": "vital-signs", "display": "Vital Signs"}]}],
        "code": {"coding": [{"system": LOINC, "code": loinc, "display": display}], "text": display},
        "subject": _ref(subject),
        "encounter": _ref(enc),
        "effectiveDateTime": when,
        "valueQuantity": {"value": value, "unit": unit, "system": UCUM, "code": ucum},
    }


def build_bundle(
    draft: ClinicalDraft,
    *,
    session_id: str,
    patient: PatientContext,
    practitioner: PractitionerContext,
    facility: FacilityContext,
    started_at: datetime,
    drafted_at: datetime | None = None,
    approved_at: datetime | None = None,
) -> dict:
    """Build the OP Consult Record document bundle. Status is final only after approval.

    Timestamps come from the draft (or approval) time, not the wall clock, so rebuilding after
    an edit only changes the resources the doctor actually touched.
    """
    final = approved_at is not None
    now = (approved_at or drafted_at or datetime.now(IST)).astimezone(IST)
    when = now.isoformat(timespec="seconds")
    start = started_at.astimezone(IST).isoformat(timespec="seconds")
    obs_status = "final" if final else "preliminary"

    ids = {k: _uuid(session_id, k) for k in ("bundle", "composition", "patient", "practitioner", "organization", "encounter")}
    resources: list[dict] = []
    section_entries: dict[str, list[str]] = {k: [] for k in SECTIONS}
    section_text: dict[str, list[str]] = {k: [] for k in SECTIONS}

    # --- Who / where ---------------------------------------------------------------------
    pat = {
        "resourceType": "Patient",
        "id": ids["patient"],
        "meta": {"profile": [NRCES + "Patient"]},
        "identifier": [],
        "name": [{"text": patient.name}],
        "gender": patient.gender if patient.gender in FHIR_GENDER else "unknown",
    }
    if patient.abha_number:
        pat["identifier"].append({
            "type": {"coding": [{"system": V2_0203, "code": "MR", "display": "Medical record number"}]},
            "system": "https://healthid.ndhm.gov.in",
            "value": patient.abha_number,
        })
    if patient.mrn:
        pat["identifier"].append({
            "type": {"coding": [{"system": V2_0203, "code": "MR", "display": "Medical record number"}]},
            "system": "https://demo-hmis.example/mrn",
            "value": patient.mrn,
        })
    if patient.mobile:
        pat["telecom"] = [{"system": "phone", "value": patient.mobile, "use": "mobile"}]
    if patient.age:
        pat["birthDate"] = str(now.year - patient.age)  # approximate year only (age stated in HMIS)

    prac = {
        "resourceType": "Practitioner",
        "id": ids["practitioner"],
        "meta": {"profile": [NRCES + "Practitioner"]},
        "identifier": [{
            "type": {"coding": [{"system": V2_0203, "code": "MD", "display": "Medical License number"}]},
            "system": "https://doctor.ndhm.gov.in" if practitioner.hpr_id else "https://demo-hmis.example/practitioner",
            "value": practitioner.hpr_id or practitioner.registration or practitioner.id,
        }],
        "name": [{"text": practitioner.name}],
    }
    if practitioner.qualification:
        prac["qualification"] = [{"code": {"text": practitioner.qualification}}]
    org = {
        "resourceType": "Organization",
        "id": ids["organization"],
        "meta": {"profile": [NRCES + "Organization"]},
        "identifier": [{
            "type": {"coding": [{"system": V2_0203, "code": "PRN", "display": "Provider number"}]},
            "system": "https://facility.ndhm.gov.in" if facility.hfr_id else "https://demo-hmis.example/facility",
            "value": facility.hfr_id or facility.id,
        }],
        "name": facility.name,
    }
    if facility.address:
        org["address"] = [{"text": facility.address}]
    if facility.phone:
        org["telecom"] = [{"system": "phone", "value": facility.phone}]

    # --- Diagnoses (Encounter.diagnosis + MedicationRequest.reasonReference) ---------------
    diag_ids = []
    for i, d in enumerate(draft.assessment):
        did = _uuid(session_id, f"diagnosis/{i}")
        diag_ids.append(did)
        resources.append({
            "resourceType": "Condition",
            "id": did,
            "meta": {"profile": [NRCES + "Condition"]},
            "clinicalStatus": {"coding": [{"system": "http://terminology.hl7.org/CodeSystem/condition-clinical", "code": "active"}]},
            "verificationStatus": {"coding": [{"system": "http://terminology.hl7.org/CodeSystem/condition-ver-status", "code": "confirmed" if d.status == "confirmed" else "provisional"}]},
            "category": [{"coding": [{"system": "http://terminology.hl7.org/CodeSystem/condition-category", "code": "encounter-diagnosis", "display": "Encounter Diagnosis"}]}],
            "code": _cc(d.text, d.coding),
            "subject": _ref(ids["patient"]),
            "encounter": _ref(ids["encounter"]),
            "recordedDate": when,
        })

    enc = {
        "resourceType": "Encounter",
        "id": ids["encounter"],
        "meta": {"profile": [NRCES + "Encounter"]},
        "status": "finished",
        "class": {"system": "http://terminology.hl7.org/CodeSystem/v3-ActCode", "code": "AMB", "display": "ambulatory"},
        "subject": _ref(ids["patient"]),
        "period": {"start": start, "end": when},
        "serviceProvider": _ref(ids["organization"]),
    }
    if diag_ids:
        enc["diagnosis"] = [{"condition": _ref(x), "rank": n + 1} for n, x in enumerate(diag_ids)]

    # --- Chief complaints ----------------------------------------------------------------
    for i, c in enumerate(draft.chief_complaints):
        cid = _uuid(session_id, f"complaint/{i}")
        label = c.text + (f" for {c.duration}" if c.duration else "")
        res = {
            "resourceType": "Condition",
            "id": cid,
            "meta": {"profile": [NRCES + "Condition"]},
            "clinicalStatus": {"coding": [{"system": "http://terminology.hl7.org/CodeSystem/condition-clinical", "code": "active"}]},
            "category": [{"coding": [{"system": "http://terminology.hl7.org/CodeSystem/condition-category", "code": "problem-list-item", "display": "Problem List Item"}]}],
            "code": _cc(label, c.coding),
            "subject": _ref(ids["patient"]),
            "recordedDate": when,
        }
        if c.duration:
            res["onsetString"] = c.duration
        if c.details:
            res["note"] = [{"text": c.details}]
        resources.append(res)
        section_entries["chief_complaints"].append(cid)
        section_text["chief_complaints"].append(label + (f" ({c.details})" if c.details else ""))

    # --- History -------------------------------------------------------------------------
    for i, h in enumerate(draft.history):
        hid = _uuid(session_id, f"history/{i}")
        resources.append({
            "resourceType": "Condition",
            "id": hid,
            "meta": {"profile": [NRCES + "Condition"]},
            "clinicalStatus": {"coding": [{"system": "http://terminology.hl7.org/CodeSystem/condition-clinical", "code": "active"}]},
            "category": [{"coding": [{"system": "http://terminology.hl7.org/CodeSystem/condition-category", "code": "problem-list-item", "display": "Problem List Item"}]}],
            "code": {"text": h},
            "subject": _ref(ids["patient"]),
        })
        section_entries["history"].append(hid)
        section_text["history"].append(h)

    # --- Allergies -----------------------------------------------------------------------
    allergy_items = list(draft.allergies)
    if draft.no_known_allergies and not allergy_items:
        aid = _uuid(session_id, "allergy/nka")
        resources.append({
            "resourceType": "AllergyIntolerance",
            "id": aid,
            "meta": {"profile": [NRCES + "AllergyIntolerance"]},
            "clinicalStatus": {"coding": [{"system": "http://terminology.hl7.org/CodeSystem/allergyintolerance-clinical", "code": "active"}]},
            "verificationStatus": {"coding": [{"system": "http://terminology.hl7.org/CodeSystem/allergyintolerance-verification", "code": "confirmed"}]},
            "code": _snomed("716186003", "No known allergy", "No known allergies"),
            "patient": _ref(ids["patient"]),
            "recordedDate": when,
        })
        section_entries["allergies"].append(aid)
        section_text["allergies"].append("No known allergies")
    for i, a in enumerate(allergy_items):
        aid = _uuid(session_id, f"allergy/{i}")
        resources.append({
            "resourceType": "AllergyIntolerance",
            "id": aid,
            "meta": {"profile": [NRCES + "AllergyIntolerance"]},
            "clinicalStatus": {"coding": [{"system": "http://terminology.hl7.org/CodeSystem/allergyintolerance-clinical", "code": "active"}]},
            "verificationStatus": {"coding": [{"system": "http://terminology.hl7.org/CodeSystem/allergyintolerance-verification", "code": "unconfirmed"}]},
            "code": {"text": a},
            "patient": _ref(ids["patient"]),
            "recordedDate": when,
        })
        section_entries["allergies"].append(aid)
        section_text["allergies"].append(a)

    # --- Vitals + examination -------------------------------------------------------------
    v = draft.vitals
    vit_specs = [
        ("temp", v.temperature_f, "8310-5", "Body temperature", "°F", "[degF]", "Temperature {:g} °F"),
        ("pulse", v.pulse_bpm, "8867-4", "Heart rate", "beats/minute", "/min", "Pulse {:g}/min"),
        ("spo2", v.spo2_pct, "59408-5", "Oxygen saturation in Arterial blood by Pulse oximetry", "%", "%", "SpO2 {:g}%"),
        ("rr", v.resp_rate, "9279-1", "Respiratory rate", "breaths/minute", "/min", "Respiratory rate {:g}/min"),
        ("weight", v.weight_kg, "29463-7", "Body weight", "kg", "kg", "Weight {:g} kg"),
    ]
    for key, value, loinc, display, unit, ucum, fmt in vit_specs:
        if value is None:
            continue
        oid = _uuid(session_id, f"vital/{key}")
        resources.append(_vital(oid, ids["patient"], ids["encounter"], when, obs_status, loinc, display, value, unit, ucum))
        section_entries["examination"].append(oid)
        section_text["examination"].append(fmt.format(value))
    if v.bp_systolic and v.bp_diastolic:
        oid = _uuid(session_id, "vital/bp")
        bp = {
            "resourceType": "Observation",
            "id": oid,
            "meta": {"profile": [NRCES + "Observation"]},
            "status": obs_status,
            "category": [{"coding": [{"system": "http://terminology.hl7.org/CodeSystem/observation-category", "code": "vital-signs", "display": "Vital Signs"}]}],
            "code": {"coding": [{"system": LOINC, "code": "85354-9", "display": "Blood pressure panel with all children optional"}], "text": "Blood pressure"},
            "subject": _ref(ids["patient"]),
            "encounter": _ref(ids["encounter"]),
            "effectiveDateTime": when,
            "component": [
                {"code": {"coding": [{"system": LOINC, "code": "8480-6", "display": "Systolic blood pressure"}]},
                 "valueQuantity": {"value": v.bp_systolic, "unit": "mmHg", "system": UCUM, "code": "mm[Hg]"}},
                {"code": {"coding": [{"system": LOINC, "code": "8462-4", "display": "Diastolic blood pressure"}]},
                 "valueQuantity": {"value": v.bp_diastolic, "unit": "mmHg", "system": UCUM, "code": "mm[Hg]"}},
            ],
        }
        resources.append(bp)
        section_entries["examination"].append(oid)
        section_text["examination"].append(f"BP {v.bp_systolic}/{v.bp_diastolic} mmHg")
    for i, finding in enumerate(draft.examination):
        oid = _uuid(session_id, f"exam/{i}")
        resources.append({
            "resourceType": "Observation",
            "id": oid,
            "meta": {"profile": [NRCES + "Observation"]},
            "status": obs_status,
            "category": [{"coding": [{"system": "http://terminology.hl7.org/CodeSystem/observation-category", "code": "exam", "display": "Exam"}]}],
            "code": {"text": "Physical examination finding"},
            "subject": _ref(ids["patient"]),
            "encounter": _ref(ids["encounter"]),
            "effectiveDateTime": when,
            "valueString": finding,
        })
        section_entries["examination"].append(oid)
        section_text["examination"].append(finding)

    # --- Reported results ----------------------------------------------------------------
    for i, r in enumerate(draft.results):
        oid = _uuid(session_id, f"result/{i}")
        res = {
            "resourceType": "Observation",
            "id": oid,
            "meta": {"profile": [NRCES + "Observation"]},
            "status": obs_status,
            "category": [{"coding": [{"system": "http://terminology.hl7.org/CodeSystem/observation-category", "code": "laboratory", "display": "Laboratory"}]}],
            "code": _cc(r.text, r.coding),
            "subject": _ref(ids["patient"]),
            "encounter": _ref(ids["encounter"]),
            "effectiveDateTime": when,
        }
        numeric = None
        try:
            numeric = float(r.value) if r.value is not None else None
        except ValueError:
            numeric = None
        if numeric is not None and r.unit:
            res["valueQuantity"] = {"value": numeric, "unit": r.unit, "system": UCUM, "code": r.unit}
        elif r.value:
            res["valueString"] = f"{r.value} {r.unit or ''}".strip()
        resources.append(res)
        section_entries["observations"].append(oid)
        section_text["observations"].append(f"{r.text}: {r.value or ''} {r.unit or ''}".strip())

    # --- Investigations advised ----------------------------------------------------------
    for i, inv in enumerate(draft.investigations):
        sid = _uuid(session_id, f"investigation/{i}")
        res = {
            "resourceType": "ServiceRequest",
            "id": sid,
            "meta": {"profile": [NRCES + "ServiceRequest"]},
            "status": "active" if final else "draft",
            "intent": "proposal" if inv.condition else "order",
            "code": _cc(inv.text, inv.coding),
            "subject": _ref(ids["patient"]),
            "encounter": _ref(ids["encounter"]),
            "authoredOn": when,
            "requester": _ref(ids["practitioner"]),
        }
        if inv.condition:
            res["note"] = [{"text": inv.condition}]
        resources.append(res)
        section_entries["investigations"].append(sid)
        section_text["investigations"].append(inv.text + (f" ({inv.condition})" if inv.condition else ""))

    # --- Medications ---------------------------------------------------------------------
    for i, m in enumerate(draft.medications):
        mid = _uuid(session_id, f"medication/{i}")
        name = " ".join(x for x in (m.name, m.strength, m.form) if x)
        res = {
            "resourceType": "MedicationRequest",
            "id": mid,
            "meta": {"profile": [NRCES + "MedicationRequest"]},
            "status": "active" if final else "draft",
            "intent": "order",
            "medicationCodeableConcept": {"text": name},
            "subject": _ref(ids["patient"]),
            "encounter": _ref(ids["encounter"]),
            "authoredOn": when,
            "requester": _ref(ids["practitioner"]),
            "dosageInstruction": [_dosage(m)],
        }
        if diag_ids:
            res["reasonReference"] = [_ref(x) for x in diag_ids]
        resources.append(res)
        section_entries["medications"].append(mid)
        section_text["medications"].append(medication_text(m))

    # --- Follow-up -----------------------------------------------------------------------
    fu = draft.follow_up
    if fu.in_days or fu.text:
        apid = _uuid(session_id, "follow-up")
        appt = {
            "resourceType": "Appointment",
            "id": apid,
            "meta": {"profile": [NRCES + "Appointment"]},
            "status": "proposed",
            "description": fu.text or f"Review in {fu.in_days} days",
            "participant": [
                {"actor": _ref(ids["patient"]), "status": "needs-action"},
                {"actor": _ref(ids["practitioner"]), "status": "accepted"},
            ],
        }
        if fu.in_days:
            day = (now + timedelta(days=fu.in_days)).date().isoformat()
            appt["requestedPeriod"] = [{"start": day}]
        resources.append(appt)
        section_entries["follow_up"].append(apid)
        section_text["follow_up"].append(appt["description"])
    section_text["follow_up"].extend(f"Advice: {a}" for a in draft.advice)

    # --- Composition (the document header) ------------------------------------------------
    sections = []
    for key, (code, display) in SECTIONS.items():
        entries = section_entries[key]
        texts = section_text[key]
        if not entries and not texts:
            continue
        sec = {"title": display, "code": _snomed(code, display), "text": _narrative(texts)}
        if entries:
            sec["entry"] = [_ref(x) for x in entries]
        sections.append(sec)

    comp = {
        "resourceType": "Composition",
        "id": ids["composition"],
        "meta": {"profile": [NRCES + "OPConsultRecord"]},
        "language": "en-IN",
        "identifier": {"system": "https://demo-hmis.example/documents", "value": ids["composition"]},
        "status": "final" if final else "preliminary",
        "type": _snomed("371530004", "Clinical consultation report", "OP Consult Record"),
        "subject": _ref(ids["patient"]),
        "encounter": _ref(ids["encounter"]),
        "date": when,
        "author": [_ref(ids["practitioner"])],
        "title": "OP Consult Record",
        "custodian": _ref(ids["organization"]),
        "section": sections,
    }
    if final:
        comp["attester"] = [{"mode": "legal", "time": when, "party": _ref(ids["practitioner"])}]

    ordered = [comp, pat, prac, org, enc] + resources
    bundle = {
        "resourceType": "Bundle",
        "id": ids["bundle"],
        "meta": {
            "versionId": "1",
            "lastUpdated": when,
            "profile": [NRCES + "DocumentBundle"],
            "security": [{"system": "http://terminology.hl7.org/CodeSystem/v3-Confidentiality", "code": "V", "display": "very restricted"}],
        },
        "identifier": {"system": "http://hip.in", "value": ids["bundle"]},
        "type": "document",
        "timestamp": when,
        "entry": [{"fullUrl": f"urn:uuid:{r['id']}", "resource": r} for r in ordered],
    }
    if final:
        # Placeholder: production signs with the facility's digital signing key (ABDM requirement).
        bundle["signature"] = {
            "type": [{"system": "urn:iso-astm:E1762-95:2013", "code": "1.2.840.10065.1.12.1.1", "display": "Author's Signature"}],
            "when": when,
            "who": _ref(ids["practitioner"]),
            "sigFormat": "image/jpeg",
            "data": "REVNTy1TSUdOQVRVUkUtUExBQ0VIT0xERVI=",  # base64("DEMO-SIGNATURE-PLACEHOLDER")
        }
    return bundle
