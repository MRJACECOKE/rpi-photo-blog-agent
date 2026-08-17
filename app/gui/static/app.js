"use strict";

const STAGES = [
  ["STAGE_1_PREP", "사진 전처리"],
  ["STAGE_2_VISION", "VLM 사진 분석"],
  ["STAGE_3_UNLOAD", "VLM 언로드 게이트"],
  ["STAGE_4_WRITE", "LLM 본문 작성"],
  ["STAGE_5_QUALITY", "품질 검사"],
  ["STAGE_6_OUTPUT", "산출물 저장"],
];

const state = { files: [], jobId: null, stream: null, busy: false };

const $ = (id) => document.getElementById(id);

function renderStages(status = {}, progress = {}) {
  $("pipeline").innerHTML = STAGES.map(([key, label]) => {
    const s = status[key] || "NOT_STARTED";
    const detail = progress.stage === key && progress.detail ? progress.detail : "";
    return `<div class="stageRow"><span class="dot ${s}"></span><span>${label}</span>
      <span class="stageDetail">${detail || s}</span></div>`;
  }).join("");
}

function setProgress(current, total, text) {
  const pct = total > 0 ? Math.round((current / total) * 100) : 0;
  $("progressFill").style.width = pct + "%";
  if (text) $("progressText").textContent = text;
}

function appendLog(line) {
  const view = $("logView");
  view.textContent += line + "\n";
  const lines = view.textContent.split("\n");
  if (lines.length > 400) view.textContent = lines.slice(-400).join("\n");
  if ($("autoscroll").checked) view.scrollTop = view.scrollHeight;
}

// ---------- 시스템 상태 ----------
async function pollSystem() {
  try {
    const r = await fetch("/api/system");
    if (!r.ok) return;
    const d = await r.json();
    $("statRam").textContent = `RAM ${(d.memory.available_mb / 1024).toFixed(1)}GB 여유 / ${d.memory.used_percent}% 사용`;
    $("statTemp").textContent = `온도 ${d.thermal.cpu_temp_c != null ? d.thermal.cpu_temp_c.toFixed(1) : "—"}°C`;
    const th = $("statThrottle");
    th.textContent = d.thermal.throttling_detected ? "스로틀 감지됨" : "스로틀 없음";
    th.classList.toggle("alert", !!d.thermal.throttling_detected);
    $("statDisk").textContent = `디스크 ${d.disk.free_gib}GB 여유`;
    $("statModel").textContent = `모델 ${d.loaded_model}`;
    state.busy = d.busy;
    $("btnStart").disabled = d.busy || state.files.length === 0;
    $("btnCancel").disabled = !d.busy;
  } catch (e) { /* 네트워크 순간 오류는 무시한다 */ }
}

// ---------- 업로드 ----------
function renderThumbs() {
  $("thumbs").innerHTML = "";
  state.files.forEach((file, index) => {
    const div = document.createElement("div");
    div.className = "thumb";
    const img = document.createElement("img");
    img.src = URL.createObjectURL(file);
    img.onload = () => URL.revokeObjectURL(img.src);
    const btn = document.createElement("button");
    btn.textContent = "×";
    btn.title = "제거";
    btn.onclick = () => { state.files.splice(index, 1); renderThumbs(); };
    div.append(img, btn);
    $("thumbs").appendChild(div);
  });
  $("uploadHint").textContent = state.files.length ? `${state.files.length}장 선택됨` : "";
  $("btnStart").disabled = state.busy || state.files.length === 0;
}

function addFiles(list) {
  for (const file of list) {
    if (!state.files.some((f) => f.name === file.name && f.size === file.size)) state.files.push(file);
  }
  renderThumbs();
}

$("dropzone").addEventListener("click", () => $("fileInput").click());
$("fileInput").addEventListener("change", (e) => addFiles(e.target.files));
["dragenter", "dragover"].forEach((ev) =>
  $("dropzone").addEventListener(ev, (e) => { e.preventDefault(); $("dropzone").classList.add("over"); }));
["dragleave", "drop"].forEach((ev) =>
  $("dropzone").addEventListener(ev, (e) => { e.preventDefault(); $("dropzone").classList.remove("over"); }));
$("dropzone").addEventListener("drop", (e) => addFiles(e.dataTransfer.files));

// ---------- 실행 ----------
async function startJob() {
  if (!state.files.length) return;
  $("btnStart").disabled = true;
  $("logView").textContent = "";

  const form = new FormData();
  state.files.forEach((f) => form.append("files", f));
  form.append("category", $("category").value);
  form.append("topic", $("topic").value);

  const created = await fetch("/api/jobs", { method: "POST", body: form });
  const data = await created.json();
  if (!created.ok) {
    appendLog("업로드 실패: " + (data.error || created.status));
    $("btnStart").disabled = false;
    return;
  }
  state.jobId = data.job_id;
  appendLog(`job 생성: ${data.job_id} (${data.saved.length}장 업로드)`);
  (data.rejected || []).forEach((r) => appendLog(`거부됨: ${r.file} — ${r.reason}`));

  openStream(state.jobId);
  const started = await fetch(`/api/jobs/${state.jobId}/run`, { method: "POST" });
  if (!started.ok) {
    const err = await started.json();
    appendLog("실행 실패: " + (err.detail || started.status));
    $("btnStart").disabled = false;
  }
}

function openStream(jobId) {
  if (state.stream) state.stream.close();
  state.stream = new EventSource(`/events?job=${encodeURIComponent(jobId)}`);
  state.stream.onmessage = (event) => {
    const msg = JSON.parse(event.data);
    if (msg.event === "log") { appendLog(msg.line); return; }
    if (msg.event === "progress") {
      setProgress(msg.current || 0, msg.total || 0, msg.detail || "");
      refreshJob(jobId);
    }
    if (msg.event === "stage") refreshJob(jobId);
    if (msg.event === "done") { appendLog("완료: " + msg.output_txt); finishJob(jobId); }
    if (msg.event === "cancelled") { appendLog(msg.message); finishJob(jobId); }
    if (msg.event === "error") { appendLog("오류: " + msg.message); finishJob(jobId); }
  };
  state.stream.onerror = () => { /* 자동 재연결에 맡긴다 */ };
}

async function refreshJob(jobId) {
  const r = await fetch(`/api/jobs/${jobId}`);
  if (!r.ok) return;
  const job = await r.json();
  renderStages(job.stage_status, job.progress || {});

  const held = job.images.filter((i) => i.status === "PRIVACY_HOLD");
  const banner = $("privacyBanner");
  if (held.length) {
    banner.classList.remove("hidden");
    banner.innerHTML = `<strong>개인정보 보류 ${held.length}장</strong> — 본문에서 제외했습니다.<br>` +
      held.map((i) => `· ${i.file}: ${(i.reasons || []).join(", ")}`).join("<br>") +
      `<br>해당 부분을 가림 처리한 뒤 다시 업로드하면 본문에 포함할 수 있습니다.`;
  } else {
    banner.classList.add("hidden");
  }
  return job;
}

async function finishJob(jobId) {
  if (state.stream) { state.stream.close(); state.stream = null; }
  const job = await refreshJob(jobId);
  await loadResult(jobId);
  await loadJobs();
  $("btnStart").disabled = state.files.length === 0;
  if (job) renderQuality(job.quality);
}

function renderQuality(quality) {
  const box = $("qualityBox");
  if (!quality || quality.passed === undefined) { box.classList.add("hidden"); return; }
  box.classList.remove("hidden");
  box.className = "quality " + (quality.passed ? "pass" : "fail");
  const stats = quality.stats || {};
  let html = quality.passed
    ? `<strong>품질 게이트 통과</strong>`
    : `<strong>품질 게이트 실패 ${quality.error_count}건</strong>`;
  html += ` · 본문 ${stats.body_chars_without_space || 0}자 · 한글비율 ${stats.hangul_ratio ?? "—"} · 자기중복 ${stats.trigram_self_overlap ?? "—"}`;
  const findings = (quality.findings || []).filter((f) => f.severity === "error");
  if (findings.length) html += "<ul>" + findings.map((f) => `<li>${f.message}</li>`).join("") + "</ul>";
  box.innerHTML = html;
}

async function loadResult(jobId) {
  const r = await fetch(`/api/jobs/${jobId}/result`);
  const enabled = r.ok;
  ["btnSave", "btnDownload", "btnCopy", "btnRegen"].forEach((id) => { $(id).disabled = !enabled; });
  if (!enabled) return;
  const d = await r.json();
  $("resultText").value = d.content;
  state.jobId = jobId;
}

async function loadJobs() {
  const r = await fetch("/api/jobs");
  if (!r.ok) return;
  const jobs = await r.json();
  $("jobList").innerHTML = jobs.map((j) => {
    const done = j.stage_status.STAGE_6_OUTPUT === "SUCCESS";
    return `<div class="jobRow" data-job="${j.job_id}">
      <span class="dot ${done ? "SUCCESS" : j.stage_status[j.stage] || "NOT_STARTED"}"></span>
      <span class="jid">${j.job_id}</span>
      <span class="badge">${j.category} · 사진 ${j.images_usable}/${j.images_total}${j.images_held ? ` · 보류 ${j.images_held}` : ""}</span>
    </div>`;
  }).join("") || '<p class="hint">아직 작업이 없습니다.</p>';

  document.querySelectorAll(".jobRow").forEach((row) => {
    row.onclick = async () => {
      const id = row.dataset.job;
      const job = await refreshJob(id);
      await loadResult(id);
      if (job) renderQuality(job.quality);
    };
  });
}

// ---------- 결과 조작 ----------
$("btnStart").onclick = startJob;

$("btnCancel").onclick = async () => {
  if (!state.jobId) return;
  await fetch(`/api/jobs/${state.jobId}/cancel`, { method: "POST" });
  appendLog("취소를 요청했습니다. 모델을 안전하게 내린 뒤 종료합니다.");
};

$("btnSave").onclick = async () => {
  const r = await fetch(`/api/jobs/${state.jobId}/result`, {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ content: $("resultText").value }),
  });
  const d = await r.json();
  appendLog(r.ok ? `저장 완료 (${d.chars}자, 백업 ${d.backup})` : `저장 실패: ${d.detail}`);
};

$("btnDownload").onclick = () => {
  window.location = `/api/jobs/${state.jobId}/result?download=true`;
};

$("btnCopy").onclick = async () => {
  try {
    await navigator.clipboard.writeText($("resultText").value);
    appendLog("클립보드에 복사했습니다.");
  } catch (e) {
    $("resultText").select();
    appendLog("클립보드 권한이 없어 텍스트를 선택했습니다. Ctrl+C로 복사하세요.");
  }
};

$("btnRegen").onclick = async () => {
  if (!state.jobId) return;
  openStream(state.jobId);
  const r = await fetch(`/api/jobs/${state.jobId}/run`, { method: "POST" });
  if (!r.ok) appendLog("재생성 실패: " + (await r.json()).detail);
  else appendLog("재생성을 시작했습니다. 완료된 스테이지는 건너뜁니다.");
};

renderStages();
pollSystem();
loadJobs();
setInterval(pollSystem, 4000);
