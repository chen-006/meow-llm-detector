const $ = id => document.getElementById(id);
const state = {token: "", snapshot: null, sessionId: null, timer: null, submitting: false, followLatest: true, followSchedule: false};

async function json(url, options = {}) {
  let response;
  try { response = await fetch(url, {...options, signal: options.signal || (options.method === "POST" ? undefined : AbortSignal.timeout(30000))}); }
  catch {
    const error = Error(uiMessage("backend_disconnected"));
    error.code = "backend_disconnected";
    throw error;
  }
  const value = await response.json();
  if (!response.ok) {
    const error = Error(value.error ? `${uiMessage(value.error.code)}${value.error.field ? ` · ${uiMessage(value.error.field)}` : ""}` : `HTTP ${response.status}`);
    error.code = value.error?.code;
    throw error;
  }
  return value;
}

function post(url, body) {
  return json(url, {
    method: "POST",
    headers: {"Content-Type": "application/json", "X-Meow-Token": state.token},
    body: JSON.stringify(body),
  });
}

function packages() {
  return (state.snapshot?.packages || []).filter(item => item.mode === $("mode").value &&
    (item.mode === "chat" || item.publisher === "maintainer" || $("show-reference-packages").checked))
    .sort((a,b)=>b.version.localeCompare(a.version,undefined,{numeric:true}));
}

function options(select, rows, emptyLabel) {
  const previous = select.value;
  select.replaceChildren(...rows.map(([value, label]) => new Option(label, value)));
  if (!rows.length) select.add(new Option(emptyLabel, ""));
  if (rows.some(([value]) => value === previous)) select.value = previous;
}

function selectedPackage() {
  return packages().find(item => `${item.id}|${item.version}` === $("package").value);
}

function defaultRequestModel(model) {
  return (model?.request_model || model?.id || "").replace(/^[^/]+\//, "");
}

function sourceBadge(item) {
  if (!item.source_providers?.includes("openrouter")) return null;
  const badge = document.createElement("span");
  badge.className = "source-badge";
  badge.textContent = t(item.collection?.historical_reuse ? "含 OpenRouter 采集样本" : "OpenRouter 采集来源");
  badge.title = t("按采集地址识别，不代表模型身份认证。");
  return badge;
}

function renderModels() {
  const item = selectedPackage();
  options($("claimed"), (item?.models || []).map(model => [model.id, model.name || model.id]), t("无可用模型"));
  const model = item?.models.find(model => model.id === $("claimed").value);
  const preset = state.snapshot?.endpoints.find(item => item.id === $("endpoint-preset").value && item.mode === $("mode").value);
  if (!$("request-model").value || state.claimedSelection !== model?.id) $("request-model").value = preset?.model || defaultRequestModel(model);
  state.claimedSelection = model?.id;
  updateEstimate();
  updateReady();
  $("benchmark-note").textContent = item ? (item.id.startsWith("synthetic-") ? t("这是合成测试基准，仅用于界面联调，不可判断真实模型。") :
    t("基准包含 {count} 个候选模型。来源与验证范围将在报告中保留。", {count: item.models.length})) : t("尚未安装适用基准。请先到基准库导入或下载。");
  if (item && !item.id.startsWith("synthetic-")) {
    const sources = [...new Set((item.collection?.sources || []).map(source => source.url).filter(Boolean))].join(" / ");
    $("benchmark-note").textContent += " " + t("采集来源：{sources}", {sources: sources || t("未提供")}) + " · " +
      t(item.publisher === "maintainer" ? "维护者发布" : "本地或社区参考，非维护者认证");
    const badge = sourceBadge(item);
    if (badge) $("benchmark-note").prepend(badge);
  }
}

function render() {
  options($("package"), packages().map(item => [
    `${item.id}|${item.version}`, `${item.name} · ${item.version} · ${item.publisher}`,
  ]), t("没有已安装基准包"));
  renderModels();
  renderPresets();
  $("base-url").disabled = Boolean($("endpoint-preset").value);
  updateReady();
  document.querySelectorAll("[data-mode]").forEach(button => button.setAttribute("aria-pressed", String(button.dataset.mode === $("mode").value)));
}

function updateReady() {
  const missing = [];
  const preset = state.snapshot?.endpoints.find(item => item.id === $("endpoint-preset").value);
  if (!selectedPackage()) missing.push(t("基准包"));
  if (!$("base-url").value.trim() && !preset) missing.push(t("API 地址"));
  if (!$("request-model").value.trim()) missing.push(t("实际请求模型"));
  if (!$("key").value.trim() && !preset?.credential_saved) missing.push("API key");
  if ($("run-mode").value === "scheduled" && !preset?.credential_saved) missing.push(t("已保存凭据的 API 连接"));
  const running = state.snapshot?.sessions.some(item => item.kind === "detection" && state.snapshot.active.includes(item.session_id));
  $("readiness").textContent = running ? t("当前检测运行中，请先等待或停止。") : missing.length ?
    t("开始前还需要：{items}", {items: missing.join(" / ")}) : t("准备就绪。只向你指定的 API 发送请求。");
  $("start").disabled = state.submitting || Boolean(running) || Boolean(missing.length);
}

function renderPresets() {
  const presets = state.snapshot?.endpoints || [];
  options($("endpoint-preset"), [["", t("仅本次手动输入")], ...presets.filter(item => item.mode === $("mode").value).map(item => [item.id, item.name])]);
  options($("preset-edit"), [["", t("添加新连接")], ...presets.map(item => [item.id, item.name])]);
}

function detectionInput() {
  const [id, version] = $("package").value.split("|");
  return {package_id: id, package_version: version, mode: $("mode").value,
    endpoint_id: $("endpoint-preset").value || undefined,
    base_url: $("base-url").value, allow_insecure: $("allow-http").checked, key: $("key").value,
    claimed_model: $("claimed").value, request_model: $("request-model").value,
    tier: $("tier").value, runtime: {workers: Number($("workers").value), retries: Number($("retries").value), retain_raw: $("retain-raw").checked}};
}

let estimateSequence = 0;
async function updateEstimate() {
  const sequence = ++estimateSequence;
  if (!selectedPackage()) { $("request-count").textContent = "—"; $("detect-estimate").textContent = t("请选择基准包。"); return; }
  try {
    const input = detectionInput();
    delete input.key;
    const estimate = await post("/api/run/estimate", input);
    if (sequence === estimateSequence) {
      $("request-count").textContent = estimate.logical_requests;
      $("detect-estimate").textContent = t("本轮 {requests} 次请求，含重试最多 {attempts} 次。费用取决于待测端，当前未知。", {requests: estimate.logical_requests, attempts: estimate.maximum_http_attempts});
    }
  } catch (error) { if (sequence === estimateSequence) $("detect-estimate").textContent = errorMessage(error); }
}

function renderReportNote(report) {
  const box=$("report-note"), fp=report.fingerprint, cells=Object.entries(fp.cells || {});
  const missing=cells.filter(([,cell])=>cell.valid<cell.minimum);
  const valid=report.progress?.valid_samples || 0;
  const make=(tag,text,cls)=>{const node=document.createElement(tag);node.textContent=text;if(cls)node.className=cls;return node;};
  box.replaceChildren();box.hidden=false;
  const heading=make("div","","report-note-heading");
  heading.append(make("strong",t(!valid ? "尚无有效样本" : missing.length ? "样本还不够，结果仅供参考" : "样本已达标，按当前答案判定")),
                 make("span",uiMessage(report.operational_status),"report-note-status"));
  box.append(heading);
  if(missing.length)box.append(make("p",t("{count} 项尚未达到最低样本量。",{count:missing.length})));
  const reasons=(fp.reasons || []).filter(code=>!["samples_incomplete","no_weighted_family"].includes(code));
  if(reasons.length)box.append(make("p",reasons.map(uiMessage).join(" / ")));
  const failures=(report.events || []).filter(event=>event.event==="attempt_decision").reduce((all,event)=>{all[event.code]=(all[event.code]||0)+1;return all;},{});
  if(missing.length || Object.keys(failures).length || report.failure){
    const details=make("details","");details.append(make("summary",t("查看明细")));
    for(const [id,cell] of missing)details.append(make("p",t("{name}：{valid}/{planned}，至少{minimum}",{name:id,valid:cell.valid,planned:cell.planned,minimum:cell.minimum})));
    if(report.failure)details.append(make("p",uiMessage(report.failure)));
    for(const [code,count] of Object.entries(failures))details.append(make("p",uiMessage(code)+" × "+count));
    if(Object.keys(failures).length)details.append(make("small",t("失败次数包含重试。")));
    box.append(details);
  }
}
function showReport(report) {
  if (!report.fingerprint) return;
  $("report-placeholder").hidden = true;
  $("verdict").hidden = false;
  $("retention-export").hidden = false;
  const color = report.fingerprint.color || "yellow";
  $("verdict").className = `verdict ${color}`;
  $("verdict").textContent = report.fingerprint.sample_policy?.version === "60-percent-v1" && report.fingerprint.quality_status !== "sufficient" ? t(report.fingerprint.quality_status === "insufficient_valid_samples" ? "有效请求不足（低于60%）" : "单项有效样本不足（低于60%）") : {green: t("强指向申报模型"), red: t("强指向其他候选模型"), yellow: t("证据不足")}[color];
  renderReportNote(report);
  const sources = [...new Set((report.benchmark.collection.sources || []).map(source => source.url).filter(Boolean))].join(" / ");
  $("report-summary").textContent = `${t("申报")} ${report.claimed_model} · ${t("实际请求名")} ${report.request_model} · ${t("基准")} ${report.benchmark.id} ${report.benchmark.version}\n` +
    `${t("基准采集网址（API 根地址）")}: ${sources || t("未提供")}\n` +
    `${t("本次检测网址（API 根地址）")}: ${report.endpoint || t("未提供")}\n` +
    t("仅为候选模型之间的指纹指向，不是身份认证。");
  $("report").textContent = JSON.stringify(report, null, 2);
  if (report.benchmark.publisher !== "maintainer") $("report-summary").textContent += " " + t("本地或社区参考，非维护者认证");
  $("match-bars").replaceChildren();
  for (const [model, score] of Object.entries(report.fingerprint.matches)) {
    const row = document.createElement("div"), label = document.createElement("span"), bar = document.createElement("progress"), value = document.createElement("strong");
    row.className = "match-row"; label.textContent = model; bar.max = 1; bar.value = score;
    bar.setAttribute("aria-label", model);
    value.textContent = `${(score * 100).toFixed(3)}%`;
    const threshold = report.fingerprint.thresholds?.[model], line = document.createElement("small");
    line.textContent = Number.isFinite(threshold) ? t("强指向线 {value}%", {value: (threshold * 100).toFixed(3)}) : t("未校准");
    value.append(line);
    row.append(label, bar, value); $("match-bars").append(row);
  }
  const note = document.createElement("p"); note.className = "match-disclaimer";
  note.textContent = t("匹配度与强指向线都不是身份概率。仅一个模型严格越线且样本达标时，才给出强指向；显示值有四舍五入。"); $("match-bars").append(note);
}

function renderHistory() {
  $("run-history").replaceChildren();
  for (const session of state.snapshot.sessions.filter(item => item.kind === "detection")) {
    const row = document.createElement("div");
    row.className = "package-row";
    const open = document.createElement("button");
    open.textContent = `${session.created_at} · ${session.claimed_model} · ${uiMessage(session.status)} · ${session.successful}/${session.planned}`;
    open.addEventListener("click", async () => {
      state.followLatest = false;
      state.sessionId = session.session_id;
      try { showReport(await json(`/api/report/${encodeURIComponent(session.session_id)}`)); }
      catch (error) { $("progress").textContent = errorMessage(error); }
    });
    row.append(open);
    if (["paused", "error"].includes(session.status)) {
      const resume = document.createElement("button");
      resume.textContent = t("按原配置恢复");
      resume.addEventListener("click", async () => {
        try {
          if (!$("key").value && !$("endpoint-preset").value) throw Error(t("请先输入该原线路的临时key或选择原连接预设。"));
          const result = await post("/api/run/start", {...detectionInput(), resume_id: session.session_id});
          state.sessionId = result.session_id;
          state.followSchedule = false;
          state.followLatest = true;
        } catch (error) { showStartError(error); }
      });
      row.append(resume);
    }
    $("run-history").append(row);
  }
}

function progress(value) {
  $("progress").textContent = t("{status} · 完成 {done}/{planned} · 有效样本 {valid} · 错误 {errors}", {status: uiMessage(value.status), done: value.logical_completed || 0, planned: value.planned || 0, valid: value.valid_samples || 0, errors: value.errors || 0});
  $("meter-bar").style.width = value.planned ? `${Math.round(value.logical_completed / value.planned * 100)}%` : "0%";
}

async function poll() {
  try {
    const snapshot = await json("/api/status", {cache: "no-store"});
    Object.assign(state.snapshot, snapshot);
    $("service-state").textContent = t("本机 · {count} 个会话", {count: snapshot.sessions.length});
    const schedule = snapshot.schedule;
    $("schedule-pause").hidden = !schedule?.enabled;
    updateReady();
    $("schedule-status").textContent = schedule ? `${t(schedule.enabled ? "计划运行中" : "计划已暂停")} · ${t("已完成 {count} 轮", {count: schedule.completed_rounds})}` +
      (schedule.next_due ? ` · ${t("下一轮 {time}", {time: new Date(schedule.next_due * 1000).toLocaleTimeString()})}` : "") +
      (schedule.error ? ` · ${uiMessage(schedule.error)}` : "") : t("未启动定时计划");
    renderHistory();
    const activeRun = snapshot.sessions.find(item => item.kind === "detection" && snapshot.active.includes(item.session_id));
    $("stop").disabled = !activeRun;
    if (state.followLatest) state.sessionId = activeRun?.session_id || (state.followSchedule ? schedule?.last_session_id : null) || state.sessionId;
    if (!state.sessionId) return;
    const session = snapshot.sessions.find(item => item.session_id === state.sessionId);
    if (session) progress(session);
    const report = await json(`/api/report/${encodeURIComponent(state.sessionId)}`, {cache: "no-store"});
    showReport(report);
  } catch (error) {
    if (["backend_disconnected", "session_token_required"].includes(error.code)) {
      document.querySelectorAll('input[type="password"]').forEach(input => { input.value = ""; });
      updateReady();
    }
    $("progress").textContent = t("读取状态失败：{error}", {error: errorMessage(error)});
  }
}

function showStartError(error) {
  $("start-error").textContent = errorMessage(error);
  $("start-error").scrollIntoView({block: "nearest", behavior: "instant"});
  $("start-error").focus({preventScroll: true});
}

$("mode").addEventListener("change", render);
document.querySelectorAll("[data-mode]").forEach(button => button.addEventListener("click", () => {
  $("mode").value = button.dataset.mode; $("base-url").disabled = false; render();
}));
for (const id of ["base-url", "key", "request-model"]) $(id).addEventListener("input", updateReady);
$("claimed").addEventListener("change", () => {
  const model = selectedPackage()?.models.find(item => item.id === $("claimed").value);
  $("request-model").value = defaultRequestModel(model);
  state.claimedSelection = model?.id;
  updateReady();
});
$("run-mode").addEventListener("change", () => {
  $("schedule-controls").hidden = $("schedule-help").hidden = $("run-mode").value !== "scheduled";
  updateReady();
});
$("package").addEventListener("change", renderModels);
$("show-reference-packages").addEventListener("change", render);
$("tier").addEventListener("change", updateEstimate);
$("retries").addEventListener("input", updateEstimate);
$("endpoint-preset").addEventListener("change", () => {
  const preset = state.snapshot.endpoints.find(item => item.id === $("endpoint-preset").value);
  $("base-url").disabled = Boolean(preset);
  if (preset) { $("allow-http").checked = preset.allow_insecure === true; $("base-url").value = preset.base_url; $("request-model").value = preset.model; $("key").value = ""; }
  updateReady();
});
$("start").addEventListener("click", async () => {
  $("start-error").textContent = "";
  $("start").disabled = true;
  state.submitting = true;
  try {
    const input = detectionInput();
    state.followLatest = true;
    if ($("run-mode").value === "scheduled") {
      if (!input.endpoint_id) throw Error(t("定时检测需要已保存凭据的连接。"));
      delete input.key;
      await post("/api/schedule/start", {detection: input, interval_seconds: Number($("schedule-minutes").value) * 60,
        round_limit: $("schedule-rounds").value ? Number($("schedule-rounds").value) : null});
      state.followSchedule = true;
    } else {
      const result = await post("/api/run/start", input);
      state.followSchedule = false;
      state.sessionId = result.session_id;
    }
    await poll();
  } catch (error) {
    showStartError(error);
  } finally {
    state.submitting = false;
    updateReady();
  }
});

$("stop").addEventListener("click", async () => {
  try {
    const activeRun = state.snapshot.sessions.find(item => item.kind === "detection" && state.snapshot.active.includes(item.session_id));
    if (activeRun) {
      state.sessionId = activeRun.session_id;
      state.followLatest = false;
      await post("/api/run/stop", {session_id: activeRun.session_id});
      await poll();
    }
  } catch (error) {
    $("progress").textContent = errorMessage(error);
  }
});

$("schedule-pause").addEventListener("click", async () => {
  try { await post("/api/schedule/pause", {}); await poll(); }
  catch (error) { $("schedule-status").textContent = errorMessage(error); }
});

(async () => {
  try {
    const bootstrap = await json("/api/bootstrap");
    state.token = bootstrap.token;
    state.tierDefaults = bootstrap.tier_defaults;
    translatePage(new URLSearchParams(location.search).get("lang") || bootstrap.locale);
    if (bootstrap.seed_pool) $("ai-seed-info").textContent = t("从 {domains} 个大类、{topics} 个主题方向中分散抽取灵感；只生成无正确答案的中立选择题。", bootstrap.seed_pool);
    state.snapshot = await json("/api/snapshot", {cache: "no-store"});
    state.followSchedule = Boolean(state.snapshot.schedule?.enabled);
    $("service-state").textContent = t("本机 · {count} 个会话", {count: state.snapshot.sessions.length});
    render();
    state.timer = setInterval(poll, 2000);
    window.dispatchEvent(new Event("workspace-ready"));
  } catch (error) {
    $("service-state").textContent = errorMessage(error);
  }
})();
