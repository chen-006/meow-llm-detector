const workbench = {draft: null, selected: new Set(), locked: new Set(), sessions: [], analysis: null, active: null, timer: null};
let analysisRevision = 0;
let programUpdate = null;
const el = (tag, text, className) => {
  const node = document.createElement(tag);
  if (text !== undefined) node.textContent = text;
  if (className) node.className = className;
  return node;
};

function action(id, operation, notice = "make-notice") {
  const button = $(id);
  $(id).addEventListener("click", async () => {
    if (button.disabled || button.ariaBusy === "true") return;
    button.ariaBusy = "true";
    try { await operation(); }
    catch (error) { $(notice).textContent = errorMessage(error); }
    finally { button.ariaBusy = "false"; }
  });
}

function download(value, filename) {
  const url = URL.createObjectURL(new Blob([JSON.stringify(value, null, 2)], {type: "application/json"}));
  const link = el("a");
  link.href = url;
  link.download = filename;
  link.click();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}

function requireDraft() {
  if (!workbench.draft) throw Error(t("请先新建或导入草稿。"));
  return workbench.draft;
}

function syncDraftModels() {
  const draft = requireDraft();
  const names = $("draft-models").value.split("\n").map(value => value.trim()).filter(Boolean);
  if (names.length < 2 || new Set(names).size !== names.length) throw Error(t("请填写至少两个不同的候选模型。"));
  if (JSON.stringify(names) !== JSON.stringify(draft.models.map(model => model.request_model)) || draft.mode !== $("draft-mode").value) {
    draft.models = names.map((name, index) => ({id: `m${index + 1}`, name, request_model: name}));
    draft.mode = $("draft-mode").value;
    draft.probes.forEach(probe => probe.cells.forEach(cell => {
      cell.profile = draft.mode === "claude" ? "claude-code" : "standard";
      if (draft.mode !== "chat") delete cell.parameters?.chat_token_field;
      if (draft.mode === "gpt") delete cell.parameters?.stop;
    }));
    changed();
  }
  draft.version = $("draft-version").value;
  draft.metadata.name = $("draft-name").value;
  return draft;
}

function addPrompt(prompt) {
  const draft = requireDraft(), id = `p-${crypto.randomUUID().slice(0, 8)}`;
  draft.probes.push({id, title: prompt ? prompt.slice(0, 40) : t("新题目"), family_id: id,
    normalizer: {id: "exact_trimmed_casefold", parameters: {max_length: 128}},
    cells: [{id, prompt, system: ".", history: [], effort: "low", parameters: {max_output_tokens: 256}}]});
  workbench.selected.add(id);
}

function changed(requestChanged = true) {
  invalidateAnalysis();
  $("similar-result").replaceChildren();
  if (requestChanged) {
    workbench.sessions = [];
    $("collect-window").value = 1;
  }
  $("make-notice").textContent = t(requestChanged ? "草稿已修改，需要按新配置采集；历史会话仍保留。" : "草稿有未保存的修改；已有采样不受影响。");
  estimateCollection();
}

function invalidateAnalysis() {
  analysisRevision++;
  workbench.analysis = null;
  $("selection-result").replaceChildren();
}

function ensureTiers(draft = requireDraft()) {
  draft.tiers ||= {};
  for (const [tier, count] of Object.entries(state.tierDefaults)) {
    draft.tiers[tier] ||= {counts: {}, thresholds: {}};
    for (const probe of draft.probes) {
      for (const cell of probe.cells) draft.tiers[tier].counts[cell.id] ??= count;
    }
  }
}

function openDraft(draft) {
  clearInterval(workbench.timer); workbench.timer = null; workbench.active = null;
  $("collect-start").disabled = true; $("collect-stop").disabled = true;
  workbench.draft = draft;
  workbench.selected = new Set(draft.selected || draft.probes.map(probe => probe.id));
  workbench.locked = new Set();
  $("draft-name").value = draft.metadata?.name || draft.id;
  $("draft-mode").value = draft.mode;
  $("draft-version").value = draft.version;
  $("draft-models").value = draft.models.map(model => model.request_model || model.id).join("\n");
  ensureTiers();
  changed();
  renderProbes();
  $("make-notice").textContent = t("草稿已打开，历史采样会自动关联。");
  restoreCollectionHistory().catch(error => { $("make-notice").textContent = errorMessage(error); });
}

async function restoreCollectionHistory() {
  const draft = requireDraft();
  const history = await post("/api/project/collections", {project: draft});
  if (workbench.draft !== draft) return;
  clearInterval(workbench.timer); workbench.timer = null; workbench.active = null;
  const windows = new Map();
  history.filter(row => row.status === "complete").forEach(row => windows.set(row.window, row));
  const completed = [...windows.values()].sort((a, b) => a.window - b.window);
  workbench.sessions = completed.map(row => row.session_id);
  const latest = completed.at(-1);
  $("collect-window").value = latest ? latest.window + 1 : 1;
  $("collect-samples").value = latest ? latest.window === 1 ? 5 : 8 : 3;
  if (!$("collect-url").value && latest) $("collect-url").value = latest.base_url;
  $("collection-history").replaceChildren();
  for (const row of history) {
    const item = el("div", t("第 {window} 窗 · {status} · {done}/{total}", {window: row.window, status: uiMessage(row.status), done: row.logical_completed, total: row.planned}), "package-row");
    if (["paused", "error"].includes(row.status)) {
      const resume = el("button", t("恢复本窗"));
      resume.addEventListener("click", async () => {
        try {
          if ($("collect-url").value.trim().replace(/\/$/, "") !== row.base_url.replace(/\/$/, "")) {
            $("collect-url").value = row.base_url;
            $("collect-key").value = "";
            throw Error(t("已切回历史采集地址，请输入该地址的 Key 后再次恢复。"));
          }
          const result = await post("/api/collection/start", {resume_id: row.session_id, base_url: $("collect-url").value.trim(),
            allow_insecure: $("collect-http").checked, key: $("collect-key").value});
          workbench.active = result.session_id;
          if (!workbench.sessions.includes(result.session_id)) workbench.sessions.push(result.session_id);
          clearInterval(workbench.timer); workbench.timer = setInterval(pollCollection, 1500);
          $("collect-stop").disabled = false;
        } catch (error) { $("collect-progress").textContent = errorMessage(error); }
      });
      item.append(resume);
    }
    item.append(el("small", row.base_url));
    $("collection-history").append(item);
  }
  $("collect-ready-at").textContent = latest ? t("下一窗最早开始时间：{time}；等待期间不会发送请求。", {time: new Date(latest.next_due * 1000).toLocaleString()}) : "";
  estimateCollection();
  const running = history.find(row => ["prepared", "running", "stopping"].includes(row.status));
  $("collect-start").disabled = Boolean(running);
  $("collect-stop").disabled = !running;
  if (running) {
    workbench.active = running.session_id;
    $("collect-window").value = running.window;
    $("collect-samples").value = running.samples;
    workbench.timer = setInterval(pollCollection, 1500);
    await pollCollection();
  }
}

function estimateCollection() {
  const draft = workbench.draft;
  if (!draft) return;
  const cells = draft.probes.filter(probe => workbench.selected.has(probe.id)).reduce((n, probe) => n + probe.cells.length, 0);
  const requests = cells * draft.models.length * Number($("collect-samples").value);
  $("collect-estimate").textContent = t("仅采勾选题：{cells} 格 × {models} 模型 × {samples} 次 = {requests} 个首次请求。每任务最多重试2次，费用未知。", {cells, models: draft.models.length, samples: $("collect-samples").value, requests});
}

function renderProbes() {
  const draft = requireDraft();
  ensureTiers();
  $("probe-rows").replaceChildren();
  for (const probe of draft.probes) {
    const row = el("tr"), checkCell = el("td"), check = el("input");
    check.type = "checkbox";
    check.checked = workbench.selected.has(probe.id);
    check.setAttribute("aria-label", t("选择 {name}", {name: probe.title}));
    check.addEventListener("change", () => {
      if (check.checked) workbench.selected.add(probe.id); else workbench.selected.delete(probe.id);
      $("simulation-result").textContent = t("选择已改变，需重新模拟。");
      estimateCollection();
    });
    checkCell.append(check);
    const content = el("td"), title = el("input");
    title.value = probe.title;
    title.setAttribute("aria-label", t("题名"));
    title.addEventListener("input", () => { probe.title = title.value; changed(false); });
    content.append(title);
    for (const cell of probe.cells) {
      const prompt = el("textarea");
      prompt.rows = 2;
      prompt.value = cell.prompt;
      prompt.setAttribute("aria-label", t("精确题面"));
      prompt.addEventListener("input", () => { cell.prompt = prompt.value; changed(); });
      content.append(prompt);
    }
    const advanced = el("details");
    advanced.append(el("summary", t("高级请求设置 / 归一器")));
    const normalizer = el("select"), normalizerLabel = el("label", t("答案归一规则"));
    options(normalizer, [["exact_trimmed_casefold", t("去首尾空白，忽略大小写")], ["exact_trimmed", t("只去首尾空白")],
      ["integer", t("整数")], ["whitespace_collapse", t("合并连续空白")], ["behavior_label", t("英文名称")],
      ["fixed_enum", t("固定选项")], ["b80_exact_3", "B80"]]);
    normalizer.value = probe.normalizer.id;
    normalizer.addEventListener("change", () => {
      probe.normalizer = {id: normalizer.value, parameters: normalizer.value === "fixed_enum" ? {values: {}} : {}};
      changed(); renderProbes();
    });
    normalizerLabel.append(normalizer); advanced.append(normalizerLabel);
    if (probe.normalizer.id === "fixed_enum") {
      const label = el("label", t("固定选项，每行一个")), values = el("textarea");
      values.value = Object.keys(probe.normalizer.parameters.values || {}).join("\n");
      values.addEventListener("change", () => {
        const names = values.value.split("\n").map(value => value.trim()).filter(Boolean);
        probe.normalizer.parameters.values = Object.fromEntries(names.map(name => [name, name.toLowerCase()])); changed();
      });
      label.append(values); advanced.append(label);
    }
    for (const cell of probe.cells) {
      const group = el("div", undefined, "grid");
      for (const [key, name, min, max] of [["max_output_tokens", "最大输出 token", 1, 65536], ["temperature", "温度（留空不发送）", 0, 2], ["top_p", "top_p（留空不发送）", 0, 1]]) {
        const label = el("label", `${cell.id} · ${t(name)}`), input = el("input");
        input.type = "number"; input.min = min; input.max = max; input.step = key === "max_output_tokens" ? 1 : 0.01;
        input.value = cell.parameters?.[key] ?? (key === "max_output_tokens" ? 256 : "");
        input.addEventListener("change", () => { cell.parameters ||= {}; if (input.value === "") delete cell.parameters[key]; else cell.parameters[key] = Number(input.value); changed(); });
        label.append(input); group.append(label);
      }
      advanced.append(group);
    }
    const jsonDetails = el("details"); jsonDetails.append(el("summary", t("编辑 JSON（可选）")));
    const editor = el("textarea");
    editor.rows = 9;
    editor.value = JSON.stringify({normalizer: probe.normalizer, cells: probe.cells}, null, 2);
    const apply = el("button", t("应用高级设置"));
    apply.addEventListener("click", () => {
      try {
        const value = JSON.parse(editor.value);
        if (!Array.isArray(value.cells) || !value.cells.length) throw Error(t("至少需要一个请求格。"));
        probe.normalizer = value.normalizer;
        probe.cells = value.cells;
        const cells = new Set(draft.probes.flatMap(probe => probe.cells.map(cell => cell.id)));
        for (const tier of Object.values(draft.tiers)) for (const id of Object.keys(tier.counts)) if (!cells.has(id)) delete tier.counts[id];
        changed();
        renderProbes();
      } catch (error) { $("make-notice").textContent = errorMessage(error); }
    });
    jsonDetails.addEventListener("toggle", () => { if (jsonDetails.open) editor.value = JSON.stringify({normalizer: probe.normalizer, cells: probe.cells}, null, 2); });
    jsonDetails.append(editor, apply); advanced.append(jsonDetails);
    content.append(advanced);
    const diagnostic = el("td"), fitted = workbench.analysis?.fitted?.cells?.[probe.cells[0].id];
    if (fitted) {
      const qualities = Object.values(fitted.quality);
      diagnostic.append(el("p", t("每模型完成 {min}～{max} 条，最少 {windows} 个窗口", {min: Math.min(...qualities.map(q => q.completed)), max: Math.max(...qualities.map(q => q.completed)), windows: Math.min(...qualities.map(q => q.nonempty_windows))})));
      const pairs = Object.values(fitted.pairwise_jsd);
      const drifts = Object.values(fitted.model_drift_max).filter(value => value !== null);
      diagnostic.append(el("p", t("JSD均值 {mean} · 最弱 {weakest}", {mean: fitted.between_model_jsd?.toFixed(3) ?? t("未知"), weakest: pairs.length ? Math.min(...pairs).toFixed(3) : t("未知")})));
      diagnostic.append(el("p", t("最大漂移 {drift} · 权重 {weight}", {drift: drifts.length ? Math.max(...drifts).toFixed(3) : t("缺窗"), weight: fitted.weight.toFixed(3)})));
      const details = el("details");
      details.append(el("summary", t("模型对与窗口明细")), el("pre", JSON.stringify(fitted, null, 2)));
      diagnostic.append(details);
    } else diagnostic.textContent = t("尚未分析 / 配置已改");
    const counts = el("td");
    for (const cell of probe.cells) {
      const group = el("div", undefined, "tier-inputs");
      for (const tier of ["low", "medium", "high"]) {
        const input = el("input");
        input.type = "number"; input.min = 0; input.max = 1000;
        input.value = draft.tiers[tier].counts[cell.id];
        input.setAttribute("aria-label", t("{cell} {tier} 次数", {cell: cell.id, tier}));
        input.addEventListener("input", () => {
          draft.tiers[tier].counts[cell.id] = Number(input.value);
          invalidateAnalysis();
          $("simulation-result").textContent = t("三档次数已改变，需重新模拟。");
        });
        group.append(input);
      }
      counts.append(group);
    }
    const controls = el("td"), remove = el("button", t("移除草稿题"));
    const lockLabel = el("label", t("推荐时保留")), lock = el("input");
    lock.type = "checkbox"; lock.checked = workbench.locked.has(probe.id);
    lock.addEventListener("change", () => { if (lock.checked) workbench.locked.add(probe.id); else workbench.locked.delete(probe.id); invalidateAnalysis(); });
    lockLabel.append(lock); controls.append(lockLabel);
    remove.addEventListener("click", () => {
      draft.probes = draft.probes.filter(item => item.id !== probe.id);
      workbench.selected.delete(probe.id);
      workbench.locked.delete(probe.id);
      for (const tier of Object.values(draft.tiers)) for (const cell of probe.cells) delete tier.counts[cell.id];
      changed(); renderProbes();
    });
    controls.append(remove);
    row.append(checkCell, content, diagnostic, counts, controls);
    $("probe-rows").append(row);
  }
  estimateCollection();
  renderSelection();
}

function resultTable(headers, rows) {
  const table = el("table"), head = el("tr");
  headers.forEach(label => head.append(el("th", t(label)))); table.append(head);
  rows.forEach(values => { const row = el("tr"); values.forEach(value => row.append(el("td", value))); table.append(row); });
  const scroll = el("div", undefined, "table-scroll"); scroll.append(table); return scroll;
}

function renderSelection() {
  const box = $("selection-result"), analysis = workbench.analysis;
  box.replaceChildren();
  if (!analysis) return;
  const draft = requireDraft(), recommendation = analysis.recommendation;
  const probeName = id => draft.probes.find(probe => probe.id === id)?.title || id;
  const modelName = id => draft.models.find(model => model.id === id)?.name || id;
  const pairName = pair => pair.split("|").map(modelName).join(" / ");
  box.append(el("h3", t("选题建议")), el("p", t("推荐 {count} 题，低档共 {requests} 次请求。", {count: recommendation.selected.length, requests: recommendation.preview_requests})));
  if (!recommendation.selected.length) box.append(el("p", t("当前数据没有可推荐的题目，请查看缺窗、权重和未覆盖模型对。")));
  const steps = el("ol");
  for (const step of recommendation.reasons) {
    const pairs = Object.entries(step.contributions).filter(([,value]) => value > 0).map(([pair]) => pairName(pair)).join("；");
    steps.append(el("li", t("{probe} · {reason}；补充 {pairs}；最弱覆盖 {before} → {after}", {
      probe: probeName(step.probe_id), reason: uiMessage(step.reason), pairs,
      before: step.minimum_before.toFixed(3), after: step.minimum_after.toFixed(3)})));
  }
  box.append(steps, el("p", t("覆盖分数只用于选题，不是准确率。"), "muted"));
  if (recommendation.uncovered_pairs.length) box.append(el("p", t("仍缺少区分依据：{pairs}", {pairs: recommendation.uncovered_pairs.map(pairName).join("；")})));
  const details = el("details"); details.append(el("summary", t("模型对覆盖与未推荐原因")));
  details.append(resultTable(["模型对", "覆盖分数"], Object.entries(recommendation.coverage).sort((a,b) => a[1]-b[1]).map(([pair,value]) => [pairName(pair), value.toFixed(3)])));
  for (const [id, reason] of Object.entries(recommendation.excluded)) details.append(el("p", `${probeName(id)} · ${uiMessage(reason)}`));
  box.append(details);
  const preview = analysis.preview;
  if (!preview) return;
  box.append(el("h3", t("选题对照预览")));
  if (preview.status !== "complete") { box.append(el("p", uiMessage(preview.status))); return; }
  box.append(el("p", t("每模型 {batches} 批，仅比较唯一最高分；不是强指向通过率，也不是独立验证。", {batches: preview.batches_per_model})));
  for (const [name, group] of Object.entries(preview.groups)) box.append(el("p", t("{name}：{count} 题 / {requests} 次请求 · {probes}", {
    name: uiMessage(name), count: group.selected.length, requests: group.requests, probes: group.selected.map(probeName).join("、")})));
  box.append(el("p", t(preview.matched_cost_and_count ? "两组题数和请求数相同，共享题目使用同一批模拟答案。" : "两组题数或请求数不同，仅供参考，不作公平优劣比较。")));
  box.append(resultTable(["模型", "互补推荐", "平均 JSD 选题"], draft.models.map(model => [model.name || model.id,
    ...["recommended", "mean_jsd"].map(name => `${(preview.groups[name].correct_rates[model.id] * 100).toFixed(2)}%`)])));
  const raw = el("details"); raw.append(el("summary", t("预览混淆明细")), el("pre", JSON.stringify(preview, null, 2))); box.append(raw);
}

async function refreshWorkbench() {
  state.snapshot = await json("/api/snapshot", {cache: "no-store"});
  if (!workbench.draft) {
    const active = state.snapshot.sessions.find(row => row.kind === "collection" && state.snapshot.active.includes(row.session_id));
    const draft = state.snapshot.projects.find(item => item.id === active?.project_id);
    if (draft) openDraft(draft);
  }
  options($("draft-saved"), state.snapshot.projects.map(item => [item.id, item.value?.metadata?.name || item.metadata?.name || item.id]), t("没有保存的草稿"));
  await refreshSimulations();
  for (const [target, items, remote] of [["local-packages", state.snapshot.packages, false], ["remote-packages", state.snapshot.catalog.packages || [], true]]) {
    $(target).replaceChildren();
    for (const item of items) {
      const card = el("article", undefined, "package-row");
      card.append(el("strong", `${item.name || item.id} · ${item.version}`), el("p", `${item.mode} · ${item.publisher}`));
      const badge = sourceBadge(item);
      if (badge) card.append(badge);
      if (item.collection) card.append(el("p", t("采集来源：{sources}", {sources: (item.collection.sources || []).map(source => source.url).join(" / ")})));
      const button = el("button", t(remote ? "下载此版本" : "导出此版本"));
      button.addEventListener("click", async () => {
        try {
          if (remote) { await post("/api/catalog/install", {id: item.id, version: item.version}); await refreshWorkbench(); render(); }
          else download(await post("/api/package/export", {id: item.id, version: item.version}), `${item.id}-${item.version}.meow.json`);
        } catch (error) { $("library-notice").textContent = errorMessage(error); }
      });
      card.append(button); $(target).append(card);
    }
  }
}

function showWorkspace(identity) {
  $("jump-to-start").hidden = identity !== "detect-view";
  document.querySelectorAll(".workspace").forEach(view => { view.hidden = view.id !== identity; });
  document.querySelectorAll("[data-view]").forEach(button => {
    if (button.dataset.view === identity) button.setAttribute("aria-current", "page"); else button.removeAttribute("aria-current");
  });
}
$("jump-to-start").addEventListener("click", () => {
  $("tier").closest(".run-card").scrollIntoView({block: "start", behavior: "instant"});
});
document.querySelectorAll("[data-view],[data-jump]").forEach(button => button.addEventListener("click", () => showWorkspace(button.dataset.view || button.dataset.jump)));
$("save-current-connection").addEventListener("click", () => {
  $("preset-edit").value = "";
  $("preset-mode").value = $("mode").value;
  $("preset-url").value = $("base-url").value;
  $("preset-http").checked = $("allow-http").checked;
  $("preset-model").value = $("request-model").value;
  $("preset-key").value = $("key").value;
  $("preset-name").value = "";
  $("preset-notice").textContent = t("给这条 API 连接起个名字，确认后保存。");
  showWorkspace("settings-view"); $("preset-name").focus();
});
window.addEventListener("workspace-ready", () => refreshWorkbench().catch(error => { $("make-notice").textContent = errorMessage(error); }));
$("collect-samples").addEventListener("input", estimateCollection);
let profileTimer;
$("collect-url").addEventListener("input", () => {
  clearTimeout(profileTimer);
  $("collect-key").value = "";
  const address = $("collect-url").value;
  $("collect-profile").textContent = "";
  if (!address.trim()) return;
  profileTimer = setTimeout(async () => {
    try {
      const profile = await post("/api/collection/profile", {base_url: address, allow_insecure: $("collect-http").checked});
      if (address === $("collect-url").value && profile.provider === "openrouter") $("collect-profile").textContent = t("已识别 OpenRouter：共享并发最多4，遇到限流按响应头等待。基准会保留来源标识。");
    } catch (error) { if (address === $("collect-url").value) $("collect-profile").textContent = errorMessage(error); }
  }, 300);
});
action("draft-create", () => {
  const names = $("draft-models").value.split("\n").map(value => value.trim()).filter(Boolean);
  if (names.length < 2) throw Error(t("至少填写两个候选模型。"));
  openDraft({schema_version: 1, id: `draft-${crypto.randomUUID().slice(0, 8)}`, version: "0.1.0", mode: $("draft-mode").value,
    metadata: {name: $("draft-name").value}, models: names.map((name, index) => ({id: `m${index + 1}`, name, request_model: name})), probes: []});
});
action("draft-save", async () => {
  workbench.draft = await post("/api/project/save", {project: syncDraftModels(), selected: [...workbench.selected]});
  renderProbes();
  $("make-notice").textContent = t("草稿已保存。");
  await refreshWorkbench();
});
$("draft-name").addEventListener("input", () => { if (workbench.draft) { workbench.draft.metadata.name = $("draft-name").value; changed(false); } });
$("draft-version").addEventListener("input", () => { if (workbench.draft) { workbench.draft.version = $("draft-version").value; changed(false); } });
action("draft-export", () => download({...syncDraftModels(), selected: [...workbench.selected]}, `${workbench.draft.id}.json`));
action("draft-open", () => {
  const item = state.snapshot.projects.find(item => item.id === $("draft-saved").value);
  if (!item) throw Error(t("请选择保存的草稿。"));
  openDraft(structuredClone(item.value || item));
});
$("draft-import").addEventListener("change", async event => {
  try {
    const value = JSON.parse(await event.target.files[0].text());
    openDraft(await post("/api/project/save", {project: value}));
    await refreshWorkbench();
  } catch (error) { $("make-notice").textContent = errorMessage(error); }
});
action("probe-add", () => {
  addPrompt(""); changed(); renderProbes();
});
action("probe-bulk-add", () => {
  const draft = requireDraft();
  const known = new Set(draft.probes.flatMap(probe => probe.cells.map(cell => cell.prompt.trim().toLowerCase())));
  let added = 0, duplicates = 0;
  for (const prompt of $("probe-bulk-text").value.split("\n").map(line => line.trim()).filter(Boolean)) {
    if (known.has(prompt.toLowerCase())) { duplicates++; continue; }
    addPrompt(prompt); known.add(prompt.toLowerCase()); added++;
  }
  if (added) { changed(); renderProbes(); $("probe-bulk-text").value = ""; }
  $("make-notice").textContent = t("新增 {count} 道草稿题，跳过 {duplicates} 道重复题。请审阅后再采集。", {count: added, duplicates});
});
action("probe-select-all", () => { workbench.selected = new Set(requireDraft().probes.map(probe => probe.id)); renderProbes(); });
action("probe-similar", async () => {
  const draft = syncDraftModels(), signature = JSON.stringify(draft.probes);
  const result = await post("/api/project/similar", {project: draft});
  if (workbench.draft !== draft || signature !== JSON.stringify(draft.probes)) return;
  const box = $("similar-result"); box.replaceChildren();
  box.append(el("p", t(result.total ? "发现 {count} 对相似题，显示前 {shown} 对。仅提醒，不会自动删除。" : "没有发现相似题。", {count: result.total, shown: result.pairs.length})));
  for (const pair of result.pairs) box.append(el("p", `${draft.probes.find(p => p.id === pair.left).title} / ${draft.probes.find(p => p.id === pair.right).title} · ${(pair.similarity * 100).toFixed(1)}%`));
});
for (const id of ["recommend-maximum", "recommend-budget"]) $(id).addEventListener("input", invalidateAnalysis);
action("probe-preview", async () => { if (await analyzeCurrent(true)) renderProbes(); });
action("ai-generate", async () => {
  const draft = requireDraft();
  if (!$("ai-consent").checked) throw Error(t("请确认本次 AI 请求费用。"));
  if (["ai-input-price", "ai-output-price", "ai-budget"].some(id => $(id).value === "")) throw Error(t("请填写费率和本次预算；未知费率不能当作零。"));
  const original = structuredClone(draft);
  original.selected = [...workbench.selected];
  $("ai-consent").checked = false;
  const result = await post("/api/candidates/generate", {key: $("ai-key").value, options: {
    mode: $("ai-mode").value, model: $("ai-model").value, base_url: $("ai-url").value,
    count: Number($("ai-count").value), seed: crypto.getRandomValues(new Uint32Array(1))[0], language: $("ai-language").value,
    input_usd_per_million: Number($("ai-input-price").value), output_usd_per_million: Number($("ai-output-price").value),
    budget_usd: Number($("ai-budget").value), output_limit: Number($("ai-output-limit").value), confirmed: true,
    existing: draft.probes.flatMap(probe => probe.cells.map(cell => cell.prompt)).filter(Boolean),
  }});
  if (result.status !== "draft") throw Error(t("AI格式不合要求，诊断 {id} 已保存，不会自动重试。", {id: result.id}));
  if (workbench.draft !== draft) {
    original.probes.push(...result.probes);
    original.selected.push(...result.probes.map(probe => probe.id));
    ensureTiers(original);
    download(original, `${original.id}-ai-${result.id}.json`);
    $("make-notice").textContent = t("当前草稿未修改。AI结果已下载为原草稿 {name} 的副本，可通过导入草稿打开。", {name: original.metadata?.name || original.id});
    return;
  }
  draft.probes.push(...result.probes);
  result.probes.forEach(probe => workbench.selected.add(probe.id));
  changed(); renderProbes();
  $("make-notice").textContent = t("新增 {count} 道草稿题，跳过 {duplicates} 道重复题。请审阅后再采集。", {count: result.returned, duplicates: result.duplicates_skipped});
});

async function pollCollection() {
  try {
    const identity = workbench.active;
    const value = await json(`/api/progress/${encodeURIComponent(identity)}`);
    if (identity !== workbench.active) return;
    $("collect-start").disabled = ["prepared", "running", "stopping"].includes(value.status);
    $("collect-progress").textContent = t("{status} · 完成 {done}/{planned} · 成功 {ok} · 错误 {errors}", {status: uiMessage(value.status), done: value.logical_completed, planned: value.planned, ok: value.successful, errors: value.errors});
    if (["complete", "paused", "error"].includes(value.status)) {
      clearInterval(workbench.timer); workbench.timer = null;
      $("collect-stop").disabled = true;
      $("collect-start").disabled = false;
      if (value.status === "complete") {
        $("collect-progress").textContent += " · " + t("本窗已结束，至少1分钟后可开始下一窗。");
      }
      await restoreCollectionHistory();
    }
  } catch (error) { $("collect-progress").textContent = errorMessage(error); }
}
action("collect-start", async () => {
  if (workbench.timer) throw Error(t("当前窗口仍在采集。"));
  if (!$("collect-consent").checked) throw Error(t("请确认采集请求数量与费用。"));
  const draft = await post("/api/project/save", {project: syncDraftModels(), selected: [...workbench.selected]});
  const result = await post("/api/collection/start", {project: draft, base_url: $("collect-url").value, allow_insecure: $("collect-http").checked,
    key: $("collect-key").value, samples: Number($("collect-samples").value), window: Number($("collect-window").value),
    prior_session_id: workbench.sessions.at(-1), probe_ids: [...workbench.selected]});
  workbench.draft = draft;
  renderProbes();
  workbench.active = result.session_id;
  workbench.sessions.push(result.session_id);
  $("collect-consent").checked = false;
  $("collect-stop").disabled = false;
  workbench.timer = setInterval(pollCollection, 1500);
  await pollCollection();
});
action("collect-stop", () => post("/api/run/stop", {session_id: workbench.active}));
action("collect-analyze", async () => {
  if (!await analyzeCurrent()) return;
  renderProbes();
  $("make-notice").textContent = t("分析已完成；推荐不会自动改变你的勾选。");
});
async function analyzeCurrent(preview = false) {
  const draft = syncDraftModels(), revision = ++analysisRevision;
  const result = await post("/api/selection", {session_ids: [...workbench.sessions], project: draft,
    options: {maximum: Number($("recommend-maximum").value), request_budget: Number($("recommend-budget").value), locked: [...workbench.locked], preview}});
  if (workbench.draft !== draft || revision !== analysisRevision) return false;
  workbench.analysis = result;
  return true;
}
action("probe-recommend", async () => {
  if (!await analyzeCurrent()) return;
  if (!confirm(t("用推荐结果替换当前勾选？"))) return;
  workbench.selected = new Set(workbench.analysis.recommendation.selected); renderProbes();
});
let simulationTimer;
async function pollSimulation() {
  const identity = workbench.simulationId;
  if (!identity) return;
  const value = await json(`/api/simulation/${encodeURIComponent(identity)}`);
  if (identity !== workbench.simulationId) return;
  renderSimulation(value);
  $("simulation-export").disabled = $("simulation-install").disabled = value.status !== "complete";
  $("simulation-resume").disabled = !["paused", "error"].includes(value.status);
  $("simulate-stop").disabled = value.status !== "running";
  if (value.status !== "running") clearInterval(simulationTimer);
  return value.status;
}
async function watchSimulation(identity) {
  clearInterval(simulationTimer);
  workbench.simulationId = identity;
  if (await pollSimulation() === "running") simulationTimer = setInterval(() => pollSimulation().catch(error => { $("simulation-result").textContent = errorMessage(error); }), 1500);
}
async function refreshSimulations() {
  const tasks = await json("/api/simulations");
  options($("simulation-history"), tasks.map(item => [item.id, `${item.name || item.project_id} · ${uiMessage(item.status)} · ${item.id.slice(0, 8)}`]), t("暂无模拟"));
  const identity = tasks.find(item => item.id === workbench.simulationId)?.id || tasks[0]?.id;
  if (identity) { $("simulation-history").value = identity; await watchSimulation(identity); }
}
$("simulation-history").addEventListener("change", () => watchSimulation($("simulation-history").value).catch(error => { $("simulation-result").textContent = errorMessage(error); }));
action("simulation-resume", async () => {
  const result = await post("/api/simulation/start", {resume_id: workbench.simulationId});
  await watchSimulation(result.id);
});
function renderSimulation(value) {
  const box = $("simulation-result");
  box.replaceChildren();
  if (!value) return;
  box.append(el("p", `${value.name || value.project_id} · ${value.package_version || value.id.slice(0, 8)}`));
  const progress = value.progress || {};
  box.append(el("p", [uiMessage(value.status), ...(value.status === "running" ?
    [uiMessage(progress.tier || ""), uiMessage(progress.stage || ""),
     t("当前模型已模拟 {count} 批", {count: progress.completed_for_model || 0})] : [])].filter(Boolean).join(" · ")));
  if (value.error) box.append(el("p", uiMessage(value.error.code)));
  if (value.tiers) {
    const tiers = Object.values(value.tiers);
    const conditional = tiers.length && tiers.every(result => result.target_denominator === "simulated_batches_of_valid_answers_not_all_http_runs");
    box.append(el("p", t(conditional ? "仅统计足额有效答案的模拟批次，不代表实际发起检测的成功率。" : "此历史结果未声明统计分母，请查看原始报告。")));
    const table = el("table"), header = el("tr");
    for (const label of ["档位", "模型", "强指向线", "模拟正确指向占比", "整档验收"]) header.append(el("th", t(label)));
    table.append(header);
    for (const tier of ["low", "medium", "high"]) {
      const result = value.tiers[tier];
      if (!result) continue;
      for (const [model, rate] of Object.entries(result.correct_rates)) {
        const row = el("tr"), name = value.models?.[model] || model;
        for (const text of [uiMessage(tier), name, `${(result.thresholds[model] * 100).toFixed(3)}%`, `${(rate * 100).toFixed(3)}%`, uiMessage(result.status)]) row.append(el("td", text));
        table.append(row);
      }
    }
    const scroll = el("div", undefined, "table-scroll"); scroll.append(table); box.append(scroll);
  }
  const details = el("details"); details.append(el("summary", t("完整模拟数据")), el("pre", JSON.stringify(value, null, 2))); box.append(details);
}
action("simulate", async () => {
  if (!workbench.analysis) throw Error(t("请先分析当前配置的数据。"));
  const draft = syncDraftModels(), selected = [...workbench.selected], enabledCells = new Set(draft.probes.filter(probe => selected.includes(probe.id)).flatMap(probe => probe.cells.map(cell => cell.id)));
  if (!workbench.analysis) throw Error(t("请先分析当前配置的数据。"));
  const tiers = Object.fromEntries(Object.entries(draft.tiers).map(([name, tier]) => [name, {
    counts: Object.fromEntries(Object.entries(tier.counts).filter(([id]) => enabledCells.has(id))), thresholds: {},
  }]));
  const result = await post("/api/simulation/start", {session_ids: workbench.sessions, project: draft, selected, tiers,
    options: {target: Number($("sim-target").value), selection_target: Number($("sim-selection-target").value), batches: Object.fromEntries(["low", "medium", "high"].map(tier => [tier, Number($("sim-batches").value)]))}});
  workbench.simulationId = result.id;
  await refreshSimulations();
});
action("simulation-export", async () => {
  const result = await post("/api/simulation/export", {id: workbench.simulationId});
  download(result, `${result.id}-${result.version}.meow.json`);
});
action("simulation-install", async () => {
  await post("/api/simulation/install", {id: workbench.simulationId});
  await refreshWorkbench(); render();
  $("make-notice").textContent = t("基准已保存到本机基准库。");
});
action("simulate-stop", () => post("/api/simulation/stop", {id: workbench.simulationId}));
action("catalog-refresh", async () => { await post("/api/catalog/refresh", {}); await refreshWorkbench(); }, "library-notice");
$("package-import").addEventListener("change", async event => {
  try {
    await post("/api/package/import", {package: JSON.parse(await event.target.files[0].text())});
    await refreshWorkbench(); render();
  } catch (error) { $("library-notice").textContent = errorMessage(error); }
});

$("preset-edit").addEventListener("change", () => {
  const item = state.snapshot.endpoints.find(item => item.id === $("preset-edit").value);
  $("preset-name").value = item?.name || "";
  $("preset-mode").value = item?.mode || "gpt";
  $("preset-url").value = item?.base_url || "";
  $("preset-http").checked = item?.allow_insecure === true;
  $("preset-model").value = item?.model || "";
  $("preset-key").value = "";
});
action("preset-save", async () => {
  await post("/api/endpoint/save", {preset: {id: $("preset-edit").value || undefined,
    name: $("preset-name").value, mode: $("preset-mode").value,
    base_url: $("preset-url").value, allow_insecure: $("preset-http").checked, model: $("preset-model").value}, key: $("preset-key").value || undefined});
  $("preset-key").value = "";
  await refreshWorkbench(); renderPresets();
  $("preset-notice").textContent = t("连接已保存；key只写入系统凭据库。");
}, "preset-notice");
action("preset-delete", async () => {
  if (!$("preset-edit").value) throw Error(t("请选择要删除的连接。"));
  if (!confirm(t("删除这个连接及它独占的系统凭据？已有定时计划不受影响。"))) return;
  await post("/api/endpoint/delete", {id: $("preset-edit").value});
  await refreshWorkbench(); renderPresets();
  $("preset-notice").textContent = t("连接及对应凭据已删除。");
}, "preset-notice");
action("preset-export", () => download(state.snapshot.endpoints.map(item => ({name: item.name, mode: item.mode,
  base_url: item.base_url, model: item.model})), "meow-connections-no-keys.json"), "preset-notice");
action("schedule-delete", async () => {
  if (!confirm(t("删除已停止的定时计划及其独立凭据？检测报告仍保留。"))) return;
  await post("/api/schedule/delete", {});
  await poll();
}, "preset-notice");

action("program-check", async () => {
  programUpdate = await post("/api/program/check-update", {locale});
  $("program-status").textContent = t("当前版本 {current} · 最新发布 {latest}", {current: programUpdate.current_version, latest: programUpdate.latest_version}) +
    " · " + t(programUpdate.available ? "有可用更新" : "没有更高的已发布版本");
  $("program-notes").textContent = programUpdate.notes;
  $("program-download").disabled = !programUpdate.available || !programUpdate.download;
}, "program-status");
$("quick-update").addEventListener("click", () => {
  showWorkspace("settings-view");
  $("program-check").scrollIntoView({block: "center", behavior: "instant"});
  if (!$("program-check").disabled) $("program-check").click();
});
action("program-download", async () => {
  if (!programUpdate?.download) throw Error(t("没有可校验的当前语言更新包。"));
  if (!confirm(t("下载并校验此版本？不会覆盖现有程序或自动安装。"))) return;
  const result = await post("/api/program/download-update", {locale, version: programUpdate.latest_version, confirmed: true});
  $("program-status").textContent = t("下载已校验，尚未安装。文件：{path}", {path: result.path});
}, "program-status");

action("retention-export", async () => {
  if (!state.sessionId) throw Error(t("请先打开一份检测报告。"));
  const sessionId = state.sessionId;
  const records = [];
  let after = 0, bytes = 0, coverage;
  while (true) {
    const page = await json(`/api/retention/${encodeURIComponent(sessionId)}?after=${after}`);
    coverage = page.coverage;
    if (!page.records.length) break;
    bytes += new TextEncoder().encode(JSON.stringify(page.records)).length;
    if (bytes > 32 * 1024 * 1024) throw Error(t("留存超过32MiB浏览器导出限制；原数据仍在本机数据库，未截断或删除。"));
    records.push(...page.records);
    after = page.records.at(-1).attempt_id;
  }
  if (!records.length) throw Error(t("本次没有留存正文；未开启留存或请求在响应前中断。"));
  download({session_id: sessionId, coverage, records}, `meow-evidence-${sessionId}.json`);
}, "progress");
