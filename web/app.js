"use strict";

const state = { config: null, dirty: false, activeTab: "overview" };

const sectionDefinitions = [
  {
    title: "基础设置",
    description: "流水线运行时使用的解释器、处理上限和本地目录。",
    fields: [
      ["python", "Python 路径", "text", "留空时使用启动 Web 服务的 Python。", true],
      ["max_works", "单轮最多作品", "number", "0 表示不额外限制。"],
      ["state_dir", "状态目录", "text", "断点、运行摘要和达人阶段状态。"],
      ["media_dir", "媒体产物目录", "text", "音频、转写和内容总结的本地产物。"],
      ["log_dir", "日志目录", "text", "流水线日志保存位置。"],
    ],
  },
  {
    title: "作品采集",
    description: "MediaCrawler、增量策略、账号池和浏览器隔离参数。",
    fields: [
      ["collection.incremental_enabled", "启用增量采集", "boolean", "遇到已知作品后停止继续翻页。", true],
      ["collection.incremental_probe_count", "增量探测作品数", "number", "默认先检查最近 3 条。"],
      ["collection.media_crawler_dir", "MediaCrawler 目录", "text", "外部 MediaCrawler 项目的本机路径。", true],
      ["collection.media_crawler_python", "MediaCrawler Python", "text", "推荐使用项目虚拟环境解释器。", true],
      ["collection.max_count", "最大采集数", "number", "单次采集允许的最大作品数。"],
      ["collection.expect_min_count", "最少预期数", "number", "少于该值会视为采集异常。"],
      ["collection.min_publish_date", "最早发布日期", "text", "格式 YYYY-MM-DD；留空表示不限制。"],
      ["collection.login_type", "登录方式", "select", "MediaCrawler 登录方式。", false, ["qrcode", "cookie"]],
      ["collection.save_data_option", "原始数据格式", "select", "推荐使用 jsonl。", false, ["jsonl", "json", "csv"]],
      ["collection.profile_max_workers", "主页采集并发", "number", "达人资料采集的最大并发数。"],
      ["collection.profile_ttl_hours", "主页缓存时长（小时）", "number", "缓存未过期时复用本地资料。"],
      ["collection.cdp_port_start", "CDP 起始端口", "number", "每位达人使用独立端口。"],
      ["collection.cdp_port_stride", "CDP 端口步长", "number", "默认每个账号间隔 10。"],
      ["collection.account_profiles", "账号池 Profile", "lines", "每行一个 profile key，按顺序尝试。", true],
      ["collection.clean_media_output", "采集前清理本轮输出", "boolean", "只清理独立运行目录，不影响历史规范化数据。", true],
    ],
  },
  {
    title: "飞书同步",
    description: "这里只维护表格标识与字段名；Base Token 本身不写入配置。",
    fields: [
      ["feishu.creator_table_id", "达人基础表 ID", "text", "正式流水线必填。", true],
      ["feishu.base_token_env", "Base Token 环境变量", "text", "填写变量名，不填写真实 Token。"],
      ["feishu.ids_file", "飞书 ID 私有文件", "text", "位于 local/ 下并被 Git 忽略。"],
      ["feishu.lark_cli", "Lark CLI", "text", "本机 lark-cli 可执行文件路径。"],
      ["feishu.work_id_field", "作品唯一键字段", "text", "必须保持为抖音作品 ID 字段。"],
      ["feishu.transcript_field", "文案回写字段", "text", "最终纠正文案写入的字段名。"],
      ["feishu.as_identity", "飞书身份", "select", "通常使用已登录的 user 身份。", false, ["user", "bot"]],
    ],
  },
  {
    title: "转写与纠正",
    description: "语音识别并发、模型入口和领域词库。",
    fields: [
      ["asr.provider", "ASR 服务", "select", "默认使用火山引擎。", false, ["volcengine", "bailian"]],
      ["asr.mode", "转写模式", "select", "direct-url 优先让服务直接读取音频 URL。", false, ["direct-url", "auto", "download"]],
      ["asr.max_workers", "ASR 并发数", "number", "配额较低时建议设置为 2。"],
      ["asr.ffmpeg", "FFmpeg 路径", "text", "留空时从 PATH 查找。"],
      ["asr.model", "ASR 模型", "text", "留空使用脚本默认值。"],
      ["asr.endpoint", "ASR Endpoint", "text", "仅自定义服务时填写。"],
      ["correction.domain", "默认纠正领域", "text", "例如 douyin_shop_ads 或 ai_media。"],
      ["correction.glossary", "领域词库", "text", "项目内 JSON 词库路径。"],
    ],
  },
  {
    title: "内容总结",
    description: "OpenAI-compatible 模型生成归档卡片；API Key 仍从环境变量读取。",
    fields: [
      ["summary.enabled", "启用内容总结", "boolean", "模型和 API Key 都可用时才会执行。", true],
      ["summary.model", "模型名称", "text", "留空会明确标记为待补充。"],
      ["summary.base_url", "API Base URL", "text", "OpenAI-compatible 接口地址。", true],
      ["summary.api_key_env", "API Key 环境变量", "text", "只填写变量名。"],
      ["summary.temperature", "Temperature", "number", "推荐 0.2。"],
      ["summary.max_tokens", "最大输出 Tokens", "number", "归档卡片最大输出长度。"],
      ["summary.timeout", "超时（秒）", "number", "单次请求超时。"],
      ["summary.retry_attempts", "重试次数", "number", "429、5xx 和临时网络错误重试次数。"],
    ],
  },
  {
    title: "备份与知识库",
    description: "IMA、夸克和 Obsidian 独立执行，单路失败不影响其他目标。",
    fields: [
      ["backups.max_workers", "备份并发数", "number", "最多并行执行的备份目标数。"],
      ["backups.mapping_cache_ttl_hours", "映射缓存时长（小时）", "number", "远端目录映射的缓存时间。"],
      ["ima.enabled", "启用 IMA", "boolean", "备份最终文案到腾讯 IMA。", true],
      ["ima.mapping", "IMA 映射文件", "text", "达人到知识库/文件夹的本地映射。"],
      ["ima.on_duplicate", "IMA 重名策略", "select", "日常任务推荐 skip。", false, ["skip", "fail", "rename"]],
      ["kuake.enabled", "启用夸克", "boolean", "备份最终文案到夸克网盘。", true],
      ["kuake.local_env", "夸克私有配置", "text", "Cookie 等凭据所在的 local 文件。"],
      ["kuake.kuake_exe", "夸克 CLI", "text", "本机 kuake 可执行文件路径。"],
      ["kuake.base_dir", "夸克根目录", "text", "留空时读取私有配置默认目录。"],
      ["obsidian.enabled", "启用 Obsidian", "boolean", "导出本地知识库笔记。", true],
      ["obsidian.original_dir", "Obsidian 达人根目录", "text", "每位达人会在这里使用独立子目录。", true],
      ["obsidian.template_file", "通用笔记模板", "text", "所有达人的基础笔记框架。", true],
      ["obsidian.summary_template_file", "默认总结提示词", "text", "这是提示词，不是直接写入笔记的正文。", true],
      ["obsidian.creator_type_field", "达人类型字段", "text", "用于选择分类总结提示词。"],
      ["obsidian.summary_templates_by_creator_type", "按达人类型映射提示词", "json", "填写 JSON 对象，例如 {\"巨量千川\": \"D:/.../提示词.md\"}。", true],
    ],
  },
];

const creatorFields = [
  ["key", "达人 Key", "text", "稳定标识，只能使用字母、数字、_ 和 -。"],
  ["creator_name", "显示名称", "text", "飞书和 IMA 使用的达人名称。"],
  ["creator_dir_name", "目录名称", "text", "夸克和 Obsidian 使用。"],
  ["creator_url", "抖音主页 / SecUID", "text", "达人主页完整链接或 SecUID。", true],
  ["works_table_id", "飞书作品表 ID", "text", "该达人的专属作品表。"],
  ["works_file", "规范化作品 JSON", "text", "MediaCrawler 规范化输出。"],
  ["profile_file", "达人资料 JSON", "text", "头像、粉丝等基础资料快照。"],
  ["media_output_dir", "MediaCrawler 输出目录", "text", "每位达人独立输出目录。"],
  ["correction_domain", "纠正领域", "text", "覆盖全局 correction.domain。"],
  ["summary_template_file", "专属总结提示词", "text", "优先级高于达人类型和全局模板。", true],
  ["account_profiles", "专属账号池", "lines", "每行一个 profile；留空时使用全局账号池。", true],
];

function el(id) { return document.getElementById(id); }
function deepClone(value) { return JSON.parse(JSON.stringify(value)); }

function getPath(object, path) {
  return path.split(".").reduce((value, key) => (value && value[key] !== undefined ? value[key] : undefined), object);
}

function setPath(object, path, value) {
  const keys = path.split(".");
  let target = object;
  keys.slice(0, -1).forEach((key) => {
    if (!target[key] || typeof target[key] !== "object" || Array.isArray(target[key])) target[key] = {};
    target = target[key];
  });
  target[keys[keys.length - 1]] = value;
}

function toast(message, type = "success") {
  const item = document.createElement("div");
  item.className = `toast ${type === "error" ? "error" : ""}`;
  item.textContent = message;
  el("toast-stack").append(item);
  setTimeout(() => item.remove(), 3800);
}

async function api(path, options = {}) {
  const response = await fetch(path, {
    headers: { "Content-Type": "application/json" },
    ...options,
  });
  const payload = await response.json().catch(() => ({}));
  if (!response.ok) {
    const details = Array.isArray(payload.details) ? `\n${payload.details.join("\n")}` : "";
    throw new Error(`${payload.error || `请求失败 (${response.status})`}${details}`);
  }
  return payload;
}

function formatTime(value) {
  if (!value) return "暂无";
  const date = new Date(value);
  if (Number.isNaN(date.valueOf())) return value;
  return new Intl.DateTimeFormat("zh-CN", {
    month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", hour12: false,
  }).format(date);
}

function formatDuration(seconds) {
  const value = Math.round(Number(seconds) || 0);
  if (!value) return "不足 1 秒";
  const minutes = Math.floor(value / 60);
  const remaining = value % 60;
  return minutes ? `${minutes} 分 ${remaining} 秒` : `${remaining} 秒`;
}

function statusText(status) {
  return ({ Ready: "待命", Running: "运行中", Disabled: "已禁用", success: "成功", partial_failure: "部分失败", failed: "失败", planned: "计划", never: "尚未运行", ready: "已有数据", not_run: "无数据" })[status] || status || "未知";
}

function renderStatus(data) {
  const running = String(data.task.state).toLowerCase() === "running";
  el("task-pill").className = `task-pill ${running ? "running" : "ready"}`;
  el("task-pill-text").textContent = running ? "任务正在运行" : `任务${statusText(data.task.state)}`;
  el("run-button").disabled = running;
  el("metric-task").textContent = statusText(data.task.state);
  el("metric-task-note").textContent = `下次 ${formatTime(data.task.next_run_time)}`;
  el("metric-run").textContent = statusText(data.latest_run.status);
  el("metric-run-note").textContent = `${formatTime(data.latest_run.finished_at)} · ${formatDuration(data.latest_run.wall_seconds)}`;
  el("metric-creators").textContent = `${data.total_creators} 位 / ${data.total_works} 条`;
  el("metric-creators-note").textContent = `待处理 ${data.pending_works} 条`;
  el("metric-accounts").textContent = data.account_profiles_total ? `${data.account_profiles_detected} / ${data.account_profiles_total}` : "未启用";
  el("schedule-last").textContent = formatTime(data.task.last_run_time);
  el("schedule-next").textContent = formatTime(data.task.next_run_time);
  el("schedule-code").textContent = data.task.last_result === null ? "暂无" : String(data.task.last_result);
  el("schedule-duration").textContent = formatDuration(data.latest_run.wall_seconds);
  el("log-name").textContent = data.latest_log ? data.latest_log.split(/[\\/]/).pop() : "暂无日志";
  el("log-output").textContent = data.log_tail || "日志为空。";

  const body = el("creator-table-body");
  body.replaceChildren();
  let failures = 0;
  data.creators.forEach((creator) => {
    if (["failed", "partial_failure", "not_run"].includes(creator.status)) failures += 1;
    const row = document.createElement("tr");
    const values = [creator.name, creator.works_count, creator.pending_count, formatTime(creator.works_updated_at)];
    values.forEach((value) => {
      const cell = document.createElement("td"); cell.textContent = value; row.append(cell);
    });
    const statusCell = document.createElement("td");
    const badge = document.createElement("span");
    badge.className = `status-badge ${creator.status}`; badge.textContent = statusText(creator.status);
    statusCell.append(badge); row.append(statusCell);
    const detail = document.createElement("td"); detail.textContent = creator.detail || "—"; row.append(detail);
    body.append(row);
  });
  if (!data.creators.length) {
    const row = document.createElement("tr");
    const cell = document.createElement("td"); cell.colSpan = 6; cell.className = "empty-cell"; cell.textContent = "尚未配置已启用的达人";
    row.append(cell); body.append(row);
  }
  el("creator-summary").textContent = `${data.total_creators - failures} 正常 · ${failures} 异常`;
  el("status-error").classList.add("hidden");
}

async function loadStatus(showFeedback = false) {
  const button = el("refresh-button");
  button.disabled = true;
  try {
    renderStatus(await api("/api/status"));
    if (showFeedback) toast("状态已刷新");
  } catch (error) {
    el("task-pill").className = "task-pill error";
    el("task-pill-text").textContent = "状态读取失败";
    el("status-error").textContent = error.message;
    el("status-error").classList.remove("hidden");
    if (showFeedback) toast(error.message, "error");
  } finally {
    button.disabled = false;
  }
}

function createField(definition, value, path, creatorKey = null) {
  const [fieldKey, label, type, hint, full, choices] = definition;
  const wrapper = document.createElement("div");
  wrapper.className = `field ${full ? "full" : ""}`;
  const labelNode = document.createElement("label"); labelNode.textContent = label;
  wrapper.append(labelNode);
  let input;
  if (type === "boolean") {
    wrapper.className = `switch-field ${full ? "full" : ""}`;
    wrapper.replaceChildren();
    const text = document.createElement("div");
    const strong = document.createElement("strong"); strong.textContent = label;
    const small = document.createElement("small"); small.textContent = hint || "";
    text.append(strong, small);
    const switchLabel = document.createElement("label"); switchLabel.className = "switch";
    input = document.createElement("input"); input.type = "checkbox"; input.checked = Boolean(value);
    const track = document.createElement("span"); switchLabel.append(input, track);
    wrapper.append(text, switchLabel);
  } else if (type === "select") {
    input = document.createElement("select");
    (choices || []).forEach((choice) => {
      const option = document.createElement("option"); option.value = choice; option.textContent = choice;
      input.append(option);
    });
    input.value = value ?? "";
    wrapper.append(input);
  } else if (["lines", "json"].includes(type)) {
    input = document.createElement("textarea");
    input.value = type === "lines" ? (Array.isArray(value) ? value.join("\n") : "") : JSON.stringify(value || {}, null, 2);
    wrapper.append(input);
  } else {
    input = document.createElement("input"); input.type = type; input.value = value ?? "";
    if (type === "number") input.step = "any";
    wrapper.append(input);
  }
  input.dataset.valueType = type;
  if (creatorKey !== null) input.dataset.creatorKey = fieldKey;
  else input.dataset.path = path;
  if (type !== "boolean") {
    const help = document.createElement("small"); help.textContent = hint || ""; wrapper.append(help);
  }
  return wrapper;
}

function renderCreatorCard(creator, index) {
  const card = document.createElement("article"); card.className = "creator-card"; card.dataset.creatorCard = String(index);
  const head = document.createElement("div"); head.className = "creator-card-head";
  const title = document.createElement("div"); title.className = "creator-title";
  const number = document.createElement("span"); number.className = "creator-index"; number.textContent = String(index + 1).padStart(2, "0");
  const name = document.createElement("strong"); name.textContent = creator.creator_name || creator.key || "新达人";
  title.append(number, name);
  const actions = document.createElement("div"); actions.className = "creator-actions";
  actions.append(createField(["enabled", "启用", "boolean", "是否参与每日任务"], creator.enabled !== false, "", "enabled"));
  const remove = document.createElement("button"); remove.type = "button"; remove.className = "danger-link"; remove.textContent = "移除";
  remove.addEventListener("click", () => {
    if (!confirm(`确认从配置中移除「${creator.creator_name || creator.key || "该达人"}」？历史作品和飞书记录不会被删除。`)) return;
    state.config.creators.splice(index, 1); markDirty(); renderConfig();
  });
  actions.append(remove); head.append(title, actions); card.append(head);
  const fields = document.createElement("div"); fields.className = "creator-fields";
  creatorFields.forEach((definition) => fields.append(createField(definition, creator[definition[0]], "", definition[0])));
  card.append(fields);
  return card;
}

function renderConfig() {
  const form = el("config-form"); form.replaceChildren();
  const grid = document.createElement("div"); grid.className = "config-grid";
  sectionDefinitions.forEach((sectionDefinition) => {
    const section = document.createElement("section"); section.className = "config-section";
    const head = document.createElement("div"); head.className = "config-section-head";
    const heading = document.createElement("h2"); heading.textContent = sectionDefinition.title;
    const description = document.createElement("p"); description.textContent = sectionDefinition.description;
    head.append(heading, description);
    const fields = document.createElement("div"); fields.className = "fields";
    sectionDefinition.fields.forEach((definition) => fields.append(createField(definition, getPath(state.config, definition[0]), definition[0])));
    section.append(head, fields); grid.append(section);
  });

  const creatorsSection = document.createElement("section"); creatorsSection.className = "config-section full";
  const creatorHead = document.createElement("div"); creatorHead.className = "config-section-head";
  const creatorHeading = document.createElement("h2"); creatorHeading.textContent = `达人配置（${state.config.creators.length}）`;
  const creatorDescription = document.createElement("p"); creatorDescription.textContent = "每位达人使用独立作品表与本地产物目录；移除配置不会删除任何历史记录。";
  creatorHead.append(creatorHeading, creatorDescription);
  const list = document.createElement("div"); list.className = "creator-list";
  state.config.creators.forEach((creator, index) => list.append(renderCreatorCard(creator, index)));
  const add = document.createElement("button"); add.type = "button"; add.className = "add-creator"; add.textContent = "+ 添加达人";
  add.addEventListener("click", () => {
    const number = state.config.creators.length + 1;
    state.config.creators.push({ key: `creator-${number}`, enabled: true, creator_url: "", creator_name: "", creator_dir_name: "", works_table_id: "", works_file: `runtime/creator-${number}-works-from-mediacrawler.json`, profile_file: `runtime/profile-creator-${number}-update.json`, media_output_dir: `runtime/mediacrawler-output-creator-${number}`, correction_domain: getPath(state.config, "correction.domain") || "douyin_shop_ads" });
    markDirty(); renderConfig();
  });
  list.append(add); creatorsSection.append(creatorHead, list); grid.append(creatorsSection);
  form.append(grid);
  form.addEventListener("input", markDirty);
  form.addEventListener("change", markDirty);
}

function readInput(input) {
  const type = input.dataset.valueType;
  if (type === "boolean") return input.checked;
  if (type === "number") {
    if (input.value.trim() === "") return 0;
    const value = Number(input.value);
    if (!Number.isFinite(value)) throw new Error("数字配置中包含无效值");
    return value;
  }
  if (type === "lines") return input.value.split(/\r?\n/).map((line) => line.trim()).filter(Boolean);
  if (type === "json") {
    try {
      const value = JSON.parse(input.value || "{}");
      if (!value || typeof value !== "object" || Array.isArray(value)) throw new Error();
      return value;
    } catch (_) { throw new Error("按达人类型映射提示词必须是有效的 JSON 对象"); }
  }
  return input.value.trim();
}

function collectConfig() {
  const config = deepClone(state.config);
  document.querySelectorAll("#config-form [data-path]").forEach((input) => setPath(config, input.dataset.path, readInput(input)));
  config.creators = [...document.querySelectorAll("[data-creator-card]")].map((card, index) => {
    const creator = deepClone(state.config.creators[index] || {});
    card.querySelectorAll("[data-creator-key]").forEach((input) => { creator[input.dataset.creatorKey] = readInput(input); });
    return creator;
  });
  return config;
}

function markDirty() {
  state.dirty = true;
  el("unsaved-badge").classList.remove("hidden");
}

function markClean() {
  state.dirty = false;
  el("unsaved-badge").classList.add("hidden");
}

async function loadConfig(showFeedback = false) {
  try {
    const payload = await api("/api/config");
    state.config = payload.config;
    if (!Array.isArray(state.config.creators)) state.config.creators = [];
    el("config-path").textContent = payload.path;
    renderConfig(); markClean();
    const notice = el("config-notice");
    if (!payload.exists) {
      notice.textContent = "本机配置尚不存在，当前显示的是模板。首次保存会创建 local/pipeline.json。";
      notice.className = "notice";
    } else notice.classList.add("hidden");
    if (showFeedback) toast("配置已重新加载");
  } catch (error) {
    el("config-notice").textContent = error.message;
    el("config-notice").className = "notice";
    toast(error.message, "error");
  }
}

async function saveConfig() {
  const button = el("save-config-button"); button.disabled = true;
  try {
    const config = collectConfig();
    const payload = await api("/api/config", { method: "PUT", body: JSON.stringify(config) });
    state.config = payload.config; markClean(); renderConfig();
    const notice = el("config-notice"); notice.textContent = payload.backup_path ? `配置已保存；上一版本备份：${payload.backup_path}` : "配置已创建并保存。";
    notice.className = "notice success";
    toast("配置已保存");
  } catch (error) {
    const notice = el("config-notice"); notice.textContent = error.message; notice.className = "notice";
    toast(error.message, "error");
  } finally { button.disabled = false; }
}

async function runNow() {
  const button = el("run-button"); button.disabled = true;
  try {
    const payload = await api("/api/run", { method: "POST", body: "{}" });
    toast(payload.message || "已请求启动定时任务");
    setTimeout(() => loadStatus(false), 1200);
  } catch (error) { toast(error.message, "error"); button.disabled = false; }
}

function switchTab(tab) {
  state.activeTab = tab;
  document.querySelectorAll("[data-tab-target]").forEach((button) => button.classList.toggle("active", button.dataset.tabTarget === tab));
  document.querySelectorAll(".tab-page").forEach((page) => page.classList.toggle("active", page.id === `tab-${tab}`));
  el("page-title").textContent = tab === "overview" ? "运行总览" : "项目配置";
}

document.querySelectorAll("[data-tab-target]").forEach((button) => button.addEventListener("click", () => switchTab(button.dataset.tabTarget)));
el("refresh-button").addEventListener("click", () => loadStatus(true));
el("run-button").addEventListener("click", runNow);
el("save-config-button").addEventListener("click", saveConfig);
el("reload-config-button").addEventListener("click", () => {
  if (state.dirty && !confirm("放弃尚未保存的配置更改？")) return;
  loadConfig(true);
});
window.addEventListener("beforeunload", (event) => {
  if (!state.dirty) return;
  event.preventDefault(); event.returnValue = "";
});

loadStatus(false);
loadConfig(false);
setInterval(() => { if (state.activeTab === "overview") loadStatus(false); }, 12_000);
