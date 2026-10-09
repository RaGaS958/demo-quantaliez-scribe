"""
Offline smoke test of the whole flow (no API keys, no network):
HMIS launch -> sample capture -> draft -> doctor edit -> approve -> saved to HMIS.

    python -m pytest -q        or        python tests/test_pipeline.py
"""

import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.chdir(ROOT)
os.environ["OFFLINE_DEMO"] = "true"
os.environ["RUNTIME_DIR"] = tempfile.mkdtemp(prefix="vs-test-")
os.environ["DOCTOR_NAME"] = "Dr. Test Sharma"
os.environ["DOCTOR_REGISTRATION"] = "UPMC-12345"
os.environ["FACILITY_NAME"] = "Test Clinic, Lucknow"
os.environ["FACILITY_ADDRESS"] = "1 Test Road, Lucknow"

from fastapi.testclient import TestClient  # noqa: E402

from app import app  # noqa: E402

client = TestClient(app)


def _run_sample(sample_id: str, patient_id: str) -> dict:
    cfg = client.get("/api/config").json()
    assert cfg["mode"] == "offline"
    sample = next(s for s in cfg["samples"] if s["id"] == sample_id)

    sess = client.post("/api/sessions", json={"patient_id": patient_id}).json()
    sid = sess["id"]
    for seg in sample["segments"]:
        r = client.post(f"/api/sessions/{sid}/segments", json={"segments": [seg], "source": "sample", "sample_id": sample_id})
        assert r.status_code == 200, r.text

    draft = client.post(f"/api/sessions/{sid}/draft")
    assert draft.status_code == 200, draft.text
    out = draft.json()
    assert out["validation"]["valid"], out["validation"]["errors"]
    assert out["bundle"]["type"] == "document"
    assert out["bundle"]["entry"][0]["resource"]["resourceType"] == "Composition"
    assert out["bundle"]["entry"][0]["resource"]["status"] == "preliminary"
    return {"sid": sid, **out}


def test_fever_flow_edit_and_approve():
    out = _run_sample("fever-cough", "P001")
    sid = out["sid"]
    rt = out["validation"]["resource_counts"]
    assert rt["MedicationRequest"] == 1 and rt["Patient"] == 1 and rt["Condition"] >= 3

    # terminology grounding
    complaints = out["draft"]["chief_complaints"]
    assert complaints[0]["coding"]["code"] == "386661006"  # Fever
    assert complaints[1]["coding"]["code"] == "11833005"  # Dry cough
    mr = next(e["resource"] for e in out["bundle"]["entry"] if e["resource"]["resourceType"] == "MedicationRequest")
    dose = mr["dosageInstruction"][0]
    assert dose["timing"]["repeat"]["frequency"] == 3
    assert dose["timing"]["repeat"]["boundsDuration"]["value"] == 3
    assert dose["text"].startswith("Paracetamol 650 mg, three times daily for 3 days")

    # doctor edits: duration 3 -> 5 days
    d = out["draft"]
    d["medications"][0]["duration_days"] = 5
    r = client.put(f"/api/sessions/{sid}/draft", json={"draft": d})
    assert r.status_code == 200, r.text
    assert "medications[0].duration_days" in r.json()["changed"]

    # approve -> final, signed, attested, saved to HMIS
    r = client.post(f"/api/sessions/{sid}/approve", json={})
    assert r.status_code == 200, r.text
    fin = r.json()
    comp = fin["bundle"]["entry"][0]["resource"]
    assert comp["status"] == "final" and comp["attester"]
    assert fin["bundle"]["signature"]
    assert fin["validation"]["valid"]
    rec_id = fin["record"]["record_id"]

    recs = client.get("/api/hmis/records", params={"patient_id": "P001"}).json()["records"]
    assert any(x["record_id"] == rec_id for x in recs)

    events = [e["event"] for e in client.get("/api/audit", params={"session": sid}).json()["events"]]
    for ev in ("session.started", "draft.generated", "draft.edited", "record.approved", "record.saved_to_hmis"):
        assert ev in events, events

    # approved records are locked
    assert client.put(f"/api/sessions/{sid}/draft", json={"draft": d}).status_code == 409


def test_other_samples_validate():
    for sample_id, patient_id in (("diabetes-bp", "P002"), ("dictation-throat", "P003")):
        out = _run_sample(sample_id, patient_id)
        assert out["validation"]["valid"]


def test_live_endpoints_fail_cleanly_offline():
    sid = client.post("/api/sessions", json={"patient_id": "P001"}).json()["id"]
    r = client.post(f"/api/sessions/{sid}/audio", files={"file": ("a.wav", b"RIFF0000", "audio/wav")}, data={"mode": "segment"})
    assert r.status_code == 503
    client.post(f"/api/sessions/{sid}/segments", json={"segments": [{"text": "hello"}], "source": "typed"})
    assert client.post(f"/api/sessions/{sid}/draft").status_code == 503  # no LLM and not a sample


def test_registered_patient_and_report():
    p = client.post("/api/hmis/patients", json={"name": "Mohd Test", "age": 30, "gender": "male", "mobile": "9000000000"}).json()
    assert p["id"].startswith("R") and p["mrn"]
    cfg = client.get("/api/config").json()
    assert cfg["patients"][0]["id"] == p["id"] and cfg["patients"][0]["demo"] is False
    assert cfg["practitioner"]["name"] == "Dr. Test Sharma"

    sess = client.post("/api/sessions", json={"patient_id": p["id"]}).json()
    assert sess["patient"]["name"] == "Mohd Test"
    sample = next(s for s in cfg["samples"] if s["id"] == "fever-cough")
    client.post(f"/api/sessions/{sess['id']}/segments", json={"segments": sample["segments"], "source": "sample", "sample_id": "fever-cough"})
    assert client.post(f"/api/sessions/{sess['id']}/draft").status_code == 200
    fin = client.post(f"/api/sessions/{sess['id']}/approve", json={}).json()
    entries = {e["resource"]["resourceType"]: e["resource"] for e in fin["bundle"]["entry"]}
    assert entries["Patient"]["telecom"][0]["value"] == "9000000000"
    assert entries["Practitioner"]["name"][0]["text"] == "Dr. Test Sharma"
    assert entries["Organization"]["address"][0]["text"] == "1 Test Road, Lucknow"

    rid = fin["record"]["record_id"]
    rec = client.get(f"/api/hmis/records/{rid}").json()
    assert rec["draft"]["medications"][0]["name"] == "Paracetamol"
    assert rec["patient"]["mobile"] == "9000000000"
    assert rec["facility_detail"]["name"] == "Test Clinic, Lucknow"
    assert client.get(f"/report/{rid}").status_code == 200
    b = client.get(f"/api/hmis/records/{rid}/bundle")
    assert b.status_code == 200 and b.json()["type"] == "document"

    assert client.delete(f"/api/hmis/patients/{p['id']}").status_code == 200
    assert client.delete("/api/hmis/patients/P001").status_code == 404  # demo patients stay


if __name__ == "__main__":
    test_registered_patient_and_report()
    test_fever_flow_edit_and_approve()
    test_other_samples_validate()
    test_live_endpoints_fail_cleanly_offline()
    print("All offline tests passed.")
