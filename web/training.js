const $ = (id) => document.getElementById(id);
const groups = {dev: "开发集", regression: "回归集", holdout: "封存集"};
const materialStates = {
  reviewed: ["已审核", "neutral"], pending: ["待试答", "pending"], active: ["已启用", "ok"],
  pending_pause: ["待停用", "pending"], paused: ["未纳入", "neutral"],
};
const trialKinds = {primary: "标准问法", alternate: "陌生问法", contrast: "范围对照", fallback: "异常回退"};
const grades = {correct: "相符", partial: "部分相符", incorrect: "不相符"};
const replyStates = {
  setup_failed: "未建立试答会话", replay_failed: "试答失败", blocked: "答复被拦截",
  timeout: "外呼模型响应超时", request_failed: "外呼模型请求失败", invalid: "外呼模型返回格式有误", unsafe_reply: "答复被拦截",
};
const isDraft = (material) => ["pending", "pending_pause"].includes(material.status);
const rate = (sample) => sample?.count ? sample.passed * 100 / sample.count : null;
const percent = (value) => value == null ? "—" : `${Number(value.toFixed(1))}%`;
const dateText = (value, full = false) => new Intl.DateTimeFormat("zh-CN", {
  month: "numeric", day: "numeric", hour: "2-digit", minute: "2-digit", ...(full ? {year: "numeric"} : {}),
}).format(new Date(value));

function favoriteIds() {
  try {
    const values = JSON.parse(localStorage.getItem("zhiyu-training-favorites") || "[]");
    return new Set(Array.isArray(values) ? values : []);
  } catch { return new Set(); }
}

async function copyText(text) {
  try { await navigator.clipboard.writeText(text); }
  catch {
    const input = document.createElement("textarea");
    const previous = document.activeElement;
    input.className = "sr-only";
    input.value = text;
    document.body.append(input);
    input.select();
    try {
      if (!document.execCommand("copy")) throw new Error("当前浏览器无法复制，可选中文本手动复制。");
    } finally { input.remove(); previous?.focus(); }
  }
}

export function setupTraining({request, element, metric}) {
  let library;
  let results;
  let busy = false;
  let editing;
  let saving = false;
  let favoriteOnly = false;
  let loadId = 0;
  let trialMaterial;
  let samplesChanged = false;
  const trialOptions = new Map();
  const favorites = favoriteIds();
  const form = $("material-form");
  const dialog = $("material-dialog");
  const field = (name) => form.elements.namedItem(name);
  const optionsForm = $("trial-options-form");
  const optionsDialog = $("trial-options-dialog");
  const trialField = (name) => optionsForm.elements.namedItem(name);
  const latestRun = () => results?.history[0];

  function feedback(id, message = "", failed = false) {
    const node = $(id);
    node.textContent = message;
    node.hidden = !message;
    node.classList.toggle("training-error", failed);
  }

  function action(label, callback, classes = "secondary compact") {
    const button = element("button", classes, label);
    button.type = "button";
    button.addEventListener("click", callback);
    return button;
  }

  function searchMatches(query, fields) {
    return fields.some((value) => String(value || "").toLocaleLowerCase().includes(query));
  }

  function materialState(item) {
    if (latestRun()?.ready_to_publish && !samplesChanged && isDraft(item)) {
      return [item.status === "pending_pause" ? "待发布停用" : "待发布", "pending"];
    }
    return materialStates[item.status];
  }

  function renderMaterials() {
    if (!library) return;
    const query = $("material-search").value.trim().toLocaleLowerCase();
    const status = $("material-filter").value;
    const rows = library.materials.filter((item) => {
      const statusMatches = status === "all" || item.status === status || status === "pending" && item.status === "pending_pause";
      return statusMatches && searchMatches(query, [item.question, item.answer, item.source, ...item.aliases]);
    });
    $("material-list").replaceChildren(...rows.map((item) => {
      const [label, color] = materialState(item);
      const meta = item.scopes.map((scope) => library.scopes[scope] || scope).join(" / ");
      const row = element("li", "training-item");
      const text = element("div", "training-item-main");
      text.append(element("strong", "", item.question), element("small", "", `${meta} · ${item.source}`));
      const button = action(item.edit_version ? "编辑" : "查看", () => openMaterial(item), "row-link");
      button.setAttribute("aria-label", `${button.textContent} ${item.question}`);
      button.dataset.materialControl = "";
      row.append(text, element("span", `chip ${color}`, label));
      if (isDraft(item)) {
        const setup = action(trialOptions.has(item.id) ? "已设样本" : "试答样本", () => openTrialOptions(item), "row-link");
        setup.setAttribute("aria-label", `${setup.textContent} ${item.question}`);
        setup.dataset.materialControl = "";
        row.append(setup);
      }
      row.append(button);
      return row;
    }));
    if (!rows.length) $("material-list").append(element("li", "training-empty", "没有符合条件的资料。"));
    $("material-count").textContent = `显示 ${rows.length} / ${library.materials.length} 条资料`;
    renderControls();
  }

  function openMaterial(item = null) {
    editing = item;
    const readOnly = Boolean(item && !item.edit_version);
    $("material-title").textContent = readOnly ? "查看已审核资料" : item ? "编辑训练资料" : "新增训练资料";
    $("material-answer-label").textContent = readOnly ? "核准答复" : "拟采用答复";
    field("scope").replaceChildren(...Object.entries(library.scopes).map(([key, title]) => new Option(title, key)));
    for (const key of ["question", "answer", "source", "aliases"]) field(key).readOnly = readOnly;
    field("scope").disabled = readOnly;
    field("question").value = item?.question ?? "";
    field("answer").value = item?.answer ?? "";
    field("source").value = item?.source ?? "";
    field("scope").value = item?.scope ?? item?.scopes[0] ?? "*";
    field("aliases").value = item?.aliases.join("\n") ?? "";
    field("enabled").checked = item?.enabled ?? true;
    field("enabled").parentElement.hidden = readOnly;
    $("material-actions").hidden = readOnly;
    const origin = $("material-origin");
    origin.hidden = !readOnly || !item.source_rows.length;
    origin.textContent = origin.hidden ? "" : `适用原表行：${item.source_rows.join("、")}`;
    feedback("material-edit-status", readOnly ? "" : item
      ? `${materialState(item)[0]}。保存草稿后需重新试答。`
      : "保存草稿后，运行试答并发布。可以在发布前修改或取消启用。");
    feedback("material-form-error");
    dialog.showModal();
    if (!readOnly) field("question").focus();
  }

  function openTrialOptions(item) {
    trialMaterial = item;
    const current = trialOptions.get(item.id) || {};
    $("trial-options-question").textContent = item.status === "pending_pause" ? "停用试答使用当前生效版本中的问法。" : item.question;
    trialField("alternate").value = current.alternate || "";
    const choices = (library.trial_cases || []).filter((itemCase) => item.scope === "*" || !itemCase.scopes.includes(item.scope));
    trialField("contrast_case_id").replaceChildren(new Option("自动选择", ""),
      ...choices.map(({id, title}) => new Option(`${id} · ${title}`, id)));
    trialField("contrast_case_id").value = current.contrast_case_id || "";
    optionsDialog.showModal();
  }

  function renderScripts() {
    if (!library) return;
    const query = $("script-search").value.trim().toLocaleLowerCase();
    const stage = $("script-filter").value;
    const rows = library.scripts.filter((item) => (stage === "all" || item.stage === stage)
      && (!favoriteOnly || favorites.has(item.id)) && searchMatches(query, [item.title, item.text, item.source]));
    $("script-list").replaceChildren(...rows.map((item) => {
      const row = element("li", "script-item");
      const detail = element("details");
      const title = element("summary");
      const body = element("div", "script-body");
      const actions = element("div", "training-actions");
      title.append(element("span", "", item.title), element("span", "chip neutral", library.stages[item.stage]));
      const copy = action("复制话术", async () => {
        try { await copyText(item.text); feedback("script-status", `已复制「${item.title}」`); }
        catch (error) { feedback("script-status", error.message, true); }
      });
      const favorite = action("", () => {
        if (favorites.has(item.id)) favorites.delete(item.id);
        else favorites.add(item.id);
        try { localStorage.setItem("zhiyu-training-favorites", JSON.stringify([...favorites])); }
        catch { /* 收藏仍在当前页面可用。 */ }
        if (favoriteOnly) renderScripts();
        else setFavorite();
      });
      function setFavorite() {
        const saved = favorites.has(item.id);
        favorite.textContent = saved ? "取消收藏" : "收藏";
        favorite.setAttribute("aria-pressed", String(saved));
      }
      setFavorite();
      actions.append(copy, favorite);
      body.append(element("p", "script-text", item.text), element("p", "subtle", item.source), actions);
      detail.append(title, body);
      row.append(detail);
      return row;
    }));
    if (!rows.length) $("script-list").append(element("li", "training-empty", favoriteOnly ? "没有符合条件的收藏话术。" : "没有符合条件的话术。"));
    $("script-count").textContent = `显示 ${rows.length} / ${library.scripts.length} 条话术`;
  }

  function renderOverview() {
    const latest = latestRun();
    const version = metric("当前知识版本", results.active === "baseline" ? "基线版本" : results.active);
    version.classList.add("training-version");
    $("rsi-latest").textContent = latest
      ? `${dateText(latest.time, true)} · ${runState(latest)[0]}`
      : "尚未运行评测";
    const metrics = Object.entries(groups).map(([key, label]) => {
      const before = rate(latest?.summary?.[`${key}_before`]);
      const after = rate(latest?.summary?.[`${key}_after`]);
      const stat = metric(`${label}通过率`, percent(after));
      if (after == null || before == null) return stat;
      const diff = Number((after - before).toFixed(1));
      stat.append(element("small", diff > 0 ? "training-up" : diff < 0 ? "training-down" : "subtle",
        diff ? `${diff > 0 ? "+" : ""}${diff} 个百分点` : "与本轮评测前持平"));
      return stat;
    });
    $("rsi-metrics").replaceChildren(version, ...metrics);
  }

  function runState(run) {
    if (run.id === results.active) return ["当前使用", "ok"];
    if (run.activated) return ["曾发布", "ok"];
    if (run.ready_to_publish) return ["待发布", "pending"];
    if (run.stale) return ["已失效", "neutral"];
    return [run.outcome === "accepted" ? "未发布" : "未通过", "neutral"];
  }

  function renderTrialAnswer(label, answer) {
    const group = element("div", "trial-answer");
    group.append(element("dt", "subtle", label), element("dd", "trial-reply", answer?.reply || "未取得答复"));
    const grade = grades[answer?.grade] || answer?.grade;
    if (grade) group.append(element("dd", "subtle", `裁判 ${grade}`));
    if (answer && answer.status !== "used") group.append(element("dd", "hint training-error", replyStates[answer.status] || "系统回退"));
    if (answer?.references?.length) group.append(element("dd", "subtle", `知识引用 ${answer.references.join("、")}`));
    return group;
  }

  function renderTrial() {
    const run = latestRun();
    const summary = $("trial-summary");
    const comparison = $("trial-comparison");
    const trial = run?.trial;
    if (!trial) {
      summary.textContent = "保存草稿后运行试答，可查看同一问法在当前版本和候选版本中的实际答复。";
      comparison.replaceChildren();
      return;
    }
    const checks = trial.checks || [];
    const state = runState(run)[0];
    const models = [trial.outbound_model && `外呼 ${trial.outbound_model}`, trial.judge_model && `裁判 ${trial.judge_model}`].filter(Boolean);
    const stale = run.stale && !run.activated ? " · 草稿、当前版本或模型设置已变化，请重新试答。" : "";
    const changed = samplesChanged && run.ready_to_publish ? " · 试答样本已调整，请重新试答。" : "";
    const passed = checks.length ? `${checks.filter((check) => check.passed).length} / ${checks.length} 项通过` : "";
    summary.textContent = [state, passed, ...models].filter(Boolean).join(" · ") + stale + changed;
    const expanded = new Set([...comparison.querySelectorAll("details[open]")].map((row) => row.dataset.material));
    const checksByMaterial = new Map();
    for (const check of checks) {
      if (!checksByMaterial.has(check.material_id)) checksByMaterial.set(check.material_id, []);
      checksByMaterial.get(check.material_id).push(check);
    }
    const rows = [...checksByMaterial].map(([id, items], index) => {
      const row = element("details", "trial-group");
      row.dataset.material = id;
      row.open = index === 0 || expanded.has(id);
      const title = element("summary");
      const first = items.find((check) => check.kind === "primary") || items[0];
      const success = items.filter((check) => check.passed).length;
      title.append(element("span", "", first.query || "试答详情"),
        element("span", `chip ${success === items.length ? "ok" : "pending"}`, `${success} / ${items.length} 通过`));
      row.append(title);
      for (const check of items) {
        const section = element("div", "trial-check");
        const header = element("div", "trial-check-header");
        const label = [trialKinds[check.kind] || "试答", check.case_id].filter(Boolean).join(" · ");
        header.append(element("strong", "", label), element("span", `chip ${check.passed ? "ok" : "pending"}`,
          check.passed ? "通过" : "未通过"));
        section.append(header);
        if (check.query && check.query !== first.query) section.append(element("p", "hint", check.query));
        if (check.expected) section.append(element("p", "hint", check.expected));
        const answers = element("dl", "trial-answers");
        answers.append(renderTrialAnswer("试答前 · 当时使用的版本", check.baseline), renderTrialAnswer("试答后 · 候选版本", check.candidate));
        section.append(answers);
        if (check.reason) section.append(element("p", check.passed ? "hint" : "hint training-error", check.reason));
        row.append(section);
      }
      return row;
    });
    if (trial.reason) rows.unshift(element("p", "training-feedback hint", trial.reason));
    comparison.replaceChildren(...rows);
  }

  function renderTrend() {
    if (!results) return;
    const trend = $("rsi-trend");
    const runs = results.history.slice(0, 8).reverse();
    if (!runs.length) {
      trend.replaceChildren(element("p", "training-empty", "完成首轮评测后，这里会显示前后的通过率。"));
      return;
    }
    const group = $("rsi-series").value;
    const legend = element("div", "chart-legend");
    legend.append(element("span", "chart-key before", "评测前"), element("span", "chart-key after", "评测后"));
    const chart = element("div", "chart-frame");
    const scale = element("div", "chart-scale");
    scale.setAttribute("aria-hidden", "true");
    for (const label of ["100%", "50%", "0%"]) scale.append(element("span", "", label));
    const series = element("div", "chart-series");
    for (const run of runs) {
      const before = rate(run.summary?.[`${group}_before`]);
      const after = rate(run.summary?.[`${group}_after`]);
      const label = `${dateText(run.time, true)}，${groups[group]} ${percent(before)} → ${percent(after)}`;
      const point = action("", () => {
        const detail = $(`assessment-${run.id}`);
        detail.open = true;
        detail.querySelector("summary").focus();
        detail.scrollIntoView({block: "nearest"});
      }, "chart-point");
      point.title = label;
      point.setAttribute("aria-label", label);
      const pair = element("span", "chart-pair");
      for (const [variant, value] of [["before", before], ["after", after]]) {
        const bar = element("i", `chart-bar ${variant}`);
        bar.style.height = `${value ?? 0}%`;
        pair.append(bar);
      }
      point.append(pair, element("span", "chart-date", dateText(run.time)), element("span", "chart-value", percent(after)));
      series.append(point);
    }
    chart.append(scale, series);
    trend.replaceChildren(legend, chart, element("p", "subtle", "最近 8 轮 · 选择柱组查看该轮记录"));
  }

  function renderHistory() {
    const list = $("rsi-list");
    const opened = new Set([...list.querySelectorAll("details[open]")].map((node) => node.id));
    list.replaceChildren(...results.history.map((run, index) => {
      const card = element("details", "rsi-run");
      card.id = `assessment-${run.id}`;
      card.open = index === 0 || opened.has(card.id);
      const [status, color] = runState(run);
      const title = element("summary");
      title.append(element("span", "mono", run.id), element("time", "subtle", dateText(run.time, true)),
        element("span", `chip ${color}`, status));
      const stats = element("div", "metrics");
      for (const [key, label] of Object.entries(groups)) {
        const before = run.summary?.[`${key}_before`];
        const after = run.summary?.[`${key}_after`];
        if (before && after) stats.append(metric(`${label} · ${before.count} 条`, `${before.passed} → ${after.passed}`));
      }
      const content = element("div");
      const patch = Object.entries(run.patch || {}).map(([key, words]) => `${key} · ${words.join("、")}`).join(" ｜ ");
      if (patch) content.append(element("p", "hint", `新增匹配词：${patch}`));
      if (run.materials?.count) content.append(element("p", "hint", `训练资料问法检查 ${run.materials.passed} / ${run.materials.count} 通过`));
      if (run.judge_model) {
        const totals = Object.entries(run.summary?.holdout_after?.judge_checks ?? {});
        const description = totals.map(([grade, count]) => `${grades[grade] ?? grade} ${count} 条`).join(" · ");
        content.append(element("p", "hint", `语义裁判 ${run.judge_model} · ${description || "本轮没有可用的抽检结果"}`));
      }
      if (run.regressions?.length) content.append(element("p", "hint", `发现 ${run.regressions.length} 项回归退化`));
      if (run.holdout_regressions?.length) content.append(element("p", "hint", `发现 ${run.holdout_regressions.length} 项封存集退化`));
      if (run.materials?.issues?.length) {
        const issues = element("ul", "training-issues");
        for (const issue of run.materials.issues) issues.append(element("li", "", `「${issue.query}」${issue.reason}`));
        content.append(issues);
      }
      if (!patch && !run.materials?.changed && !run.materials?.count) content.append(element("p", "hint", "本轮未发现可用的知识改动。"));
      const actions = element("div", "training-actions");
      const link = element("a", "link-button", "下载评测记录");
      link.href = `/api/rsi/${encodeURIComponent(run.id)}/trace`;
      actions.append(link);
      if (run.activated && run.id !== results.active) {
        const restore = action("恢复此版本", () => changeVersion(run.id));
        restore.dataset.restoreVersion = run.id;
        restore.disabled = busy;
        actions.append(restore);
      }
      if (stats.childElementCount) content.append(stats);
      content.append(actions);
      card.append(title, content);
      return card;
    }));
    if (!results.history.length) list.append(element("p", "training-empty", "暂无记录，可点击上方运行一轮 RSI。"));
  }

  function renderControls() {
    const locked = busy || saving;
    $("run-rsi").disabled = locked || !results || !library;
    $("publish-rsi").disabled = locked || samplesChanged || !latestRun()?.ready_to_publish;
    $("rollback").disabled = locked || !results || results.active === "baseline";
    $("material-add").disabled = locked || !library;
    document.querySelectorAll("#rsi [data-restore-version], #rsi [data-material-control], #rsi [data-ai-settings]")
      .forEach((button) => { button.disabled = locked; });
  }

  async function load() {
    const id = ++loadId;
    const [data, history] = await Promise.allSettled([request("/api/training"), request("/api/rsi")]);
    if (id !== loadId) return;
    if (data.status === "fulfilled") {
      library = data.value;
      const filter = $("script-filter");
      const selected = filter.value;
      filter.replaceChildren(new Option("全部环节", "all"), ...Object.entries(library.stages).map(([key, label]) => new Option(label, key)));
      filter.value = selected;
      renderScripts();
    } else {
      $("material-count").textContent = "资料暂不可用";
      $("script-count").textContent = "话术暂不可用";
      feedback("material-status", data.reason.message, true);
    }
    if (history.status === "fulfilled") {
      results = history.value;
      renderOverview();
      renderTrend();
      renderHistory();
      renderTrial();
    } else {
      results = undefined;
      feedback("rsi-status", history.reason.message, true);
    }
    if (data.status === "fulfilled") renderMaterials();
    renderControls();
  }

  async function changeVersion(revision) {
    if (busy || saving || !results) return;
    busy = true;
    renderControls();
    try {
      await request("/api/rsi/rollback", {revision});
      await load();
      feedback("rsi-status", revision === "baseline" ? "已恢复基线版本。" : `已恢复版本 ${revision}。`);
    } catch (error) { feedback("rsi-status", error.message, true); }
    finally { busy = false; renderControls(); }
  }

  async function publishVersion() {
    const run = latestRun();
    if (busy || saving || samplesChanged || !run?.ready_to_publish) return;
    busy = true;
    renderControls();
    try {
      const published = await request("/api/rsi/publish", {revision: run.id});
      await load();
      feedback("rsi-status", `已发布 ${published.active}。新会话将使用此版本。`);
    } catch (error) {
      await load();
      feedback("rsi-status", error.message, true);
    } finally { busy = false; renderControls(); }
  }

  $("material-search").addEventListener("input", renderMaterials);
  $("material-filter").addEventListener("change", renderMaterials);
  $("material-add").addEventListener("click", () => openMaterial());
  $("material-close").addEventListener("click", () => dialog.close());
  dialog.addEventListener("cancel", (event) => { if (saving) event.preventDefault(); });
  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    if (busy || saving || editing && !editing.edit_version) return;
    const payload = Object.fromEntries(["question", "answer", "source", "scope"].map((name) => [name, field(name).value.trim()]));
    payload.aliases = field("aliases").value.split("\n").map((value) => value.trim()).filter(Boolean);
    payload.enabled = field("enabled").checked;
    if (editing) payload.edit_version = editing.edit_version;
    saving = true;
    $("material-save").disabled = $("material-close").disabled = true;
    renderControls();
    feedback("material-form-error");
    try {
      const path = "/api/training/materials" + (editing ? `/${editing.id}` : "");
      const saved = await request(path, payload, editing ? "PUT" : "POST");
      dialog.close();
      $("material-search").value = saved.question;
      $("material-filter").value = "all";
      await load();
      feedback("material-status", payload.enabled ? "草稿已保存。运行试答通过后即可发布。"
        : "已保存为未纳入。已启用的资料将在停用变更发布后停止使用。");
      $("material-search").focus();
    } catch (error) { feedback("material-form-error", error.message, true); }
    finally {
      saving = false;
      $("material-save").disabled = $("material-close").disabled = false;
      renderControls();
    }
  });
  $("trial-options-close").addEventListener("click", () => optionsDialog.close());
  optionsForm.addEventListener("submit", (event) => {
    event.preventDefault();
    const id = trialMaterial.id;
    const option = {alternate: trialField("alternate").value.trim(), contrast_case_id: trialField("contrast_case_id").value};
    const previous = trialOptions.get(id) || {alternate: "", contrast_case_id: ""};
    if (option.alternate !== previous.alternate || option.contrast_case_id !== previous.contrast_case_id) {
      samplesChanged = Boolean(latestRun()?.ready_to_publish);
      if (option.alternate || option.contrast_case_id) trialOptions.set(id, option);
      else trialOptions.delete(id);
      renderMaterials();
      renderTrial();
    }
    optionsDialog.close();
  });
  $("script-search").addEventListener("input", renderScripts);
  $("script-filter").addEventListener("change", renderScripts);
  $("script-favorites").addEventListener("click", () => {
    favoriteOnly = !favoriteOnly;
    $("script-favorites").setAttribute("aria-pressed", String(favoriteOnly));
    renderScripts();
  });
  $("rsi-series").addEventListener("change", renderTrend);
  $("rollback").addEventListener("click", () => changeVersion("baseline"));
  $("publish-rsi").addEventListener("click", publishVersion);
  $("run-rsi").addEventListener("click", async () => {
    if (busy || saving || !results || !library) return;
    busy = true;
    renderControls();
    feedback("rsi-status", "正在对比当前版本与候选版本的答复，并运行回归检查…");
    try {
      const options = Object.fromEntries(library.materials.filter((item) => isDraft(item) && trialOptions.has(item.id))
        .map(({id}) => [id, Object.fromEntries(Object.entries(trialOptions.get(id)).filter(([, value]) => value))]));
      const result = await request("/api/rsi/run", {options});
      samplesChanged = false;
      await load();
      const message = result.ready_to_publish ? "试答与回归检查通过，可以发布知识版本。"
        : result.trial?.reason || (result.trial?.passed ? "候选版本未达到发布条件，请查看最新评测记录。" : "试答未通过，请查看答复对比和未通过原因。");
      feedback("rsi-status", message, !result.ready_to_publish);
    } catch (error) { feedback("rsi-status", error.message, true); }
    finally { busy = false; renderControls(); }
  });

  return {load};
}
