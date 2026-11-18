const $ = (id) => document.getElementById(id);
const statusText = {
  notified: "已通知", wrong_party: "身份不符", callback: "已登记回电", jingmai: "京麦处理",
  handoff: "转人工复核", ended: "已结束", write_pending: "等待回写"
};
const live = {mode: null, id: null, pc: null, dc: null, sender: null, mic: null, context: null,
  inbound: null, outbound: null, start: 0, done: false, closing: false,
  speaking: false, lastMic: null, end: null, waiting: null, latencies: [], interrupts: 0,
  lastStats: 0, selected: null, accepting: false, micBusy: false, drain: null, drainTimer: null, lastOutput: 0};
let ready = false;
let taskList = [];
let qualityId = null;
let taskRoute = "all";
let callRecords = [];
let historyRequest = 0;

async function request(url, body) {
  let response;
  try {
    response = await fetch(url, body === undefined ? {} : {
      method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify(body)
    });
  } catch {
    throw new Error("无法连接本地服务，请确认程序正在运行后重试。");
  }
  let data;
  try { data = await response.json(); }
  catch { throw new Error("服务暂时不可用，请稍后重试。"); }
  if (!response.ok) throw new Error(typeof data.detail === "string" ? data.detail : "请求未完成");
  return data;
}

function element(tag, className, content) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (content != null) node.textContent = content;
  return node;
}

function navigate(name) {
  for (const page of document.querySelectorAll(".page")) page.hidden = page.id !== name;
  for (const button of document.querySelectorAll("nav [data-page]")) {
    button.classList.toggle("selected", button.dataset.page === name);
    button.setAttribute("aria-current", button.dataset.page === name ? "page" : "false");
  }
  $("page-name").textContent = {work: "外呼工作台", history: "通话记录", rsi: "知识评测"}[name];
  if (name === "history") readHistory().catch(showQualityError);
  if (name === "rsi") readRsi().catch((e) => rsiStatus(e.message));
}

function renderTasks() {
  const query = $("task-search").value.trim().toLowerCase();
  const tasks = taskList.filter((task) => (taskRoute === "all" || task.route === taskRoute)
    && [task.id, task.title, task.merchant].join(" ").toLowerCase().includes(query));
  $("task-count").textContent = tasks.length;
  const rows = tasks.map((task) => {
    const selected = task.id === $("case").value;
    const button = element("button", "task-row" + (selected ? " active" : ""));
    button.setAttribute("aria-pressed", String(selected));
    button.disabled = Boolean(live.mode);
    button.dataset.caseId = task.id;
    const top = element("span", "task-row-top");
    top.append(element("span", "mono", task.id), element("span", "chip neutral", task.route === "phone" ? "电话" : "京麦"));
    button.append(top, element("strong", "", task.merchant), element("small", "", task.title));
    button.addEventListener("click", () => selectTask(task.id));
    return button;
  });
  $("task-list").replaceChildren(...(rows.length ? rows : [element("p", "list-empty", "没有符合条件的任务")]));
}

function selectTask(id) {
  if (live.mode || id === live.selected) return;
  $("case").value = id;
  for (const node of $("messages").querySelectorAll(".msg")) node.remove();
  $("call-empty").hidden = false;
  $("receipt").hidden = true;
  $("manual-example").hidden = true;
  $("mode").textContent = "未连接";
  $("dialog-title").textContent = "通话记录";
  $("dialog-hint").textContent = "选择任务并发起通话，查看实时沟通记录。";
  $("clock").textContent = "00:00";
  $("live-latency").textContent = "—";
  $("live-interrupt").textContent = "0 次";
  $("composer-idle").textContent = "通话接通后，可在此输入商家回复";
  live.selected = id;
  current("等待发起", false);
  stages();
  error();
  renderRoute();
}

function renderRoute() {
  const task = taskList.find((item) => item.id === $("case").value);
  const phone = task?.route === "phone";
  $("route").textContent = phone ? "电话外呼" : "京麦书面处理";
  $("start").textContent = phone ? "开始语音外呼" : "登记京麦结果";
  $("start").disabled = Boolean(live.mode) || !task || (phone && !ready);
  $("text-start").disabled = Boolean(live.mode) || !phone || !ready;
  $("case").disabled = Boolean(live.mode) || !taskList.length;
  $("record").disabled = Boolean(live.mode) || !phone;
  $("init-actions").hidden = Boolean(live.mode);
  $("merchant-name").textContent = task?.merchant ?? "—";
  $("case-number").textContent = task?.id ?? "—";
  $("case-title").textContent = task?.title ?? "—";
  $("case-attempt").textContent = task ? "第 " + task.attempt + " 次" : "—";
  $("case-channel").textContent = phone ? "电话外呼" : "京麦书面处理";
  $("route-note").textContent = phone
    ? "先确认商家负责人身份，再说明审核意见与整改要求。沟通结束后自动登记处理结果。"
    : "此单据按业务规则通过京麦书面处理。登记结果后，可在通话记录中核对回执。";
  $("operation-note").textContent = live.mode
    ? "通话进行中，结束后可切换其他单据。"
    : phone ? "请允许浏览器播放音频，建议使用耳麦。" : "此任务无需建立语音通话。";
  renderTasks();
}

function renderInputControls() {
  const active = live.mode === "voice" && live.accepting && !live.done;
  $("microphone").hidden = !active;
  $("microphone").disabled = live.micBusy;
  $("microphone").textContent = live.mic ? "关闭麦克风" : "开启麦克风";
  $("composer-idle").hidden = active;
  if (!active) return;
  $("text-form").hidden = false;
  $("manual-example").hidden = live.selected !== "POP-001";
  $("mode").textContent = live.mic ? "语音与文字" : "文字输入 · 语音播报";
  $("dialog-hint").textContent = live.mic
    ? "可用麦克风或输入文字，助手均会播报回复。"
    : "输入文字后按 Enter 或点击发送，也可随时开启麦克风。";
}

function error(message = "") {
  $("work-error").textContent = message;
  $("work-error").hidden = !message;
}

function bubble(role, text, source, answerModel, timestamp) {
  const user = role === "user";
  const outer = element("div", "msg " + role);
  const description = element("div");
  const head = element("div", "msg-head", user ? "商家" : "语音助手");
  if (answerModel) head.append(element("span", "chip neutral", "模型辅助"));
  if (timestamp) head.append(element("time", "", timestamp));
  description.append(head, element("div", "msg-body", text));
  if (source) description.append(element("small", "msg-source", source));
  outer.append(element("div", "avatar", user ? "商" : "智"), description);
  return outer;
}

function append(role, text, source, answerModel) {
  $("call-empty").hidden = true;
  $("messages").append(bubble(role, text, source, answerModel, $("clock").textContent));
  $("messages").scrollTop = $("messages").scrollHeight;
}

function stages(values = []) {
  for (const item of document.querySelectorAll("[data-stage]")) {
    item.classList.toggle("complete", values.includes(item.dataset.stage));
  }
}

function current(text, active = Boolean(live.mode)) {
  $("call-status").textContent = text;
  $("live-dot").classList.toggle("active", active);
}

function prepare(mode) {
  for (const node of $("messages").querySelectorAll(".msg")) node.remove();
  $("call-empty").hidden = false;
  $("partial").hidden = true;
  $("receipt").hidden = true;
  $("live-actions").hidden = false;
  $("no-answer").disabled = false;
  $("hangup").textContent = "结束通话";
  $("live-latency").textContent = "—";
  $("live-interrupt").textContent = "0 次";
  $("clock").textContent = "00:00";
  $("mode").textContent = mode === "voice" ? "WebRTC" : "人工输入";
  $("dialog-title").textContent = mode === "voice" ? "实时转写" : "对话记录";
  $("dialog-hint").textContent = mode === "voice" ? "正在建立语音连接。" : "正在登记京麦结果。";
  $("message-input").value = "";
  $("composer-idle").hidden = false;
  $("composer-idle").textContent = "正在建立连接…";
  $("manual-example").open = false;
  Object.assign(live, {mode, id: null, closing: false, done: false, start: performance.now(),
    speaking: false, lastMic: null, end: null, waiting: null, latencies: [], interrupts: 0, lastStats: 0,
    selected: $("case").value, accepting: false, micBusy: false, drain: null, drainTimer: null, lastOutput: 0});
  stages();
  error();
  renderRoute();
}

function reply(data) {
  append("assistant", data.text, data.source, data.answer_model);
  stages(data.stages);
  $("no-answer").disabled = Boolean(data.verified);
  if (data.state === "write_pending") $("hangup").textContent = "重试回写";
  if (data.state !== "done") {
    const first = !live.accepting;
    live.accepting = true;
    renderInputControls();
    if (first && live.mode === "voice" && !live.mic) $("message-input").focus();
    return;
  }
  live.done = true;
  qualityId = live.id;
  $("outcome").textContent = statusText[data.disposition] ?? data.disposition;
  $("outcome").className = "chip " + (["notified", "jingmai"].includes(data.disposition) ? "ok" : "neutral");
  $("receipt-body").textContent = [live.selected, data.receipt, data.ticket].filter(Boolean).join(" · ");
  $("receipt").hidden = false;
  $("no-answer").disabled = true;
  $("text-form").hidden = true;
  $("manual-example").hidden = true;
  renderInputControls();
  if (live.mode === "text") disconnect("处理结果已登记");
}

function send(type, properties = {}) {
  if (live.dc?.readyState === "open") live.dc.send(JSON.stringify({type, ...properties}));
}

function voiceMessage({data: raw}) {
  const data = JSON.parse(raw);
  switch (data.type) {
    case "ready": send("start"); break;
    case "partial":
      if (!live.mic) break;
      $("partial").textContent = data.text ? "正在识别 · " + data.text : "";
      $("partial").hidden = !data.text;
      break;
    case "speech_start":
      if (!live.mic) break;
      live.speaking = true;
      live.lastMic = performance.now();
      current("正在听取商家");
      break;
    case "speech_end":
      if (!live.mic) break;
      live.speaking = false;
      live.end = live.lastMic;
      $("partial").hidden = true;
      break;
    case "utterance": append("user", data.text); current("正在处理"); break;
    case "reply": reply(data); break;
    case "audio_start":
      $("remote").muted = false;
      current("正在播报");
      live.waiting = data.server_ms != null && live.end
        ? {turn: data.turn, end: live.end, at: performance.now()} : null;
      live.end = null;
      break;
    case "audio_end":
      if (live.done) {
        current("处理结果已登记");
        live.drain = performance.now();
        clearTimeout(live.drainTimer);
        const pc = live.pc;
        live.drainTimer = setTimeout(() => {
          if (live.pc === pc && live.drain !== null) disconnect("处理结果已登记");
        }, 2200);
      } else current("等待商家回应");
      break;
    case "interrupted":
      $("remote").muted = true;
      live.waiting = null;
      live.interrupts += 1;
      $("live-interrupt").textContent = live.interrupts + " 次";
      $("messages").append(element("div", "msg msg-note", "商家发言，播报已中断"));
      $("messages").scrollTop = $("messages").scrollHeight;
      break;
    case "error": error(data.detail); break;
  }
}

function meter(context, stream) {
  const analyser = context.createAnalyser();
  analyser.fftSize = 512;
  context.createMediaStreamSource(stream).connect(analyser);
  return {analyser, samples: new Float32Array(analyser.fftSize)};
}

function rms(source) {
  if (!source) return 0;
  source.analyser.getFloatTimeDomainData(source.samples);
  let sum = 0;
  for (const sample of source.samples) sum += sample * sample;
  return Math.sqrt(sum / source.samples.length);
}

async function setMicrophone(enabled) {
  const {pc, sender, context} = live;
  if (!sender || live.micBusy) return;
  live.micBusy = true;
  renderInputControls();
  let candidate = null;
  try {
    if (enabled) {
      if (!navigator.mediaDevices?.getUserMedia) throw new Error("请通过本机地址或 HTTPS 开启麦克风。");
      candidate = await navigator.mediaDevices.getUserMedia({
        audio: {echoCancellation: true, noiseSuppression: true, autoGainControl: true, channelCount: 1}
      });
      if (live.pc !== pc || live.done) return;
      const [track] = candidate.getAudioTracks();
      if (!track) throw new Error("找不到可用麦克风。");
      await sender.replaceTrack(track);
      if (live.pc !== pc || live.done) return;
      live.mic = candidate;
      live.inbound = meter(context, candidate);
      candidate = null;
    } else {
      const previous = live.mic;
      await sender.replaceTrack(null);
      for (const track of previous?.getTracks() ?? []) track.stop();
      if (live.pc !== pc) return;
      live.mic = null;
      live.inbound = null;
      live.speaking = false;
      $("partial").hidden = true;
    }
    send("microphone", {enabled: Boolean(live.mic)});
    error();
  } finally {
    for (const track of candidate?.getTracks() ?? []) track.stop();
    if (live.pc === pc) {
      live.micBusy = false;
      renderInputControls();
    }
  }
}

async function startVoice(withMicrophone) {
  const caseId = $("case").value;
  if (taskList.find((item) => item.id === caseId)?.route === "jingmai") return registerJingmai();
  const record = $("record").checked;
  prepare("voice");
  current("正在连接音频");
  live.context = new AudioContext();
  await live.context.resume();
  const pc = new RTCPeerConnection({iceServers: []});
  live.pc = pc;
  live.sender = pc.addTransceiver("audio", {direction: "sendrecv"}).sender;
  if (withMicrophone) {
    try { await setMicrophone(true); }
    catch { error("麦克风未开启，可以用文字输入并收听回复。"); }
  }
  pc.addEventListener("track", ({track, streams}) => {
    if (track.kind !== "audio" || live.pc !== pc) return;
    const stream = streams[0] || new MediaStream([track]);
    const remote = $("remote");
    remote.muted = false;
    remote.srcObject = stream;
    live.outbound = meter(live.context, stream);
    remote.play().catch(() => error("浏览器未允许播放语音，请重新发起通话。"));
  });
  pc.addEventListener("connectionstatechange", () => {
    if (live.pc !== pc) return;
    if (pc.connectionState === "failed") {
      error("WebRTC 连接失败，请检查麦克风权限与本机网络。");
      disconnect("连接失败");
    }
  });
  const dc = pc.createDataChannel("control", {ordered: true});
  live.dc = dc;
  dc.addEventListener("message", voiceMessage);
  dc.addEventListener("open", () => send("start"));
  const iceReady = new Promise((resolve) => {
    pc.onicegatheringstatechange = () => { if (pc.iceGatheringState === "complete") resolve(); };
    setTimeout(resolve, 2200);
  });
  await pc.setLocalDescription(await pc.createOffer());
  await iceReady;
  const answer = await request("/api/offer", {case_id: caseId, sdp: pc.localDescription.sdp, record,
    microphone: Boolean(live.mic)});
  live.id = answer.session_id;
  try {
    await pc.setRemoteDescription({type: "answer", sdp: answer.sdp});
  } catch (err) {
    await disconnect("连接失败");
    throw err;
  }
  setTimeout(() => {
    if (live.pc === pc && dc.readyState !== "open") {
      error("语音连接超时，请重新发起。");
      disconnect("连接超时");
    }
  }, 10000);
}

async function registerJingmai() {
  const caseId = $("case").value;
  prepare("text");
  current("正在登记京麦结果");
  const response = await request("/api/text", {case_id: caseId});
  live.id = response.session_id;
  reply(response);
}

async function disconnect(label) {
  if (live.closing) return;
  live.closing = true;
  const {pc, mic, context, id, mode} = live;
  live.mode = null;
  $("live-actions").hidden = true;
  $("text-form").hidden = true;
  $("manual-example").hidden = true;
  $("partial").hidden = true;
  $("microphone").hidden = true;
  $("composer-idle").hidden = false;
  $("composer-idle").textContent = live.done ? "本次处理已结束，可查看质检或发起新的通话" : "通话接通后，可在此输入商家回复";
  $("mode").textContent = live.done ? "已结束" : "未连接";
  $("dialog-hint").textContent = live.done
    ? "本次处理已结束，可查看质检详情或发起新的通话。"
    : "连接已结束，可以重新发起通话。";
  current(label, false);
  live.pc = null;
  live.dc = null;
  live.sender = null;
  live.mic = null;
  live.inbound = null;
  live.outbound = null;
  live.waiting = null;
  live.drain = null;
  clearTimeout(live.drainTimer);
  live.drainTimer = null;
  pc?.close();
  for (const track of mic?.getTracks() ?? []) track.stop();
  if (context && context.state !== "closed") await context.close().catch(() => {});
  $("remote").srcObject = null;
  if (mode === "voice" && id) await request("/api/calls/" + id + "/close", {}).catch(() => {});
  live.closing = false;
  renderRoute();
}

async function command(type) {
  if (!live.id) return;
  if (live.mode === "voice") send(type);
  if (live.mode === "text") reply(await request("/api/text/" + live.id + "/terminal", {command: type}));
}

function med(values) {
  const ordered = values.slice().sort((a, b) => a - b);
  const middle = Math.floor(ordered.length / 2);
  return Math.round(ordered.length % 2 ? ordered[middle] : (ordered[middle] + ordered[middle - 1]) / 2);
}

async function rtcStats(pc) {
  const values = {};
  for (const report of (await pc.getStats()).values()) {
    if (report.type === "inbound-rtp" && (report.kind === "audio" || report.mediaType === "audio")) {
      values.loss = Math.max(0, report.packetsLost ?? 0);
      values.packets = report.packetsReceived;
      values.jitter_ms = Math.round(1000 * (report.jitter ?? 0));
      values.concealed_samples = report.concealedSamples;
    }
    if (report.type === "candidate-pair" && report.state === "succeeded" && report.currentRoundTripTime != null) {
      values.rtt_ms = Math.round(report.currentRoundTripTime * 1000);
    }
  }
  if (live.pc === pc) send("rtc_stats", values);
}

const wave = $("wave").getContext("2d");
function tick(now) {
  const input = rms(live.inbound);
  const output = rms(live.outbound);
  if (output > .008) live.lastOutput = now;
  if (live.drain !== null && now - live.drain >= 480 && now - live.lastOutput >= 160) {
    disconnect("处理结果已登记");
  }
  if (live.speaking && input > .009) live.lastMic = now;
  if (live.waiting && now - live.waiting.at > 7000) live.waiting = null;
  if (live.waiting && output > .008 && now > live.waiting.end) {
    const ms = now - live.waiting.end;
    if (ms < 15000) {
      live.latencies.push(ms);
      $("live-latency").textContent = med(live.latencies) + " ms";
      send("client_audio", {turn: live.waiting.turn, ms});
    }
    live.waiting = null;
  }
  if (live.mode && live.start) {
    const secs = Math.floor((now - live.start) / 1000);
    $("clock").textContent = [Math.floor(secs / 60), secs % 60].map((n) => String(n).padStart(2, "0")).join(":");
  }
  if (live.pc?.connectionState === "connected" && now - live.lastStats > 2300) {
    live.lastStats = now;
    rtcStats(live.pc).catch(() => {});
  }
  wave.clearRect(0, 0, 440, 100);
  const energy = Math.min(1, Math.max(input, output) * 12);
  wave.fillStyle = output > input ? "#668ac0" : "#7c9e93";
  for (let index = 0; index < 48; index++) {
    const shape = Math.sin(Math.PI * index / 48);
    const signal = .35 + Math.abs(Math.sin(index * 1.52 + now / 95)) * .65;
    const height = 2 + shape * (energy > .02 ? 82 * energy * signal : 2);
    wave.globalAlpha = .42 + .55 * shape;
    wave.fillRect(4 + index * 9, 50 - height / 2, 4, height);
  }
  requestAnimationFrame(tick);
}

function metric(label, value) {
  const node = element("div", "metric");
  node.append(element("span", "", label), element("strong", "", value));
  return node;
}

const ms = (value) => value == null ? "—" : value + " ms";

async function readHistory() {
  const sequence = ++historyRequest;
  const [list, stats] = await Promise.all([request("/api/calls"), request("/api/metrics")]);
  if (sequence !== historyRequest) return;
  callRecords = list;
  $("metrics").replaceChildren(metric("完成通知 / 电话处理", stats.completed + " / " + stats.phone_finished),
    metric("播放 P50", ms(stats.client_p50)), metric("播放 P95", ms(stats.client_p95)),
    metric("严重输出违规", stats.violations));
  await filterHistory();
}

async function filterHistory() {
  const query = $("history-search").value.trim().toLowerCase();
  const filter = $("history-filter").value;
  const list = callRecords.filter((call) => {
    const task = taskList.find((item) => item.id === call.case_id);
    return (filter === "all" || (call.payload?.disposition ?? "pending") === filter)
      && [call.case_id, call.id, task?.merchant ?? ""].join(" ").toLowerCase().includes(query);
  });
  $("history-count").textContent = list.length + " / " + callRecords.length + " 条";
  if (!list.some((call) => call.id === qualityId)) qualityId = list[0]?.id ?? null;
  $("call-list").replaceChildren();
  if (!list.length) {
    const row = element("tr");
    const cell = element("td", "list-empty", callRecords.length ? "没有符合条件的通话记录" : "暂无通话记录，完成一次外呼后会显示在这里");
    cell.colSpan = 4;
    row.append(cell);
    $("call-list").append(row);
    $("quality").replaceChildren(element("div", "panel-heading", "质检详情"),
      element("div", "empty", "暂无可查看的通话"));
    return;
  }
  for (const call of list) {
    const row = element("tr", qualityId === call.id ? "active" : "");
    const task = taskList.find((item) => item.id === call.case_id);
    const details = element("td", "mono", call.case_id);
    details.append(element("small", "", (task?.merchant ?? "") + " · " + (call.mode === "voice" ? "语音" : "文字")));
    const date = new Date(call.started * 1000);
    const time = element("td", "date-cell", date.toLocaleDateString("zh-CN", {month: "2-digit", day: "2-digit"}));
    time.append(element("small", "", date.toLocaleTimeString("zh-CN", {hour12: false})));
    const status = element("td");
    status.append(element("span", "chip " + (call.payload?.disposition === "notified" ? "ok" : "neutral"),
      statusText[call.payload?.disposition] || "未回写"));
    const action = element("td");
    const button = element("button", "row-link", "查看");
    button.setAttribute("aria-label", "查看 " + call.case_id + " " + date.toLocaleTimeString("zh-CN") + " 的质检");
    button.setAttribute("aria-pressed", String(qualityId === call.id));
    button.addEventListener("click", () => { qualityId = call.id; filterHistory().catch(showQualityError); });
    action.append(button);
    row.append(details, time, status, action);
    $("call-list").append(row);
  }
  if (qualityId) await readQuality(qualityId);
}

function showQualityError(exc) { $("quality").replaceChildren(element("p", "error", exc.message)); }

async function readQuality(id) {
  const report = await request("/api/calls/" + id + "/quality");
  if (id !== qualityId) return;
  const tags = element("dl", "quality-meta");
  for (const [label, value] of [["任务阶段", report.stages.length + " / 6"], ["沟通轮次", report.turns],
    ["播报打断", report.audio.interruptions], ["合规拦截", report.compliance_intercepted],
    ["播放 P50", ms(report.audio.client_p50)], ["出包 P50", ms(report.audio.server_p50)]]) {
    const field = element("div");
    field.append(element("dt", "", label), element("dd", "", value));
    tags.append(field);
  }
  const exports = element("div", "exports");
  for (const [suffix, label] of [["trace", "下载原始日志"], ...(report.recording ? [["audio", "下载通话录音"]] : [])]) {
    const link = element("a", "", label);
    link.href = "/api/calls/" + id + "/" + suffix;
    exports.append(link);
  }
  const transcript = element("div", "detail-transcript");
  for (const line of report.transcript) transcript.append(bubble(line.role, line.text, line.source, line.answer_model));
  const evidence = report.receipt ? "回执 " + report.receipt + (report.ticket ? " · 工单 " + report.ticket : "") : "此通话尚未回写";
  const stats = report.audio.rtc_stats;
  const header = element("div", "panel-heading");
  const title = element("div");
  title.append(element("h2", "", "质检详情 · " + report.case_id), element("p", "mono", "会话 " + id.slice(0, 12)));
  header.append(title, element("span", "chip " + (report.complete ? "ok" : "neutral"), statusText[report.result?.disposition] ?? "未回写"));
  const info = element("div", "quality-evidence");
  info.append(element("p", "mono", evidence));
  if (stats) info.append(element("p", "", "WebRTC 抖动 " + ms(stats.jitter_ms) + " · 丢包 " + (stats.loss ?? "—")));
  info.append(exports);
  $("quality").replaceChildren(header, tags, info, transcript);
}

async function readRsi() {
  const data = await request("/api/rsi");
  $("revision").textContent = data.active;
  $("rollback").disabled = data.active === "baseline";
  $("rsi-list").replaceChildren();
  if (!data.history.length) $("rsi-list").append(element("div", "empty", "暂无评测记录，点击右上角运行评测。"));
  for (const run of data.history) {
    const card = element("details", "rsi-run");
    card.open = data.history[0] === run;
    const heading = element("summary");
    heading.append(element("span", "mono", run.id), element("span", "chip " + (run.activated ? "ok" : "neutral"),
      run.id === data.active ? "当前使用" : run.activated ? "回归通过" : "保留当前版本"));
    const metrics = element("div", "metrics");
    for (const [group, label] of [["dev", "开发集"], ["regression", "回归集"], ["holdout", "封存集"]]) {
      const before = run.summary[group + "_before"];
      const after = run.summary[group + "_after"];
      metrics.append(metric(label + " · " + before.count + " 条", before.passed + " → " + after.passed));
    }
    const link = element("a", "link-button", "下载评测记录");
    link.href = "/api/rsi/" + run.id + "/trace";
    const patch = Object.entries(run.patch).map(([id, words]) => id + " · " + words.join("、")).join(" ｜ ");
    const content = element("div");
    content.append(element("p", "hint", patch ? "新增匹配词：" + patch : "本轮无可验证的新补丁。"), metrics, link);
    card.append(heading, content);
    $("rsi-list").append(card);
  }
}

function rsiStatus(text) {
  $("rsi-status").textContent = text;
  $("rsi-status").hidden = !text;
}

function run(handler) {
  return async (...args) => {
    try { await handler(...args); }
    catch (err) {
      error(err.message);
      if (live.mode && !live.id) await disconnect("暂时未连接");
    }
  };
}

$("case").addEventListener("change", () => selectTask($("case").value));
$("task-search").addEventListener("input", renderTasks);
for (const button of document.querySelectorAll("[data-route]")) button.addEventListener("click", () => {
  taskRoute = button.dataset.route;
  for (const filter of document.querySelectorAll("[data-route]")) {
    filter.classList.toggle("selected", filter === button);
    filter.setAttribute("aria-pressed", String(filter === button));
  }
  renderTasks();
});
$("history-search").addEventListener("input", () => filterHistory().catch(showQualityError));
$("history-filter").addEventListener("change", () => filterHistory().catch(showQualityError));
$("start").addEventListener("click", run(() => startVoice(true)));
$("text-start").addEventListener("click", run(() => startVoice(false)));
$("microphone").addEventListener("click", run(() => setMicrophone(!live.mic)));
$("no-answer").addEventListener("click", run(() => command("no_answer")));
$("hangup").addEventListener("click", run(() => command($("hangup").textContent === "重试回写" ? "retry" : "hangup")));
$("text-form").addEventListener("submit", (event) => {
  event.preventDefault();
  run(async () => {
    const input = $("message-input");
    const text = input.value.trim();
    if (!text || live.mode !== "voice" || live.done || input.disabled) return;
    const callId = live.id;
    error();
    input.disabled = true;
    $("text-send").disabled = true;
    $("text-send").textContent = "处理中";
    try {
      await request("/api/calls/" + callId + "/turn", {text});
      if (live.id === callId) input.value = "";
    } finally {
      input.disabled = false;
      $("text-send").disabled = false;
      $("text-send").textContent = "发送";
      if (live.id === callId && live.mode === "voice" && !live.done) input.focus();
    }
  })();
});
for (const nav of document.querySelectorAll("nav button")) nav.addEventListener("click", () => navigate(nav.dataset.page));
$("open-quality").addEventListener("click", () => {
  $("history-search").value = "";
  $("history-filter").value = "all";
  navigate("history");
});
$("refresh").addEventListener("click", () => readHistory().catch(showQualityError));
$("run-rsi").addEventListener("click", async () => {
  $("run-rsi").disabled = true;
  rsiStatus("正在重放开发集、回归集和封存集…");
  try {
    const result = await request("/api/rsi/run", {});
    await readRsi();
    rsiStatus(result.activated ? "回归通过，后续通话将使用新版本。" : "已完成评测，当前版本继续生效。");
  } catch (err) { rsiStatus(err.message); }
  finally { $("run-rsi").disabled = false; }
});
$("rollback").addEventListener("click", async () => {
  try { await request("/api/rsi/rollback", {revision: "baseline"}); await readRsi(); rsiStatus("已恢复基线。"); }
  catch (err) { rsiStatus(err.message); }
});
window.addEventListener("pagehide", () => {
  if (live.mode === "voice" && live.id) navigator.sendBeacon("/api/calls/" + live.id + "/close");
});

function loadWorkbench() {
  error();
  $("readiness").textContent = "正在连接服务";
  $("task-list").replaceChildren(element("p", "list-empty", "正在加载任务…"));
  return Promise.all([request("/api/status"), request("/api/cases")]).then(([status, tasks]) => {
    ready = status.voice;
    taskList = tasks;
    $("scene").textContent = status.scene;
    $("readiness").textContent = ready ? "离线语音就绪" : "语音模型未就绪";
    $("readiness").className = "service-status " + (ready ? "ok" : "pending");
    $("case").replaceChildren(...tasks.map((task) => {
      const option = element("option", "", task.id + " · " + task.title);
      option.value = task.id;
      return option;
    }));
    renderRoute();
  }).catch((err) => {
    $("readiness").textContent = "服务未连接";
    $("readiness").className = "service-status pending";
    const failure = element("div", "load-error");
    const retry = element("button", "secondary compact", "重新连接");
    retry.addEventListener("click", loadWorkbench);
    failure.append(element("p", "hint", "任务加载失败"), retry);
    $("task-list").replaceChildren(failure);
    error(err.message);
  });
}
loadWorkbench();
requestAnimationFrame(tick);
