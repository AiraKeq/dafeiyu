(() => {
  "use strict";

  const $ = (id) => document.getElementById(id);
  const text = (value, fallback = "—") => typeof value === "string" ? value : typeof value === "number" && Number.isFinite(value) ? String(value) : fallback;
  const number = (value) => typeof value === "number" && Number.isFinite(value) ? value.toLocaleString("zh-CN") : "—";
  const chars = (value) => Array.from(value).length;
  const setText = (id, value) => { $(id).textContent = value; };
  const element = (tag, className, content) => {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (content !== undefined) node.textContent = content;
    return node;
  };
  const icon = (name) => {
    const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
    svg.setAttribute("class", "icon");
    svg.setAttribute("aria-hidden", "true");
    const use = document.createElementNS("http://www.w3.org/2000/svg", "use");
    use.setAttribute("href", `#i-${name}`);
    svg.append(use);
    return svg;
  };
  const state = {
    authenticated: false, csrf: null, view: "overview", status: null, statusValid: false,
    writing: false, confirming: false, loads: new Set(), statusPromise: null,
    personality: { loaded: false, text: "", revision: null, characters: null, combined: null, budget: 2000 },
    style: { loaded: false, scope: "", notes: "", revision: null },
    memory: { scope: "", kind: "events", query: "", next: null, page: 0, asOf: null, items: [], sequence: 0, loaded: false },
    event: null,
  };
  const pageTitles = { overview: "总览", personality: "性格与成长", memory: "记忆档案", backups: "备份与规则" };
  const ruleLabels = {
    enabled: "记忆与性格功能", group_ids: "记录与回复的 QQ 群", admin_ids: "记忆与风格管理者 QQ",
    names: "直接唤醒称呼", provider_id: "每日整理使用的模型", active_days: "活跃事件队列天数",
    important_threshold: "过期后保留核心的重要度下限", half_life_days: "新近度评分半衰期（天）",
    raw_retention_days: "已整理原文保留天数", reply_start_hour: "开始回复小时（北京时间）",
    reply_end_hour: "停止回复小时（24 为午夜）", proactive_enabled: "普通群消息自动回应与冷却队列",
    proactive_gap_seconds: "普通回复冷却（秒）", segment_score_enabled: "分段偏好评分",
    segment_weight_1: "一段基础权重", segment_weight_2: "两段基础权重", segment_weight_3: "三段基础权重",
    segment_score_micro_random: "近分方案轻微随机", delivery_guard_enabled: "防刷屏动态预算与分页",
    reply_polish_enabled: "按需轻量人格润色", vision_enabled: "原生视觉与图片冷却缓存",
    review_hour: "每日整理小时", review_minute: "每日整理分钟", review_batches_per_pass: "每次整理最多批次",
    semantic_enabled: "本地中文语义记忆", semantic_similarity_threshold: "语义候选相似度门槛",
    semantic_top_k: "语义候选上限", semantic_query_timeout_seconds: "语义检索等待上限（秒）",
    semantic_sync_interval_seconds: "索引核对间隔（秒）",
    ignore_commands: "QQ命令静默拦截", web_lookup_enabled: "不确定文字/表情包联网核验",
  };
  const actionNames = {
    personality_save: "保存性格", style_save: "保存成长便笺", event_update: "保存记忆",
    event_delete: "删除记忆", backup: "手动冷态备份", service_start: "启用服务", service_stop: "停止服务",
  };
  let toastTimer;
  let confirmResolver = null;

  class ApiError extends Error {
    constructor(code, message) { super(message); this.code = code; }
  }

  function dateTime(value) {
    if (value === null || value === undefined || value === "") return "暂无记录";
    const date = new Date(typeof value === "number" ? value * 1000 : value);
    return Number.isNaN(date.getTime()) ? text(value, "时间未知") : date.toLocaleString("zh-CN", { hour12: false });
  }

  function badge(id, label, tone = "neutral", dot = true) {
    const node = $(id);
    node.className = `badge ${tone}`;
    node.replaceChildren();
    if (dot) node.append(element("span", "status-dot"));
    node.append(document.createTextNode(label));
  }

  function errorMessage(error) {
    const code = String(error?.code || "").toLowerCase();
    if (code.includes("conflict") || code.includes("revision") || code.includes("stale") || code.includes("cas")) {
      return "版本已变化，操作未提交。你的修改仍保留；请核对后重新载入最新版本，再决定是否保存。事件可关闭编辑后重新搜索。";
    }
    if (code.includes("unauth") || code === "http_401") return "登录已失效。请使用本机启动器提供的新登录链接重新进入。";
    if (code.includes("csrf")) return "会话校验失败。本次没有自动重试，请重新检查登录后再操作。";
    if (code === "timeout") return "请求超时，结果尚未确认。请先刷新状态或重新载入内容核对，不要连续重复写入。";
    if (code === "network") return "无法连接本机管理服务。请确认本机启动器仍在运行，再重试。";
    if (code === "invalid_response") return "管理服务返回了无法识别的数据。请检查服务日志；界面不会猜测状态。";
    return error instanceof ApiError ? error.message : "页面操作未完成，请重新载入当前数据后重试。";
  }

  function showError(id, error) {
    $(id).hidden = !error;
    $(id).textContent = error ? errorMessage(error) : "";
  }

  function toast(message, isError = false) {
    clearTimeout(toastTimer);
    const node = element("div", `toast${isError ? " error" : ""}`);
    node.append(icon(isError ? "info" : "check"), element("span", "", message));
    $("toastRegion").replaceChildren(node);
    toastTimer = setTimeout(() => $("toastRegion").replaceChildren(), isError ? 10000 : 6500);
  }

  function lockSession(expired = false) {
    state.authenticated = false;
    state.csrf = null;
    state.statusValid = false;
    $("appContent").hidden = true;
    $("authPanel").hidden = false;
    $("sessionRetry").hidden = false;
    badge("sessionBadge", "未登录", "neutral");
    setText("authTitle", expired ? "会话已锁定" : "从本机登录链接进入");
    setText("authDescription", expired
      ? "登录已失效，私人内容已隐藏，未保存的修改没有提交。请使用本机启动器提供的新登录链接重新进入。"
      : "请使用本机启动器提供的登录链接打开管理器。链接中的一次性令牌会兑换为安全会话，并从地址栏清除。这里不接收或展示密码与令牌。");
    if ($("confirmDialog").open) finishConfirm(false);
    syncControls();
  }

  async function request(path, payload, mutation = false) {
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), mutation ? 180000 : 60000);
    try {
      const headers = { Accept: "application/json" };
      if (payload !== undefined) headers["Content-Type"] = "application/json";
      if ((path === "/api/action" || path === "/api/session") && state.csrf) headers["X-CSRF-Token"] = state.csrf;
      const response = await fetch(path, {
        method: payload === undefined ? "GET" : "POST", credentials: "same-origin", cache: "no-store",
        headers, body: payload === undefined ? undefined : JSON.stringify(payload), signal: controller.signal,
        redirect: "error", referrerPolicy: "no-referrer",
      });
      let result;
      try { result = await response.json(); } catch { throw new ApiError("invalid_response", "响应不是有效 JSON。"); }
      if (response.status === 401 && path !== "/api/login") lockSession(state.authenticated);
      if (!response.ok || result?.ok !== true) {
        throw new ApiError(text(result?.error?.code, `http_${response.status}`), text(result?.error?.message, "管理服务拒绝了这次请求。"));
      }
      if (!Object.prototype.hasOwnProperty.call(result, "data")) throw new ApiError("invalid_response", "响应缺少数据。");
      return result.data;
    } catch (error) {
      if (error instanceof ApiError) throw error;
      throw new ApiError(error?.name === "AbortError" ? "timeout" : "network", "无法完成请求。");
    } finally {
      clearTimeout(timer);
    }
  }

  const action = (name, fields = {}, mutation = false) => request("/api/action", { action: name, ...fields }, mutation);
  const serviceState = () => state.statusValid ? state.status?.service?.state : "unknown";
  function serviceRevision() {
    const service = state.statusValid ? state.status?.service : null;
    return service && typeof service.id === "string" && service.id && typeof service.started_at === "string" ? `${service.id}:${service.started_at}` : null;
  }
  const paused = () => state.authenticated && serviceState() === "stopped" && Boolean(serviceRevision());
  const writable = () => paused() && !state.writing;
  const personalityDirty = () => state.personality.loaded && $("personalityText").value !== state.personality.text;
  const styleDirty = () => state.style.loaded && $("styleNotes").value !== state.style.notes;
  function eventDraft() {
    return {
      title: $("eventTitle").value, detail: $("eventDetail").value, core: $("eventCore").value,
      importance: $("eventImportance").value, keywords: $("eventKeywords").value,
    };
  }
  function draftKeywords() {
    if (state.event?.baseline && $("eventKeywords").value === state.event.baseline.keywords) return [...state.event.originalKeywords];
    return $("eventKeywords").value.split(/\r?\n/).map((value) => value.trim()).filter(Boolean);
  }
  const eventDirty = () => Boolean(state.event) && JSON.stringify(eventDraft()) !== JSON.stringify(state.event.baseline);
  const anyDirty = () => personalityDirty() || styleDirty() || eventDirty();

  function contentLoading() {
    return ["personality", "style", "memory"].some((key) => state.loads.has(key));
  }

  function syncControls() {
    const allowed = writable();
    const active = state.authenticated && !state.writing;
    $("refreshButton").disabled = !active || state.loads.has("status");
    $("refreshButton").classList.toggle("is-loading", state.loads.has("status"));
    $("startButton").disabled = !allowed;
    $("stopButton").disabled = !active || serviceState() !== "running" || !serviceRevision();
    $("backupButton").disabled = !allowed;
    for (const node of document.querySelectorAll(".navigation button")) node.disabled = state.writing || contentLoading();
    for (const node of document.querySelectorAll(".edit-state")) {
      node.textContent = paused() ? "已暂停 · 可编辑" : serviceState() === "running" ? "运行中 · 只读" : "状态未确认 · 只读";
      node.className = `badge edit-state ${paused() ? "paused" : "neutral"}`;
    }
    for (const node of document.querySelectorAll(".write-lock-note")) node.hidden = paused();
    for (const id of ["personalityText", "styleNotes"]) {
      const loaded = id === "personalityText" ? state.personality.loaded : state.style.loaded;
      $(id).disabled = !loaded || !state.authenticated;
      $(id).readOnly = !allowed || state.loads.has(id === "personalityText" ? "personality" : "style");
    }
    $("personalitySave").disabled = !allowed || !state.personality.revision || !personalityDirty() || state.loads.has("personality");
    $("styleSave").disabled = !allowed || !state.style.revision || !styleDirty() || state.loads.has("style");
    $("personalityReload").disabled = !active || state.loads.has("personality");
    $("styleReload").disabled = !active || !state.style.scope || state.loads.has("style");
    $("styleScope").disabled = !active || state.loads.has("style");
    $("memoryScope").disabled = !active || state.loads.has("memory");
    $("memoryQuery").disabled = !active || state.loads.has("memory");
    for (const id of ["searchButton", "memoryReload"]) $(id).disabled = !active || !state.memory.scope || state.loads.has("memory");
    for (const id of ["kindEvents", "kindMessages"]) $(id).disabled = !active || state.loads.has("memory");
    $("memoryFirst").disabled = !active || state.loads.has("memory");
    $("memoryNext").disabled = !active || !state.memory.next || state.loads.has("memory");
    $("backupsReload").disabled = !active || state.loads.has("backups");
    for (const id of ["eventTitle", "eventDetail", "eventCore", "eventImportance", "eventKeywords"]) $(id).readOnly = !allowed;
    $("eventClose").disabled = state.writing;
    $("eventSave").disabled = !allowed || !state.event?.revision || !eventDirty();
    $("eventDelete").disabled = !allowed || !state.event?.revision;
    setText("eventLock", paused() ? "仅修改这一条事件。保存前自动备份，服务端执行版本校验。" : "当前只读：请先停止服务并确认已暂停，再保存或删除；已输入的修改不会被状态刷新覆盖。");
    for (const button of document.querySelectorAll("[data-open-event]")) button.disabled = state.writing;
    updateCounts();
  }

  function updateCounts() {
    const count = chars($("personalityText").value);
    setText("personalityCount", `${number(count)} / 5848 字`);
    setText("styleCount", `${number(chars($("styleNotes").value))} / 1000 字`);
    for (const [id, limit] of [["personalityText", 5848], ["styleNotes", 1000], ["eventTitle", 100], ["eventDetail", 640], ["eventCore", 160]]) {
      $(id).setCustomValidity(chars($(id).value) > limit ? `此项最多 ${limit} 字，当前 ${chars($(id).value)} 字。` : "");
    }
    const keywords = draftKeywords();
    $("eventKeywords").setCustomValidity(keywords.length > 16 ? "最多填写 16 个关键词。" : keywords.some((value) => chars(value) > 48) ? "每个关键词最多 48 字。" : new Set(keywords).size !== keywords.length ? "关键词不能重复。" : "");
    setText("eventCounts", `标题 ${chars($("eventTitle").value)}/100 字 · 详情 ${chars($("eventDetail").value)}/640 字 · 核心 ${chars($("eventCore").value)}/160 字 · 关键词 ${keywords.length}/16 个（每个最多 48 字）`);
    $("personalityDirty").hidden = !personalityDirty();
    $("styleDirty").hidden = !styleDirty();
    $("eventDirty").hidden = !eventDirty();
    setText("personalitySaveState", personalityDirty() ? "有未保存的修改" : "未修改");
    setText("styleSaveState", styleDirty() ? "有未保存的修改" : "未修改");
    const p = state.personality;
    const known = p.loaded && typeof p.combined === "number" && typeof p.characters === "number";
    const projected = known ? Math.max(0, p.combined - p.characters + count) : null;
    setText("polishBudget", `${projected === null ? "—" : number(projected)} / ${number(p.budget)} 字`);
    const meter = $("budgetMeter");
    meter.max = p.budget;
    meter.value = projected === null ? 0 : Math.min(projected, p.budget);
    meter.setAttribute("aria-valuetext", projected === null ? "尚未获取实际字数" : `${projected} 字，预算 ${p.budget} 字`);
    meter.closest(".budget-box").classList.toggle("over-budget", projected !== null && projected > p.budget);
    setText("budgetHint", !known
      ? "以服务端返回的合计字数为准；不虚构当前预算占用。"
      : `已保存上下文合计 ${number(p.combined)} 字，包含固定身份前缀、分隔提示与基础性格，不含成长便笺。${personalityDirty() ? "当前数值按本次性格修改的字数差估算。" : ""}${projected > p.budget ? "已超过润色预算；界面不会截断内容，保存仍由服务端校验。" : `剩余 ${number(Math.max(0, p.budget - projected))} 字润色预算。`}`);
  }

  function ruleValue(value) {
    if (typeof value === "boolean") return value ? "已启用" : "未启用";
    if (Array.isArray(value)) return value.length ? value.map((item) => text(item, "")).filter(Boolean).join("、") : "未配置";
    return text(value, "未识别格式") || "未配置";
  }

  function appendRule(container, label, value) {
    const row = element("div");
    row.append(element("dt", "", label), element("dd", "", value));
    container.append(row);
  }

  function renderRules() {
    const rules = state.statusValid && state.status?.rules && typeof state.status.rules === "object" ? state.status.rules : {};
    const overview = $("overviewRules");
    const all = $("allRules");
    overview.replaceChildren();
    all.replaceChildren();
    const has = (key) => Object.prototype.hasOwnProperty.call(rules, key);
    const hour = (value) => String(value).padStart(2, "0");
    if (has("reply_start_hour") && has("reply_end_hour")) appendRule(overview, "回复时段 · 北京时间", `${hour(rules.reply_start_hour)}:00 – ${hour(rules.reply_end_hour)}:00`);
    if (has("proactive_gap_seconds")) appendRule(overview, "普通回复冷却", `${ruleValue(rules.proactive_gap_seconds)} 秒`);
    if (has("raw_retention_days")) appendRule(overview, "已整理原文保留", `${ruleValue(rules.raw_retention_days)} 天`);
    if (has("review_hour") && has("review_minute")) appendRule(overview, "每日整理时间", `${hour(rules.review_hour)}:${hour(rules.review_minute)}`);
    if (has("reply_polish_enabled")) appendRule(overview, "轻量人格润色", ruleValue(rules.reply_polish_enabled));
    // 仅渲染已核对的规则字段；不透传配置对象或任何凭据字段。
    for (const [key, label] of Object.entries(ruleLabels)) if (has(key)) appendRule(all, label, ruleValue(rules[key]));
    if (!overview.childElementCount) overview.append(element("div", "empty-inline", "尚无可显示的已配置关键规则。"));
    if (!all.childElementCount) all.append(element("div", "empty-inline", "尚无可显示的已配置规则；不以默认值代替实际配置。"));
  }

  function scopeLabel(item) {
    const value = text(item.scope, "未命名空间");
    const match = /^aiocqhttp:(GroupMessage|FriendMessage):(\d+)$/.exec(value);
    if (!match) return text(item.label, value);
    const rules = state.status?.rules || {};
    const kind = match[1] === "FriendMessage"
      ? (Array.isArray(rules.admin_ids) && rules.admin_ids.includes(match[2]) ? "管理员私聊" : "私聊")
      : (Array.isArray(rules.group_ids) ? (rules.group_ids.includes(match[2]) ? "正式群" : "历史群") : "QQ群");
    return `${kind} · ${match[2]}`;
  }

  function populateScopes() {
    const scopes = Array.isArray(state.status?.scopes) ? state.status.scopes : [];
    for (const [id, selected] of [["styleScope", state.style.scope], ["memoryScope", state.memory.scope]]) {
      const select = $(id);
      select.replaceChildren(element("option", "", "选择一个对话空间"));
      select.firstElementChild.value = "";
      const seen = new Set();
      for (const item of scopes) {
        if (typeof item.scope !== "string" || !item.scope || seen.has(item.scope)) continue;
        seen.add(item.scope);
        const option = element("option", "", scopeLabel(item));
        option.value = item.scope;
        select.append(option);
      }
      if (selected && !seen.has(selected)) {
        const option = element("option", "", `${selected}（当前选择）`);
        option.value = selected;
        select.append(option);
      }
      select.value = selected;
    }
  }

  function renderSemantic(data) {
    const semantic = data?.semantic;
    const label = { ready: "索引就绪", stale: "待同步", missing: "尚未建立", unavailable: "状态未知" };
    badge("semanticBadge", label[semantic?.state] || "状态未知", semantic?.state === "ready" ? "success" : "neutral", false);
    setText("semanticModel", semantic ? `${text(semantic.model)} / ${number(semantic.dimensions)} 维` : "尚未读取");
    setText("semanticCount", `${number(semantic?.indexed_events)} / ${number(semantic?.effective_events)}`);
    setText("semanticSync", `${number(semantic?.pending_events)} 项 / ${dateTime(semantic?.last_sync)}`);
    setText("semanticDescription", !semantic ? "语义状态未提供；不把未知当成已启用。" :
      `${semantic.enabled === true ? "已配置语义召回" : semantic.enabled === false ? "语义召回未启用" : "配置状态未知"}。${serviceState() === "stopped" ? "聊天当前暂停，索引就绪不代表正在聊天。" : "查询失败会回退关键词召回；索引状态不代表模型运行时一定就绪。"} 事件修改/删除后会失效并重新核验，读取状态不触发模型或重建。`);
  }

  function renderStatus() {
    const data = state.statusValid ? state.status : null;
    const service = serviceState();
    if (service === "stopped") {
      badge("serviceBadge", "已暂停", "paused");
      setText("serviceTitle", "已暂停，可以安心整理了");
      setText("serviceDescription", "聊天服务当前处于停止状态。可以查看档案，并在确认后整理性格与记忆。");
    } else if (service === "running") {
      badge("serviceBadge", "运行中", "success");
      setText("serviceTitle", "聊天服务正在运行");
      setText("serviceDescription", "运行期间仅开放查阅。需要编辑时，请先停止服务；QQ 连接状态请看下方。");
    } else {
      badge("serviceBadge", "状态未知", "neutral");
      setText("serviceTitle", "海面状态暂未确认");
      setText("serviceDescription", "当前无法确认服务是否暂停，所有写入与启停操作已锁定。请刷新状态后再决定。");
    }
    setText("lastChecked", data?.checked_at ? `更新于 ${dateTime(data.checked_at)}` : "尚未获得有效状态");
    const scopesKnown = Array.isArray(data?.scopes) && (data.scopes.length > 0 || (service !== "unknown" && !data?.warnings?.length));
    setText("scopeCount", scopesKnown ? number(data.scopes.length) : "—");
    setText("queueCount", number(data?.queue?.pending));
    setText("queueDescription", `失败 ${number(data?.queue?.failures)} 批 · 不在此触发处理`);
    setText("reviewDay", text(data?.review?.completed_day, "—") || "暂无记录");
    const review = data?.review?.last_result;
    setText("reviewDescription", review && typeof review === "object"
      ? `成功 ${number(review.processed_batches)} 批 / 失败 ${number(review.failed_batches)} 批 / 事件 ${number(review.events)} / 成长 ${typeof review.style_updated === "boolean" ? (review.style_updated ? "已更新" : "未更新") : number(review.style_updated)}`
      : "暂无已完成日评记录");
    setText("versionValue", data?.version === "unknown" ? "未知" : text(data?.version));
    const qqState = data?.qq?.state;
    badge("qqBadge", qqState === "online" ? "在线" : qqState === "offline" ? "离线" : "未知", qqState === "online" ? "success" : qqState === "offline" ? "warning" : "neutral", false);
    setText("qqAccount", data?.qq?.account ? `账号 ${text(data.qq.account)}` : "账号尚未获取");
    setText("qqReason", text(data?.qq?.reason, "QQ 状态独立于聊天服务；未验证时不显示在线。"));
    setText("modelName", text(data?.model?.name, "模型尚未获取") || "模型名称未配置");
    setText("modelKey", data?.model?.key_configured === true ? "模型密钥已配置 · 不显示密钥值" : data?.model?.key_configured === false ? "模型密钥未配置" : "模型密钥配置状态未知");
    setText("paginationState", `活跃 ${number(data?.pagination?.active)} / 过期 ${number(data?.pagination?.expired)}`);
    renderSemantic(data);
    const warnings = Array.isArray(data?.warnings) ? data.warnings.filter((item) => typeof item === "string") : [];
    $("statusWarnings").hidden = !warnings.length;
    $("statusWarnings").replaceChildren();
    if (warnings.length) {
      const list = element("ul");
      for (const warning of warnings) list.append(element("li", "", warning));
      $("statusWarnings").append(icon("info"), list);
    }
    const table = $("scopeTable");
    table.replaceChildren();
    for (const scope of Array.isArray(data?.scopes) ? data.scopes : []) {
      const row = element("tr");
      const nameCell = element("td");
      const name = element("div", "scope-cell");
      const label = scopeLabel(scope);
      const copy = element("div");
      copy.append(element("span", "scope-name", label), element("span", "scope-code", text(scope.scope)));
      name.append(element("span", "scope-avatar", Array.from(label)[0] || "海"), copy);
      nameCell.append(name);
      row.append(nameCell, element("td", "", number(scope.messages)), element("td", "", number(scope.events)), element("td", "", text(scope.style_day, "暂无记录") || "暂无记录"));
      const actionCell = element("td");
      const button = element("button", "text-button", "查阅档案");
      button.type = "button";
      button.disabled = typeof scope.scope !== "string" || !scope.scope;
      button.append(icon("arrow"));
      listen(button, "click", () => navigate("memory", scope.scope));
      actionCell.append(button);
      row.append(actionCell);
      table.append(row);
    }
    if (!table.childElementCount) emptyTable(table, 5, scopesKnown ? "还没有已记录的对话空间。" : "暂未确认对话空间；请重试状态刷新。");
    populateScopes();
    renderRules();
    syncControls();
  }

  async function refreshStatus() {
    if (!state.authenticated) return;
    if (state.statusPromise) return state.statusPromise;
    state.loads.add("status");
    syncControls();
    state.statusPromise = (async () => {
      try {
        const data = await action("status");
        if (!data || !["running", "stopped", "unknown"].includes(data.service?.state)) throw new ApiError("invalid_response", "状态数据不完整。");
        state.status = data;
        state.statusValid = state.authenticated;
        showError("globalError", null);
      } catch (error) {
        state.statusValid = false;
        showError("globalError", error);
      } finally {
        state.loads.delete("status");
        state.statusPromise = null;
        renderStatus();
      }
    })();
    return state.statusPromise;
  }

  function finishConfirm(accepted) {
    const resolve = confirmResolver;
    confirmResolver = null;
    state.confirming = false;
    if ($("confirmDialog").open) $("confirmDialog").close();
    $("confirmPhrase").value = "";
    if (resolve) resolve(accepted);
  }

  function confirm(options) {
    if (state.confirming || state.writing) return Promise.resolve(false);
    state.confirming = true;
    setText("confirmTitle", options.title);
    setText("confirmDescription", options.description);
    setText("confirmSubmit", options.confirmText || "确认");
    $("confirmSubmit").className = `button ${options.danger ? "danger" : "primary"}`;
    $("confirmIcon").className = `dialog-icon${options.danger ? " danger" : ""}`;
    const details = $("confirmDetails");
    details.replaceChildren();
    for (const item of options.details || []) details.append(element("li", "", item));
    details.hidden = !details.childElementCount;
    $("confirmPhraseGroup").hidden = !options.phrase;
    $("confirmPhrase").value = "";
    $("confirmPhrase").dataset.expected = options.phrase || "";
    setText("confirmPhraseLabel", options.phrase || "");
    $("confirmSubmit").disabled = Boolean(options.phrase);
    return new Promise((resolve) => {
      confirmResolver = resolve;
      $("confirmDialog").showModal();
      (options.phrase ? $("confirmPhrase") : $("confirmCancel")).focus();
    });
  }

  async function discardIf(dirty) {
    return !dirty || await confirm({
      title: "放弃未保存的修改？", description: "这次切换会丢弃尚未提交的编辑。已保存的内容不会改变。",
      confirmText: "放弃修改", danger: true,
    });
  }

  function restoreDrafts() {
    $("personalityText").value = state.personality.text;
    $("styleNotes").value = state.style.notes;
    closeEvent();
    syncControls();
  }

  async function navigate(view, scope) {
    if (!Object.prototype.hasOwnProperty.call(pageTitles, view) || state.writing) return;
    if (contentLoading()) { toast("当前档案正在读取，请稍候再切换。"); return; }
    if (view === state.view && !scope) return;
    if (!await discardIf(anyDirty())) return;
    restoreDrafts();
    state.view = view;
    for (const node of document.querySelectorAll(".view")) node.hidden = node.id !== `view-${view}`;
    for (const node of document.querySelectorAll(".navigation [data-view]")) {
      const selected = node.dataset.view === view;
      node.classList.toggle("active", selected);
      if (selected) node.setAttribute("aria-current", "page"); else node.removeAttribute("aria-current");
    }
    setText("breadcrumbCurrent", pageTitles[view]);
    document.title = `${pageTitles[view]} · 大肥鱼聊天管理器`;
    if (scope && typeof scope === "string") {
      state.memory.scope = scope;
      state.memory.query = "";
      $("memoryQuery").value = "";
      populateScopes();
    }
    $("mainContent").focus({ preventScroll: true });
    window.scrollTo({ top: 0, behavior: "instant" });
    if (!state.authenticated) return;
    if (view === "personality") {
      await loadPersonality();
      if (state.style.scope) await loadStyle();
    }
    if (view === "memory" && state.memory.scope) await loadMemory(false);
    if (view === "backups") await loadBackups();
  }

  async function loadPersonality() {
    if (!state.authenticated || state.loads.has("personality")) return;
    state.loads.add("personality");
    showError("personalityError", null);
    setText("personalityRevision", "正在载入…");
    syncControls();
    try {
      const data = await action("personality");
      if (typeof data?.text !== "string" || typeof data.revision !== "string") throw new ApiError("invalid_response", "性格数据不完整。");
      state.personality = {
        loaded: true, text: data.text, revision: data.revision || null,
        characters: Number.isFinite(data.characters) ? data.characters : null,
        combined: Number.isFinite(data.combined_characters) ? data.combined_characters : null,
        budget: Number.isFinite(data.polish_budget) && data.polish_budget > 0 ? data.polish_budget : 2000,
      };
      $("personalityText").value = data.text;
      setText("personalityRevision", data.revision ? "已载入 · 版本校验就绪" : "缺少版本，暂不可保存");
    } catch (error) {
      showError("personalityError", error);
      setText("personalityRevision", "载入失败 · 保留当前内容");
    } finally {
      state.loads.delete("personality");
      syncControls();
    }
  }

  async function loadStyle() {
    if (!state.authenticated || !state.style.scope || state.loads.has("style")) return;
    const scope = state.style.scope;
    state.loads.add("style");
    showError("styleError", null);
    setText("styleRevision", "正在载入…");
    syncControls();
    try {
      const data = await action("style_get", { scope });
      if (data?.scope !== scope || typeof data.notes !== "string" || typeof data.revision !== "string") throw new ApiError("invalid_response", "成长便笺数据不完整。");
      state.style = { loaded: true, scope, notes: data.notes, revision: data.revision || null };
      $("styleNotes").value = data.notes;
      setText("styleDay", data.day ? `最近记录日期：${text(data.day)}` : "没有记录更新日期 · 不推测成长历史");
      setText("styleRevision", data.revision ? "已载入 · 版本校验就绪" : "缺少版本，暂不可保存");
    } catch (error) {
      showError("styleError", error);
      setText("styleRevision", "载入失败 · 请重试");
    } finally {
      state.loads.delete("style");
      syncControls();
    }
  }

  async function mutate(name, fields, options = {}) {
    if (state.writing || !state.authenticated) return false;
    const permitted = name === "service_stop" ? serviceState() === "running" && serviceRevision() : paused();
    if (!permitted) {
      toast("服务状态已变化，当前操作已锁定。请刷新状态后重试。", true);
      return false;
    }
    state.writing = true;
    syncControls();
    if (options.errorId) showError(options.errorId, null);
    toast("操作进行中，请稍候。不要重复提交或关闭本机管理服务。");
    try {
      const result = await action(name, fields, true);
      if (options.success) await options.success(result);
      toast(options.message || "操作已完成。");
      if (typeof result?.warning === "string" && result.warning) toast(result.warning);
      if (state.statusPromise) await state.statusPromise;
      await refreshStatus();
      return true;
    } catch (error) {
      if (options.errorId) showError(options.errorId, error);
      toast(errorMessage(error), true);
      if (state.statusPromise) await state.statusPromise;
      await refreshStatus();
      return false;
    } finally {
      state.writing = false;
      syncControls();
    }
  }

  async function savePersonality(event) {
    event.preventDefault();
    if (!writable() || !personalityDirty() || !state.personality.revision || !$("personalityForm").reportValidity()) return;
    const value = $("personalityText").value;
    const revision = state.personality.revision;
    if (!await confirm({ title: "保存新的基础性格？", description: "会先备份当前人设，再校验版本并保存。成长便笺不会因此清空。", details: [`本次性格 ${number(chars(value))} 字`, "不会启动聊天服务或调用模型润色。"], confirmText: "保存性格" })) return;
    await mutate("personality_save", { text: value, revision, confirm: "保存性格" }, {
      errorId: "personalityError", message: "性格已保存，并已创建修改前备份。",
      success: async (result) => {
        const p = state.personality;
        if (typeof p.combined === "number" && typeof p.characters === "number") p.combined += chars(value) - p.characters;
        p.characters = chars(value);
        p.text = value;
        p.revision = typeof result?.revision === "string" ? result.revision : null;
        await loadPersonality();
      },
    });
  }

  async function saveStyle(event) {
    event.preventDefault();
    if (!writable() || !styleDirty() || !state.style.revision || !$("styleForm").reportValidity()) return;
    const { scope, revision } = state.style;
    const notes = $("styleNotes").value;
    if (!await confirm({ title: "保存当前成长便笺？", description: "只保存所选对话空间的当前便笺。会先备份并校验版本，不虚构或追加历史。", details: [`对话空间：${scope}`, `本次便笺 ${number(chars(notes))} 字`], confirmText: "保存成长便笺" })) return;
    await mutate("style_save", { scope, notes, revision, confirm: "保存成长便笺" }, {
      errorId: "styleError", message: "成长便笺已保存，并已创建修改前备份。",
      success: async (result) => {
        state.style.notes = notes;
        state.style.revision = typeof result?.revision === "string" ? result.revision : null;
        await loadStyle();
        if (!personalityDirty()) await loadPersonality();
      },
    });
  }

  function emptyState(title, description, symbol = "book") {
    const node = element("div", "empty-state");
    node.append(icon(symbol), element("h3", "", title), element("p", "", description));
    return node;
  }

  function resetMemory() {
    Object.assign(state.memory, { next: null, page: 0, asOf: null, items: [], loaded: false });
    closeEvent();
    renderMemory();
  }

  async function loadMemory(next = false) {
    const memory = state.memory;
    if (!state.authenticated || !memory.scope || state.loads.has("memory") || (next && !memory.next)) return;
    state.loads.add("memory");
    const sequence = ++memory.sequence;
    const fields = { scope: memory.scope, kind: memory.kind, query: memory.query, limit: 20 };
    if (next) fields.cursor = memory.next;
    showError("memoryError", null);
    setText("memoryMeta", "正在读取所选空间的档案…");
    $("memoryResults").setAttribute("aria-busy", "true");
    syncControls();
    try {
      const data = await action("memory_list", fields);
      if (!Array.isArray(data?.items) || !(data.next_cursor === null || typeof data.next_cursor === "string")) throw new ApiError("invalid_response", "分页数据不完整。");
      if (sequence !== memory.sequence) return;
      Object.assign(memory, { items: data.items, next: data.next_cursor || null, asOf: data.as_of_now, page: next ? memory.page + 1 : 1, loaded: true });
      renderMemory();
    } catch (error) {
      if (sequence === memory.sequence) {
        showError("memoryError", error);
        setText("memoryMeta", "读取失败 · 当前档案未更新");
        if (!memory.loaded) $("memoryResults").replaceChildren(emptyState("这次没能读取档案", "请检查连接后重新搜索。不会把加载失败当成没有记忆。", "info"));
      }
    } finally {
      state.loads.delete("memory");
      $("memoryResults").setAttribute("aria-busy", "false");
      syncControls();
    }
  }

  function renderMemory() {
    const memory = state.memory;
    setText("memoryResultsTitle", memory.kind === "events" ? "事件记忆" : "聊天原文 · 只读");
    $("memoryQuery").placeholder = memory.kind === "events" ? "搜索标题、内容或关键词…" : "搜索原始聊天内容…";
    for (const node of document.querySelectorAll("[data-kind]")) {
      const selected = node.dataset.kind === memory.kind;
      node.classList.toggle("selected", selected);
      node.setAttribute("aria-pressed", String(selected));
    }
    const results = $("memoryResults");
    results.replaceChildren();
    if (!memory.scope) {
      results.append(emptyState("先选一处相遇", "选择一个对话空间，再查阅属于它的记忆。"));
      setText("memoryMeta", "尚未选择对话空间");
    } else if (!memory.loaded) {
      results.append(emptyState("等待读取档案", "只请求当前选择的对话空间，不会跨空间搜索。"));
      setText("memoryMeta", "尚未读取");
    } else {
      setText("memoryMeta", `${memory.items.length} 条 · 查询基准 ${dateTime(memory.asOf)}`);
      if (!memory.items.length) results.append(emptyState("这一页很安静", memory.query ? "没有找到匹配记录。可以换一个关键词再试试。" : "当前空间没有这一类记录；界面不会补入示例或虚构历史。"));
      for (const item of memory.items) results.append(memory.kind === "events" ? renderEvent(item) : renderMessage(item));
    }
    $("memoryFirst").hidden = memory.page <= 1;
    setText("memoryPageInfo", memory.page ? `第 ${memory.page} 页 · 每页最多 20 条${memory.next ? "" : " · 已到末页"}` : "每页最多 20 条 · 版本校验分页");
    syncControls();
  }

  function renderEvent(item) {
    const card = element("article", "memory-item");
    const heading = element("div", "memory-item-heading");
    const copy = element("div");
    copy.append(element("h3", "", text(item.title, "无标题事件") || "无标题事件"));
    const meta = element("div", "memory-item-meta");
    meta.append(element("span", "", `记录日期 ${text(item.day, "未知")}`), element("span", "", `重要度 ${number(item.importance)} / 10`), element("span", "", `新近度 ${number(item.recency)}`), element("span", "", `层级 ${item.tier === "active" ? "活跃" : item.tier === "archive" ? "归档" : text(item.tier, "未知")}`));
    copy.append(meta);
    const edit = element("button", "text-button", "查看 / 编辑");
    edit.type = "button";
    edit.dataset.openEvent = "true";
    edit.append(icon("edit"));
    listen(edit, "click", () => openEvent(item));
    heading.append(copy, edit);
    card.append(heading);
    if (typeof item.core === "string" && item.core) {
      const core = element("div", "memory-core");
      core.append(element("span", "core-label", "核心摘要"), element("p", "memory-body", item.core));
      card.append(core);
    }
    const details = element("details", "memory-details");
    details.append(element("summary", "", "查看完整事件详情"), element("p", "memory-body", text(item.detail, "暂无详情") || "暂无详情"));
    const detailsMeta = element("p", "small muted", `记录时间 ${dateTime(item.ts)} · 事件 ${text(item.id)}`);
    details.append(detailsMeta);
    const predicted = new Map([["keep", "保留"], ["delete", "删除"], ["archive", "归档"], ["compress", "压缩"]]);
    if (item.predicted_action) details.append(element("p", "small muted", `预计整理动作：${predicted.get(item.predicted_action) || text(item.predicted_action)}（仅规则预测，不在此执行）`));
    card.append(details);
    if (Array.isArray(item.keywords) && item.keywords.length) {
      const tags = element("div", "memory-tags");
      for (const keyword of item.keywords) if (typeof keyword === "string") tags.append(element("span", "memory-tag", keyword));
      card.append(tags);
    }
    return card;
  }

  function renderMessage(item) {
    const card = element("article", "memory-item");
    const heading = element("div", "memory-item-heading");
    const copy = element("div");
    const title = element("h3");
    const roles = { user: "用户", assistant: "助手", system: "系统", bot: "机器人" };
    title.append(element("span", "message-role", Object.prototype.hasOwnProperty.call(roles, item.role) ? roles[item.role] : text(item.role, "未知角色")), document.createTextNode(text(item.sender_name, "未记录名称") || "未记录名称"));
    const meta = element("div", "memory-item-meta");
    const processed = item.processed === true || item.processed === 1 ? "已整理" : item.processed === false || item.processed === 0 ? "未整理" : "整理状态未知";
    meta.append(element("span", "", dateTime(item.ts)), element("span", "", `记录日期 ${text(item.day, "未知")}`), element("span", "", processed));
    copy.append(title, meta);
    heading.append(copy, element("span", "badge neutral", "只读原文"));
    card.append(heading, element("p", "memory-body", text(item.text, "") || "（空消息）"));
    return card;
  }

  async function openEvent(item) {
    if (state.writing || !await discardIf(eventDirty())) return;
    state.event = { id: item.id, revision: typeof item.revision === "string" ? item.revision : null, scope: state.memory.scope, baseline: null, originalKeywords: Array.isArray(item.keywords) ? item.keywords.filter((value) => typeof value === "string") : [] };
    $("eventTitle").value = text(item.title, "");
    $("eventDetail").value = text(item.detail, "");
    $("eventCore").value = text(item.core, "");
    $("eventImportance").value = typeof item.importance === "number" && Number.isFinite(item.importance) ? String(item.importance) : "";
    $("eventKeywords").value = Array.isArray(item.keywords) ? item.keywords.filter((value) => typeof value === "string").join("\n") : "";
    state.event.baseline = eventDraft();
    $("eventEditor").hidden = false;
    showError("eventError", null);
    syncControls();
    $("eventEditor").scrollIntoView({ block: "center", behavior: "instant" });
    $("eventTitle").focus({ preventScroll: true });
  }

  function closeEvent() {
    state.event = null;
    $("eventEditor").hidden = true;
    for (const id of ["eventTitle", "eventDetail", "eventCore", "eventImportance", "eventKeywords"]) $(id).value = "";
    showError("eventError", null);
  }

  async function saveEvent(event) {
    event.preventDefault();
    if (!writable() || !state.event?.revision || !eventDirty() || !$("eventForm").reportValidity()) return;
    const importance = $("eventImportance").valueAsNumber;
    if (!Number.isFinite(importance) || importance < 0 || importance > 10 || !$("eventTitle").value.trim() || !$("eventCore").value.trim()) {
      showError("eventError", new ApiError("validation", "请填写标题与核心摘要，并将重要度设为 0–10 之间的有限数值。"));
      return;
    }
    const fields = {
      scope: state.event.scope, id: state.event.id, revision: state.event.revision,
      title: $("eventTitle").value, detail: $("eventDetail").value, core: $("eventCore").value, importance,
      keywords: draftKeywords(), confirm: "保存记忆",
    };
    if (!await confirm({ title: "保存这条事件记忆？", description: "先备份，再使用载入时的版本进行校验。只修改当前事件，原始聊天消息与成长便笺保持不变。", details: [`对话空间：${fields.scope}`, `事件：${fields.title}`], confirmText: "保存记忆" })) return;
    await mutate("event_update", fields, {
      errorId: "eventError", message: "事件记忆已保存；原文与成长便笺没有改动。",
      success: async () => { closeEvent(); await loadMemory(false); },
    });
  }

  async function deleteEvent() {
    if (!writable() || !state.event?.revision) return;
    const fields = { scope: state.event.scope, id: state.event.id, revision: state.event.revision, confirm: "删除这条记忆" };
    if (!await confirm({
      title: "删除这一条事件记忆？", danger: true, phrase: "删除这条记忆", confirmText: "确认删除",
      description: "这会删除指定事件，不能在此一键撤销。删除前会保留备份。原始消息、成长便笺与已有备份不会删除，这不等于彻底遗忘。",
      details: [`对话空间：${fields.scope}`, `事件：${state.event.baseline.title}`, ...(eventDirty() ? ["当前未保存的事件编辑也会丢弃。"] : []), "原文可能在后续整理中再次形成衍生记忆。"],
    })) return;
    await mutate("event_delete", fields, {
      errorId: "eventError", message: "指定事件已删除；原文、成长便笺与备份仍然保留。",
      success: async () => { closeEvent(); await loadMemory(false); },
    });
  }

  function emptyTable(table, columns, content) {
    const row = element("tr");
    const cell = element("td", "table-empty", content);
    cell.colSpan = columns;
    row.append(cell);
    table.append(row);
  }

  async function loadBackups() {
    if (!state.authenticated || state.loads.has("backups")) return;
    state.loads.add("backups");
    showError("backupsError", null);
    syncControls();
    try {
      const data = await action("backups");
      if (!Array.isArray(data?.items)) throw new ApiError("invalid_response", "备份列表不完整。");
      const table = $("backupsTable");
      table.replaceChildren();
      for (const item of data.items) {
        const row = element("tr");
        row.append(element("td", "scope-name", text(item.backup_id)), element("td", "", dateTime(item.created_at)), element("td", "", Object.prototype.hasOwnProperty.call(actionNames, item.action) ? actionNames[item.action] : text(item.action, "未记录来源")));
        table.append(row);
      }
      if (!data.items.length) emptyTable(table, 3, "管理器尚未创建备份。暂停后，可以留下第一份安全副本。");
    } catch (error) {
      showError("backupsError", error);
    } finally {
      state.loads.delete("backups");
      syncControls();
    }
  }

  async function changeService(start) {
    if (state.writing || !state.authenticated) return;
    const revision = serviceRevision();
    if (!revision || (start ? !paused() : serviceState() !== "running")) return;
    if (!await confirm(start ? {
      title: "现在启用大肥鱼？", description: "将显式启动已核验的 AstrBot 聊天服务。启用后可能处理积压队列，并进入日评等生命周期。",
      details: ["不会启动 NapCat，不会拉取镜像或创建另一套机器人。", "服务运行不代表 QQ 已在线，也不代表模型可用。", "启用后，性格、成长便笺与事件记忆会切换为只读。"], confirmText: "启用大肥鱼",
    } : {
      title: "停止大肥鱼聊天服务？", description: "将停止已核验的 AstrBot 服务，正在进行的聊天处理可能中断。停止后需要重新确认状态，才能编辑记忆。",
      details: ["不会停止或重新启动 NapCat。", "如果停止失败，不会用启动操作进行回滚。"], confirmText: "停止大肥鱼", danger: true,
    })) return;
    await mutate(start ? "service_start" : "service_stop", { revision, confirm: start ? "启用大肥鱼" : "停止大肥鱼" }, {
      errorId: "globalError", message: start ? "启用请求已完成。请分别核对服务、QQ 与模型状态。" : "停止请求已完成，正在核对实际服务状态。",
    });
  }

  async function createBackup() {
    if (!writable()) return;
    const revision = serviceRevision();
    if (!await confirm({ title: "创建一份冷态备份？", description: "在暂停状态下备份记忆库、成长便笺与当前人设。备份保存在云端，不自动下载聊天正文。", details: ["不会回传全配置或模型密钥。", "恢复属于维护操作，此处不提供一键恢复。"], confirmText: "备份记忆" })) return;
    await mutate("backup", { revision, confirm: "备份记忆" }, { errorId: "backupsError", message: "冷态备份已创建。可在备份记录中查看。", success: loadBackups });
  }

  function listen(node, event, handler) {
    node.addEventListener(event, async (ev) => {
      try { await handler(ev); } catch (error) { toast(errorMessage(error), true); }
    });
  }

  async function session() {
    if (state.loads.has("session")) return;
    state.loads.add("session");
    $("sessionRetry").disabled = true;
    try {
      const data = await request("/api/session");
      if (data?.authenticated !== true || typeof data.csrf !== "string" || !data.csrf) throw new ApiError("invalid_response", "会话数据不完整。");
      state.authenticated = true;
      state.csrf = data.csrf;
      badge("sessionBadge", "本机会话已连接", "neutral");
      $("authPanel").hidden = true;
      $("appContent").hidden = false;
      await refreshStatus();
      if (state.view === "personality") {
        if (!personalityDirty()) await loadPersonality();
        if (state.style.scope && !styleDirty()) await loadStyle();
      }
      if (state.view === "memory" && state.memory.scope && !eventDirty()) await loadMemory(false);
      if (state.view === "backups") await loadBackups();
    } catch (error) {
      lockSession();
      if (!(String(error.code).toLowerCase().includes("unauth") || String(error.code).toLowerCase() === "http_401")) {
        setText("authTitle", "暂时没能连接到管理器");
        setText("authDescription", errorMessage(error));
      }
    } finally {
      state.loads.delete("session");
      $("sessionRetry").disabled = false;
      syncControls();
    }
  }

  async function bootstrap() {
    // 先清除 fragment，再发出任何请求；令牌只在本次兑换的局部变量中存在。
    let token = null;
    if (window.location.hash) {
      try { token = new URLSearchParams(window.location.hash.slice(1)).get("token"); }
      finally { history.replaceState(null, "", `${window.location.pathname}${window.location.search}`); }
    }
    if (token) {
      try {
        const grant = await request("/api/login", { token }, true);
        if (typeof grant?.csrf !== "string" || !grant.csrf) throw new ApiError("invalid_response", "登录授权数据不完整。");
        state.csrf = grant.csrf;
      }
      catch {
        lockSession();
        setText("authTitle", "登录链接未能兑换");
        setText("authDescription", "链接可能已失效或已使用，或者本机服务暂时不可达。令牌已从地址栏移除。请重新打开本机启动器生成的登录链接；已有会话可点击下方重新检查。");
        return;
      } finally { token = null; }
    }
    await session();
  }

  for (const node of document.querySelectorAll("[data-view]")) listen(node, "click", () => navigate(node.dataset.view));
  listen($("brandHome"), "click", (event) => { event.preventDefault(); return navigate("overview"); });
  listen($("refreshButton"), "click", refreshStatus);
  listen($("sessionRetry"), "click", session);
  listen($("startButton"), "click", () => changeService(true));
  listen($("stopButton"), "click", () => changeService(false));
  listen($("backupButton"), "click", createBackup);
  listen($("backupsReload"), "click", loadBackups);
  listen($("personalityForm"), "submit", savePersonality);
  listen($("styleForm"), "submit", saveStyle);
  listen($("eventForm"), "submit", saveEvent);
  listen($("eventDelete"), "click", deleteEvent);
  for (const id of ["personalityText", "styleNotes", "eventTitle", "eventDetail", "eventCore", "eventImportance", "eventKeywords"]) listen($(id), "input", syncControls);
  listen($("personalityReload"), "click", async () => {
    if (!await discardIf(personalityDirty())) return;
    $("personalityText").value = state.personality.text;
    await loadPersonality();
  });
  listen($("styleReload"), "click", async () => {
    if (!await discardIf(styleDirty())) return;
    $("styleNotes").value = state.style.notes;
    await loadStyle();
  });
  listen($("styleScope"), "change", async () => {
    const scope = $("styleScope").value;
    $("styleScope").value = state.style.scope;
    if (!await discardIf(styleDirty())) return;
    state.style = { loaded: false, scope, notes: "", revision: null };
    $("styleScope").value = scope;
    $("styleNotes").value = "";
    setText("styleDay", scope ? "正在读取当前便笺…" : "选择空间后读取当前便笺");
    setText("styleRevision", "尚未载入");
    showError("styleError", null);
    syncControls();
    if (scope) await loadStyle();
  });
  listen($("memoryScope"), "change", async () => {
    const scope = $("memoryScope").value;
    $("memoryScope").value = state.memory.scope;
    if (!await discardIf(eventDirty())) return;
    state.memory.scope = scope;
    $("memoryScope").value = scope;
    state.memory.query = $("memoryQuery").value.trim();
    resetMemory();
    if (scope) await loadMemory(false);
  });
  for (const node of document.querySelectorAll("[data-kind]")) listen(node, "click", async () => {
    if (state.memory.kind === node.dataset.kind || !await discardIf(eventDirty())) return;
    state.memory.kind = node.dataset.kind;
    state.memory.query = $("memoryQuery").value.trim();
    resetMemory();
    await loadMemory(false);
  });
  async function searchMemory(event) {
    if (event) event.preventDefault();
    if (!state.memory.scope || !await discardIf(eventDirty())) return;
    state.memory.query = $("memoryQuery").value.trim();
    resetMemory();
    await loadMemory(false);
  }
  listen($("memorySearch"), "submit", searchMemory);
  listen($("memoryReload"), "click", searchMemory);
  listen($("memoryFirst"), "click", async () => {
    if (!await discardIf(eventDirty())) return;
    closeEvent();
    await loadMemory(false);
  });
  listen($("memoryNext"), "click", async () => {
    if (!await discardIf(eventDirty())) return;
    closeEvent();
    await loadMemory(true);
  });
  listen($("eventClose"), "click", async () => {
    if (!await discardIf(eventDirty())) return;
    closeEvent();
    syncControls();
  });
  listen($("confirmPhrase"), "input", () => { $("confirmSubmit").disabled = $("confirmPhrase").value !== $("confirmPhrase").dataset.expected; });
  listen($("confirmForm"), "submit", (event) => {
    event.preventDefault();
    const expected = $("confirmPhrase").dataset.expected;
    if (expected && $("confirmPhrase").value !== expected) return;
    finishConfirm(true);
  });
  listen($("confirmCancel"), "click", () => finishConfirm(false));
  listen($("confirmDialog"), "cancel", (event) => { event.preventDefault(); finishConfirm(false); });
  listen($("confirmDialog"), "close", () => { if (!$("confirmDialog").open && confirmResolver) finishConfirm(false); });
  window.addEventListener("beforeunload", (event) => {
    if (anyDirty() || state.writing) { event.preventDefault(); event.returnValue = ""; }
  });
  // 自动刷新只读状态，不请求人设、便笺或聊天正文，也不触碰编辑草稿。
  setInterval(() => {
    if (state.authenticated && !document.hidden && !state.writing && !state.confirming && !state.loads.size) refreshStatus();
  }, 30000);
  syncControls();
  bootstrap().catch(() => {
    lockSession();
    setText("authTitle", "页面初始化未完成");
    setText("authDescription", "请重新打开本机启动器生成的登录链接。界面不会自动执行任何写入或启用操作。");
  });
})();
