"use strict";
/* Quantaliez Voice Scribe: review workspace (capture -> transcribe -> understand -> build -> approve). */

const $ = (s, el = document) => el.querySelector(s);
const $$ = (s, el = document) => [...el.querySelectorAll(s)];
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const icon = (n, cls = "i") => `<svg class="${cls}"><use href="#i-${n}"/></svg>`;
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

const STEP_DEFAULT = {
  capture: "the conversation",
  transcribe: "in many languages",
  understand: "the medical meaning",
  build: "the ABDM FHIR record",
  approve: "reviews and approves",
};
const FREQS = ["OD", "BD", "TDS", "QID", "HS", "SOS"];
const MIN_CHUNK_MS = 5000;

const S = {
  cfg: null, session: null, patient: null,
  source: "sample", sampleId: null, file: null,
  phase: "ready", // ready | capturing | captured | drafting | review | approved
  draft: null, note: null, bundle: null, validation: null, engine: null,
  editing: false, showRaw: false, open: new Set(),
  segCount: 0, startedAt: 0, skip: false, sampleRunning: false, speaking: false,
  edits: 0, saveTimer: null, saving: null,
};

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

async function api(path, opts = {}) {
  const o = { headers: {}, ...opts };
  if (o.body && !(o.body instanceof FormData)) {
    o.headers["Content-Type"] = "application/json";
    o.body = JSON.stringify(o.body);
  }
  const r = await fetch(path, o);
  let data = null;
  try { data = await r.json(); } catch { /* empty */ }
  if (!r.ok) {
    const d = data && data.detail;
    let msg = typeof d === "string" ? d : `Request failed (${r.status})`;
    if (d && d.message) msg = d.message + (d.errors ? ": " + d.errors.join("; ") : "");
    throw new Error(msg);
  }
  return data;
}

let toastTimer = null;
function toast(msg, kind = "", ms = 4500) {
  let t = $(".toast");
  if (!t) { t = document.createElement("div"); document.body.appendChild(t); }
  t.className = "toast " + kind;
  t.textContent = msg;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => t.remove(), ms);
}

function setStep(name, state, note) {
  const el = $(`.step[data-step="${name}"]`);
  el.classList.remove("active", "done");
  if (state) el.classList.add(state);
  if (note !== undefined) $(".t2", el).innerHTML = note;
}

function setPill(kind, text) {
  const p = $("#statusPill");
  p.className = "pill " + kind;
  p.textContent = text;
}

function scrollBottom(el) { el.scrollTop = el.scrollHeight; }

function live() { return S.cfg.stt.enabled && S.cfg.llm.enabled; }

function prettyModel(m) {
  return String(m || "").replace(/^openai\//, "").replace("whisper-large-v3", "Whisper large-v3");
}

function sysLabel(system) {
  if (!system) return "";
  if (system.includes("snomed")) return "SNOMED";
  if (system.includes("loinc")) return "LOINC";
  return "CODE";
}

function chip(coding) {
  if (!coding) return `<span class="code none" title="Not in the demo terminology table: kept as text for a coder to map">text</span>`;
  return `<span class="code" title="${esc(coding.display)}">${sysLabel(coding.system)} ${esc(coding.code)}</span>`;
}

function getPath(obj, path) {
  return path.split(".").reduce((o, k) => (o == null ? o : o[k]), obj);
}
function setPath(obj, path, value) {
  const keys = path.split(".");
  const last = keys.pop();
  const target = keys.reduce((o, k) => (o[k] ??= {}), obj);
  target[last] = value;
}

// ---------------------------------------------------------------------------
// Init
// ---------------------------------------------------------------------------

async function init() {
  try {
    S.cfg = await api("/api/config");
  } catch (e) {
    toast("Cannot reach the scribe server: " + e.message, "warn", 10000);
    return;
  }
  const params = new URLSearchParams(location.search);
  const pid = params.get("patient") || S.cfg.patients[0].id;
  S.patient = S.cfg.patients.find((p) => p.id === pid) || S.cfg.patients[0];
  S.session = await api("/api/sessions", { method: "POST", body: { patient_id: S.patient.id } });
  S.sampleId = params.get("sample") || S.patient.sample || S.cfg.samples[0].id;
  const src = params.get("source");
  if (src && ["sample", "mic", "upload", "livekit"].includes(src) && (src === "sample" || live())) S.source = src;
  else if (live() && !S.patient.demo) S.source = "mic"; // your own patients: real voice by default

  renderHeader();
  renderSources();
  renderEmpty();
  bind();
  refreshAudit();
}

function renderHeader() {
  const p = S.patient;
  const g = { male: "M", female: "F", other: "O" }[p.gender] || "";
  $("#ctx").innerHTML =
    `${icon("id")}<b>${esc(p.name)}</b><span class="sep">·</span>${[g, p.age != null ? p.age + " y" : ""].filter(Boolean).join(", ")}` +
    `<span class="sep">·</span><span class="abha">${p.abha_number ? "ABHA " + esc(p.abha_number) : esc(p.mrn || "ABHA not linked")}</span>` +
    `<span class="sep">·</span><span>${esc(S.cfg.practitioner.name)}</span>`;
  $("#sessionId").textContent = S.session.id;
  const m = $("#mode");
  if (live()) {
    m.className = "mode live";
    $(".lbl", m).textContent = `Live · ${prettyModel(S.cfg.stt.model)} + ${prettyModel(S.cfg.llm.model)} · ${S.cfg.llm.host}`;
    m.title = "Open-weight models through an OpenAI-compatible endpoint. In production: vLLM inside NHA cloud.";
  } else {
    m.className = "mode offline";
    $(".lbl", m).textContent = "Offline demo · built-in sample consultations";
    m.title = "Add GROQ_API_KEY to .env for live microphone, upload and AI drafting.";
  }
}

function renderSources() {
  $$("#sources button").forEach((b) => {
    const src = b.dataset.src;
    const needsLive = src !== "sample" && !live();
    b.disabled = needsLive || S.phase !== "ready";
    b.title = needsLive ? "Live mode only: add GROQ_API_KEY to .env" : "";
    b.classList.toggle("on", src === S.source);
  });
  renderOpts();
}

function renderOpts() {
  const o = $("#opts");
  const start = $("#btnStart");
  start.innerHTML = icon(S.source === "upload" ? "upload" : "mic") + (S.source === "upload" ? "Transcribe and draft" : "Start consultation");
  if (S.source === "sample") {
    o.innerHTML =
      `<select id="sampleSel" aria-label="Sample consultation">${S.cfg.samples
        .map((s) => `<option value="${s.id}" ${s.id === S.sampleId ? "selected" : ""}>${esc(s.title)} · ${esc(s.subtitle)}</option>`)
        .join("")}</select>` +
      `<div class="note">${live()
        ? "Plays a scripted consultation; the transcript is sent to the live model for understanding."
        : "Offline demo: plays a scripted consultation and uses its pre-computed draft. Add GROQ_API_KEY for live AI."}</div>`;
    $("#sampleSel").onchange = (e) => (S.sampleId = e.target.value);
  } else if (S.source === "mic") {
    o.innerHTML = `<div class="note">Speak naturally: Hindi, English or both. Speech is cut at pauses and transcribed by ${esc(prettyModel(S.cfg.stt.model))}; doctor and patient roles are assigned when the consultation ends.</div>`;
  } else if (S.source === "upload") {
    o.innerHTML =
      `<label class="dropzone" id="drop"><input type="file" id="file" accept="audio/*,.wav,.mp3,.flac,.m4a,.ogg,.webm" hidden>` +
      `<b>Choose an audio file</b> or drop it here<br><span class="small">WAV, MP3, FLAC, M4A, OGG, WEBM · up to ${S.cfg.max_upload_mb} MB</span>` +
      `<div id="fileName" class="small" style="margin-top:6px;color:var(--ink)">${S.file ? esc(S.file.name) : ""}</div></label>`;
    const drop = $("#drop");
    const pick = (f) => { if (f) { S.file = f; $("#fileName").textContent = f.name; } };
    $("#file").onchange = (e) => pick(e.target.files[0]);
    drop.ondragover = (e) => { e.preventDefault(); drop.classList.add("drag"); };
    drop.ondragleave = () => drop.classList.remove("drag");
    drop.ondrop = (e) => { e.preventDefault(); drop.classList.remove("drag"); pick(e.dataTransfer.files[0]); };
  } else if (S.source === "livekit") {
    o.innerHTML = `<div class="note" id="lkNote">Ambient capture through your LiveKit agent. It will start automatically when you click <b>Start consultation</b>.</div>`;
  }
}

function renderEmpty() {
  $("#transcript").innerHTML = `<div class="empty">${icon("mic")}Press <b>Start consultation</b>. The scribe listens in the background while the doctor talks to the patient.</div>`;
  $("#note").innerHTML = `<div class="empty">${icon("pencil")}The draft note appears here when the consultation ends. The doctor can edit every field.</div>`;
  $("#fhir").innerHTML = `<div class="empty">${icon("file")}An ABDM <b>OP Consult Record</b> (FHIR R4 document bundle) is built from the note and validated on every edit.</div>`;
}

function bind() {
  $$("#sources button").forEach((b) =>
    b.addEventListener("click", () => {
      if (b.disabled) return;
      S.source = b.dataset.src;
      renderSources();
    }));
  $("#btnStart").onclick = onStart;
  $("#btnSkip").onclick = () => (S.skip = true);
  $("#chkEn").onchange = (e) => $("#transcript").classList.toggle("no-en", !e.target.checked);
  $("#btnEdit").onclick = toggleEdit;
  $("#btnApprove").onclick = approve;
  $("#btnRaw").onclick = () => { S.showRaw = !S.showRaw; $("#btnRaw").textContent = S.showRaw ? "Resources" : "Raw JSON"; renderFhir(); };
  $("#btnDl").onclick = () => (location.href = `/api/sessions/${S.session.id}/bundle`);
  $("#btnAudit").onclick = () => { refreshAudit(); $("#drawer").classList.add("open"); };
  $("#btnCloseDrawer").onclick = () => $("#drawer").classList.remove("open");
  $("#typedAdd").onclick = addTyped;
  $("#typedInput").onkeydown = (e) => { if (e.key === "Enter") addTyped(); };
  document.addEventListener("keydown", (e) => { if (e.key === "Escape") $("#drawer").classList.remove("open"); });
}

// ---------------------------------------------------------------------------
// Capture
// ---------------------------------------------------------------------------

async function onStart() {
  if (S.phase === "ready") {
    if (S.source === "upload") return runUpload();
    beginCapture();
    if (S.source === "sample") playSample();
    else if (S.source === "mic") startMic();
    else if (S.source === "livekit") startLivekit();
  } else if (S.phase === "capturing") {
    endCapture();
  } else if (S.phase === "captured") {
    draftNow();
  }
}

function beginCapture() {
  S.phase = "capturing";
  S.startedAt = performance.now();
  renderSources();
  $("#capture").classList.add("capturing");
  $("#transcript").innerHTML = "";
  setStep("capture", "active", "listening…");
  setStep("transcribe", "active", S.source === "sample" ? "streaming…" : "waiting for speech…");
  setPill("listening", "Listening…");
  const b = $("#btnStart");
  b.innerHTML = icon("stop") + "End consultation";
  b.classList.replace("btn-primary", "btn-orange");
  if (S.source === "mic" || S.source === "livekit") $("#typed").classList.remove("hidden");
  $("#trLabel").textContent = "Transcript · live";
  startWave();
  refreshAudit();
}

function addTurnEl(speaker, text, english, src, pending = false) {
  const empty = $("#transcript .empty");
  if (empty) empty.remove();
  const el = document.createElement("div");
  const role = (speaker || "").toLowerCase();
  el.className = `turn ${role}${pending ? " pending" : ""}`;
  const srcTag = src && src !== "sample" ? `<span class="src">${esc(src)}</span>` : "";
  el.innerHTML =
    `<div class="who">${esc(speaker || "Speaker")}${srcTag}</div>` +
    `<div class="text">${esc(text)}</div>` +
    (english ? `<div class="en">${esc(english)}</div>` : "");
  $("#transcript").appendChild(el);
  scrollBottom($("#trBody"));
  return el;
}

async function postSegments(segments, source, sampleId) {
  return api(`/api/sessions/${S.session.id}/segments`, {
    method: "POST",
    body: { segments, source, sample_id: sampleId || null },
  });
}

function bumpSegments(n, latency) {
  S.segCount += n;
  setStep("transcribe", "active", `${S.segCount} utterance${S.segCount === 1 ? "" : "s"}` + (latency != null ? ` · <span class="ms">${latency}s</span>` : ""));
}

// --- Sample consultation (scripted, for offline demos) ---------------------

async function playSample() {
  const sample = S.cfg.samples.find((s) => s.id === S.sampleId);
  S.sampleRunning = true;
  S.skip = false;
  $("#btnSkip").classList.remove("hidden");
  for (const seg of sample.segments) {
    const el = addTurnEl(seg.speaker, "", null, "sample");
    const textEl = $(".text", el);
    const words = seg.text.split(" ");
    S.speaking = true;
    for (let i = 0; i < words.length && !S.skip; i++) {
      textEl.innerHTML = esc(words.slice(0, i + 1).join(" ")) + '<span class="caret"></span>';
      scrollBottom($("#trBody"));
      await sleep(140 + Math.random() * 70);
    }
    textEl.textContent = seg.text;
    S.speaking = false;
    await postSegments([{ text: seg.text, speaker: seg.speaker, english: seg.english }], "sample", sample.id);
    bumpSegments(1);
    if (!S.skip) await sleep(380 + Math.random() * 200);
  }
  S.sampleRunning = false;
  $("#btnSkip").classList.add("hidden");
  await endCapture();
}

// --- Microphone: utterances are cut at pauses (simple energy VAD) ----------

function pickMime() {
  const c = ["audio/webm;codecs=opus", "audio/webm", "audio/mp4", "audio/ogg;codecs=opus"];
  return window.MediaRecorder ? c.find((m) => MediaRecorder.isTypeSupported(m)) || "" : "";
}
function extFor(type) {
  if (/mp4|m4a|aac/.test(type)) return "mp4";
  if (/ogg/.test(type)) return "ogg";
  return "webm";
}

async function startMic() {
  if (!navigator.mediaDevices || !window.MediaRecorder) {
    toast("This browser cannot record audio. Use Chrome or Edge, or the Upload / Sample source.", "warn");
    return resetToReady();
  }
  try {
    S.stream = await navigator.mediaDevices.getUserMedia({ audio: { echoCancellation: true, noiseSuppression: true, autoGainControl: true } });
  } catch (e) {
    toast("Microphone unavailable: " + e.message, "warn");
    return resetToReady();
  }
  S.actx = new (window.AudioContext || window.webkitAudioContext)();
  const src = S.actx.createMediaStreamSource(S.stream);
  S.analyser = S.actx.createAnalyser();
  S.analyser.fftSize = 2048;
  src.connect(S.analyser);
  S.vad = { noise: 0.008, lastVoice: 0 };
  S.mime = pickMime();
  S.uploadChain = Promise.resolve();
  startRecorder();
  S.vadTimer = setInterval(vadTick, 50);
  setStep("capture", "active", "microphone on");
}

function startRecorder() {
  const rec = new MediaRecorder(S.stream, S.mime ? { mimeType: S.mime } : undefined);
  const chunks = [];
  const meta = { hadSpeech: false, start: performance.now() };
  rec.ondataavailable = (e) => e.data.size && chunks.push(e.data);
  rec.onstop = () => {
    const dur = performance.now() - meta.start;
    if (meta.hadSpeech && dur > 600) queueUpload(new Blob(chunks, { type: rec.mimeType || S.mime || "audio/webm" }));
  };
  rec.start();
  S.rec = rec;
  S.recMeta = meta;
}

function micLevel() {
  const buf = new Float32Array(S.analyser.fftSize);
  S.analyser.getFloatTimeDomainData(buf);
  let s = 0;
  for (const v of buf) s += v * v;
  return Math.sqrt(s / buf.length);
}

function vadTick() {
  if (!S.analyser || !S.rec) return;
  const rms = micLevel();
  S.level = rms;
  const v = S.vad;
  const now = performance.now();
  const thr = Math.max(0.012, v.noise * 2.8);
  if (rms > thr) {
    v.lastVoice = now;
    S.recMeta.hadSpeech = true;
  } else {
    v.noise = v.noise * 0.97 + rms * 0.03;
  }
  S.speaking = now - v.lastVoice < 300;
  const segDur = now - S.recMeta.start;
  // Cut at a pause once the chunk is >= 5 s (keeps request rate low on free API tiers), or force at 25 s.
  if (S.recMeta.hadSpeech && ((now - v.lastVoice > 800 && segDur > MIN_CHUNK_MS) || segDur > 25000)) cutSegment();
  else if (!S.recMeta.hadSpeech && segDur > 15000) cutSegment(); // drop long silences
}

function cutSegment() {
  const old = S.rec;
  startRecorder();
  try { old.stop(); } catch { /* already stopped */ }
}

function queueUpload(blob) {
  const ph = addTurnEl(null, "Transcribing…", null, "mic", true);
  setStep("transcribe", "active", "transcribing…");
  S.uploadChain = S.uploadChain.then(async () => {
    const fd = new FormData();
    fd.append("file", blob, "utterance." + extFor(blob.type));
    fd.append("mode", "segment");
    try {
      const r = await api(`/api/sessions/${S.session.id}/audio`, { method: "POST", body: fd });
      if (!r.added.length) { ph.remove(); return; }
      $(".text", ph).textContent = r.added.map((x) => x.text).join(" ");
      ph.classList.remove("pending");
      bumpSegments(r.added.length, r.latency_s);
    } catch (e) {
      ph.remove();
      toast(e.message, "warn");
    }
  });
}

async function stopMic() {
  clearInterval(S.vadTimer);
  const r = S.rec;
  S.rec = null;
  if (r && r.state !== "inactive") {
    await new Promise((res) => {
      const prev = r.onstop;
      r.onstop = () => { prev && prev(); res(); };
      r.stop();
    });
  }
  S.stream && S.stream.getTracks().forEach((t) => t.stop());
  S.actx && S.actx.close();
  S.analyser = null;
  await S.uploadChain;
}

// --- LiveKit ambient agent ------------------------------------------------

async function startLivekit() {
  try { await api("/api/ambient/start", { method: "POST" }); } catch (e) { console.error(e); }
  try {
    const r = await api("/api/ambient/segments?after=999999999");
    S.lkAfter = r.last_id;
  } catch { S.lkAfter = 0; }
  S.lkTimer = setInterval(pollLivekit, 1000);
  pollLivekit();
}

async function pollLivekit() {
  let r;
  try { r = await api(`/api/ambient/segments?after=${S.lkAfter}`); } catch { return; }
  const note = $("#lkNote");
  if (note) {
    note.innerHTML = r.connected
      ? `<b style="color:var(--teal)">● LiveKit agent connected</b>${r.room ? " · room " + esc(r.room) : ""}`
      : `Starting the ambient agent...`;
  }
  if (r.segments.length) {
    S.lkAfter = r.last_id;
    const segs = r.segments.map((s) => ({ text: s.text, speaker: s.speaker }));
    await postSegments(segs, "livekit");
    segs.forEach((s) => addTurnEl(s.speaker, s.text, null, "livekit"));
    bumpSegments(segs.length);
    S.speaking = true;
    clearTimeout(S.lkSpeak);
    S.lkSpeak = setTimeout(() => (S.speaking = false), 1500);
  }
}

// --- Typed fallback -------------------------------------------------------

async function addTyped() {
  const inp = $("#typedInput");
  const text = inp.value.trim();
  if (!text || S.phase !== "capturing") return;
  inp.value = "";
  await postSegments([{ text }], "typed");
  addTurnEl(null, text, null, "typed");
  bumpSegments(1);
}

// --- Upload ---------------------------------------------------------------

async function runUpload() {
  if (!S.file) return toast("Choose an audio file first.", "warn");
  S.phase = "capturing";
  renderSources();
  $("#btnStart").disabled = true;
  $("#transcript").innerHTML = "";
  setStep("capture", "done", esc(S.file.name));
  setStep("transcribe", "active", "transcribing file…");
  setPill("listening", "Transcribing…");
  const fd = new FormData();
  fd.append("file", S.file, S.file.name);
  fd.append("mode", "file");
  try {
    const r = await api(`/api/sessions/${S.session.id}/audio`, { method: "POST", body: fd });
    if (!r.added.length) throw new Error("No speech found in the file.");
    r.added.forEach((seg) => addTurnEl(null, seg.text, null, "upload"));
    S.segCount = r.added.length;
    setStep("transcribe", "done", `${r.added.length} segments · <span class="ms">${r.latency_s}s</span>`);
  } catch (e) {
    toast(e.message, "warn");
    return resetToReady();
  }
  await draftNow();
}

function resetToReady() {
  S.phase = "ready";
  S.lkTimer && clearInterval(S.lkTimer);
  if (S.source === "livekit") api("/api/ambient/stop", { method: "POST" }).catch(()=>{});
  stopWave();
  $("#capture").classList.remove("capturing");
  const b = $("#btnStart");
  b.disabled = false;
  b.classList.replace("btn-orange", "btn-primary");
  ["capture", "transcribe", "understand", "build", "approve"].forEach((s) => setStep(s, null, STEP_DEFAULT[s]));
  setPill("", "Ready");
  renderSources();
}

async function endCapture() {
  if (S.phase !== "capturing") return;
  if (S.source === "sample" && S.sampleRunning) { S.skip = true; return; } // finish posting the script first
  $("#btnStart").disabled = true;
  $("#btnSkip").classList.add("hidden");
  $("#typed").classList.add("hidden");
  if (S.source === "mic") { setPill("drafting", "Finishing transcription…"); await stopMic(); }
  if (S.source === "livekit") { clearInterval(S.lkTimer); await pollLivekit(); await api("/api/ambient/stop", { method: "POST" }); }
  stopWave();
  $("#capture").classList.remove("capturing");
  if (!S.segCount) {
    toast("Nothing was captured. Try again, or type a line.", "warn");
    return resetToReady();
  }
  const secs = Math.round((performance.now() - S.startedAt) / 1000);
  setStep("capture", "done", `${secs}s captured`);
  setStep("transcribe", "done", $(`.step[data-step="transcribe"] .t2`).innerHTML.replace("…", ""));
  await draftNow();
}

// ---------------------------------------------------------------------------
// Understand + build
// ---------------------------------------------------------------------------

async function draftNow() {
  S.phase = "drafting";
  $("#btnStart").disabled = true;
  setStep("understand", "active", "interpreting…");
  setPill("drafting", "Drafting the record…");
  $("#note").innerHTML = skeleton();
  $("#fhir").innerHTML = skeleton();
  try {
    const out = await api(`/api/sessions/${S.session.id}/draft`, { method: "POST" });
    S.engine = out.engine;
    applyOut(out, true);
    const offline = /offline/i.test(out.engine || "");
    setStep("understand", "done", offline ? "cached draft (offline)" : `${prettyModel(S.cfg.llm.model)} · <span class="ms">${out.timings.understand_s}s</span>`);
    setStep("build", "active", "building…");
    await sleep(300);
    const n = Object.values(out.validation.resource_counts).reduce((a, b) => a + b, 0);
    setStep("build", "done", `${n} resources · <span class="ms">${out.timings.build_ms} ms</span>`);
    setStep("approve", "active", "awaiting approval");
    setPill("awaiting", "Awaiting approval");
    S.phase = "review";
    $("#capture").classList.add("hidden");
    const srcLabel = { sample: "built-in sample", mic: "microphone", upload: "uploaded audio", livekit: "LiveKit ambient agent" }[S.source];
    $("#captured").innerHTML = `Captured from ${srcLabel} · ${S.segCount} utterance${S.segCount === 1 ? "" : "s"} · speaker roles and translation by the understanding step`;
    $("#captured").classList.remove("hidden");
    $("#btnEdit").disabled = false;
    $("#btnRaw").disabled = false;
    $("#btnDl").disabled = false;
    $("#btnApprove").disabled = !out.validation.valid;
    if (out.notice && live()) toast("Live model unavailable, so this shows the cached draft for this sample. " + out.notice, "warn", 8000);
  } catch (e) {
    toast(e.message, "warn", 8000);
    setStep("understand", null, STEP_DEFAULT.understand);
    setPill("", "Captured");
    $("#note").innerHTML = `<div class="empty">${icon("alert")}${esc(e.message)}</div>`;
    $("#fhir").innerHTML = "";
    S.phase = "captured";
    const b = $("#btnStart");
    b.disabled = false;
    b.innerHTML = icon("steth") + "Retry drafting";
  }
  refreshAudit();
}

function skeleton() {
  const bar = (w) => `<div style="height:12px;width:${w}%;border-radius:6px;background:linear-gradient(90deg,#eef3f2,#e0eae8,#eef3f2);background-size:200% 100%;animation:shimmer 1.2s linear infinite;margin:12px 0"></div>`;
  return `<style>@keyframes shimmer{to{background-position:-200% 0}}</style>${bar(40)}${bar(85)}${bar(30)}${bar(70)}${bar(55)}${bar(80)}`;
}

function applyOut(out, first) {
  const prev = S.bundle;
  S.draft = out.draft;
  S.note = out.note;
  S.bundle = out.bundle;
  S.validation = out.validation;
  if (first) renderTurns(out.draft.turns);
  if (out.draft.languages) $("#trLabel").textContent = "Transcript · " + out.draft.languages;
  if (!S.editing || first) renderNote();
  renderFhir(prev);
}

function renderTurns(turns) {
  if (!turns || !turns.length) return;
  $("#transcript").innerHTML = "";
  turns.forEach((t) => {
    const same = !t.english || t.english.trim().toLowerCase() === t.text.trim().toLowerCase();
    addTurnEl(t.speaker, t.text, same ? null : t.english, null);
  });
  $("#trBody").scrollTop = 0;
}

// ---------------------------------------------------------------------------
// Note (view + edit)
// ---------------------------------------------------------------------------

function renderNote() {
  const el = $("#note");
  el.classList.toggle("editing", S.editing);
  if (S.editing) return renderEditor();
  const d = S.draft;
  const n = S.note;
  let html = "";
  if (d.needs_review && d.needs_review.length && S.phase !== "approved") {
    html += `<div class="flags"><div class="lbl">${icon("alert")}Scribe flagged for review</div><ul>${d.needs_review.map((x) => `<li>${esc(x)}</li>`).join("")}</ul></div>`;
  }
  const sec = (label, items) =>
    items && items.length ? `<div class="nsec"><div class="lbl">${label}</div><div class="val">${items.map((x) => `<div>${x}</div>`).join("")}</div></div>` : "";
  const withChips = (lines, objs) => (lines || []).map((l, i) => esc(l) + (objs && objs[i] ? chip(objs[i].coding) : ""));
  html += sec("Chief complaint", withChips(n["Chief complaint"], d.chief_complaints));
  html += sec("History", withChips(n["History"]));
  html += sec("Allergies", withChips(n["Allergies"]));
  html += sec("Vitals", n["Vitals"] && n["Vitals"].length ? [esc(n["Vitals"].join("  ·  "))] : []);
  html += sec("Examination", withChips(n["Examination"]));
  html += sec("Results", withChips(n["Results"], d.results));
  html += sec("Assessment", withChips(n["Assessment"], d.assessment));
  html += sec("Medication", withChips(n["Medication"]));
  html += sec("Investigations", withChips(n["Investigations"], d.investigations));
  html += sec("Advice", withChips(n["Advice"]));
  html += sec("Follow-up", withChips(n["Follow-up"]));
  if (d.summary) html += `<div class="nsec"><div class="lbl">Summary</div><div class="summary">${esc(d.summary)}</div></div>`;
  el.innerHTML = html || `<div class="empty">Nothing extracted.</div>`;
}

function inp(path, { cls = "grow", type = "text", ph = "" } = {}) {
  const v = getPath(S.draft, path);
  return `<input class="${cls}" type="${type}" data-path="${path}" data-type="${type}" value="${esc(v ?? "")}" placeholder="${esc(ph)}"${type === "number" ? ' step="any"' : ""}>`;
}
function del(path) {
  return `<button class="x" data-del="${path}" title="Remove">${icon("x")}</button>`;
}
function add(list, label) {
  return `<button class="add" data-add="${list}">+ ${label}</button>`;
}

function renderEditor() {
  const d = S.draft;
  const listRows = (key, label, render, addLabel) =>
    `<div class="nsec"><div class="lbl">${label}</div><div class="val">${(d[key] || []).map((_, i) => render(i)).join("")}${add(key, addLabel)}</div></div>`;
  const strRow = (key) => (i) => `<div class="ed-row">${inp(`${key}.${i}`)}${del(`${key}.${i}`)}</div>`;

  let h = "";
  h += listRows("chief_complaints", "Chief complaint", (i) =>
    `<div class="ed-row">${inp(`chief_complaints.${i}.text`, { ph: "Complaint" })}${inp(`chief_complaints.${i}.duration`, { cls: "w-m", ph: "Duration" })}<span data-chip="chief_complaints.${i}">${chip(d.chief_complaints[i].coding)}</span>${del(`chief_complaints.${i}`)}</div>` +
    `<div class="ed-row">${inp(`chief_complaints.${i}.details`, { ph: "Details / negatives" })}</div>`, "Add complaint");
  h += listRows("history", "History", strRow("history"), "Add history");
  h += `<div class="nsec"><div class="lbl">Allergies</div><div class="val">
        <label class="toggle" style="margin-bottom:6px"><input type="checkbox" data-path="no_known_allergies" data-type="bool" ${d.no_known_allergies ? "checked" : ""}> No known allergies</label>
        ${(d.allergies || []).map((_, i) => strRow("allergies")(i)).join("")}${add("allergies", "Add allergy")}</div></div>`;
  const vit = (k, label) => `<label>${label}${inp(`vitals.${k}`, { type: "number", cls: "" })}</label>`;
  h += `<div class="nsec"><div class="lbl">Vitals</div><div class="vgrid">
        ${vit("temperature_f", "Temp °F")}${vit("pulse_bpm", "Pulse /min")}${vit("spo2_pct", "SpO2 %")}
        ${vit("bp_systolic", "BP systolic")}${vit("bp_diastolic", "BP diastolic")}${vit("resp_rate", "Resp. rate")}
        ${vit("weight_kg", "Weight kg")}</div></div>`;
  h += listRows("examination", "Examination", strRow("examination"), "Add finding");
  h += listRows("results", "Results", (i) =>
    `<div class="ed-row">${inp(`results.${i}.text`, { ph: "Test" })}${inp(`results.${i}.value`, { cls: "w-s", ph: "Value" })}${inp(`results.${i}.unit`, { cls: "w-s", ph: "Unit" })}<span data-chip="results.${i}">${chip(d.results[i].coding)}</span>${del(`results.${i}`)}</div>`, "Add result");
  h += listRows("assessment", "Assessment", (i) =>
    `<div class="ed-row">${inp(`assessment.${i}.text`, { ph: "Diagnosis" })}
      <select data-path="assessment.${i}.status" class="w-m">${["provisional", "confirmed"].map((s) => `<option ${d.assessment[i].status === s ? "selected" : ""}>${s}</option>`).join("")}</select>
      <span data-chip="assessment.${i}">${chip(d.assessment[i].coding)}</span>${del(`assessment.${i}`)}</div>`, "Add diagnosis");
  h += listRows("medications", "Medication", (i) => {
    const m = d.medications[i];
    return `<div class="med-card">
      <div class="ed-row">${inp(`medications.${i}.name`, { ph: "Medicine" })}${inp(`medications.${i}.strength`, { cls: "w-m", ph: "Strength" })}${inp(`medications.${i}.form`, { cls: "w-m", ph: "Form" })}${del(`medications.${i}`)}</div>
      <div class="ed-row">
        <select data-path="medications.${i}.frequency_code" class="w-m" title="Frequency">${["", ...FREQS].map((f) => `<option value="${f}" ${(m.frequency_code || "") === f ? "selected" : ""}>${f || "Frequency"}</option>`).join("")}</select>
        ${inp(`medications.${i}.duration_days`, { cls: "w-s", type: "number", ph: "Days" })}
        ${inp(`medications.${i}.timing`, { ph: "Timing (e.g. after food)" })}
      </div></div>`;
  }, "Add medicine");
  h += listRows("investigations", "Investigations", (i) =>
    `<div class="ed-row">${inp(`investigations.${i}.text`, { ph: "Test" })}${inp(`investigations.${i}.condition`, { ph: "Condition (optional)" })}<span data-chip="investigations.${i}">${chip(d.investigations[i].coding)}</span>${del(`investigations.${i}`)}</div>`, "Add investigation");
  h += listRows("advice", "Advice", strRow("advice"), "Add advice");
  h += `<div class="nsec"><div class="lbl">Follow-up</div><div class="ed-row">${inp("follow_up.in_days", { cls: "w-s", type: "number", ph: "Days" })}${inp("follow_up.text", { ph: "Follow-up instruction" })}</div></div>`;

  const el = $("#note");
  el.innerHTML = h;
  $$("[data-path]", el).forEach((x) => {
    const evt = x.tagName === "SELECT" || x.type === "checkbox" ? "change" : "input";
    x.addEventListener(evt, () => {
      let val = x.value;
      if (x.dataset.type === "bool") val = x.checked;
      else if (x.dataset.type === "number") val = val === "" ? null : Number(val);
      else if (val === "") val = null;
      const path = x.dataset.path;
      if (path.endsWith(".frequency_code")) {
        const i = path.split(".")[1];
        const per = { OD: 1, BD: 2, TDS: 3, QID: 4, HS: 1 }[val] || null;
        S.draft.medications[i].times_per_day = per;
        S.draft.medications[i].prn = val === "SOS";
      }
      if (/^(advice|history|examination|allergies)\.\d+$/.test(path) && val === null) val = "";
      setPath(S.draft, path, val);
      scheduleSave();
    });
  });
  $$("[data-add]", el).forEach((b) =>
    b.addEventListener("click", () => {
      const key = b.dataset.add;
      const tpl = {
        chief_complaints: { text: "", duration: null, details: null },
        assessment: { text: "", status: "provisional" },
        medications: { name: "", strength: null, form: "tablet", route: "oral", frequency_code: null, times_per_day: null, duration_days: null, timing: null, prn: false },
        investigations: { text: "", condition: null },
        results: { text: "", value: null, unit: null },
      }[key];
      (S.draft[key] ||= []).push(tpl ? structuredClone(tpl) : "");
      renderEditor();
      const last = $$(`[data-path^="${key}."]`, el).filter((x) => x.tagName === "INPUT").slice(-1)[0];
      if (last) last.focus();
    }));
  $$("[data-del]", el).forEach((b) =>
    b.addEventListener("click", () => {
      const [key, idx] = b.dataset.del.split(".");
      S.draft[key].splice(Number(idx), 1);
      renderEditor();
      scheduleSave(0);
    }));
}

function cleanDraft(d) {
  const c = structuredClone(d);
  const keepText = (arr, k) => (arr || []).filter((x) => (k ? (x[k] || "").trim() : (x || "").trim()));
  c.chief_complaints = keepText(c.chief_complaints, "text");
  c.assessment = keepText(c.assessment, "text");
  c.medications = keepText(c.medications, "name");
  c.investigations = keepText(c.investigations, "text");
  c.results = keepText(c.results, "text");
  ["history", "allergies", "examination", "advice"].forEach((k) => (c[k] = keepText(c[k])));
  return c;
}

function scheduleSave(delay = 450) {
  clearTimeout(S.saveTimer);
  $("#saving").textContent = "Editing…";
  S.saveTimer = setTimeout(save, delay);
}

async function save() {
  S.saveTimer = null;
  $("#saving").textContent = "Rebuilding FHIR…";
  const p = api(`/api/sessions/${S.session.id}/draft`, { method: "PUT", body: { draft: cleanDraft(S.draft) } });
  S.saving = p;
  try {
    const out = await p;
    const prev = S.bundle;
    S.note = out.note;
    S.bundle = out.bundle;
    S.validation = out.validation;
    // pick up re-grounded codes without disturbing the field being typed in
    ["chief_complaints", "assessment", "investigations", "results"].forEach((k) => {
      const server = out.draft[k] || [];
      (S.draft[k] || []).forEach((item) => {
        const match = server.find((x) => x.text === item.text);
        item.coding = match ? match.coding : null;
      });
    });
    $$("[data-chip]").forEach((el) => {
      const item = getPath(S.draft, el.dataset.chip);
      el.innerHTML = chip(item && item.coding);
    });
    if (out.changed && out.changed.length) S.edits += 1;
    renderFhir(prev);
    $("#btnApprove").disabled = !out.validation.valid;
    $("#saving").textContent = out.changed && out.changed.length ? "Saved · FHIR rebuilt and re-validated" : "No changes";
  } catch (e) {
    $("#saving").textContent = "";
    toast(e.message, "warn");
  } finally {
    S.saving = null;
  }
}

async function toggleEdit() {
  if (S.phase !== "review") return;
  if (S.editing) {
    if (S.saveTimer) { clearTimeout(S.saveTimer); await save(); }
    if (S.saving) await S.saving;
    S.editing = false;
    $("#btnEdit").textContent = "Edit";
  } else {
    S.editing = true;
    $("#btnEdit").textContent = "Done editing";
  }
  renderNote();
}

// ---------------------------------------------------------------------------
// FHIR panel
// ---------------------------------------------------------------------------

function hl(json) {
  return esc(json).replace(
    /(&quot;(?:\\.|[^&\\]|&(?!quot;))*?&quot;)(\s*:)?|\b(true|false|null)\b|-?\b\d+(?:\.\d+)?(?:[eE][+-]?\d+)?\b/g,
    (m, str, colon, lit) => {
      if (str) return colon ? `<span class="j-k">${str}</span>${colon}` : `<span class="j-s">${str}</span>`;
      if (lit) return `<span class="j-b">${m}</span>`;
      return `<span class="j-n">${m}</span>`;
    });
}

function qty(q) { return q ? `${q.value} ${q.unit || ""}`.trim() : ""; }

function summarize(r) {
  const t = r.resourceType;
  const code = r.code || {};
  const coded = code.coding && code.coding[0] ? ` · ${sysLabel(code.coding[0].system)} ${code.coding[0].code}` : "";
  switch (t) {
    case "Composition": return `${r.title} · ${r.section.length} sections`;
    case "Patient": return `${r.name[0].text}${r.identifier[0] ? " · ABHA " + r.identifier[0].value : ""}`;
    case "Practitioner": return `${r.name[0].text} · HPR ${r.identifier[0].value}`;
    case "Organization": return `${r.name} · HFR ${r.identifier[0].value}`;
    case "Encounter": return `Outpatient (AMB)${r.diagnosis ? " · " + r.diagnosis.length + " diagnosis" : ""}`;
    case "Condition": return `${code.text}${coded}`;
    case "AllergyIntolerance": return `${code.text}${coded}`;
    case "Observation":
      if (r.component) return `${code.text}: ${r.component.map((c) => c.valueQuantity.value).join("/")} mmHg`;
      return `${code.text}${r.valueQuantity ? ": " + qty(r.valueQuantity) : r.valueString ? ": " + r.valueString : ""}`;
    case "MedicationRequest": return r.dosageInstruction[0].text;
    case "ServiceRequest": return `${code.text}${coded}`;
    case "Appointment": return `${r.description}${r.requestedPeriod ? " · " + r.requestedPeriod[0].start : ""}`;
    default: return "";
  }
}

function statusOf(r) {
  if (r.status) return r.status;
  if (r.verificationStatus) return r.verificationStatus.coding[0].code;
  if (r.category && r.category[0] && r.category[0].coding) return r.category[0].coding[0].code.replace("encounter-diagnosis", "diagnosis").replace("problem-list-item", "complaint");
  return "";
}

function renderFhir(prev) {
  const el = $("#fhir");
  const b = S.bundle;
  const v = S.validation;
  if (!b) return;
  const total = b.entry.length;
  const banner = v.valid
    ? `<div class="vbanner ok">${icon("check")}<div><b>Structural check passed</b> · ${v.rules_checked} rules · ${v.warnings.length} warning${v.warnings.length === 1 ? "" : "s"} · ${v.info.length} note${v.info.length === 1 ? "" : "s"}</div><button class="more" id="vMore">Details</button></div>`
    : `<div class="vbanner err">${icon("alert")}<div><b>${v.errors.length} error${v.errors.length === 1 ? "" : "s"}</b>: fix before approval</div><button class="more" id="vMore">Details</button></div>`;
  const details =
    `<div class="vdetails hidden" id="vDetails">` +
    (v.errors.length ? `<b class="e">Errors</b><ul>${v.errors.map((x) => `<li class="e">${esc(x)}</li>`).join("")}</ul>` : "") +
    (v.warnings.length ? `<b class="w">Warnings</b><ul>${v.warnings.map((x) => `<li class="w">${esc(x)}</li>`).join("")}</ul>` : "") +
    (v.info.length ? `<b>Notes</b><ul>${v.info.map((x) => `<li>${esc(x)}</li>`).join("")}</ul>` : "") +
    `<div>${esc(v.validator)}</div></div>`;

  if (S.showRaw) {
    el.innerHTML = banner + details + `<pre class="json">${hl(JSON.stringify(b, null, 2))}</pre>`;
  } else {
    const prevMap = {};
    if (prev) prev.entry.forEach((e) => (prevMap[e.resource.id] = JSON.stringify(e.resource)));
    const comp = b.entry[0].resource;
    const head =
      `<div class="bhead">{ <span class="k">"resourceType"</span>: "Bundle", <span class="k">"type"</span>: "document",<br>` +
      `&nbsp;&nbsp;<span class="k">"meta.profile"</span>: "…/DocumentBundle",<br>` +
      `&nbsp;&nbsp;<span class="k">"Composition.status"</span>: "<b>${esc(comp.status)}</b>"${b.signature ? `, <span class="k">"signature"</span>: ✓` : ""},<br>` +
      `&nbsp;&nbsp;<span class="k">"entry"</span>: [ ${total} resources ] }</div>`;
    const rows = b.entry
      .map((e) => {
        const r = e.resource;
        const changed = prev && prevMap[r.id] !== JSON.stringify(r);
        const open = S.open.has(r.id);
        return `<div class="entry${changed ? " changed" : ""}" data-id="${r.id}">
          <button aria-expanded="${open}"><span class="rt">${r.resourceType}</span><span class="sm">${esc(summarize(r))}</span><span class="st">${esc(statusOf(r))}</span></button>
          ${open ? `<pre>${hl(JSON.stringify(r, null, 2))}</pre>` : ""}</div>`;
      })
      .join("");
    el.innerHTML = banner + details + head + `<div class="entries">${rows}</div>`;
    $$(".entry > button", el).forEach((btn) =>
      btn.addEventListener("click", () => {
        const id = btn.parentElement.dataset.id;
        S.open.has(id) ? S.open.delete(id) : S.open.add(id);
        renderFhir();
      }));
  }
  $("#vMore").onclick = () => $("#vDetails").classList.toggle("hidden");
}

// ---------------------------------------------------------------------------
// Approve
// ---------------------------------------------------------------------------

async function approve() {
  if (S.phase !== "review") return;
  if (S.saveTimer) { clearTimeout(S.saveTimer); await save(); }
  if (S.saving) await S.saving;
  const btn = $("#btnApprove");
  btn.disabled = true;
  btn.textContent = "Approving…";
  try {
    const out = await api(`/api/sessions/${S.session.id}/approve`, { method: "POST", body: {} });
    S.phase = "approved";
    S.editing = false;
    applyOut(out, false);
    renderNote();
    const t = out.record.saved_at.slice(11, 16);
    setStep("approve", "done", `approved · ${t}`);
    setPill("approved", "Approved · saved to HMIS");
    $("#btnEdit").disabled = true;
    $("#btnEdit").textContent = "Edit";
    $("#saving").textContent = "";
    btn.innerHTML = icon("check") + "Approved and saved";
    showReceipt(out);
  } catch (e) {
    toast(e.message, "warn", 8000);
    btn.disabled = false;
    btn.textContent = "Approve and save to HMIS";
  }
  refreshAudit();
}

function showReceipt(out) {
  const rec = out.record;
  const n = out.bundle.entry.length;
  $("#modalRoot").innerHTML = `
  <div class="scrim" id="scrim">
    <div class="modal" role="dialog" aria-modal="true" aria-label="Saved to HMIS">
      <div class="mh"><div class="ok">${icon("check")}</div><div><div class="small" style="opacity:.85">OP Consult Record</div><h3>Saved to HMIS</h3></div></div>
      <div class="mb">
        <dl class="kv">
          <dt>Record ID</dt><dd class="mono">${esc(rec.record_id)}</dd>
          <dt>Patient</dt><dd>${esc(S.patient.name)} · ${S.patient.abha_number ? "ABHA " + esc(S.patient.abha_number) : esc(S.patient.mrn || "")}</dd>
          <dt>Approved by</dt><dd>${esc(S.cfg.practitioner.name)} · ${esc(rec.saved_at.replace("T", " ").slice(0, 16))}</dd>
          <dt>FHIR document</dt><dd>${n} resources · Composition <b>final</b> · attested · signed</dd>
          <dt>Validation</dt><dd>${out.validation.valid ? "Structural check passed" : "Has errors"}</dd>
          <dt>ABDM</dt><dd>${esc(rec.abdm.status)}</dd>
        </dl>
        <div class="flow"><span>AI draft</span><i>→</i><span>Clinician edits (${S.edits})</span><i>→</i><span class="o">Clinician approves</span><i>→</i><span>Saved in host app</span></div>
      </div>
      <div class="mf">
        <button class="btn btn-quiet" id="mClose">Close</button>
        <button class="btn btn-ghost" id="mDl">${icon("download")}FHIR bundle</button>
        <button class="btn btn-ghost" id="mNew">New consultation</button>
        <button class="btn btn-ghost" id="mBack">${icon("left")}Return to HMIS</button>
        <button class="btn btn-primary" id="mReport">${icon("file")}Open report (PDF)</button>
      </div>
    </div>
  </div>`;
  $("#mClose").onclick = () => ($("#modalRoot").innerHTML = "");
  $("#scrim").onclick = (e) => { if (e.target.id === "scrim") $("#modalRoot").innerHTML = ""; };
  $("#mDl").onclick = () => (location.href = `/api/sessions/${S.session.id}/bundle`);
  $("#mNew").onclick = () => (location.href = `/scribe?patient=${encodeURIComponent(S.patient.id)}`);
  $("#mBack").onclick = () => (location.href = `/?highlight=${encodeURIComponent(rec.record_id)}`);
  $("#mReport").onclick = () => window.open(`/report/${encodeURIComponent(rec.record_id)}`, "_blank");
}

// ---------------------------------------------------------------------------
// Audit trail
// ---------------------------------------------------------------------------

const EVENT_LABEL = {
  "session.started": "Session started",
  "transcription.completed": "Audio transcribed",
  "transcription.failed": "Transcription failed",
  "consultation.ended": "Consultation ended",
  "draft.generated": "Draft generated",
  "draft.failed": "Draft failed",
  "draft.edited": "Field edited",
  "record.approved": "Record approved",
  "record.saved_to_hmis": "Saved to HMIS",
  "abdm.care_context_ready": "ABDM care context ready",
};

async function refreshAudit() {
  if (!S.session) return;
  try {
    const r = await api(`/api/audit?session=${encodeURIComponent(S.session.id)}`);
    $("#auditList").innerHTML = r.events
      .slice()
      .reverse()
      .map((ev) => `<div class="ev"><div class="t">${esc(ev.time.slice(11, 19))}</div><div><div class="e">${esc(EVENT_LABEL[ev.event] || ev.event)}</div><div class="a">${esc(ev.actor)}</div><div class="d">${esc(ev.detail)}</div></div></div>`)
      .join("") || `<div class="empty">No events yet.</div>`;
  } catch { /* ignore */ }
}

// ---------------------------------------------------------------------------
// Waveform
// ---------------------------------------------------------------------------

function startWave() {
  S.levels = new Array(96).fill(0.05);
  S.frame = 0;
  cancelAnimationFrame(S.waveRAF);
  const draw = () => {
    const c = $("#wave");
    const dpr = window.devicePixelRatio || 1;
    const w = c.clientWidth, h = c.clientHeight;
    if (!w) { S.waveRAF = requestAnimationFrame(draw); return; }
    if (c.width !== Math.round(w * dpr)) { c.width = Math.round(w * dpr); c.height = Math.round(h * dpr); }
    const ctx = c.getContext("2d");
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.clearRect(0, 0, w, h);
    if (++S.frame % 3 === 0) {
      let lv;
      if (S.source === "mic" && S.analyser) lv = Math.min(1, (S.level || 0) * 10);
      else lv = S.speaking ? 0.25 + Math.random() * 0.75 : 0.04 + Math.random() * 0.06;
      S.levels.push(lv);
      S.levels.shift();
    }
    const n = S.levels.length, gap = w / n, bw = Math.max(2, gap * 0.5);
    S.levels.forEach((lv, i) => {
      const bh = Math.max(3, lv * (h - 6));
      ctx.fillStyle = i > n - 8 && lv > 0.15 ? "#d9691e" : "#0f6e66";
      const x = i * gap + (gap - bw) / 2, y = (h - bh) / 2;
      if (ctx.roundRect) { ctx.beginPath(); ctx.roundRect(x, y, bw, bh, bw / 2); ctx.fill(); }
      else ctx.fillRect(x, y, bw, bh);
    });
    S.waveRAF = requestAnimationFrame(draw);
  };
  S.waveRAF = requestAnimationFrame(draw);
}

function stopWave() { cancelAnimationFrame(S.waveRAF); }

init();
