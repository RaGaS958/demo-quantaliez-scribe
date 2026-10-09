"use strict";
/* Demo HMIS: the host application that launches Voice Scribe and receives the approved record. */

const $ = (s, el = document) => el.querySelector(s);
const $$ = (s, el = document) => [...el.querySelectorAll(s)];
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const icon = (n) => `<svg class="i"><use href="#i-${n}"/></svg>`;

async function api(path, opts) {
  const r = await fetch(path, opts);
  let data = null;
  try { data = await r.json(); } catch { /* empty */ }
  if (!r.ok) {
    const d = data && data.detail;
    const msg = typeof d === "string" ? d : Array.isArray(d) ? d.map((x) => x.msg).join("; ") : `Request failed (${r.status})`;
    throw new Error(msg);
  }
  return data;
}

let lastAdded = null;

async function load() {
  const [cfg, recs] = await Promise.all([api("/api/config"), api("/api/hmis/records")]);
  $("#facility").textContent = `${cfg.facility.name} · OPD · ${cfg.practitioner.name}`;
  $("#mode").textContent = cfg.mode === "live" ? "Scribe: live AI (real voice)" : "Scribe: offline demo";

  const byPatient = {};
  recs.records.forEach((r) => (byPatient[r.patient_id] ||= []).push(r));

  $("#queue").innerHTML = cfg.patients
    .map((p) => {
      const done = byPatient[p.id];
      const g = { male: "M", female: "F", other: "O" }[p.gender] || "";
      const meta = [g, p.age != null ? `${p.age} y` : "", p.mrn].filter(Boolean).map(esc).join(" · ");
      const tag = p.demo ? `<span class="tag-demo" title="Synthetic demo patient">demo</span>` : "";
      const del = p.demo ? "" : `<button class="btn btn-quiet x-del" data-del="${esc(p.id)}" title="Remove this patient">${icon("x")}</button>`;
      return `<tr class="${p.id === lastAdded ? "row-new" : ""}">
        <td class="tok">#${p.token}</td>
        <td class="nw"><b>${esc(p.name)}</b>${tag}<div class="small muted">${meta}</div></td>
        <td class="hide-s nw"><div class="abha">${esc(p.abha_address || "")}</div><div class="abha">${esc(p.abha_number || (p.demo ? "" : "ABHA not linked"))}</div></td>
        <td class="hide-s">${esc(p.reason)}</td>
        <td>${done ? `<span class="status-chip done">${icon("check")}Record saved</span>` : `<span class="status-chip">Waiting</span>`}</td>
        <td class="nw"><a class="btn ${done ? "btn-ghost" : "btn-primary"}" href="/scribe?patient=${encodeURIComponent(p.id)}">${icon("mic")}Launch Voice Scribe</a>${del}</td>
      </tr>`;
    })
    .join("");
  $$("#queue a.btn").forEach((a) => (a.style.textDecoration = "none"));
  $$("#queue [data-del]").forEach((b) =>
    b.addEventListener("click", async () => {
      await api(`/api/hmis/patients/${encodeURIComponent(b.dataset.del)}`, { method: "DELETE" });
      load();
    }));

  const hl = new URLSearchParams(location.search).get("highlight");
  const list = recs.records;
  $("#records").innerHTML = list.length
    ? list
        .map((r) => {
          const n = r.note || {};
          const line = (label) => (n[label] && n[label].length ? `<div class="rl"><b>${label}:</b> ${esc(n[label].join("; "))}</div>` : "");
          return `<div class="rec${r.record_id === hl ? " hl" : ""}" id="${esc(r.record_id)}">
            <div class="rh"><span class="rn">${esc(r.patient_name)}</span><span class="rid">${esc(r.record_id)}</span><span class="rt">${esc(r.saved_at.replace("T", " ").slice(0, 16))}</span></div>
            <div class="small muted">${esc(r.document_type)} · approved by ${esc(r.practitioner)}</div>
            ${line("Chief complaint")}${line("Assessment")}${line("Medication")}${line("Follow-up")}
            <div class="abdm">${icon("exchange")}${esc(r.abdm.status)}</div>
            <div class="rec-actions">
              <a class="btn btn-ghost small" href="/report/${encodeURIComponent(r.record_id)}" target="_blank">${icon("file")}Report / PDF</a>
              <a class="btn btn-quiet small" href="/api/hmis/records/${encodeURIComponent(r.record_id)}/bundle">${icon("download")}FHIR JSON</a>
            </div>
            <details data-id="${esc(r.record_id)}"><summary>View FHIR bundle</summary><div class="fhir-slot small muted" style="margin-top:6px">Loading…</div></details>
          </div>`;
        })
        .join("")
    : `<div class="empty">${icon("file")}No records yet. Launch Voice Scribe for a patient, then approve the draft.</div>`;
  $$("#records a.btn").forEach((a) => (a.style.textDecoration = "none"));

  $$("#records details").forEach((d) =>
    d.addEventListener("toggle", async () => {
      if (!d.open || d.dataset.loaded) return;
      const rec = await api(`/api/hmis/records/${encodeURIComponent(d.dataset.id)}`);
      d.dataset.loaded = "1";
      $(".fhir-slot", d).outerHTML = `<pre class="json" style="max-height:420px;margin-top:8px">${esc(JSON.stringify(rec.fhir_bundle, null, 2))}</pre>`;
    }));

  if (hl && document.getElementById(hl)) document.getElementById(hl).scrollIntoView({ behavior: "smooth", block: "center" });
}

// --- Register a patient ------------------------------------------------------

function toggleForm(show) {
  $("#regForm").classList.toggle("hidden", !show);
  $("#btnAddPatient").classList.toggle("hidden", show);
  if (show) $("#rName").focus();
  $("#regErr").textContent = "";
}

$("#btnAddPatient").onclick = () => toggleForm(true);
$("#rCancel").onclick = () => toggleForm(false);
$("#regForm").onsubmit = async (e) => {
  e.preventDefault();
  const val = (id) => $(id).value.trim();
  const body = {
    name: val("#rName"),
    age: val("#rAge") === "" ? null : Number(val("#rAge")),
    gender: $("#rGender").value,
    mobile: val("#rMobile") || null,
    abha_number: val("#rAbha") || null,
    abha_address: val("#rAbhaAddr") || null,
    reason: val("#rReason") || null,
  };
  if (!body.name) { $("#regErr").textContent = "Name is required."; return; }
  try {
    const p = await api("/api/hmis/patients", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
    lastAdded = p.id;
    $("#regForm").reset();
    toggleForm(false);
    await load();
  } catch (err) {
    $("#regErr").textContent = err.message;
  }
};

$("#btnReset").onclick = async () => {
  await fetch("/api/demo/reset", { method: "POST" });
  history.replaceState(null, "", "/");
  load();
};

load().catch((e) => {
  $("#records").innerHTML = `<div class="empty">Cannot reach the scribe server: ${esc(e.message)}</div>`;
});
