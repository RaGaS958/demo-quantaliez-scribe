"""
Structural pre-check of the FHIR document bundle.

This is a fast, dependency-free check that runs on every edit so the doctor sees problems
before approving. It is NOT a replacement for the official validator: production runs the
HL7 FHIR Validator / HAPI with the NRCeS ABDM IG package before a record is saved.
"""

from __future__ import annotations

from collections import Counter
from typing import Any


def _walk_refs(node: Any, path: str = ""):
    if isinstance(node, dict):
        for k, v in node.items():
            if k == "reference" and isinstance(v, str):
                yield path, v
            else:
                yield from _walk_refs(v, f"{path}.{k}" if path else k)
    elif isinstance(node, list):
        for i, v in enumerate(node):
            yield from _walk_refs(v, f"{path}[{i}]")


def validate_bundle(bundle: dict) -> dict:
    errors: list[str] = []
    warnings: list[str] = []
    info: list[str] = []
    rules = 0

    def check(cond: bool, msg: str, level: str = "error") -> None:
        nonlocal rules
        rules += 1
        if not cond:
            (errors if level == "error" else warnings if level == "warning" else info).append(msg)

    check(bundle.get("resourceType") == "Bundle", "Root resource must be a Bundle")
    check(bundle.get("type") == "document", "Bundle.type must be 'document'")
    check(bool(bundle.get("identifier", {}).get("value")), "Bundle.identifier is required for documents")
    check(bool(bundle.get("timestamp")), "Bundle.timestamp is required for documents")
    check(any("DocumentBundle" in p for p in bundle.get("meta", {}).get("profile", [])),
          "Bundle should declare the NRCeS DocumentBundle profile", "warning")

    entries = bundle.get("entry", [])
    check(bool(entries), "Bundle has no entries")
    full_urls = set()
    for i, e in enumerate(entries):
        res = e.get("resource", {})
        rid = res.get("id")
        check(bool(rid), f"entry[{i}] resource has no id")
        check(e.get("fullUrl") == f"urn:uuid:{rid}", f"entry[{i}] fullUrl does not match resource id")
        check(bool(res.get("meta", {}).get("profile")), f"{res.get('resourceType')}/{rid} declares no profile", "warning")
        full_urls.add(e.get("fullUrl"))

    counts = Counter(e.get("resource", {}).get("resourceType") for e in entries)

    # Composition rules
    comp = entries[0].get("resource", {}) if entries else {}
    check(comp.get("resourceType") == "Composition", "First entry must be the Composition")
    if comp.get("resourceType") == "Composition":
        for field in ("status", "type", "subject", "encounter", "date", "author", "title", "section"):
            check(bool(comp.get(field)), f"Composition.{field} is required")
        check(any("OPConsultRecord" in p for p in comp.get("meta", {}).get("profile", [])),
              "Composition should declare the OPConsultRecord profile", "warning")
        for s in comp.get("section", []):
            check(bool(s.get("code")), f"Section '{s.get('title')}' has no code")
            check(bool(s.get("entry") or s.get("text")), f"Section '{s.get('title')}' needs entries or narrative")
        if comp.get("status") == "final":
            check(bool(comp.get("attester")), "Final record must carry a legal attester (approving clinician)")
            check(bool(bundle.get("signature")), "Final document bundle must be signed")

    # Every reference must resolve inside the bundle
    for e in entries:
        res = e.get("resource", {})
        for path, ref in _walk_refs(res):
            check(ref in full_urls, f"{res.get('resourceType')}.{path} -> {ref} does not resolve in the bundle")

    # Content rules
    for e in entries:
        res = e.get("resource", {})
        rt = res.get("resourceType")
        if rt == "Patient":
            check(bool(res.get("identifier")), "Patient has no ABHA / MRN identifier", "warning")
        elif rt == "MedicationRequest":
            dose = (res.get("dosageInstruction") or [{}])[0]
            name = res.get("medicationCodeableConcept", {}).get("text", "medication")
            check(bool(dose.get("text")), f"{name}: dosage instruction text missing")
            rep = dose.get("timing", {}).get("repeat", {})
            check(bool(rep.get("frequency") or dose.get("asNeededBoolean")),
                  f"{name}: frequency not structured", "warning")
            check(bool(rep.get("boundsDuration") or dose.get("asNeededBoolean")),
                  f"{name}: duration not stated (treated as ongoing)", "warning")
            check(bool(res.get("medicationCodeableConcept", {}).get("coding")),
                  f"{name}: text only, drug code to be mapped by the terminology service", "info")
        elif rt == "Condition":
            code = res.get("code", {})
            check(bool(code.get("coding")),
                  f"Condition '{code.get('text')}' has no SNOMED CT code (kept as text)", "info")
        elif rt == "ServiceRequest":
            code = res.get("code", {})
            check(bool(code.get("coding")),
                  f"Investigation '{code.get('text')}' has no LOINC code (kept as text)", "info")

    return {
        "valid": not errors,
        "errors": errors,
        "warnings": warnings,
        "info": info,
        "rules_checked": rules,
        "resource_counts": dict(counts),
        "validator": "Structural pre-check (demo). Production: HL7 FHIR Validator + NRCeS ABDM IG.",
    }
