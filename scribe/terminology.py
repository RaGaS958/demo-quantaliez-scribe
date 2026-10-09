"""
Terminology grounding: attach SNOMED CT / LOINC codes to the draft.

This replaces the Lauki agent's vector-search RAG. For clinical codes we want an exact,
explainable lookup rather than "nearest embedding": if a term is not in the table it stays
uncoded (text only) and the UI flags it, so nothing is coded by guesswork.
"""

from __future__ import annotations

import json
import re
from functools import lru_cache
from pathlib import Path

from scribe.models import ClinicalDraft, Coding

SNOMED = "http://snomed.info/sct"
LOINC = "http://loinc.org"

_RESOURCE = Path(__file__).parent / "resources" / "terminology.json"


def _norm(text: str) -> str:
    text = text.lower()
    text = re.sub(r"[^\w\s/ऀ-ॿ]", " ", text)  # keep Devanagari
    return re.sub(r"\s+", " ", text).strip()


@lru_cache(maxsize=1)
def _table() -> dict[str, list[tuple[str, dict]]]:
    data = json.loads(_RESOURCE.read_text(encoding="utf-8"))
    out: dict[str, list[tuple[str, dict]]] = {}
    for group in ("symptoms", "diagnoses", "labs"):
        pairs = []
        for entry in data[group]:
            for syn in entry["synonyms"]:
                pairs.append((_norm(syn), entry))
        # longest synonym first, so "dry cough" wins over "cough"
        pairs.sort(key=lambda p: len(p[0]), reverse=True)
        out[group] = pairs
    return out


def lookup(text: str, group: str) -> Coding | None:
    """Return the best coding for `text` in group symptoms|diagnoses|labs, or None."""
    if not text:
        return None
    t = f" {_norm(text)} "
    system = LOINC if group == "labs" else SNOMED
    for syn, entry in _table()[group]:
        if f" {syn} " in t:
            return Coding(system=system, code=entry["code"], display=entry["display"])
    return None


def ground(draft: ClinicalDraft) -> ClinicalDraft:
    """Attach codes in place (doctor edits re-run this, so codes follow the text)."""
    for c in draft.chief_complaints:
        c.coding = lookup(c.text, "symptoms")
    for d in draft.assessment:
        d.coding = lookup(d.text, "diagnoses")
    for i in draft.investigations:
        i.coding = lookup(i.text, "labs")
    for r in draft.results:
        r.coding = lookup(r.text, "labs")
    return draft
