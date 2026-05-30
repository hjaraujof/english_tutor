// english_tutor frontend — vanilla JS, no build step.

const $ = (selector) => document.querySelector(selector);
const $$ = (selector) => Array.from(document.querySelectorAll(selector));

function setStatus(text) {
  const el = $("#status");
  if (el) el.textContent = text;
}

// ---------- Tab switching ----------
$$(".tab").forEach((tab) => {
  tab.addEventListener("click", () => {
    $$(".tab").forEach((t) => t.classList.remove("is-active"));
    tab.classList.add("is-active");
    const target = tab.dataset.tab;
    $$(".panel").forEach((p) => p.classList.toggle("hidden", p.dataset.panel !== target));
    if (target === "history") loadHistory();
  });
});

// ---------- Health/config ----------
async function bootstrap() {
  try {
    const response = await fetch("/api/config");
    if (!response.ok) throw new Error(`config ${response.status}`);
    const config = await response.json();
    setStatus(`L1: ${config.native_language} · ${config.cefr_level} · ${config.model}`);
  } catch (err) {
    setStatus("offline — start backend with `uv run uvicorn backend.main:app`");
  }
}
bootstrap();

// ---------- Recorder ----------
let mediaRecorder = null;
let audioChunks = [];
let recordStart = 0;
let timerHandle = null;

const recordButton = $("#recordButton");
const recordTimer = $("#recordTimer");

function formatDuration(milliseconds) {
  const total = Math.max(0, Math.floor(milliseconds / 1000));
  const minutes = String(Math.floor(total / 60)).padStart(2, "0");
  const seconds = String(total % 60).padStart(2, "0");
  return `${minutes}:${seconds}`;
}

recordButton.addEventListener("click", async () => {
  if (mediaRecorder && mediaRecorder.state === "recording") {
    mediaRecorder.stop();
    return;
  }
  try {
    const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
    mediaRecorder = new MediaRecorder(stream, { mimeType: "audio/webm;codecs=opus" });
    audioChunks = [];
    mediaRecorder.addEventListener("dataavailable", (event) => {
      if (event.data.size > 0) audioChunks.push(event.data);
    });
    mediaRecorder.addEventListener("stop", async () => {
      stream.getTracks().forEach((track) => track.stop());
      clearInterval(timerHandle);
      recordButton.classList.remove("is-recording");
      recordButton.textContent = "Start recording";
      const blob = new Blob(audioChunks, { type: "audio/webm" });
      await uploadAudio(blob);
    });
    mediaRecorder.start();
    recordStart = Date.now();
    recordButton.classList.add("is-recording");
    recordButton.textContent = "Stop & review";
    timerHandle = setInterval(() => {
      recordTimer.textContent = formatDuration(Date.now() - recordStart);
    }, 250);
  } catch (err) {
    alert(`Microphone error: ${err.message}`);
  }
});

async function uploadAudio(blob) {
  const result = $("#recordResult");
  result.innerHTML = "<p>Transcribing &amp; reviewing… (this can take 5–15 s)</p>";
  const formData = new FormData();
  formData.append("audio", blob, "recording.webm");
  try {
    const response = await fetch("/api/sessions", { method: "POST", body: formData });
    if (!response.ok) {
      const text = await response.text();
      result.innerHTML = `<p class="err">Error: ${response.status} ${text}</p>`;
      return;
    }
    const data = await response.json();
    renderReview(result, data);
  } catch (err) {
    result.innerHTML = `<p class="err">Network error: ${err.message}</p>`;
  }
}

// ---------- Paste-text path ----------
$("#pasteSubmit").addEventListener("click", async () => {
  const text = $("#pasteText").value.trim();
  if (!text) return;
  const result = $("#pasteResult");
  result.innerHTML = "<p>Reviewing…</p>";
  try {
    const response = await fetch("/api/review", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ text }),
    });
    if (!response.ok) {
      const body = await response.text();
      result.innerHTML = `<p class="err">Error: ${response.status} ${body}</p>`;
      return;
    }
    const data = await response.json();
    renderReview(result, { transcript: text, ...data });
  } catch (err) {
    result.innerHTML = `<p class="err">Network error: ${err.message}</p>`;
  }
});

// ---------- Render ----------
function escapeHtml(value) {
  return String(value)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#39;");
}

function annotateTranscript(transcript, corrections) {
  if (!corrections || corrections.length === 0) return escapeHtml(transcript);
  const items = corrections
    .filter((item) => item.original && transcript.includes(item.original))
    .sort((a, b) => transcript.indexOf(a.original) - transcript.indexOf(b.original));
  if (items.length === 0) return escapeHtml(transcript);
  let cursor = 0;
  const parts = [];
  for (const item of items) {
    const index = transcript.indexOf(item.original, cursor);
    if (index < 0) continue;
    parts.push(escapeHtml(transcript.slice(cursor, index)));
    const tooltip = `${item.corrected} — ${item.explanation}`;
    parts.push(
      `<span class="err" data-explanation="${escapeHtml(tooltip)}">${escapeHtml(item.original)}</span>`
    );
    cursor = index + item.original.length;
  }
  parts.push(escapeHtml(transcript.slice(cursor)));
  return parts.join("");
}

function renderReview(container, data) {
  const review = data.review || {};
  const metrics = data.metrics || {};
  const corrections = review.corrections || [];
  const transcript = data.transcript || "";

  const stats = [
    ["WPM", metrics.words_per_minute],
    ["Words", metrics.word_count],
    ["TTR", metrics.type_token_ratio],
    ["Fillers", metrics.filler_count],
    ["Filler ratio", metrics.filler_ratio],
    ["Longest pause (s)", metrics.longest_pause_seconds],
  ];

  container.innerHTML = `
    <div class="transcript">${annotateTranscript(transcript, corrections)}</div>
    <div class="report-card">
      ${stats
        .map(([label, value]) => `<div class="stat"><div class="label">${label}</div><div class="value">${value ?? "—"}</div></div>`)
        .join("")}
    </div>
    <div class="feedback">
      <h3>Overall</h3>
      <p>${escapeHtml(review.overall?.summary || "")}</p>
      ${
        review.overall?.strengths?.length
          ? `<h3>Strengths</h3><ul>${review.overall.strengths.map((value) => `<li>${escapeHtml(value)}</li>`).join("")}</ul>`
          : ""
      }
      ${
        review.overall?.next_focus?.length
          ? `<h3>Focus next</h3><ul>${review.overall.next_focus.map((value) => `<li>${escapeHtml(value)}</li>`).join("")}</ul>`
          : ""
      }
      ${
        corrections.length
          ? `<h3>Corrections</h3><ul>${corrections
              .map(
                (correction) =>
                  `<li><strong>${escapeHtml(correction.type)}</strong>: <s>${escapeHtml(correction.original)}</s> → <em>${escapeHtml(correction.corrected)}</em> — ${escapeHtml(correction.explanation)}</li>`
              )
              .join("")}</ul>`
          : ""
      }
      ${
        review.fluency_notes?.length
          ? `<h3>Fluency notes</h3><ul>${review.fluency_notes
              .map((note) => `<li>${escapeHtml(note.observation)} <em>${escapeHtml(note.suggestion)}</em></li>`)
              .join("")}</ul>`
          : ""
      }
      ${
        review.vocabulary_suggestions?.length
          ? `<h3>Vocabulary</h3><ul>${review.vocabulary_suggestions
              .map(
                (vocab) =>
                  `<li><code>${escapeHtml(vocab.phrase)}</code> → <code>${escapeHtml(vocab.alternative)}</code> (${escapeHtml(vocab.register || "neutral")})</li>`
              )
              .join("")}</ul>`
          : ""
      }
    </div>
  `;
}

// ---------- History ----------
async function loadHistory() {
  try {
    const [trendResponse, recurringResponse, sessionsResponse] = await Promise.all([
      fetch("/api/sessions/trend/words_per_minute"),
      fetch("/api/sessions/recurring-errors"),
      fetch("/api/sessions"),
    ]);
    const trendWPM = (await trendResponse.json()).points || [];
    const recurring = (await recurringResponse.json()).recurring || [];
    const sessions = (await sessionsResponse.json()).sessions || [];

    const fillerResponse = await fetch("/api/sessions/trend/filler_ratio");
    const ttrResponse = await fetch("/api/sessions/trend/type_token_ratio");
    drawLineChart($("#chartWPM"), trendWPM.map((point) => point.value));
    drawLineChart($("#chartFiller"), ((await fillerResponse.json()).points || []).map((point) => point.value));
    drawLineChart($("#chartTTR"), ((await ttrResponse.json()).points || []).map((point) => point.value));

    $("#recurringList").innerHTML = recurring.length
      ? recurring.map((row) => `<li><strong>${escapeHtml(row.type)}</strong> · ${row.count} occurrences</li>`).join("")
      : "<li>No recurring patterns yet — keep practicing.</li>";

    $("#historyList").innerHTML = sessions
      .map((session) => {
        const wpm = session.metrics?.words_per_minute ?? "—";
        const errors = session.review?.corrections?.length ?? 0;
        const snippet = session.transcript.slice(0, 90).replace(/\s+/g, " ");
        return `<li><span class="when">${escapeHtml(session.created_at)}</span><strong>${session.kind}</strong> · WPM ${wpm} · ${errors} corrections · <em>${escapeHtml(snippet)}…</em></li>`;
      })
      .join("");
  } catch (err) {
    setStatus(`history error: ${err.message}`);
  }
}

function drawLineChart(canvas, values) {
  if (!canvas) return;
  const context = canvas.getContext("2d");
  const width = canvas.width;
  const height = canvas.height;
  context.clearRect(0, 0, width, height);
  if (!values || values.length === 0) {
    context.fillStyle = "#8b91a4";
    context.font = "12px sans-serif";
    context.fillText("No data yet", 10, 20);
    return;
  }
  const padding = 24;
  const maximum = Math.max(...values, 1);
  const minimum = Math.min(...values, 0);
  const range = maximum - minimum || 1;
  context.strokeStyle = "#262a36";
  context.beginPath();
  context.moveTo(padding, height - padding);
  context.lineTo(width - padding, height - padding);
  context.stroke();
  context.strokeStyle = "#6ea8ff";
  context.lineWidth = 2;
  context.beginPath();
  values.forEach((value, index) => {
    const x = padding + (index / Math.max(1, values.length - 1)) * (width - padding * 2);
    const y = height - padding - ((value - minimum) / range) * (height - padding * 2);
    if (index === 0) context.moveTo(x, y);
    else context.lineTo(x, y);
  });
  context.stroke();
  context.fillStyle = "#8b91a4";
  context.font = "11px sans-serif";
  context.fillText(`min ${minimum.toFixed?.(2) ?? minimum}`, padding, 14);
  context.fillText(`max ${maximum.toFixed?.(2) ?? maximum}`, width - padding - 70, 14);
}

// ---------- Live conversation (Phase 2) ----------
const LIVE_SAMPLE_RATE = 16000;
const LIVE_FRAME_MS = 32;
const LIVE_FRAME_SAMPLES = (LIVE_SAMPLE_RATE * LIVE_FRAME_MS) / 1000;

let liveSocket = null;
let liveAudioContext = null;
let liveStream = null;
let liveProcessor = null;
let liveSource = null;
let liveAssistantTurn = null;

function appendTurn(role, text) {
  const log = $("#liveLog");
  const turn = document.createElement("div");
  turn.className = `turn ${role}`;
  turn.innerHTML = `<div class="role">${role}</div><div class="text"></div>`;
  turn.querySelector(".text").textContent = text;
  log.appendChild(turn);
  log.scrollTop = log.scrollHeight;
  return turn;
}

function appendCorrection(correction) {
  const log = $("#liveLog");
  const node = document.createElement("div");
  node.className = "correction";
  node.textContent = `↪ "${correction.original}" → "${correction.corrected}" — ${correction.reason}`;
  log.appendChild(node);
  log.scrollTop = log.scrollHeight;
}

async function playWavBytes(bytes) {
  if (!bytes || bytes.byteLength === 0) return;
  if (!liveAudioContext) liveAudioContext = new (window.AudioContext || window.webkitAudioContext)();
  try {
    const buffer = await liveAudioContext.decodeAudioData(bytes.slice(0));
    const source = liveAudioContext.createBufferSource();
    source.buffer = buffer;
    source.connect(liveAudioContext.destination);
    source.start();
  } catch (err) {
    console.warn("audio decode failed", err);
  }
}

function downsampleTo16k(input, sourceRate) {
  if (sourceRate === LIVE_SAMPLE_RATE) return input;
  const ratio = sourceRate / LIVE_SAMPLE_RATE;
  const length = Math.floor(input.length / ratio);
  const out = new Int16Array(length);
  let outIndex = 0;
  let position = 0;
  while (outIndex < length) {
    const nextPosition = Math.floor((outIndex + 1) * ratio);
    let sum = 0;
    let count = 0;
    for (let i = position; i < nextPosition && i < input.length; i++) {
      sum += input[i];
      count++;
    }
    const sample = count > 0 ? sum / count : 0;
    const clamped = Math.max(-1, Math.min(1, sample));
    out[outIndex] = clamped < 0 ? clamped * 0x8000 : clamped * 0x7fff;
    outIndex++;
    position = nextPosition;
  }
  return out;
}

async function startLive() {
  const button = $("#liveButton");
  const stateEl = $("#liveState");
  $("#liveLatency").textContent = "";
  try {
    liveStream = await navigator.mediaDevices.getUserMedia({ audio: { sampleRate: LIVE_SAMPLE_RATE, channelCount: 1, echoCancellation: true } });
  } catch (err) {
    alert(`Microphone error: ${err.message}`);
    return;
  }
  liveAudioContext = new (window.AudioContext || window.webkitAudioContext)();
  liveSource = liveAudioContext.createMediaStreamSource(liveStream);
  liveProcessor = liveAudioContext.createScriptProcessor(4096, 1, 1);
  liveSource.connect(liveProcessor);
  liveProcessor.connect(liveAudioContext.destination);

  const protocol = window.location.protocol === "https:" ? "wss:" : "ws:";
  liveSocket = new WebSocket(`${protocol}//${window.location.host}/ws/live`);
  liveSocket.binaryType = "arraybuffer";

  liveSocket.addEventListener("open", () => {
    stateEl.textContent = "listening";
    button.classList.add("is-recording");
    button.textContent = "Stop conversation";
  });
  liveSocket.addEventListener("message", (event) => {
    if (event.data instanceof ArrayBuffer) {
      playWavBytes(event.data);
      return;
    }
    let payload;
    try { payload = JSON.parse(event.data); } catch { return; }
    if (payload.type === "transcript") {
      appendTurn("user", payload.text);
      liveAssistantTurn = appendTurn("assistant", "");
    } else if (payload.type === "reply_delta") {
      if (!liveAssistantTurn) liveAssistantTurn = appendTurn("assistant", "");
      const textEl = liveAssistantTurn.querySelector(".text");
      textEl.textContent += payload.text;
      $("#liveLog").scrollTop = $("#liveLog").scrollHeight;
    } else if (payload.type === "reply_end") {
      liveAssistantTurn = null;
    } else if (payload.type === "correction") {
      appendCorrection(payload);
    } else if (payload.type === "latency") {
      $("#liveLatency").textContent = `latency  ASR ${payload.asr_ms}ms · LLM ${payload.llm_ms}ms · TTS ${payload.tts_ms}ms`;
    } else if (payload.type === "error") {
      stateEl.textContent = `error: ${payload.detail}`;
    }
  });
  liveSocket.addEventListener("close", () => {
    stateEl.textContent = "idle";
    button.classList.remove("is-recording");
    button.textContent = "Start conversation";
  });

  liveProcessor.onaudioprocess = (event) => {
    if (!liveSocket || liveSocket.readyState !== WebSocket.OPEN) return;
    const channel = event.inputBuffer.getChannelData(0);
    const downsampled = downsampleTo16k(channel, liveAudioContext.sampleRate);
    // Send in FRAME_SAMPLES-sized chunks for the VAD's preferred granularity.
    let offset = 0;
    while (offset + LIVE_FRAME_SAMPLES <= downsampled.length) {
      const slice = downsampled.subarray(offset, offset + LIVE_FRAME_SAMPLES);
      liveSocket.send(slice.buffer.slice(slice.byteOffset, slice.byteOffset + slice.byteLength));
      offset += LIVE_FRAME_SAMPLES;
    }
  };
}

function stopLive() {
  if (liveSocket && liveSocket.readyState === WebSocket.OPEN) {
    try { liveSocket.send(JSON.stringify({ type: "end" })); } catch {}
    liveSocket.close();
  }
  liveSocket = null;
  if (liveProcessor) liveProcessor.disconnect();
  if (liveSource) liveSource.disconnect();
  if (liveStream) liveStream.getTracks().forEach((track) => track.stop());
  liveProcessor = null;
  liveSource = null;
  liveStream = null;
}

const liveButton = $("#liveButton");
if (liveButton) {
  liveButton.addEventListener("click", () => {
    if (liveSocket && liveSocket.readyState === WebSocket.OPEN) {
      stopLive();
    } else {
      startLive();
    }
  });
}
