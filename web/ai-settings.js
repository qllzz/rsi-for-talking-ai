const $ = (id) => document.getElementById(id);
const roles = {
  outbound: "用于新的外呼和文字会话。保存时会用一条模拟消息验证接口。",
  judge: "用于试答和封存集的语义评测。裁判会收到评测问题、参考答案和模型答复，应与外呼模型独立配置。",
};

export function setupAISettings({request, onChange}) {
  const dialog = $("ai-settings-dialog");
  const form = $("ai-settings-form");
  const field = (name) => form.elements.namedItem(name);
  const profileButtons = [...form.querySelectorAll("[data-ai-profile]")];
  let role;
  let current;
  let busy = false;
  let loadId = 0;

  function feedback(message = "", error = false) {
    const node = $("ai-settings-feedback");
    node.textContent = message;
    node.classList.toggle("ai-settings-feedback-error", error);
    node.hidden = !message;
  }

  function renderControls() {
    const locked = busy || !current?.editable;
    for (const name of ["enabled", "url", "model", "clear_token"]) field(name).disabled = locked;
    field("token").disabled = locked || field("clear_token").checked;
    $("ai-settings-clear").hidden = !current?.has_token;
    $("ai-settings-test").disabled = $("ai-settings-save").disabled = locked;
    $("ai-settings-close").disabled = busy;
    for (const button of profileButtons) {
      button.disabled = busy;
      button.setAttribute("aria-pressed", String(button.dataset.aiProfile === role));
    }
  }

  function fill(profile) {
    current = profile;
    field("enabled").checked = profile.enabled;
    field("url").value = profile.url;
    field("model").value = profile.model;
    field("token").value = "";
    field("token").placeholder = profile.has_token ? "已保存，留空保留原密钥" : "服务商要求鉴权时填写";
    field("clear_token").checked = false;
    const status = $("ai-settings-current");
    status.textContent = profile.enabled ? "已启用" : "未启用";
    status.className = `chip ${profile.enabled ? "ok" : "neutral"}`;
    renderControls();
  }

  async function load(nextRole) {
    const id = ++loadId;
    role = nextRole;
    fill({url: "", model: "", enabled: false, has_token: false, editable: false});
    $("ai-settings-current").textContent = "读取设置中";
    $("ai-settings-scope").textContent = roles[role];
    feedback();
    try {
      const profile = await request(`/api/ai-settings/${role}`);
      if (id !== loadId || !dialog.open) return;
      fill(profile);
      if (!profile.editable) feedback("此接口由启动环境管理，当前页面仅可查看。");
    } catch (error) {
      if (id !== loadId || !dialog.open) return;
      $("ai-settings-current").textContent = "读取失败";
      feedback(error.message, true);
    }
  }

  async function submit(test) {
    if (busy || !current?.editable) return;
    const enabled = field("enabled").checked;
    const url = field("url").value.trim();
    const model = field("model").value.trim();
    if ((test || enabled) && !(url && model)) {
      feedback("请填写 API 基础地址和模型名称。", true);
      (url ? field("model") : field("url")).focus();
      return;
    }
    if (!form.reportValidity()) return;
    const payload = {url, model, enabled, version: current.version};
    const token = field("token").value.trim();
    if (field("clear_token").checked) payload.token = "";
    else if (token) payload.token = token;
    busy = true;
    renderControls();
    feedback(test ? "正在发送测试消息…" : "正在保存设置…");
    try {
      const path = `/api/ai-settings/${role}`;
      const result = await request(test ? `${path}/test` : path, payload, test ? "POST" : "PUT");
      if (test) {
        feedback(`连接成功，响应耗时 ${(result.latency_ms / 1000).toFixed(2)} 秒。配置尚未更改。`);
      } else {
        fill(result);
        onChange(role, result);
        feedback(role === "outbound" ? "已保存，新会话使用此设置。" : "已保存，下次试答和评测使用此设置。");
      }
    } catch (error) { feedback(error.message, true); }
    finally { busy = false; renderControls(); }
  }

  for (const button of document.querySelectorAll("[data-ai-settings]")) {
    button.addEventListener("click", () => { dialog.showModal(); load(button.dataset.aiSettings); });
  }
  for (const button of profileButtons) button.addEventListener("click", () => load(button.dataset.aiProfile));
  $("ai-settings-close").addEventListener("click", () => dialog.close());
  dialog.addEventListener("cancel", (event) => { if (busy) event.preventDefault(); });
  dialog.addEventListener("close", () => { loadId++; field("token").value = ""; });
  field("clear_token").addEventListener("change", renderControls);
  form.addEventListener("input", () => { if (!busy && current?.editable) feedback(); });
  $("ai-settings-test").addEventListener("click", () => submit(true));
  form.addEventListener("submit", (event) => { event.preventDefault(); submit(false); });
}
