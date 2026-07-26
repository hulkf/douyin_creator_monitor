"use strict";

const state = {
  config: null,
  dirty: false,
  activeTab: "overview",
  activeConfigCategory: "general",
  activeCreatorIndex: 0,
  accountPool: { profiles: [] },
  history: { stats: {}, runs: [] },
  funnel: null,
  lastStatus: null,
};

const sectionDefinitions = [
  {
    key: "general",
    icon: "01",
    title: "基础设置",
    summary: "运行路径与目录",
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
    key: "collection",
    icon: "02",
    title: "作品采集",
    summary: "MediaCrawler 与增量策略",
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
      ["collection.headless", "使用无头浏览器", "boolean", "关闭后采集时打开可见 Chrome；可见模式更便于观察，也可能降低账号被限制的概率。", true],
      ["collection.browser_window_width", "可见窗口宽度", "number", "有头模式下 Chrome 窗口宽度（像素），默认 480。"],
      ["collection.browser_window_height", "可见窗口高度", "number", "有头模式下 Chrome 窗口高度（像素），默认 360。"],
      ["collection.per_creator_profile_pool", "按达人隔离主 Profile", "boolean", "主账号可为每位达人复用独立 Profile 并发采集；账号池中的后续槽位仅作故障切换。"],
      ["collection.save_data_option", "原始数据格式", "select", "推荐使用 jsonl。", false, ["jsonl", "json", "csv"]],
      ["collection.profile_max_workers", "主页采集并发", "number", "达人资料采集的最大并发数。"],
      ["collection.profile_ttl_hours", "主页缓存时长（小时）", "number", "缓存未过期时复用本地资料。"],
      ["collection.cdp_port_start", "CDP 起始端口", "number", "每位达人使用独立端口。"],
      ["collection.cdp_port_stride", "CDP 端口步长", "number", "默认每个账号间隔 10。"],
      ["collection.clean_media_output", "采集前清理本轮输出", "boolean", "只清理独立运行目录，不影响历史规范化数据。", true],
    ],
  },
  {
    key: "feishu",
    icon: "03",
    title: "飞书同步",
    summary: "Base 表格与字段",
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
    key: "transcription",
    icon: "04",
    title: "转写与纠正",
    summary: "ASR 与领域词库",
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
    key: "summary",
    icon: "05",
    title: "内容总结",
    summary: "模型与提示词",
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
    key: "backups",
    icon: "06",
    title: "备份策略",
    summary: "三种备份统一管理",
    description: "IMA、夸克和 Obsidian 的全部配置集中在本页，三路备份仍会独立执行。",
    groups: [
      {
        title: "公共备份策略",
        description: "统一控制三种备份的并发和目录映射缓存。",
        fields: [
          ["backups.max_workers", "备份并发数", "number", "最多并行执行的备份目标数。"],
          ["backups.mapping_cache_ttl_hours", "映射缓存时长（小时）", "number", "远端目录映射的缓存时间。"],
        ],
      },
      {
        title: "腾讯 IMA 知识库",
        description: "最终文案备份到 IMA 知识库或达人文件夹。",
        fields: [
          ["ima.enabled", "启用 IMA", "boolean", "备份最终文案到腾讯 IMA。", true],
          ["ima.mapping", "IMA 映射文件", "text", "达人到知识库/文件夹的本地映射。"],
          ["ima.on_duplicate", "IMA 重名策略", "select", "日常任务推荐 skip。", false, ["skip", "fail", "rename"]],
        ],
      },
      {
        title: "夸克网盘",
        description: "最终文案按达人目录备份到夸克网盘。",
        fields: [
          ["kuake.enabled", "启用夸克", "boolean", "备份最终文案到夸克网盘。", true],
          ["kuake.local_env", "夸克私有配置", "text", "Cookie 等凭据所在的 local 文件。"],
          ["kuake.kuake_exe", "夸克 CLI", "text", "本机 kuake 可执行文件路径。"],
          ["kuake.base_dir", "夸克根目录", "text", "留空时读取私有配置默认目录。"],
        ],
      },
      {
        title: "Obsidian 本地知识库",
        description: "最终文案与内容总结导出到本地 Obsidian 达人目录。",
        fields: [
          ["obsidian.enabled", "启用 Obsidian", "boolean", "导出本地知识库笔记。", true],
          ["obsidian.original_dir", "Obsidian 达人根目录", "text", "每位达人会在这里使用独立子目录。", true],
          ["obsidian.template_file", "通用笔记模板", "text", "所有达人的基础笔记框架。", true],
          ["obsidian.summary_template_file", "默认总结提示词", "text", "这是提示词，不是直接写入笔记的正文。", true],
          ["obsidian.creator_type_field", "达人类型字段", "text", "用于选择分类总结提示词。"],
          ["obsidian.summary_templates_by_creator_type", "按达人类型映射提示词", "json", "填写 JSON 对象，例如 {\"巨量千川\": \"D:/.../提示词.md\"}。", true],
        ],
      },
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
];

const creatorCategoryDefinition = {
  key: "creators",
  icon: "07",
  title: "达人信息",
  summary: "统一达人模板",
  description: "所有达人复用同一套信息模板，通过列表切换当前编辑对象。",
};

const accountCategoryDefinition = {
  key: "accounts",
  icon: "池",
  title: "账号池管理",
  summary: "扫码登录与 Profile",
  description: "预先维护多个抖音账号槽位，逐个扫码登录并把登录态保存在本机独立 Profile 中。",
};

const configCategories = [
  sectionDefinitions[0],
  accountCategoryDefinition,
  ...sectionDefinitions.slice(1),
  creatorCategoryDefinition,
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

function formatDateTime(value, includeYear = false) {
  if (!value) return "暂无";
  const date = new Date(value);
  if (Number.isNaN(date.valueOf())) return value;
  return new Intl.DateTimeFormat("zh-CN", {
    ...(includeYear ? { year: "numeric" } : {}),
    month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", hour12: false,
  }).format(date);
}

function formatTime(value) {
  return formatDateTime(value);
}

function formatFullTime(value) {
  return formatDateTime(value, true);
}

function formatDuration(seconds) {
  const value = Math.round(Number(seconds) || 0);
  if (!value) return "不足 1 秒";
  const minutes = Math.floor(value / 60);
  const remaining = value % 60;
  return minutes ? `${minutes} 分 ${remaining} 秒` : `${remaining} 秒`;
}

function statusText(status) {
  return ({ Ready: "待命", Running: "运行中", running: "进行中", Disabled: "已禁用", success: "成功", partial_failure: "部分失败", failed: "失败", planned: "计划", never: "尚未运行", ready: "已有数据", not_run: "无数据" })[status] || status || "未知";
}

function renderStatus(data) {
  state.lastStatus = data;
  const running = String(data.task.state).toLowerCase() === "running";
  el("task-pill").className = `task-pill ${running ? "running" : "ready"}`;
  el("task-pill-text").textContent = running ? "任务正在运行" : `任务${statusText(data.task.state)}`;
  el("run-button").disabled = running;
  el("metric-creators").textContent = `${data.total_creators} 位`;
  el("metric-creators-note").textContent = `已启用 ${data.total_creators} 位达人`;
  el("metric-works").textContent = `${data.total_works} 条`;
  el("metric-works-note").textContent = `跨 ${data.total_creators} 位达人`;
  el("metric-pending").textContent = `${data.pending_works} 条`;
  el("metric-accounts").textContent = data.account_profiles_total ? `${data.account_profiles_detected} / ${data.account_profiles_total}` : "未启用";
  el("current-task-state").textContent = statusText(data.task.state);
  el("current-task-note").textContent = `下次 ${formatTime(data.task.next_run_time)}`;
  el("current-run-status").textContent = running ? "运行中" : "未开始";
  el("current-run-note").textContent = running
    ? `本轮开始于 ${formatTime(data.task.last_run_time)}`
    : "等待下次计划执行";
  el("current-duration").textContent = running ? "进行中" : "—";
  el("schedule-state").textContent = running ? "正在执行" : "等待触发";
  el("schedule-start").textContent = running ? formatTime(data.task.last_run_time) : "尚未开始";
  el("schedule-next").textContent = formatTime(data.task.next_run_time);
  el("log-name").textContent = data.latest_log ? data.latest_log.split(/[\\/]/).pop() : "暂无日志";
  el("log-output").textContent = data.log_tail || "日志为空。";

  const body = el("creator-table-body");
  body.replaceChildren();
  data.creators.forEach((creator) => {
    const row = document.createElement("tr");
    const values = [
      creator.name,
      creator.works_count,
      creator.pending_count,
      formatTime(creator.latest_publish_time),
      formatTime(creator.works_updated_at),
    ];
    values.forEach((value) => {
      const cell = document.createElement("td"); cell.textContent = value; row.append(cell);
    });
    const statusCell = document.createElement("td");
    const badge = document.createElement("span");
    const latestFailed = ["failed", "partial_failure"].includes(creator.status);
    const badgeStatus = creator.pending_count ? "planned" : latestFailed ? "failed" : creator.status === "not_run" ? "not_run" : "success";
    badge.className = `status-badge ${badgeStatus}`;
    badge.textContent = creator.pending_count ? "有待处理" : latestFailed ? "最近运行异常" : creator.status === "not_run" ? "暂无运行数据" : "数据正常";
    statusCell.append(badge); row.append(statusCell);
    body.append(row);
  });
  if (!data.creators.length) {
    const row = document.createElement("tr");
    const cell = document.createElement("td"); cell.colSpan = 6; cell.className = "empty-cell"; cell.textContent = "尚未配置已启用的达人";
    row.append(cell); body.append(row);
  }
  el("creator-summary").textContent = `${data.total_creators} 位达人 · ${data.total_works} 条作品`;

  const events = Array.isArray(data.activity_events) ? data.activity_events : [];
  const currentBody = el("current-creator-table-body");
  currentBody.replaceChildren();
  const hasEvents = events.length > 0;
  const useFinalRunStatus = !running
    && ["success", "partial_failure", "failed"].includes(String(data.latest_run?.status || ""));
  const showCurrentRun = running || useFinalRunStatus || hasEvents;
  const currentCreators = showCurrentRun ? data.creators : [];
  let currentSuccess = 0;
  let currentFailed = 0;
  let currentActive = 0;
  currentCreators.forEach((creator) => {
    const creatorEvents = events.filter((event) => {
      const message = String(event.message || "");
      return message.includes(creator.name) || message.includes(creator.key);
    });
    const latestEvent = creatorEvents[creatorEvents.length - 1];
    const finalEvent = [...creatorEvents].reverse().find((event) => String(event.message || "").includes("达人结束"));
    let currentStatus = "planned";
    if (useFinalRunStatus) {
      if (creator.status === "success") currentStatus = "success";
      else if (["failed", "partial_failure"].includes(creator.status)) currentStatus = "failed";
    } else if (finalEvent?.level === "failed") currentStatus = "failed";
    else if (finalEvent?.level === "success") currentStatus = "success";
    else if (latestEvent) currentStatus = "running";
    if (currentStatus === "success") currentSuccess += 1;
    else if (currentStatus === "failed") currentFailed += 1;
    else currentActive += 1;
    const row = document.createElement("tr");
    [creator.name, creator.works_count, creator.pending_count].forEach((value) => {
      const cell = document.createElement("td"); cell.textContent = value; row.append(cell);
    });
    const statusCell = document.createElement("td");
    const badge = document.createElement("span"); badge.className = `status-badge ${currentStatus}`;
    badge.textContent = currentStatus === "planned" ? "等待中" : statusText(currentStatus);
    statusCell.append(badge); row.append(statusCell);
    const detail = document.createElement("td");
    let currentDetail = useFinalRunStatus ? creator.detail : (finalEvent?.message || latestEvent?.message);
    if (!currentDetail && useFinalRunStatus && currentStatus !== "planned") {
      currentDetail = `达人结束: ${creator.name}，状态 ${creator.status}`;
    }
    detail.textContent = currentDetail || "等待本轮开始处理";
    row.append(detail);
    currentBody.append(row);
  });
  if (!currentCreators.length) {
    const row = document.createElement("tr");
    const cell = document.createElement("td"); cell.colSpan = 5; cell.className = "empty-cell"; cell.textContent = "当前没有正在执行的任务；最近结果请到“历史运行”查看";
    row.append(cell); currentBody.append(row);
  }
  const finishedCreators = currentSuccess + currentFailed;
  el("current-creator-result").textContent = showCurrentRun ? `${finishedCreators} / ${data.total_creators}` : "—";
  el("current-creator-result-note").textContent = running ? "已结束 / 全部达人" : showCurrentRun ? "最近一次运行已结束" : "等待任务开始";
  el("current-creator-summary").textContent = running
    ? `${currentSuccess} 正常 · ${currentFailed} 异常 · ${currentActive} 等待或进行中`
    : showCurrentRun
    ? `${currentSuccess} 正常 · ${currentFailed} 异常`
    : "当前任务待命";

  const timeline = el("activity-timeline"); timeline.replaceChildren();
  events.forEach((event) => {
    const item = document.createElement("div"); item.className = `activity-item ${event.level || "info"}`;
    const dot = document.createElement("span"); dot.className = "activity-dot";
    const text = document.createElement("div");
    const message = document.createElement("strong"); message.textContent = event.message;
    const time = document.createElement("small"); time.textContent = event.time;
    text.append(message, time); item.append(dot, text); timeline.append(item);
  });
  if (!events.length) {
    const empty = document.createElement("div"); empty.className = "empty-cell"; empty.textContent = "暂无可展示的执行事件";
    timeline.append(empty);
  }
  el("activity-count").textContent = `${events.length} 条事件`;
  renderOverviewExtras();
  el("status-error").classList.add("hidden");
  syncHeadlessToggle();
}

function syncHeadlessToggle() {
  const toggle = el("headless-toggle");
  if (!toggle) return;
  const value = Boolean(state.config && state.config.collection && state.config.collection.headless);
  toggle.checked = value;
}

async function onHeadlessToggle() {
  const toggle = el("headless-toggle");
  if (!toggle || !state.config) return;
  if (!state.config.collection || typeof state.config.collection !== "object") state.config.collection = {};
  const next = toggle.checked;
  state.config.collection.headless = next;
  try {
    const payload = await api("/api/config", { method: "PUT", body: JSON.stringify(state.config) });
    state.config = payload.config;
    syncHeadlessToggle();
    toast(next ? "已切换为无头模式（后台运行，不弹窗口）" : "已切换为有头模式（采集时打开可见 Chrome）");
  } catch (error) {
    syncHeadlessToggle();
    toast(error.message, "error");
  }
}

function renderHistory(data) {
  state.history = data;
  const stats = data.stats || {};
  el("metric-history-rate").textContent = `${stats.success_rate || 0}%`;
  el("metric-history-note").textContent = `${stats.successful_runs || 0} 次成功 · ${stats.issue_runs || 0} 次异常`;
  el("overview-history-total").textContent = `${stats.total_runs || 0} 次`;
  el("overview-history-success").textContent = `${stats.successful_runs || 0} 次`;
  el("overview-history-issues").textContent = `${stats.issue_runs || 0} 次`;
  el("overview-latest-success").textContent = formatTime(stats.latest_success_at);
  el("history-total").textContent = stats.total_runs || 0;
  el("history-success").textContent = stats.successful_runs || 0;
  el("history-issues").textContent = stats.issue_runs || 0;
  el("history-rate").textContent = `${stats.success_rate || 0}%`;
  el("history-latest-success").textContent = `最近成功：${formatFullTime(stats.latest_success_at)}`;
  if (el("metric-last-success")) el("metric-last-success").textContent = formatTime(stats.latest_success_at);
  if (el("overview-health-headline")) updateHealthHeadline();

  const list = el("history-list"); list.replaceChildren();
  (data.runs || []).forEach((run, index) => {
    const card = document.createElement("article"); card.className = `history-run-card ${run.status}`;
    const marker = document.createElement("div"); marker.className = "history-run-marker";
    marker.textContent = index === 0 ? "上一次" : index === 1 ? "上上次" : `第 ${index + 1} 条`;
    const main = document.createElement("div"); main.className = "history-run-main";
    const heading = document.createElement("div"); heading.className = "history-run-heading";
    const title = document.createElement("strong"); title.textContent = formatFullTime(run.started_at);
    const badge = document.createElement("span"); badge.className = `status-badge ${run.status}`; badge.textContent = statusText(run.status);
    heading.append(title, badge);
    const headline = document.createElement("p"); headline.textContent = run.headline;
    const meta = document.createElement("div"); meta.className = "history-run-meta";
    [
      `耗时 ${formatDuration(run.wall_seconds)}`,
      `达人 ${run.successful_creators}/${run.creator_count} 正常`,
      `处理 ${run.selected_count} 条作品`,
    ].forEach((value) => { const span = document.createElement("span"); span.textContent = value; meta.append(span); });
    main.append(heading, headline, meta);
    if (Array.isArray(run.issues) && run.issues.length) {
      const issues = document.createElement("div"); issues.className = "history-run-issues";
      run.issues.forEach((issue) => {
        const item = document.createElement("div");
        const name = document.createElement("strong"); name.textContent = issue.creator;
        const message = document.createElement("span"); message.textContent = issue.message;
        item.append(name, message); issues.append(item);
      });
      main.append(issues);
    }
    card.append(marker, main); list.append(card);
  });
  if (!(data.runs || []).length) {
    const empty = document.createElement("div"); empty.className = "empty-cell"; empty.textContent = "还没有历史运行记录";
    list.append(empty);
  }
}

function updateHealthHeadline() {
  const host = el("overview-health-headline");
  if (!host) return;
  const runs = (state.history && state.history.runs) || [];
  host.textContent = runs.length ? runs[0].headline : "还没有历史运行记录";
}

function renderAccountDetail() {
  const host = el("overview-account-list");
  if (!host) return;
  const profiles = (state.accountPool && state.accountPool.profiles) || [];
  host.replaceChildren();
  if (!profiles.length) {
    const empty = document.createElement("div"); empty.className = "empty-cell"; empty.textContent = "账号池未配置";
    host.append(empty); return;
  }
  profiles.forEach((profile) => {
    const row = document.createElement("div"); row.className = "account-detail-row";
    const left = document.createElement("div"); left.className = "account-detail-id";
    const dot = document.createElement("span"); dot.className = `account-state-dot ${profile.status}`;
    const key = document.createElement("strong"); key.textContent = profile.key;
    left.append(dot, key);
    const right = document.createElement("div"); right.className = "account-detail-meta";
    const badge = document.createElement("span"); badge.className = `account-state-badge ${profile.status}`; badge.textContent = accountStatusText(profile.status);
    const updated = document.createElement("small"); updated.textContent = profile.updated_at ? `更新于 ${formatTime(profile.updated_at)}` : "未检测到登录文件";
    right.append(badge, updated);
    if (profile.status !== "ready" && profile.status !== "running") {
      const btn = document.createElement("button"); btn.type = "button"; btn.className = "button secondary small"; btn.textContent = "去扫码";
      btn.addEventListener("click", () => startAccountLoginByKey(profile.key));
      right.append(btn);
    }
    row.append(left, right);
    host.append(row);
  });
}

function renderChannels() {
  const host = el("overview-channels");
  if (!host) return;
  const config = state.config || {};
  const items = [
    ["飞书", Boolean(getPath(config, "feishu.creator_table_id"))],
    ["转写", Boolean(getPath(config, "asr.provider"))],
    ["总结", Boolean(getPath(config, "summary.enabled"))],
    ["IMA", Boolean(getPath(config, "ima.enabled"))],
    ["夸克", Boolean(getPath(config, "kuake.enabled"))],
    ["Obsidian", Boolean(getPath(config, "obsidian.enabled"))],
  ];
  host.replaceChildren();
  items.forEach(([label, on]) => {
    const chip = document.createElement("span"); chip.className = `channel-chip ${on ? "on" : "off"}`;
    chip.textContent = `${label} ${on ? "开" : "关"}`;
    host.append(chip);
  });
}

function renderFunnel() {
  const host = el("funnel-bars");
  if (!host || !state.funnel) return;
  const funnel = state.funnel;
  const total = funnel.total_works_with_state || 0;
  const totalEl = el("funnel-total");
  if (totalEl) totalEl.textContent = total ? `${total} 部作品已处理` : "—";
  host.replaceChildren();
  if (!total) {
    const empty = document.createElement("div"); empty.className = "empty-cell"; empty.textContent = "还没有作品处理记录";
    host.append(empty); return;
  }
  funnel.stages.forEach((stage) => {
    const success = stage.success || 0;
    const ratio = total ? success / total : 0;
    const cls = success === total ? "full" : success === 0 ? "empty" : "partial";
    const row = document.createElement("div"); row.className = "funnel-row";
    const label = document.createElement("div"); label.className = "funnel-label"; label.textContent = stage.label;
    const bar = document.createElement("div"); bar.className = "funnel-track";
    const fill = document.createElement("div"); fill.className = `funnel-fill ${cls}`; fill.style.width = `${Math.round(ratio * 100)}%`;
    bar.append(fill);
    const count = document.createElement("div"); count.className = "funnel-count"; count.textContent = `${success}/${total}`;
    row.append(label, bar, count);
    host.append(row);
  });
  const fc = el("funnel-creators");
  if (fc) {
    fc.replaceChildren();
    if (!funnel.creators.length) {
      const empty = document.createElement("div"); empty.className = "empty-cell"; empty.textContent = "暂无达人";
      fc.append(empty);
    } else {
      funnel.creators.forEach((creator) => {
        const done = (creator.stages && creator.stages.summarized) || 0;
        const row = document.createElement("div"); row.className = "funnel-creator-row";
        const name = document.createElement("span"); name.className = "funnel-creator-name"; name.textContent = creator.name;
        const track = document.createElement("div"); track.className = "funnel-track small";
        const fill = document.createElement("div"); fill.className = `funnel-fill ${done === creator.total ? "full" : done === 0 ? "empty" : "partial"}`;
        fill.style.width = creator.total ? `${Math.round(done / creator.total * 100)}%` : "0%";
        track.append(fill);
        const count = document.createElement("span"); count.className = "funnel-creator-count"; count.textContent = `总结 ${done}/${creator.total}`;
        row.append(name, track, count);
        fc.append(row);
      });
    }
  }
}

function deriveAlerts() {
  const host = el("overview-alerts");
  if (!host) return;
  const alerts = [];
  const status = state.lastStatus || {};
  const profiles = (state.accountPool && state.accountPool.profiles) || [];
  profiles.forEach((profile) => {
    if (profile.status === "missing") alerts.push({ level: "error", text: `账号槽位 ${profile.key} 登录态缺失，最近运行可能因此失败`, key: profile.key });
    else if (profile.status === "failed") alerts.push({ level: "error", text: `账号槽位 ${profile.key} 登录未完成`, key: profile.key });
  });
  (status.creators || []).forEach((creator) => {
    if (["failed", "partial_failure", "not_run"].includes(creator.status)) {
      const detail = creator.detail ? `：${creator.detail}` : "";
      alerts.push({ level: creator.status === "not_run" ? "warn" : "error", text: `达人 ${creator.name} 最近运行异常${detail}` });
    }
  });
  if (status.pending_works > 0) {
    alerts.push({ level: "warn", text: `${status.pending_works} 条作品待处理（等待转写 / 总结 / 回写 / 备份）` });
  }
  const runs = (state.history && state.history.runs) || [];
  if (runs.length && ["failed", "partial_failure"].includes(runs[0].status)) {
    (runs[0].issues || []).slice(0, 3).forEach((issue) => alerts.push({ level: "error", text: `${issue.creator}：${issue.message}` }));
  }
  if (state.funnel && state.funnel.total_works_with_state) {
    const summarized = state.funnel.stages.find((stage) => stage.key === "summarized");
    const total = state.funnel.total_works_with_state;
    if (summarized && summarized.success < total * 0.8) {
      alerts.push({ level: "warn", text: `内容总结仅完成 ${summarized.success}/${total} 部，其余未生成归档卡片` });
    }
  }
  host.replaceChildren();
  if (!alerts.length) { host.classList.add("hidden"); return; }
  host.classList.remove("hidden");
  alerts.slice(0, 6).forEach((alert) => {
    const row = document.createElement("div"); row.className = `alert-item ${alert.level}`;
    const dot = document.createElement("span"); dot.className = "alert-dot";
    const text = document.createElement("span"); text.className = "alert-text"; text.textContent = alert.text;
    row.append(dot, text);
    if (alert.key) {
      const btn = document.createElement("button"); btn.type = "button"; btn.className = "alert-action"; btn.textContent = "去扫码";
      btn.addEventListener("click", () => startAccountLoginByKey(alert.key));
      row.append(btn);
    }
    host.append(row);
  });
}

function renderOverviewExtras() {
  renderAccountDetail();
  renderChannels();
  renderFunnel();
  updateHealthHeadline();
  deriveAlerts();
}

function startAccountLoginByKey(key) {
  const profiles = getPath(state.config, "collection.account_profiles") || [];
  const index = profiles.indexOf(key);
  if (index < 0) { toast(`账号槽位 ${key} 不在配置中`, "error"); return; }
  startAccountLogin(index);
}

async function loadFunnel(showFeedback = false) {
  try {
    state.funnel = await api("/api/funnel");
    renderOverviewExtras();
    if (showFeedback) toast("处理漏斗已刷新");
  } catch (error) {
    if (showFeedback) toast(error.message, "error");
  }
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

async function loadHistory(showFeedback = false) {
  try {
    renderHistory(await api("/api/history"));
    if (showFeedback) toast("历史运行记录已刷新");
  } catch (error) {
    if (showFeedback) toast(error.message, "error");
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
  if (path === "feishu.work_id_field") {
    input.readOnly = true;
    input.title = "作品唯一键由项目数据保护规则固定";
  }
  input.dataset.valueType = type;
  if (creatorKey !== null) input.dataset.creatorKey = fieldKey;
  else input.dataset.path = path;
  if (type !== "boolean") {
    const help = document.createElement("small"); help.textContent = hint || ""; wrapper.append(help);
  }
  return wrapper;
}

function configSectionShell(definition) {
  const section = document.createElement("section");
  section.className = "config-section config-section-active";
  const head = document.createElement("div"); head.className = "config-section-head config-section-titlebar";
  const titleBlock = document.createElement("div");
  const kicker = document.createElement("span"); kicker.className = "config-section-number"; kicker.textContent = definition.icon;
  const heading = document.createElement("h2"); heading.textContent = definition.title;
  const description = document.createElement("p"); description.textContent = definition.description;
  titleBlock.append(kicker, heading, description); head.append(titleBlock); section.append(head);
  return section;
}

function renderConfigCategoryNavigation() {
  const nav = el("config-category-nav"); nav.replaceChildren();
  configCategories.forEach((definition) => {
    const button = document.createElement("button");
    button.type = "button";
    button.className = `config-category-button ${state.activeConfigCategory === definition.key ? "active" : ""}`;
    button.dataset.configCategory = definition.key;
    const number = document.createElement("span"); number.className = "config-category-number"; number.textContent = definition.icon;
    const text = document.createElement("span");
    const title = document.createElement("strong"); title.textContent = definition.title;
    const summary = document.createElement("small");
    summary.textContent = definition.key === "creators" ? `${state.config.creators.length} 位达人 · ${definition.summary}` : definition.summary;
    text.append(title, summary); button.append(number, text);
    button.addEventListener("click", () => switchConfigCategory(definition.key));
    nav.append(button);
  });
}

function renderStandardConfigSection(definition) {
  const section = configSectionShell(definition);
  if (Array.isArray(definition.groups)) {
    const groups = document.createElement("div"); groups.className = "backup-config-groups";
    definition.groups.forEach((groupDefinition) => {
      const group = document.createElement("section"); group.className = "backup-config-group";
      const head = document.createElement("div"); head.className = "backup-config-group-head";
      const title = document.createElement("h3"); title.textContent = groupDefinition.title;
      const description = document.createElement("p"); description.textContent = groupDefinition.description;
      head.append(title, description);
      const fields = document.createElement("div"); fields.className = "fields";
      groupDefinition.fields.forEach((fieldDefinition) => {
        fields.append(createField(fieldDefinition, getPath(state.config, fieldDefinition[0]), fieldDefinition[0]));
      });
      group.append(head, fields); groups.append(group);
    });
    section.append(groups);
    return section;
  }
  const fields = document.createElement("div"); fields.className = "fields";
  definition.fields.forEach((fieldDefinition) => {
    fields.append(createField(fieldDefinition, getPath(state.config, fieldDefinition[0]), fieldDefinition[0]));
  });
  section.append(fields);
  return section;
}

function accountStatusText(status) {
  return ({
    ready: "已保存登录态",
    running: "等待扫码",
    failed: "登录未完成",
    missing: "尚未登录",
  })[status] || "尚未登录";
}

function renderAccountPoolManager() {
  const section = configSectionShell(accountCategoryDefinition);
  section.classList.add("account-pool-section");
  const body = document.createElement("div"); body.className = "account-pool-body";
  const intro = document.createElement("div"); intro.className = "account-pool-intro";
  const introText = document.createElement("div");
  const introTitle = document.createElement("strong"); introTitle.textContent = "账号槽位与达人 Profile 相互隔离";
  const introDescription = document.createElement("p");
  introDescription.textContent = "列表第一项永远是主账号，后续项目依次是备用账号；删除第一项并保存后，新的第一项自动成为主账号。主账号扫码成功后会立即刷新达人并发 Profile，Cookie 只保存在本机 MediaCrawler/browser_data。";
  introText.append(introTitle, introDescription);
  const refresh = document.createElement("button"); refresh.type = "button"; refresh.className = "button secondary"; refresh.textContent = "刷新登录状态";
  refresh.addEventListener("click", () => loadAccountPoolStatus(true));
  intro.append(introText, refresh); body.append(intro);

  const profiles = getPath(state.config, "collection.account_profiles") || [];
  const statuses = new Map((state.accountPool.profiles || []).map((item) => [item.key, item]));
  const list = document.createElement("div"); list.className = "account-pool-list";
  profiles.forEach((profileKey, index) => {
    const status = statuses.get(profileKey) || { status: "missing", updated_at: null };
    const row = document.createElement("article"); row.className = "account-pool-row";
    const identity = document.createElement("div"); identity.className = "account-pool-identity";
    const number = document.createElement("span"); number.textContent = String(index + 1).padStart(2, "0");
    const field = document.createElement("div"); field.className = "field";
    const label = document.createElement("label");
    label.textContent = index === 0 ? "账号槽位名称 · 主账号" : `账号槽位名称 · 备用账号 ${index}`;
    const input = document.createElement("input"); input.type = "text"; input.value = profileKey; input.dataset.accountProfileIndex = String(index);
    const help = document.createElement("small"); help.textContent = "稳定标识，例如 account-a；修改名称会创建新的 Profile。";
    field.append(label, input, help); identity.append(number, field);

    const stateBlock = document.createElement("div"); stateBlock.className = "account-pool-state";
    const badge = document.createElement("span"); badge.className = `account-state-badge ${status.status}`; badge.textContent = accountStatusText(status.status);
    const updated = document.createElement("small"); updated.textContent = status.updated_at ? `更新于 ${formatTime(status.updated_at)}` : "本机尚未检测到登录文件";
    stateBlock.append(badge, updated);

    const actions = document.createElement("div"); actions.className = "account-pool-actions";
    const login = document.createElement("button"); login.type = "button"; login.className = "button primary";
    login.textContent = status.status === "running" ? "等待扫码…" : status.status === "ready" ? "重新扫码登录" : "保存并扫码登录";
    login.disabled = status.status === "running";
    login.addEventListener("click", () => startAccountLogin(index));
    const remove = document.createElement("button"); remove.type = "button"; remove.className = "danger-link"; remove.textContent = "移出账号池";
    remove.addEventListener("click", () => {
      try { syncVisibleConfigToState(); } catch (error) { showConfigInputError(error); return; }
      state.config.collection.account_profiles.splice(index, 1);
      markDirty(); renderConfig();
      toast("已从账号池配置移除；本地登录 Profile 未删除");
    });
    actions.append(login, remove); row.append(identity, stateBlock, actions); list.append(row);
  });
  if (!profiles.length) {
    const empty = document.createElement("div"); empty.className = "account-pool-empty";
    const title = document.createElement("strong"); title.textContent = "账号池还是空的";
    const text = document.createElement("p"); text.textContent = "先添加账号槽位，再逐个点击扫码登录。";
    empty.append(title, text); list.append(empty);
  }
  body.append(list);
  const add = document.createElement("button"); add.type = "button"; add.className = "add-creator account-add-button"; add.textContent = "+ 添加账号槽位";
  add.addEventListener("click", () => {
    try { syncVisibleConfigToState(); } catch (error) { showConfigInputError(error); return; }
    if (!state.config.collection || typeof state.config.collection !== "object") state.config.collection = {};
    if (!Array.isArray(state.config.collection.account_profiles)) state.config.collection.account_profiles = [];
    let number = state.config.collection.account_profiles.length + 1;
    let key = `account-${number}`;
    while (state.config.collection.account_profiles.includes(key)) { number += 1; key = `account-${number}`; }
    state.config.collection.account_profiles.push(key); markDirty(); renderConfig();
  });
  body.append(add); section.append(body); return section;
}

function createNewCreator() {
  const number = state.config.creators.length + 1;
  return {
    key: `creator-${number}`,
    enabled: true,
    creator_url: "",
    creator_name: "",
    creator_dir_name: "",
    works_table_id: "",
    works_file: `runtime/creator-${number}-works-from-mediacrawler.json`,
    profile_file: `runtime/profile-creator-${number}-update.json`,
    media_output_dir: `runtime/mediacrawler-output-creator-${number}`,
    correction_domain: getPath(state.config, "correction.domain") || "douyin_shop_ads",
  };
}

function switchCreator(index) {
  try {
    syncVisibleConfigToState();
  } catch (error) {
    showConfigInputError(error);
    return;
  }
  state.activeCreatorIndex = index;
  renderConfig();
}

function renderCreatorEditor() {
  const definition = creatorCategoryDefinition;
  const section = configSectionShell(definition);
  section.classList.add("creator-template-section");
  const creators = state.config.creators;
  state.activeCreatorIndex = Math.min(Math.max(state.activeCreatorIndex, 0), Math.max(creators.length - 1, 0));

  const workspace = document.createElement("div"); workspace.className = "creator-template-workspace";
  const selector = document.createElement("aside"); selector.className = "creator-selector";
  const selectorHead = document.createElement("div"); selectorHead.className = "creator-selector-head";
  const selectorTitle = document.createElement("strong"); selectorTitle.textContent = "达人列表";
  const selectorCount = document.createElement("span"); selectorCount.textContent = `${creators.length} 位`;
  selectorHead.append(selectorTitle, selectorCount); selector.append(selectorHead);

  const selectorList = document.createElement("div"); selectorList.className = "creator-selector-list";
  creators.forEach((creator, index) => {
    const button = document.createElement("button"); button.type = "button";
    button.className = `creator-selector-item ${index === state.activeCreatorIndex ? "active" : ""}`;
    const avatar = document.createElement("span"); avatar.textContent = String(index + 1).padStart(2, "0");
    const text = document.createElement("span");
    const name = document.createElement("strong"); name.textContent = creator.creator_name || creator.key || "未命名达人";
    const key = document.createElement("small"); key.textContent = creator.key || "待填写 Key";
    text.append(name, key); button.append(avatar, text);
    button.addEventListener("click", () => switchCreator(index)); selectorList.append(button);
  });
  selector.append(selectorList);
  const add = document.createElement("button"); add.type = "button"; add.className = "add-creator"; add.textContent = "+ 添加达人";
  add.addEventListener("click", () => {
    try { syncVisibleConfigToState(); } catch (error) { showConfigInputError(error); return; }
    state.config.creators.push(createNewCreator());
    state.activeCreatorIndex = state.config.creators.length - 1;
    markDirty(); renderConfig();
  });
  selector.append(add); workspace.append(selector);

  const editor = document.createElement("div"); editor.className = "creator-template-editor";
  if (!creators.length) {
    const empty = document.createElement("div"); empty.className = "creator-template-empty";
    const heading = document.createElement("strong"); heading.textContent = "还没有达人配置";
    const text = document.createElement("p"); text.textContent = "点击左侧“添加达人”，使用统一信息模板录入第一位达人。";
    empty.append(heading, text); editor.append(empty);
  } else {
    const creator = creators[state.activeCreatorIndex];
    editor.dataset.creatorEditor = String(state.activeCreatorIndex);
    const editorHead = document.createElement("div"); editorHead.className = "creator-template-editor-head";
    const editorTitle = document.createElement("div");
    const heading = document.createElement("strong"); heading.textContent = creator.creator_name || creator.key || "未命名达人";
    const hint = document.createElement("small"); hint.textContent = "使用统一达人信息模板编辑当前对象";
    editorTitle.append(heading, hint);
    const actions = document.createElement("div"); actions.className = "creator-actions";
    actions.append(createField(["enabled", "启用", "boolean", "是否参与每日任务"], creator.enabled !== false, "", "enabled"));
    const remove = document.createElement("button"); remove.type = "button"; remove.className = "danger-link"; remove.textContent = "移除达人";
    remove.addEventListener("click", () => {
      try { syncVisibleConfigToState(); } catch (error) { showConfigInputError(error); return; }
      const current = state.config.creators[state.activeCreatorIndex];
      if (!confirm(`确认从配置中移除「${current.creator_name || current.key || "该达人"}」？历史作品和飞书记录不会被删除。`)) return;
      state.config.creators.splice(state.activeCreatorIndex, 1);
      state.activeCreatorIndex = Math.min(state.activeCreatorIndex, Math.max(state.config.creators.length - 1, 0));
      markDirty(); renderConfig();
    });
    actions.append(remove); editorHead.append(editorTitle, actions); editor.append(editorHead);
    const fields = document.createElement("div"); fields.className = "creator-fields creator-template-fields";
    creatorFields.forEach((fieldDefinition) => {
      fields.append(createField(fieldDefinition, creator[fieldDefinition[0]], "", fieldDefinition[0]));
    });
    editor.append(fields);
  }
  workspace.append(editor); section.append(workspace);
  return section;
}

function renderConfig() {
  renderConfigCategoryNavigation();
  const content = el("config-category-content"); content.replaceChildren();
  const definition = configCategories.find((item) => item.key === state.activeConfigCategory) || sectionDefinitions[0];
  if (definition.key === "creators") content.append(renderCreatorEditor());
  else if (definition.key === "accounts") content.append(renderAccountPoolManager());
  else content.append(renderStandardConfigSection(definition));
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

function showConfigInputError(error) {
  const notice = el("config-notice"); notice.textContent = error.message; notice.className = "notice";
  toast(error.message, "error");
}

function syncVisibleConfigToState() {
  document.querySelectorAll("#config-form [data-path]").forEach((input) => {
    setPath(state.config, input.dataset.path, readInput(input));
  });
  const creatorEditor = document.querySelector("[data-creator-editor]");
  if (creatorEditor) {
    const index = Number(creatorEditor.dataset.creatorEditor);
    const creator = state.config.creators[index];
    creatorEditor.querySelectorAll("[data-creator-key]").forEach((input) => {
      creator[input.dataset.creatorKey] = readInput(input);
    });
  }
  const accountInputs = [...document.querySelectorAll("[data-account-profile-index]")];
  if (accountInputs.length) {
    if (!state.config.collection || typeof state.config.collection !== "object") state.config.collection = {};
    state.config.collection.account_profiles = accountInputs.map((input) => input.value.trim());
  }
}

function collectConfig() {
  syncVisibleConfigToState();
  return deepClone(state.config);
}

function switchConfigCategory(categoryKey) {
  if (state.activeConfigCategory === categoryKey) return;
  try {
    syncVisibleConfigToState();
  } catch (error) {
    showConfigInputError(error);
    return;
  }
  state.activeConfigCategory = categoryKey;
  renderConfig();
}

function markDirty() {
  state.dirty = true;
  el("unsaved-badge").classList.remove("hidden");
}

function markClean() {
  state.dirty = false;
  el("unsaved-badge").classList.add("hidden");
}

async function loadAccountPoolStatus(showFeedback = false) {
  try {
    state.accountPool = await api("/api/accounts");
    if (state.activeConfigCategory === "accounts" && !state.dirty && state.config) renderConfig();
    if (showFeedback) toast("账号池状态已刷新");
  } catch (error) {
    if (showFeedback) toast(error.message, "error");
  }
}

async function startAccountLogin(index) {
  try {
    const config = collectConfig();
    const profileKey = config.collection.account_profiles[index];
    if (!profileKey) throw new Error("账号槽位名称不能为空");
    const saved = await api("/api/config", { method: "PUT", body: JSON.stringify(config) });
    state.config = saved.config; markClean();
    const result = await api("/api/accounts/login", {
      method: "POST",
      body: JSON.stringify({ profile_key: profileKey }),
    });
    toast(result.message || "扫码登录窗口已打开");
    await loadAccountPoolStatus(false);
    renderConfig();
  } catch (error) {
    showConfigInputError(error);
  }
}

async function loadConfig(showFeedback = false) {
  try {
    const payload = await api("/api/config");
    state.config = payload.config;
    if (!Array.isArray(state.config.creators)) state.config.creators = [];
    if (!state.config.collection || typeof state.config.collection !== "object") state.config.collection = {};
    if (!Array.isArray(state.config.collection.account_profiles)) state.config.collection.account_profiles = [];
    el("config-path").textContent = payload.path;
    markClean();
    await loadAccountPoolStatus(false);
    renderConfig();
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
  el("page-title").textContent = ({
    overview: "项目总览",
    current: "当前任务",
    history: "历史运行",
    config: "项目配置",
  })[tab] || "项目总览";
  // “立即运行”只在“当前任务”页出现：总览与历史是只读页面，没有可执行的东西。
  el("run-button").style.display = tab === "current" ? "" : "none";
  if (tab === "current") loadStatus(false);
  if (tab === "history") loadHistory(false);
}

async function refreshActivePage() {
  if (state.activeTab === "history") return loadHistory(true);
  if (state.activeTab === "overview") {
    await Promise.all([loadStatus(false), loadHistory(false), loadFunnel(false)]);
    toast("项目总览已刷新");
    return;
  }
  if (state.activeTab === "config") return loadConfig(true);
  return loadStatus(true);
}

document.querySelectorAll("[data-tab-target]").forEach((button) => button.addEventListener("click", () => switchTab(button.dataset.tabTarget)));
el("refresh-button").addEventListener("click", refreshActivePage);
el("run-button").addEventListener("click", runNow);
el("headless-toggle").addEventListener("change", onHeadlessToggle);
el("save-config-button").addEventListener("click", saveConfig);
el("config-form").addEventListener("input", markDirty);
el("config-form").addEventListener("change", markDirty);
el("reload-config-button").addEventListener("click", () => {
  if (state.dirty && !confirm("放弃尚未保存的配置更改？")) return;
  loadConfig(true);
});
window.addEventListener("beforeunload", (event) => {
  if (!state.dirty) return;
  event.preventDefault(); event.returnValue = "";
});

// 初始页为“项目总览”，默认隐藏“立即运行”按钮（仅“当前任务”页显示）。
el("run-button").style.display = "none";
loadStatus(false);
loadHistory(false);
loadConfig(false);
loadFunnel(false);
setInterval(() => { if (["overview", "current"].includes(state.activeTab)) loadStatus(false); }, 12_000);
setInterval(() => { if (["overview", "history"].includes(state.activeTab)) loadHistory(false); }, 30_000);
setInterval(() => {
  if (state.activeTab === "config" && state.activeConfigCategory === "accounts" && !state.dirty) {
    loadAccountPoolStatus(false);
  }
}, 3_000);
