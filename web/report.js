"use strict";
/* Printable OP consultation report + prescription, built from an approved HMIS record. */

const $ = (s, el = document) => el.querySelector(s);
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const icon = (n) => `<svg class="i"><use href="#i-${n}"/></svg>`;

const FREQ = {
  OD: { en: "Once a day", hi: "दिन में एक बार" },
  BD: { en: "Twice a day", hi: "दिन में दो बार", grid: "1-0-1" },
  TDS: { en: "Three times a day", hi: "दिन में तीन बार", grid: "1-1-1" },
  TID: { en: "Three times a day", hi: "दिन में तीन बार", grid: "1-1-1" },
  QID: { en: "Four times a day", hi: "दिन में चार बार", grid: "1-1-1-1" },
  HS: { en: "At bedtime", hi: "रात को सोते समय", grid: "0-0-1" },
  SOS: { en: "When required", hi: "ज़रूरत पड़ने पर", grid: "SOS" },
};

function timingHi(t) {
  if (!t) return "";
  const s = t.toLowerCase();
  const out = [];
  if (/after (food|meal)/.test(s)) out.push("खाने के बाद");
  if (/before (food|meal)/.test(s)) out.push("खाने से पहले");
  if (/empty stomach/.test(s)) out.push("खाली पेट");
  if (/morning/.test(s)) out.push("सुबह");
  if (/night|bedtime/.test(s)) out.push("रात को");
  return out.join(", ");
}

function fmtDate(d) {
  return d.toLocaleDateString("en-IN", { day: "2-digit", month: "short", year: "numeric" });
}
function fmtDateTime(iso) {
  const d = new Date(iso);
  return `${fmtDate(d)}, ${d.toLocaleTimeString("en-IN", { hour: "2-digit", minute: "2-digit" })}`;
}

function section(title, inner) {
  return inner ? `<section class="rs"><h3>${title}</h3>${inner}</section>` : "";
}
function list(items) {
  return items && items.length ? `<ul>${items.map((x) => `<li>${x}</li>`).join("")}</ul>` : "";
}

async function main() {
  const id = decodeURIComponent(location.pathname.split("/").pop());
  let rec, cfg;
  try {
    [rec, cfg] = await Promise.all([
      fetch(`/api/hmis/records/${encodeURIComponent(id)}`).then((r) => { if (!r.ok) throw new Error("Record not found"); return r.json(); }),
      fetch("/api/config").then((r) => r.json()),
    ]);
  } catch (e) {
    $("#paper").innerHTML = `<div class="empty">${esc(e.message)}</div>`;
    return;
  }
  $("#btnJson").href = `/api/hmis/records/${encodeURIComponent(id)}/bundle`;

  const p = rec.patient || { name: rec.patient_name };
  const doc = rec.practitioner_detail || { name: rec.practitioner };
  const fac = rec.facility_detail || { name: rec.facility };
  const d = rec.draft;
  const n = rec.note || {};
  const saved = new Date(rec.saved_at);
  const isDemo = (cfg.patients || []).some((x) => x.id === p.id && x.demo);
  const g = { male: "Male", female: "Female", other: "Other" }[p.gender] || "Not stated";

  document.title = `${p.name} - OP Consultation - ${fmtDate(saved)}`;

  // --- clinical content (structured draft when available, else the note lines) ---
  let complaints, history, allergies, vitals, exam, results, dx, meds, inv, advice, fu;
  if (d) {
    complaints = d.chief_complaints.map((c) => `<b>${esc(c.text)}</b>${c.duration ? ` for ${esc(c.duration)}` : ""}${c.details ? ` <span class="muted">(${esc(c.details)})</span>` : ""}`);
    history = d.history.map(esc);
    allergies = d.allergies.length ? d.allergies.map(esc) : d.no_known_allergies ? ["No known allergies"] : [];
    const v = d.vitals || {};
    vitals = [
      ["Temp", v.temperature_f != null && `${v.temperature_f} °F`],
      ["Pulse", v.pulse_bpm != null && `${v.pulse_bpm}/min`],
      ["BP", v.bp_systolic && v.bp_diastolic && `${v.bp_systolic}/${v.bp_diastolic} mmHg`],
      ["SpO2", v.spo2_pct != null && `${v.spo2_pct}%`],
      ["RR", v.resp_rate != null && `${v.resp_rate}/min`],
      ["Weight", v.weight_kg != null && `${v.weight_kg} kg`],
    ].filter((x) => x[1]);
    exam = d.examination.map(esc);
    results = d.results.map((r) => `${esc(r.text)}: <b>${esc(r.value || "")} ${esc(r.unit || "")}</b>`);
    dx = d.assessment.map((a) => `${esc(a.text)} ${a.status !== "confirmed" ? "<em>(provisional)</em>" : ""}${a.coding ? `<span class="cd">SNOMED ${esc(a.coding.code)}</span>` : ""}`);
    meds = d.medications;
    inv = d.investigations.map((i) => `${esc(i.text)}${i.condition ? ` <span class="muted">(${esc(i.condition)})</span>` : ""}`);
    advice = d.advice.map(esc);
    fu = d.follow_up || {};
  } else {
    const L = (k) => (n[k] || []).map(esc);
    complaints = L("Chief complaint"); history = L("History"); allergies = L("Allergies");
    vitals = (n["Vitals"] || []).map((x) => ["", x]);
    exam = L("Examination"); results = L("Results"); dx = L("Assessment");
    meds = null; inv = L("Investigations"); advice = L("Advice");
    fu = { text: (n["Follow-up"] || [])[0] };
  }

  const rxRows = meds
    ? meds.map((m, i) => {
        const code = (m.frequency_code || "").toUpperCase();
        const f = FREQ[code] || (m.prn ? FREQ.SOS : null);
        let grid = f && f.grid ? f.grid : code;
        if (code === "OD" && m.timing) grid = /morning/i.test(m.timing) ? "1-0-0" : /night|bed/i.test(m.timing) ? "0-0-1" : "OD";
        const dur = m.duration_days ? `${m.duration_days} day${m.duration_days === 1 ? "" : "s"}` : m.prn || code === "SOS" ? "As needed" : "Until review";
        const durHi = m.duration_days ? `${m.duration_days} दिन तक` : m.prn || code === "SOS" ? "" : "अगली जांच तक";
        const hi = [f ? f.hi : "", timingHi(m.timing), durHi].filter(Boolean).join(", ");
        const instr = [m.timing, m.instructions].filter(Boolean).map(esc).join("; ");
        return `<tr>
          <td class="n">${i + 1}</td>
          <td class="med"><b>${esc(m.name)}</b> ${esc(m.strength || "")}<div class="muted small">${esc([m.form, m.route].filter(Boolean).join(" · "))}</div></td>
          <td><span class="sch">${esc(grid || "–")}</span><div class="small">${esc(f ? f.en : m.times_per_day ? m.times_per_day + " times a day" : "")}</div></td>
          <td>${esc(dur)}</td>
          <td>${instr || (hi ? "" : '<span class="muted">–</span>')}${hi ? `<span class="hi">${esc(hi)}</span>` : ""}</td>
        </tr>`;
      }).join("")
    : (n["Medication"] || []).map((x, i) => `<tr><td class="n">${i + 1}</td><td colspan="4">${esc(x)}</td></tr>`).join("");

  let fuHtml = "";
  if (fu && (fu.text || fu.in_days)) {
    const date = fu.in_days ? new Date(saved.getTime() + fu.in_days * 86400000) : null;
    fuHtml = `<div class="fu">${date ? `<span class="date">${fmtDate(date)}</span>` : ""}<span>${esc(fu.text || `Review after ${fu.in_days} days`)}</span></div>`;
  }

  const bundleId = (rec.fhir_bundle && rec.fhir_bundle.id) || "";
  const approvedAt = (rec.fhir_bundle && rec.fhir_bundle.timestamp) || rec.saved_at;

  $("#paper").innerHTML = `
    <header class="lh">
      <img src="/static/assets/q-mark.png" alt="">
      <div class="fac">
        <h1>${esc(fac.name)}</h1>
        ${fac.address ? `<div>${esc(fac.address)}</div>` : ""}
        <div>${[fac.phone ? "Ph " + esc(fac.phone) : "", fac.hfr_id ? "HFR ID " + esc(fac.hfr_id) : ""].filter(Boolean).join(" · ")}</div>
      </div>
      <div class="doc">
        <b>${esc(doc.name)}</b>
        ${doc.qualification ? `<div>${esc(doc.qualification)}</div>` : ""}
        ${doc.registration ? `<div>Reg. No. ${esc(doc.registration)}</div>` : ""}
        ${doc.hpr_id ? `<div>HPR ID ${esc(doc.hpr_id)}</div>` : ""}
      </div>
    </header>

    <div class="titlebar">
      <h2>OP Consultation Record &amp; Prescription${isDemo ? '<span class="demo-badge">Demo patient</span>' : ""}</h2>
      <div class="meta">Record <b>${esc(rec.record_id)}</b><br>${esc(fmtDateTime(rec.saved_at))}</div>
    </div>

    <div class="pbox">
      <div><span>Patient</span><b>${esc(p.name)}</b></div>
      <div><span>Age / Sex</span><b>${p.age != null ? esc(p.age) + " y" : "–"} / ${esc(g)}</b></div>
      <div><span>UHID / MRN</span><b class="mono">${esc(p.mrn || "–")}</b></div>
      <div><span>ABHA number</span><b class="mono">${esc(p.abha_number || "Not linked")}</b></div>
      ${p.abha_address ? `<div><span>ABHA address</span><b class="mono">${esc(p.abha_address)}</b></div>` : ""}
      ${p.mobile ? `<div><span>Mobile</span><b>${esc(p.mobile)}</b></div>` : ""}
    </div>

    ${section("Chief complaints", list(complaints))}
    <div class="cols2">
      ${section("History", list(history))}
      ${section("Allergies", list(allergies))}
    </div>
    ${vitals.length ? section("Vitals", `<div class="vit">${vitals.map(([k, v]) => `<div>${k ? `<span>${k}</span>` : ""}<b>${esc(v)}</b></div>`).join("")}</div>`) : ""}
    <div class="cols2">
      ${section("Examination", list(exam))}
      ${section("Results", list(results))}
    </div>
    ${section("Diagnosis", dx.length ? dx.map((x) => `<div class="dx">${x}</div>`).join("") : "")}

    ${rxRows ? `<div class="rx-head"><span class="rx">℞</span><h3>Medicines</h3></div>
    <table class="rx-t"><thead><tr><th></th><th>Medicine</th><th>Schedule</th><th>Duration</th><th>Instructions</th></tr></thead><tbody>${rxRows}</tbody></table>` : ""}

    ${section("Investigations advised", list(inv))}
    ${section("Advice", list(advice))}
    ${section("Follow-up", fuHtml)}

    <footer class="sign">
      <div class="left">
        <p>Drafted by Quantaliez Voice Scribe (AI) from the consultation, then reviewed${rec.edits ? `, edited (${rec.edits}×)` : ""} and approved by the clinician.</p>
        <p>Structured copy: ABDM OP Consult Record, FHIR R4 document bundle ${esc(bundleId.slice(0, 8))}…, shared through ABDM only with the patient's consent.</p>
        ${isDemo ? "<p><b>Synthetic demo patient. Not a real medical record.</b></p>" : ""}
      </div>
      <div class="right">
        <span class="ok">${icon("check")}Electronically approved</span>
        <b>${esc(doc.name)}</b>
        <div>${esc(fmtDateTime(approvedAt))}</div>
      </div>
    </footer>`;
}

$("#btnPrint").onclick = () => window.print();
$("#chkHi").onchange = (e) => document.body.classList.toggle("no-hi", !e.target.checked);
main();
