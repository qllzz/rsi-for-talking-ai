const $ = (id) => document.getElementById(id);
const groups = {dev: "开发集", regression: "回归集", holdout: "封存集"};
const materialStates = {
  reviewed: ["已审核", "neutral"], pending: ["待评测", "pending"], active: ["已启用", "ok"],
  pending_pause: ["待停用", "pending"], paused: ["未纳入", "neutral"],
};
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
  const favorites = favoriteIds();
  const form = $("material-form");
  const dialog = $("material-dialog");
  const field = (name) => form.elements.namedItem(name);

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

  function renderMaterials() {
    if (!library) return;
    const query = $("material-search").value.trim().toLocaleLowerCase();
    const status = $("material-filter").value;
    const rows = library.materials.filter((item) => {
      const statusMatches = status === "all" || item.status === status || status === "pending" && item.status === "pending_pause";
      return statusMatches && searchMatches(query, [item.question, item.answer, item.source, ...item.aliases]);
    });
    $("material-list").replaceChildren(...rows.map((item) => {
      const [label, color] = materialStates[item.status];
      const meta = item.scopes.map((scope) => library.scopes[scope] || scope).join(" / ");
      const row = element("li", "training-item");
      const text = element("div", "training-item-main");
      text.append(element("strong", "", item.question), element("small", "", `${meta} · ${item.source}`));
      const button = action(item.edit_version ? "编辑" : "查看", () => openMaterial(item), "row-link");
      button.setAttribute("aria-label", `${button.textContent} ${item.question}`);
      row.append(text, element("span", `chip ${color}`, label), button);
      return row;
    }));
    if (!rows.length) $("material-list").append(element("li", "training-empty", "没有符合条件的资料。"));
    $("material-count").textContent = `显示 ${rows.length} / ${library.materials.length} 条资料`;
  }

  function openMaterial(item = null) {
    editing = item;
    const readOnly = Boolean(item && !item.edit_version);
    $("material-title").textContent = readOnly ? "查看已审核资料" : item ? "编辑训练资料" : "新增训练资料";
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
    feedback("material-form-error");
    dialog.showModal();
    if (!readOnly) field("question").focus();
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
    const latest = results.history[0];
    const version = metric("当前知识版本", results.active === "baseline" ? "基线版本" : results.active);
    version.classList.add("training-version");
    $("rsi-latest").textContent = latest
      ? `${dateText(latest.time, true)} · ${latest.outcome === "accepted" ? "最近一轮已通过" : "最近一轮未启用"}`
      : "尚未运行评测";
    const metrics = Object.entries(groups).map(([key, label]) => {
      const before = rate(latest?.summary[`${key}_before`]);
      const after = rate(latest?.summary[`${key}_after`]);
      const stat = metric(`${label}通过率`, percent(after));
      if (after == null || before == null) return stat;
      const diff = Number((after - before).toFixed(1));
      stat.append(element("small", diff > 0 ? "training-up" : diff < 0 ? "training-down" : "subtle",
        diff ? `${diff > 0 ? "+" : ""}${diff} 个百分点` : "与本轮评测前持平"));
      return stat;
    });
    $("rsi-metrics").replaceChildren(version, ...metrics);
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
      const before = rate(run.summary[`${group}_before`]);
      const after = rate(run.summary[`${group}_after`]);
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
      const active = run.id === results.active;
      const accepted = run.outcome === "accepted";
      const title = element("summary");
      title.append(element("span", "mono", run.id), element("time", "subtle", dateText(run.time, true)),
        element("span", `chip ${active || accepted ? "ok" : "neutral"}`, active ? "当前使用" : accepted ? "已通过" : "未启用"));
      const stats = element("div", "metrics");
      for (const [key, label] of Object.entries(groups)) {
        const before = run.summary[`${key}_before`];
        const after = run.summary[`${key}_after`];
        stats.append(metric(`${label} · ${before.count} 条`, `${before.passed} → ${after.passed}`));
      }
      const content = element("div");
      const patch = Object.entries(run.patch).map(([key, words]) => `${key} · ${words.join("、")}`).join(" ｜ ");
      if (patch) content.append(element("p", "hint", `新增匹配词：${patch}`));
      if (run.materials?.count) content.append(element("p", "hint", `训练资料问法检查 ${run.materials.passed} / ${run.materials.count} 通过`));
      if (run.judge_model) {
        const labels = {correct: "相符", partial: "部分相符", incorrect: "不相符"};
        const grades = Object.entries(run.summary.holdout_after.judge_checks ?? {});
        const description = grades.map(([grade, count]) => `${labels[grade] ?? grade} ${count} 条`).join(" · ");
        content.append(element("p", "hint", `语义裁判 ${run.judge_model} · ${description || "本轮没有可用的抽检结果"}`));
      }
      if (run.regressions?.length) content.append(element("p", "hint", `发现 ${run.regressions.length} 项回归退化`));
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
      if (accepted && !active) {
        const restore = action("恢复此版本", () => changeVersion(run.id));
        restore.dataset.restoreVersion = run.id;
        restore.disabled = busy;
        actions.append(restore);
      }
      content.append(stats, actions);
      card.append(title, content);
      return card;
    }));
    if (!results.history.length) list.append(element("p", "training-empty", "暂无记录，可点击上方运行一轮 RSI。"));
  }

  function renderControls() {
    $("run-rsi").disabled = busy || !results;
    $("rollback").disabled = busy || !results || results.active === "baseline";
    document.querySelectorAll("[data-restore-version]").forEach((button) => { button.disabled = busy; });
  }

  async function load() {
    const id = ++loadId;
    const [data, history] = await Promise.allSettled([request("/api/training"), request("/api/rsi")]);
    if (id !== loadId) return;
    if (data.status === "fulfilled") {
      library = data.value;
      $("material-add").disabled = false;
      const filter = $("script-filter");
      const selected = filter.value;
      filter.replaceChildren(new Option("全部环节", "all"), ...Object.entries(library.stages).map(([key, label]) => new Option(label, key)));
      filter.value = selected;
      renderMaterials();
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
    } else feedback("rsi-status", history.reason.message, true);
    renderControls();
  }

  async function changeVersion(revision) {
    if (busy || !results) return;
    busy = true;
    renderControls();
    try {
      await request("/api/rsi/rollback", {revision});
      await load();
      feedback("rsi-status", revision === "baseline" ? "已恢复基线版本。" : `已恢复版本 ${revision}。`);
    } catch (error) { feedback("rsi-status", error.message, true); }
    finally { busy = false; renderControls(); }
  }

  $("material-search").addEventListener("input", renderMaterials);
  $("material-filter").addEventListener("change", renderMaterials);
  $("material-add").addEventListener("click", () => openMaterial());
  $("material-close").addEventListener("click", () => dialog.close());
  dialog.addEventListener("cancel", (event) => { if (saving) event.preventDefault(); });
  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    if (saving || editing && !editing.edit_version) return;
    const payload = Object.fromEntries(["question", "answer", "source", "scope"].map((name) => [name, field(name).value.trim()]));
    payload.aliases = field("aliases").value.split("\n").map((value) => value.trim()).filter(Boolean);
    payload.enabled = field("enabled").checked;
    if (editing) payload.edit_version = editing.edit_version;
    saving = true;
    $("material-save").disabled = $("material-close").disabled = true;
    feedback("material-form-error");
    try {
      const path = "/api/training/materials" + (editing ? `/${editing.id}` : "");
      const saved = await request(path, payload, editing ? "PUT" : "POST");
      dialog.close();
      $("material-search").value = saved.question;
      $("material-filter").value = "all";
      await load();
      feedback("material-status", payload.enabled ? "已保存，可在下一轮评测中检查。" : "已保存为未纳入，变更通过评测后生效。");
      $("material-search").focus();
    } catch (error) { feedback("material-form-error", error.message, true); }
    finally { saving = false; $("material-save").disabled = $("material-close").disabled = false; }
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
  $("run-rsi").addEventListener("click", async () => {
    if (busy || !results) return;
    busy = true;
    renderControls();
    feedback("rsi-status", "正在检查训练资料并评测开发集、回归集和封存集…");
    try {
      const result = await request("/api/rsi/run", {});
      await load();
      const message = result.activated ? "评测通过，新版本已启用。" : result.materials?.issues?.length
        ? "资料检查未全部通过，请查看最新记录中的问题。" : "评测已完成，当前版本继续生效。";
      feedback("rsi-status", message);
    } catch (error) { feedback("rsi-status", error.message, true); }
    finally { busy = false; renderControls(); }
  });

  return {load};
}
