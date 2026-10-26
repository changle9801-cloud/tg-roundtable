"use strict";

// ---------- helpers ----------

const $ = (sel, root = document) => root.querySelector(sel);

function h(tag, attrs = {}, ...children) {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v === null || v === undefined || v === false) continue;
    if (k === "class") el.className = v;
    else if (k.startsWith("on")) el.addEventListener(k.slice(2), v);
    else if (k === "value") el.value = v;
    else if (k === "checked" || k === "disabled" || k === "hidden" || k === "selected") el[k] = true;
    else el.setAttribute(k, v === true ? "" : v);
  }
  for (const c of children.flat()) {
    if (c === null || c === undefined || c === false) continue;
    el.append(c instanceof Node ? c : document.createTextNode(String(c)));
  }
  return el;
}

async function api(method, path, body) {
  const res = await fetch(path, {
    method,
    headers: body === undefined ? {} : { "Content-Type": "application/json" },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  let data = {};
  try { data = await res.json(); } catch { /* empty body */ }
  if (!res.ok) throw new Error(data.error || `请求失败（HTTP ${res.status}）`);
  return data;
}

let toastTimer = null;
function toast(text, isError = false) {
  const el = $("#toast");
  el.textContent = text;
  el.className = "toast" + (isError ? " err" : "");
  el.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => (el.hidden = true), isError ? 6000 : 2500);
}

const clone = (x) => JSON.parse(JSON.stringify(x));

// ---------- state ----------

let server = null; // last response of /api/state
let draft = null; // editable copy of server.settings
let pending = { bots: {}, api_keys: {} }; // secret edits: string = set, null = clear
const expanded = new Set();
const newIds = new Set(); // members added here and not saved yet: their ID is still editable
const results = {}; // member id -> {text|node, err}
const paramsText = {}; // member id -> raw JSON text being edited
const invalid = new Set(); // inputs currently holding an invalid value

const PRESETS = [
  { id: "claude", label: "Claude 订阅", hint: "本机已登录的 Claude Code", m: { kind: "claude_cli", name: "Claude", model: "sonnet", effort: "low" } },
  { id: "codex", label: "Codex 订阅", hint: "本机已登录的 Codex CLI", m: { kind: "codex_cli", name: "Codex", effort: "low", aliases: ["GPT"] } },
  { id: "grok", label: "Grok 订阅", hint: "本机已登录的 Grok Build CLI", m: { kind: "grok_cli", name: "Grok", effort: "low" } },
  { id: "claude-api", label: "Claude API", hint: "console.anthropic.com 申请 key，按量计费", m: { kind: "api", provider: "anthropic", name: "Claude", model: "claude-opus-5-5", effort: "low" } },
  { id: "gpt", label: "OpenAI API（GPT）", hint: "platform.openai.com 申请 key，模型用「查询可用模型」选", m: { kind: "api", provider: "openai", name: "GPT" } },
  { id: "grok-api", label: "Grok API", hint: "console.x.ai 申请 key，模型用「查询可用模型」选", m: { kind: "api", provider: "grok", name: "Grok" } },
  { id: "deepseek", label: "DeepSeek API", hint: "platform.deepseek.com 申请 key", m: { kind: "api", provider: "deepseek", name: "DeepSeek", model: "deepseek-flash" } },
  { id: "gemini", label: "Gemini API", hint: "aistudio.google.com 申请 key", m: { kind: "api", provider: "gemini", name: "Gemini", model: "gemini-flash-latest", effort: "low" } },
  { id: "doubao", label: "豆包 API（火山方舟）", hint: "模型填方舟的模型 ID 或接入点 ID", m: { kind: "api", provider: "doubao", name: "豆包" } },
  { id: "custom", label: "其他 OpenAI 兼容 API", hint: "填写接口地址，如 OpenRouter、Ollama", m: { kind: "api", provider: "", name: "新成员" } },
];

// Everyday characters. Applying one fills the role card (not the name); edit freely after.
const ROLE_TEMPLATES = [
  { id: "roaster", label: "吐槽担当", role: {
    tagline: "嘴毒心软的吐槽担当",
    persona: "二十八岁，广告公司策划。看到什么都想损两句，但分寸拿得很准，从不真的伤人。其实很会照顾人，最怕群里冷场。",
    style: "短句为主，爱反问，口头禅是「好家伙」「就这？」，偶尔一个 😅。从不长篇大论。",
    examples: "就这？我还以为多大事呢\n好家伙，你这操作我是真没想到\n行吧行吧，你开心就好😅",
    activity: "high" } },
  { id: "warm", label: "暖心姐姐", role: {
    tagline: "群里的知心大姐姐",
    persona: "三十岁的护士，温柔细心，记得每个人的小事。别人难过时第一个出来安慰，也会提醒大家按时吃饭睡觉。",
    style: "语气软，常用「呀」「啦」「嘛」，偶尔一个 🥺 或 ❤️。会先接住别人的情绪，再说自己的想法。",
    examples: "抱抱你呀，今天是不是很累啦\n先去吃点东西嘛，饿着肚子心情更差\n没事的，慢慢来就好❤️",
    activity: "normal" } },
  { id: "foodie", label: "吃货", role: {
    tagline: "什么话题都能聊到吃上",
    persona: "二十五岁，美食博主，走遍大半个中国找小馆子。对各地小吃如数家珍，聊什么都能绕回吃的。",
    style: "热情，说到吃的就停不下来，描述很有画面感，爱用「绝了」「馋哭」「必须安排」。",
    examples: "说到这个我就馋了，那家的肥肠粉真的绝\n必须安排！周末谁去\n听你说完我口水都下来了",
    activity: "high" } },
  { id: "artsy", label: "文艺青年", role: {
    tagline: "爱书、爱电影、爱深夜感慨",
    persona: "二十七岁，书店店员，喜欢老电影、诗和一个人旅行。常常把小事说得很有意境，对生活有自己的小坚持。",
    style: "句子稍长，节奏慢，偶尔引一句歌词或书里的话，不用网络流行语，很少用 emoji。",
    examples: "下雨天适合重看《情书》，那句「你好吗」每次都戳中我\n有些路只适合一个人慢慢走\n今天的晚霞像被人打翻的橘子汽水",
    activity: "normal" } },
  { id: "contrarian", label: "杠精", role: {
    tagline: "总想听听反方意见",
    persona: "三十二岁，辩论队出身，现在做销售。别人都同意的事他总要找出另一面，但讲道理，被说服了会大方承认。",
    style: "开口常是「也不一定吧」「我反对」，爱举反例，语气欠欠的但不刻薄。",
    examples: "也不一定吧，我就见过反例\n我反对，理由有三，算了说一个就够了\n行，这次你说服我了",
    activity: "normal" } },
  { id: "elder", label: "热心长辈", role: {
    tagline: "群里的大家长",
    persona: "五十六岁的退休语文老师，热心肠，关心年轻人的工作、身体和婚恋，爱分享养生知识和人生经验。",
    style: "称呼大家「孩子们」，常说「要注意身体」，偶尔发个 🌹 或 👍，说话慢条斯理。",
    examples: "孩子们，天冷了记得添衣服🌹\n年轻人别总熬夜，身体是本钱\n这个想法不错👍，就是别太累着",
    activity: "low" } },
  { id: "sunny", label: "乐天派", role: {
    tagline: "群里的开心果",
    persona: "二十三岁，刚毕业的健身教练，什么事都往好处想，情绪稳定，爱给大家打气。",
    style: "语气轻快，爱用「哈哈哈」「冲！」「稳了」，emoji 比较多 💪✨。",
    examples: "哈哈哈哈这也太好笑了\n冲！明天又是元气满满的一天💪\n没事没事，换个角度想其实挺好的✨",
    activity: "high" } },
  { id: "quiet", label: "冷面学霸", role: {
    tagline: "话少，但句句在点上",
    persona: "二十六岁的历史系博士生，知道很多冷知识，平时潜水，开口就是一针见血。",
    style: "极简短，经常一句话结束，不用 emoji，偶尔冒一个冷知识。",
    examples: "不对。\n其实古代人一天只吃两顿。\n嗯，有道理。",
    activity: "low" } },
];

const PROVIDER_LABELS = {
  anthropic: "Claude（Anthropic）",
  openai: "OpenAI",
  grok: "Grok（xAI）",
  deepseek: "DeepSeek",
  gemini: "Gemini",
  doubao: "豆包（火山方舟）",
};

function blankMember(id) {
  return {
    id, name: "", kind: "api", enabled: true, model: "", effort: "", aliases: [], persona: "",
    command: [], provider: "", base_url: "", api_key_env: "", params: {}, max_history: 40, web_search: false,
    tagline: "", style: "", relationships: "", examples: "", activity: "normal", max_turns: 0,
    proactive: false, proactive_topics: "", proactive_per_day: 3, proactive_hours: "10-23",
    proactive_idle_minutes: 60, proactive_replies: 1,
    browse: false, browse_topics: "", browse_per_day: 4, browse_hours: "10-23", journal_days: 3, journal_keep_days: 7,
    appearance: "", can_draw: false, draws_per_day: 5, vision: false,
  };
}

function hasPendingSecrets() {
  return "controller_token" in pending || Object.keys(pending.bots).length > 0 || Object.keys(pending.api_keys).length > 0;
}
function settingsDirty() {
  return JSON.stringify(draft) !== JSON.stringify(server.settings);
}
function changed() {
  $("#savebar").hidden = !(settingsDirty() || hasPendingSecrets());
}

async function load() {
  server = await api("GET", "/api/state");
  resetDraft();
}

function resetDraft() {
  draft = clone(server.settings);
  pending = { bots: {}, api_keys: {} };
  newIds.clear();
  invalid.clear();
  for (const k of Object.keys(paramsText)) delete paramsText[k];
  renderAll();
  changed();
}

async function save() {
  if (invalid.size) {
    toast("有填写不正确的内容（标红的输入框），请先修正", true);
    return;
  }
  const btn = $("#save-btn");
  btn.disabled = true;
  btn.textContent = "保存中…";
  try {
    if (settingsDirty()) server = await api("PUT", "/api/settings", draft);
    if (hasPendingSecrets()) server = await api("PUT", "/api/secrets", pending);
    resetDraft();
    toast("已保存，配置已生效");
  } catch (e) {
    toast(e.message, true);
  } finally {
    btn.disabled = false;
    btn.textContent = "保存并生效";
  }
}

function renderAll() {
  renderMembers();
  renderRules();
  renderPrompts();
  renderStickerSettings();
  renderStickers();
  renderKaomoji();
  renderByMember();
  renderMemory();
  renderMemes();
  renderStatus(server.status);
}

// ---------- reusable fields ----------

function field(label, input, hint) {
  return h("label", { class: "field" }, h("span", {}, label, hint ? h("small", {}, hint) : null), input);
}

function textInput(value, onChange, attrs = {}) {
  return h("input", { value: value ?? "", oninput: (e) => { onChange(e.target.value); changed(); }, ...attrs });
}

/** A write-only secret: shows "已设置 …abcd", accepts a new value, or can be cleared. */
function secretField(group, pid) {
  const current = group === "controller_token" ? server.secrets.controller_token : server.secrets[group][pid] || "";
  const pendingValue = () => (group === "controller_token" ? pending.controller_token : pending[group][pid]);
  const stateEl = h("span", { class: "state" });
  const refresh = () => {
    const p = pendingValue();
    if (p === null) { stateEl.textContent = "保存后清除"; stateEl.className = "state pending"; }
    else if (p) { stateEl.textContent = "待保存"; stateEl.className = "state pending"; }
    else if (current) { stateEl.textContent = "已设置 " + current; stateEl.className = "state set"; }
    else { stateEl.textContent = "未设置"; stateEl.className = "state"; }
  };
  const set = (value) => {
    if (group === "controller_token") {
      if (value === undefined) delete pending.controller_token;
      else pending.controller_token = value;
    } else if (value === undefined) delete pending[group][pid];
    else pending[group][pid] = value;
    refresh();
    changed();
  };
  const input = h("input", {
    type: "password",
    autocomplete: "off",
    placeholder: current ? "输入新值以替换" : "粘贴到这里",
    value: typeof pendingValue() === "string" ? pendingValue() : "",
    oninput: (e) => set(e.target.value.trim() ? e.target.value.trim() : undefined),
  });
  const clear = current
    ? h("button", { type: "button", class: "danger", onclick: () => { input.value = ""; set(null); } }, "清除")
    : null;
  refresh();
  return h("div", { class: "secret" }, input, stateEl, clear);
}

// ---------- members ----------

function memberStatus(id) {
  return (server.status.members || []).find((m) => m.id === id);
}

function kindLabel(m) {
  if (m.kind !== "api") return server.meta.kinds[m.kind] || m.kind;
  return "API · " + (PROVIDER_LABELS[m.provider] || m.provider || "自定义接口");
}

function renderMembers() {
  const box = $("#members");
  box.replaceChildren();
  if (!draft.participants.length) {
    box.append(h("div", { class: "card empty" }, "还没有成员。点右上角「添加成员」开始。"));
    return;
  }
  for (const m of draft.participants) box.append(memberCard(m));
}

function memberCard(m) {
  const st = memberStatus(m.id);
  const isNew = newIds.has(m.id);
  let dot = "disabled", msg = "已停用";
  if (m.enabled) {
    if (isNew || !st) { dot = ""; msg = "保存后生效"; }
    else { dot = st.state; msg = st.message || (st.bot_username ? "@" + st.bot_username : "正常"); }
  }
  const open = expanded.has(m.id);
  const toggleOpen = () => { open ? expanded.delete(m.id) : expanded.add(m.id); renderMembers(); };

  const enabled = h("label", { class: "switch", title: "启用 / 停用", onclick: (e) => e.stopPropagation() },
    h("input", { type: "checkbox", checked: m.enabled, onchange: (e) => { m.enabled = e.target.checked; changed(); renderMembers(); } }),
    h("span"));

  const head = h("div", { class: "member-head", onclick: toggleOpen },
    enabled,
    h("span", { class: "dot " + dot }),
    h("div", {}, h("div", { class: "title" }, m.name || "（未命名）"), h("div", { class: "kind" }, kindLabel(m))),
    h("div", { class: "spacer" }),
    h("span", { class: "msg", title: msg }, msg),
    h("span", { class: "muted" }, open ? "▾" : "▸"));

  const card = h("div", { class: "card member" + (open ? "" : " collapsed") + (m.enabled ? "" : " disabled") }, head);
  if (open) card.append(memberBody(m, isNew));
  return card;
}

function memberBody(m, isNew) {
  const idInput = h("input", {
    value: m.id,
    disabled: !isNew,
    title: isNew ? "" : "ID 保存后不能修改",
    oninput: (e) => {
      const next = e.target.value.trim();
      const ok = /^[a-z0-9][a-z0-9_-]{0,31}$/.test(next) && !draft.participants.some((o) => o !== m && o.id === next);
      e.target.style.borderColor = ok ? "" : "var(--err)";
      if (!ok) { invalid.add("id:" + m.id); return; }
      invalid.delete("id:" + m.id);
      renameMember(m, next);
    },
  });

  const area = (value, rows, placeholder, onInput) => h("textarea", {
    rows, placeholder,
    oninput: (e) => { onInput(e.target.value); changed(); schedulePreview(); },
  }, value || "");

  const templateSelect = h("select", {
    onchange: (e) => {
      const t = ROLE_TEMPLATES.find((t) => t.id === e.target.value);
      if (!t) return;
      Object.assign(m, clone(t.role));
      changed();
      renderMembers();
      schedulePreview();
    },
  },
    h("option", { value: "" }, "套用角色模板…"),
    ...ROLE_TEMPLATES.map((t) => h("option", { value: t.id }, t.label)));

  const rows = [
    h("div", { class: "row-between section-title" }, h("h3", {}, "角色"), templateSelect),
    h("div", { class: "grid2" },
      field("角色名", textInput(m.name, (v) => (m.name = v)), "群里显示的名字，也会同步成 bot 的名字"),
      field("一句话介绍", textInput(m.tagline, (v) => (m.tagline = v), { maxlength: 120, placeholder: "例如：嘴毒心软的吐槽担当" }),
        "会显示在 bot 的资料里")),
    field("身份和性格", area(m.persona, 3, "例如：二十八岁的广告策划，嘴上不饶人，其实很会照顾人。最怕冷场。", (v) => (m.persona = v))),
    field("外形", area(m.appearance, 2, "例如：蓝色头发、穿女仆装、带鲸鱼头鳍和大尾巴的鲸鱼娘", (v) => (m.appearance = v)),
      "它长什么样；有人给它画表情包时照这个画"),
    field("说话风格", area(m.style, 2, "例如：短句多，爱反问，常说「好家伙」，偶尔一个 😅，从不长篇大论", (v) => (m.style = v))),
    field("怎么看群里其他人", area(m.relationships, 2, "例如：觉得小雅太容易感动，和老周是互怼的老朋友", (v) => (m.relationships = v))),
    field("示例台词", area(m.examples, 3, "每行一句，最能定住说话的语气。例如：\n就这？\n你这话说的，我都不知道怎么接", (v) => (m.examples = v)),
      "给几句它会说的话，学语气用，不会照抄"),
    h("div", { class: "grid3" },
      field("活跃度", h("select", {
        onchange: (e) => { m.activity = e.target.value; changed(); schedulePreview(); },
      },
        h("option", { value: "low", selected: m.activity === "low" }, "话少（多半在旁边看）"),
        h("option", { value: "normal", selected: !m.activity || m.activity === "normal" }, "正常"),
        h("option", { value: "high", selected: m.activity === "high" }, "话痨（更常被点到）"))),
      field("每轮发言上限", h("input", {
        type: "number", min: 0, max: 10, value: m.max_turns || 0,
        oninput: (e) => { m.max_turns = Number(e.target.value) || 0; changed(); },
      }), "0 = 跟随群聊规则"),
      field("别名", textInput((m.aliases || []).join("，"), (v) => (m.aliases = v.split(/[,，]/).map((s) => s.trim()).filter(Boolean))),
        "叫到这些名字会优先回话")),

    ...browseSection(m),
    ...proactiveSection(m),
    ...drawingSection(m),

    h("h3", { class: "section-title" }, "技术设置"),
    h("div", { class: "grid3" },
      field("ID", idInput, "小写字母、数字、- 和 _"),
      field("模型", textInput(m.model, (v) => (m.model = v), { placeholder: m.kind === "api" ? "必填" : "留空使用默认模型" })),
      field("思考强度", textInput(m.effort, (v) => (m.effort = v), { placeholder: "如 low / medium / high" }))),
  ];

  if (m.kind === "api") {
    const providers = server.meta.providers;
    const providerSelect = h("select", {
      onchange: (e) => { m.provider = e.target.value; changed(); renderMembers(); },
    },
      ...Object.keys(providers).map((p) => h("option", { value: p, selected: m.provider === p }, PROVIDER_LABELS[p] || p)),
      h("option", { value: "", selected: !m.provider }, "自定义接口"));
    const defaultUrl = m.provider && providers[m.provider] ? providers[m.provider].base_url : "";
    rows.push(
      h("div", { class: "grid2" },
        field("服务商", providerSelect),
        field("接口地址", textInput(m.base_url, (v) => (m.base_url = v), { placeholder: defaultUrl || "例如 https://openrouter.ai/api/v1" }),
          defaultUrl ? "留空使用默认地址" : "必填")),
      field("API key", secretField("api_keys", m.id),
        m.provider && providers[m.provider] ? `也可以不填，改为设置环境变量 ${providers[m.provider].env}` : ""),
      h("div", { class: "grid2" },
        field("历史条数", h("input", {
          type: "number", min: 2, value: m.max_history,
          oninput: (e) => { m.max_history = Number(e.target.value) || 2; changed(); },
        }), "每次请求带上多少条历史消息，越多越贵"),
        field("附加参数（JSON）", paramsInput(m),
          m.provider === "anthropic" ? "一般留空；Claude 已不接受 temperature 等采样参数" : "例如 {\"temperature\": 0.8}")),
    );
  }

  const canSearch = ["claude_cli", "codex_cli", "grok_cli"].includes(m.kind) || (m.kind === "api" && m.provider === "anthropic");
  rows.push(h("label", { class: "field check" },
    h("input", {
      type: "checkbox",
      checked: canSearch && m.web_search,
      disabled: !canSearch,
      onchange: (e) => { m.web_search = e.target.checked; changed(); schedulePreview(); },
    }),
    h("span", {}, "联网搜索",
      h("small", {}, canSearch
        ? (m.kind === "grok_cli" ? "用 Grok 自带的搜索查网页和 X（推特），只读，不能操作你的电脑" : "用模型自带的搜索查网页，只读，不能操作你的电脑")
        : "这类成员暂不支持（等插件功能）"))));

  if (m.kind === "api" || m.kind === "claude_cli") {
    rows.push(h("label", { class: "field check" },
      h("input", { type: "checkbox", checked: m.vision, onchange: (e) => { m.vision = e.target.checked; changed(); schedulePreview(); } }),
      h("span", {}, "能看图",
        h("small", {}, m.kind === "api"
          ? "模型支持图片输入时打开（例如 DeepSeek、GPT、Claude、Gemini 的多模态模型），群里发的图会直接给它看"
          : "群里发的图会直接给它看；有人发图时会多用一些 Claude 订阅额度，平时聊天不受影响"))));
  }

  rows.push(field("Bot token", secretField("bots", m.id), "这个成员在 Telegram 里发言用的 bot，在 @BotFather 创建"));

  if (m.kind !== "api") {
    rows.push(h("details", {},
      h("summary", {}, "高级"),
      field("自定义命令", textInput((m.command || []).join(" "), (v) => (m.command = v.trim() ? v.trim().split(/\s+/) : [])),
        "一般留空，会自动查找已安装的 CLI；路径里有空格时不支持")));
  }

  const result = results[m.id];
  const resultEl = result ? h("div", { class: "result" + (result.err ? " err" : "") }, result.node || result.text) : null;

  const testBtn = h("button", { onclick: () => testMember(m, testBtn) }, "测试对话");
  const modelsBtn = m.kind === "api" ? h("button", { onclick: () => listModels(m, modelsBtn) }, "查询可用模型") : null;
  const actions = h("div", { class: "member-actions" },
    testBtn, modelsBtn,
    h("div", { style: "flex:1" }),
    h("button", { class: "danger", onclick: () => deleteMember(m) }, "删除成员"));

  return h("div", { class: "member-body" }, ...rows, resultEl, actions);
}

function browseSection(m) {
  const canSearch = (["claude_cli", "codex_cli", "grok_cli"].includes(m.kind) || (m.kind === "api" && m.provider === "anthropic")) && m.web_search;
  const num = (key, min, max) => h("input", {
    type: "number", min, max, value: m[key],
    oninput: (e) => { m[key] = Number(e.target.value); changed(); },
  });
  const info = (server.status.browse || {})[m.id];
  let statusText = "保存后开始安排";
  if (m.browse && info) {
    const next = info.next_at ? new Date(info.next_at * 1000) : null;
    const when = next ? `${next.getHours()}:${String(next.getMinutes()).padStart(2, "0")}` : "—";
    statusText = info.running ? "正在浏览……"
      : info.done_for_today ? `今天刷了 ${info.posts_today} 次有收获，今天到此为止`
      : !info.in_hours ? `今天刷到东西 ${info.posts_today} 次，现在不在浏览时间段`
      : `今天刷到东西 ${info.posts_today} 次，下次大约 ${when}`;
  }
  const tryBtn = h("button", { disabled: !canSearch, onclick: () => tryBrowse(m, tryBtn) }, "现在去刷一次");

  const fields = m.browse ? [
    field("平时爱刷什么", h("textarea", {
      rows: 2,
      placeholder: "例如：数码评测、效率工具和生活小技巧（留空则用「主动发言」里的关注方向）",
      oninput: (e) => { m.browse_topics = e.target.value; changed(); },
    }, m.browse_topics || "")),
    h("div", { class: "grid3" },
      field("每天刷几次", num("browse_per_day", 1, 12)),
      field("时间段", textInput(m.browse_hours, (v) => (m.browse_hours = v), { placeholder: "10-23" })),
      h("div", {})),
    h("div", { class: "grid3" },
      field("见闻保留几天", num("journal_days", 1, 30), "普通的看过就忘"),
      field("印象深的保留几天", num("journal_keep_days", 1, 60), "很想分享的、已经分享过的"),
      h("div", {})),
    h("div", { class: "row-between", style: "margin-bottom:12px" },
      h("span", { class: "muted small" }, statusText), tryBtn),
  ] : [];

  return [
    h("h3", { class: "section-title" }, "日常浏览与见闻"),
    h("label", { class: "field check" },
      h("input", {
        type: "checkbox", checked: m.browse, disabled: !canSearch && !m.browse,
        onchange: (e) => { m.browse = e.target.checked; changed(); renderMembers(); },
      }),
      h("span", {}, "平时自己上网刷一刷，把看到的东西记进见闻",
        h("small", {}, canSearch ? "被问「在干嘛」时照见闻回答；冷场时从见闻里挑一条分享" : "需要先打开下面「技术设置」里的联网搜索"))),
    ...fields,
  ];
}

async function tryBrowse(m, btn) {
  if (settingsDirty() || hasPendingSecrets()) {
    toast("请先保存更改再试", true);
    return;
  }
  btn.disabled = true;
  btn.textContent = "正在刷……";
  try {
    const r = await api("POST", `/api/members/${encodeURIComponent(m.id)}/browse`);
    journalData = r;
    results[m.id] = r.added.length
      ? { text: "记下了：\n" + r.added.map((e) => `· ${e.title}（${e.url}）`).join("\n") }
      : { text: "这次没刷到值得记的东西。" };
  } catch (e) {
    results[m.id] = { text: e.message, err: true };
  }
  renderMembers();
}

function drawingSection(m) {
  if (m.kind !== "codex_cli") return [];
  return [
    h("h3", { class: "section-title" }, "画表情包"),
    h("label", { class: "field check" },
      h("input", { type: "checkbox", checked: m.can_draw, onchange: (e) => { m.can_draw = e.target.checked; changed(); renderMembers(); schedulePreview(); } }),
      h("span", {}, "群里有人请它画表情包时，它会接活",
        h("small", {}, "用你 ChatGPT 订阅的生图额度，每张约 1 分钟；画好的图会进「表情包」库"))),
    m.can_draw ? h("div", { class: "grid3" },
      field("每天最多画几张", h("input", {
        type: "number", min: 1, max: 20, value: m.draws_per_day,
        oninput: (e) => { m.draws_per_day = Number(e.target.value); changed(); },
      })),
      h("div", {}), h("div", {})) : null,
  ];
}

function proactiveSection(m) {
  const canSearch = (["claude_cli", "codex_cli", "grok_cli"].includes(m.kind) || (m.kind === "api" && m.provider === "anthropic")) && m.web_search;
  const num = (key, min, max) => h("input", {
    type: "number", min, max, value: m[key],
    oninput: (e) => { m[key] = Number(e.target.value); changed(); },
  });
  const info = (server.status.proactive?.members || {})[m.id];
  let statusText = "";
  if (m.proactive && info) {
    const next = info.next_at ? new Date(info.next_at * 1000) : null;
    const when = next ? `${next.getHours()}:${String(next.getMinutes()).padStart(2, "0")}` : "—";
    statusText = info.done_for_today ? `今天已发 ${info.posts_today} 次，今天到此为止`
      : !info.in_hours ? `今天已发 ${info.posts_today} 次，现在不在发言时间段`
      : `今天已发 ${info.posts_today} 次，下次大约 ${when} 看看（群里冷场才会发）`;
  }
  const tryBtn = h("button", { onclick: () => tryProactive(m, tryBtn) }, "立即试一次");

  const fields = m.proactive ? [
    field("关注什么", h("textarea", {
      rows: 2,
      placeholder: canSearch ? "例如：AI 圈八卦、太空和宇宙的新发现、离谱新闻、好笑的推文" : "例如：吃的、天气、周末去哪玩",
      oninput: (e) => { m.proactive_topics = e.target.value; changed(); },
    }, m.proactive_topics || ""),
      canSearch ? (m.kind === "grok_cli" ? "它会去 X 和网上找相关的新内容，附上链接" : "它会去网上找相关的新内容，附上链接")
        : "没开联网搜索，它只会随口开个话题，不会分享新闻"),
    h("div", { class: "grid3" },
      field("每天最多几次", num("proactive_per_day", 1, 12)),
      field("时间段", textInput(m.proactive_hours, (v) => (m.proactive_hours = v), { placeholder: "10-23" }), "只在这几点之间发"),
      field("冷场多久才发（分钟）", num("proactive_idle_minutes", 0, 1440), "不打断正在进行的聊天")),
    h("div", { class: "grid3" },
      field("别人接几句", num("proactive_replies", 0, 3), "它发完后其他角色每人最多接几句"),
      h("div", {}), h("div", {})),
    h("div", { class: "row-between", style: "margin-bottom:12px" },
      h("span", { class: "muted small" }, statusText || "保存后开始安排"), tryBtn),
  ] : [];

  return [
    h("h3", { class: "section-title" }, "主动发言"),
    h("label", { class: "field check" },
      h("input", { type: "checkbox", checked: m.proactive, onchange: (e) => { m.proactive = e.target.checked; changed(); renderMembers(); } }),
      h("span", {}, "群里冷场时，偶尔主动分享点东西",
        h("small", {}, "找不到有意思的就不发；发过的链接不会重复"))),
    ...fields,
  ];
}

async function tryProactive(m, btn) {
  if (settingsDirty() || hasPendingSecrets()) {
    toast("请先保存更改再试", true);
    return;
  }
  btn.disabled = true;
  btn.textContent = "正在找…";
  try {
    const r = await api("POST", `/api/members/${encodeURIComponent(m.id)}/proactive`);
    results[m.id] = r.posted
      ? { text: `已发到群 ${r.chat}：\n${r.posted}` }
      : { text: "这次没找到值得发的，没有发言（这很正常，宁缺毋滥）。" };
  } catch (e) {
    results[m.id] = { text: e.message, err: true };
  }
  renderMembers();
}

function paramsInput(m) {
  const text = paramsText[m.id] ?? (Object.keys(m.params || {}).length ? JSON.stringify(m.params) : "");
  return h("input", {
    value: text,
    class: "mono",
    oninput: (e) => {
      paramsText[m.id] = e.target.value;
      try {
        const v = e.target.value.trim() ? JSON.parse(e.target.value) : {};
        if (typeof v !== "object" || Array.isArray(v) || v === null) throw new Error();
        m.params = v;
        invalid.delete("params:" + m.id);
        e.target.style.borderColor = "";
      } catch {
        invalid.add("params:" + m.id);
        e.target.style.borderColor = "var(--err)";
      }
      changed();
    },
  });
}

function renameMember(m, next) {
  const old = m.id;
  if (old === next) return;
  m.id = next;
  newIds.delete(old); newIds.add(next);
  if (expanded.delete(old)) expanded.add(next);
  for (const g of ["bots", "api_keys"]) {
    if (old in pending[g]) { pending[g][next] = pending[g][old]; delete pending[g][old]; }
  }
  if (draft.telegram.controller_participant === old) draft.telegram.controller_participant = next;
  changed();
}

function deleteMember(m) {
  if (!confirm(`确定删除成员「${m.name || m.id}」吗？它的 bot token 和 API key 也会一起删除。`)) return;
  draft.participants = draft.participants.filter((o) => o !== m);
  delete pending.bots[m.id];
  delete pending.api_keys[m.id];
  newIds.delete(m.id);
  if (draft.telegram.controller_participant === m.id) draft.telegram.controller_participant = "";
  changed();
  renderAll();
}

function addMember(preset) {
  const taken = new Set(draft.participants.map((p) => p.id));
  let id = preset.id, n = 2;
  while (taken.has(id)) id = `${preset.id}-${n++}`;
  const m = Object.assign(blankMember(id), clone(preset.m));
  draft.participants.push(m);
  newIds.add(id);
  expanded.add(id);
  changed();
  renderAll();
  $("#members").lastElementChild?.scrollIntoView({ behavior: "smooth", block: "start" });
}

async function testMember(m, btn) {
  if (settingsDirty() || hasPendingSecrets()) {
    toast("请先保存更改再测试", true);
    return;
  }
  btn.disabled = true;
  btn.textContent = "测试中…";
  try {
    const r = await api("POST", `/api/members/${encodeURIComponent(m.id)}/test`);
    results[m.id] = { text: `${r.text}\n\n（用时 ${r.seconds} 秒）` };
  } catch (e) {
    results[m.id] = { text: e.message, err: true };
  }
  renderMembers();
}

async function listModels(m, btn) {
  if (settingsDirty() || hasPendingSecrets()) {
    toast("请先保存更改再查询", true);
    return;
  }
  btn.disabled = true;
  btn.textContent = "查询中…";
  try {
    const { models } = await api("GET", `/api/members/${encodeURIComponent(m.id)}/models`);
    const node = h("div", {},
      h("div", { class: "small muted", style: "margin:0 0 6px" }, `共 ${models.length} 个，点击即可填入「模型」：`),
      h("div", {}, ...models.map((id) =>
        h("button", { class: "link", onclick: () => { m.model = id; changed(); renderMembers(); } }, id))));
    results[m.id] = { node };
  } catch (e) {
    results[m.id] = { text: e.message, err: true };
  }
  renderMembers();
}

function renderAddMenu() {
  const menu = $("#add-member-menu");
  menu.replaceChildren(...PRESETS.map((p) =>
    h("button", { onclick: () => { menu.hidden = true; addMember(p); } }, p.label, h("small", {}, p.hint))));
}

// ---------- rules ----------

function parseIds(text, key, inputEl) {
  const parts = text.split(/[\s,，]+/).filter(Boolean);
  const ok = parts.every((p) => /^-?\d+$/.test(p));
  inputEl.style.borderColor = ok ? "" : "var(--err)";
  if (ok) { invalid.delete(key); return parts.map(Number); }
  invalid.add(key);
  return null;
}

function renderRules() {
  const tg = draft.telegram;
  const select = $("#controller-select");
  select.replaceChildren(
    ...draft.participants.map((p) => h("option", { value: p.id, selected: tg.controller_participant === p.id }, `兼任：使用「${p.name || p.id}」的 bot`)),
    h("option", { value: "", selected: !tg.controller_participant }, "单独的总控 bot"),
  );
  select.onchange = (e) => { tg.controller_participant = e.target.value; changed(); renderRules(); };
  $("#controller-token-field").hidden = !!tg.controller_participant;
  $("#controller-token").replaceChildren(secretField("controller_token"));

  const users = $("#allowed-users");
  users.value = tg.allowed_user_ids.join(", ");
  users.oninput = (e) => { const v = parseIds(e.target.value, "users", users); if (v) tg.allowed_user_ids = v; changed(); };
  const chats = $("#allowed-chats");
  chats.value = tg.allowed_chat_ids.join(", ");
  chats.oninput = (e) => { const v = parseIds(e.target.value, "chats", chats); if (v) tg.allowed_chat_ids = v; changed(); };

  const r = draft.round;
  const turns = $("#max-turns");
  turns.value = r.max_turns_per_model;
  turns.oninput = (e) => { r.max_turns_per_model = Number(e.target.value); changed(); };
  const timeout = $("#call-timeout");
  timeout.value = r.call_timeout;
  timeout.oninput = (e) => { r.call_timeout = Number(e.target.value); changed(); };
  const pass = $("#pass-consumes");
  pass.checked = r.pass_consumes_turn;
  pass.onchange = (e) => { r.pass_consumes_turn = e.target.checked; changed(); };
  const slow = $("#slow-after");
  slow.value = r.slow_after;
  slow.oninput = (e) => { r.slow_after = Number(e.target.value); changed(); };
  const pchat = $("#proactive-chat");
  pchat.value = draft.proactive.chat_id || "";
  pchat.oninput = (e) => {
    const v = e.target.value.trim();
    const ok = v === "" || /^-?\d+$/.test(v);
    pchat.style.borderColor = ok ? "" : "var(--err)";
    if (!ok) { invalid.add("pchat"); return; }
    invalid.delete("pchat");
    draft.proactive.chat_id = v ? Number(v) : 0;
    changed();
  };
  const vision = $("#vision-member");
  const viewers = draft.participants.filter((p) => p.kind === "codex_cli");
  const pick = draft.vision.member;
  vision.replaceChildren(
    h("option", { value: "help", selected: pick === "help" }, "能看图的直接看，看不了的找朋友帮忙（推荐）"),
    h("option", { value: "auto", selected: pick === "auto" },
      viewers.length ? `由 ${viewers[0].name || viewers[0].id} 先看一遍，描述给所有人` : "先描述给所有人（需要一个 Codex 订阅成员）"),
    ...(viewers.length > 1 ? viewers : []).map((p) =>
      h("option", { value: p.id, selected: pick === p.id }, `由 ${p.name || p.id} 先看一遍，描述给所有人`)),
    h("option", { value: "off", selected: pick === "off" }, "关闭：模型只知道发了一张图"),
  );
  vision.onchange = (e) => { draft.vision.member = e.target.value; changed(); };
  $("#proactive-chat-current").textContent = server.status.proactive?.chat
    ? `现在会发到：群 ${server.status.proactive.chat}` : "现在还不知道发到哪个群（先在群里聊一句）";
  for (const [id, key] of [["#follow-up-chance", "follow_up_chance"], ["#follow-up-decay", "follow_up_decay"]]) {
    const input = $(id);
    input.value = Math.round(r[key] * 100);
    input.oninput = (e) => { r[key] = Math.min(Math.max(Number(e.target.value) || 0, 0), 100) / 100; changed(); };
  }
  const parallel = $("#max-parallel");
  parallel.value = r.max_parallel;
  parallel.oninput = (e) => { r.max_parallel = Number(e.target.value); changed(); };
}

// ---------- prompts ----------

function renderPrompts() {
  const scene = $("#prompt-scene");
  scene.value = draft.prompts.scene || "";
  scene.oninput = (e) => { draft.prompts.scene = e.target.value; changed(); schedulePreview(); };
  const noCoding = $("#prompt-no-coding");
  noCoding.checked = draft.prompts.no_coding;
  noCoding.onchange = (e) => { draft.prompts.no_coding = e.target.checked; changed(); schedulePreview(); };

  const def = server.meta.default_prompt;
  const area = $("#prompt-template");
  area.value = draft.prompts.system_template || def;
  area.oninput = (e) => {
    draft.prompts.system_template = e.target.value.trim() === def.trim() ? "" : e.target.value;
    changed();
    renderPreview();
  };
  $("#prompt-reset").onclick = () => {
    draft.prompts.system_template = "";
    area.value = def;
    changed();
    renderPreview();
  };
  $("#placeholders").replaceChildren(...Object.entries(server.meta.placeholders).flatMap(([k, d], i) =>
    [i ? "　" : "", h("code", {}, `{${k}}`), ` ${d}`]));

  const sel = $("#preview-member");
  const current = sel.value;
  sel.replaceChildren(...draft.participants.map((p) => h("option", { value: p.id, selected: p.id === current }, p.name || p.id)));
  sel.onchange = renderPreview;
  renderPreview();
}

// The preview is built by the server from the unsaved draft, so it is exactly what the
// model will get after saving.
let previewTimer = null;
function schedulePreview() {
  clearTimeout(previewTimer);
  previewTimer = setTimeout(renderPreview, 300);
}

async function renderPreview() {
  const out = $("#prompt-preview");
  const id = $("#preview-member").value;
  if (!id) {
    out.textContent = "（添加成员后可以预览）";
    return;
  }
  try {
    const r = await api("POST", "/api/preview", { settings: draft, member: id });
    out.textContent = r.prompt;
  } catch (e) {
    out.textContent = "预览失败：" + e.message;
  }
}

// ---------- expressions: stickers, kaomoji, who gets which ----------

const memberIds = () => draft.participants.map((p) => p.id);
const allows = (item, id) => !item.members.length || item.members.includes(id);

function whoTag(members) {
  if (!members.length) return h("span", { class: "who-tag" }, "所有人");
  const known = members.filter((id) => draft.participants.some((p) => p.id === id));
  if (!known.length) return h("span", { class: "who-tag none" }, "没有人能用");
  return h("span", { class: "who-tag" }, known.map(memberName).join("、"));
}

// The members list after switching one member on or off. Everyone ticked again means "everyone".
function toggledMembers(members, id, on) {
  const all = memberIds();
  let next = members.length ? [...members] : [...all];
  next = on ? [...new Set([...next, id])] : next.filter((x) => x !== id);
  return all.every((x) => next.includes(x)) ? [] : next.filter((x) => all.includes(x));
}

// Chips: 所有人, then each member. ``onChange`` gets the new list ([] = everyone).
function memberPicker(members, onChange) {
  const everyone = !members.length;
  return h("div", { class: "who" },
    h("label", {}, h("input", { type: "checkbox", checked: everyone, onchange: (e) => onChange(e.target.checked ? [] : memberIds()) }), "所有人"),
    draft.participants.map((p) => h("label", {},
      h("input", { type: "checkbox", checked: everyone || members.includes(p.id), disabled: everyone,
        onchange: (e) => onChange(toggledMembers(members, p.id, e.target.checked)) }),
      p.name || p.id)));
}

function showSubtab(name) {
  for (const b of document.querySelectorAll("#sticker-subtabs button")) b.classList.toggle("active", b.dataset.sub === name);
  for (const p of document.querySelectorAll("[data-subpanel]")) p.hidden = p.dataset.subpanel !== name;
  if (name === "by-member") renderByMember();
}

// ---- stickers ----

function renderStickerSettings() {
  const st = draft.stickers;
  const enabled = $("#stickers-enabled");
  enabled.checked = st.enabled;
  enabled.onchange = (e) => { st.enabled = e.target.checked; changed(); renderPreview(); };
  const max = $("#stickers-max");
  max.value = st.max_per_message;
  max.oninput = (e) => { st.max_per_message = Number(e.target.value); changed(); };
  const react = $("#reactions-enabled");
  react.checked = st.reactions;
  react.onchange = (e) => { st.reactions = e.target.checked; changed(); renderPreview(); };
  const kao = $("#kaomoji-enabled");
  kao.checked = st.kaomoji;
  kao.onchange = (e) => { st.kaomoji = e.target.checked; changed(); renderPreview(); };
  $("#sticker-exts").textContent = "支持 " + server.meta.sticker_exts.join(" ") + "，单个文件不超过 10 MB";
}

function stickerThumb(s) {
  const url = `/api/stickers/${s.id}/file?v=${encodeURIComponent(s.file)}`;
  if (s.ext === ".tgs") return h("span", {}, "TGS 动画贴纸\n（无法预览）");
  if (s.ext === ".webm" || s.ext === ".mp4") return h("video", { src: url, autoplay: true, loop: true, muted: true, playsinline: true });
  return h("img", { src: url, alt: s.label, loading: "lazy" });
}

function renderStickers() {
  const list = server.stickers || [];
  const filter = $("#sticker-filter").value.trim().toLowerCase();
  const shown = filter ? list.filter((s) => s.label.toLowerCase().includes(filter) || s.description.toLowerCase().includes(filter)) : list;
  const labels = new Set(list.filter((s) => s.enabled).map((s) => s.label));
  $("#sticker-count").textContent = list.length ? `${list.length} 张 · ${labels.size} 个名称可用` : "";
  $("#sub-count-stickers").textContent = list.length || "";
  const grid = $("#sticker-grid");
  if (!list.length) {
    grid.replaceChildren(h("div", { class: "empty", style: "grid-column:1/-1" }, "表情库还是空的，上传几张图片或导入一个贴纸包吧。"));
    return;
  }
  grid.replaceChildren(...shown.map((s) =>
    h("div", { class: "sticker" + (s.enabled ? "" : " off"), title: "点击编辑", onclick: () => openStickerDialog(s.id) },
      h("div", { class: "thumb" }, stickerThumb(s)),
      h("div", { class: "name" }, s.label),
      h("div", { class: "desc" }, s.description || (s.enabled ? "" : "已停用")),
      whoTag(s.members))));
}

function applyStickerResponse(r) {
  server.stickers = r.stickers;
  server.meta.sticker_prompt = r.sticker_prompt;
  renderStickers();
  renderByMember();
  renderPreview();
}

async function stickerUpdate(id, changes) {
  try {
    applyStickerResponse(await api("PUT", `/api/stickers/${id}`, changes));
    return true;
  } catch (e) {
    toast(e.message, true);
    renderStickers();
    renderByMember();
    return false;
  }
}

async function postFiles(method, url, files, fields = {}) {
  const form = new FormData();
  for (const [k, v] of Object.entries(fields)) form.append(k, v);
  for (const f of files) form.append("file", f, f.name);
  const res = await fetch(url, { method, body: form });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data.error || `上传失败（HTTP ${res.status}）`);
  return data;
}

async function uploadStickers(files) {
  if (!files.length) return;
  toast(`正在上传 ${files.length} 个文件…`);
  try {
    const data = await postFiles("POST", "/api/stickers", files, { label: $("#upload-label").value.trim() });
    applyStickerResponse(data);
    $("#upload-label").value = "";
    toast(`已添加 ${data.added} 个表情`);
  } catch (e) {
    toast(e.message, true);
  }
}

async function importStickerSet() {
  const name = $("#set-name").value.trim();
  if (!name) return;
  const btn = $("#import-set");
  btn.disabled = true;
  btn.textContent = "导入中…";
  try {
    const r = await api("POST", "/api/stickers/import", { set: name });
    applyStickerResponse(r);
    $("#set-name").value = "";
    toast(`已导入 ${r.added} 张贴纸`);
  } catch (e) {
    toast(e.message, true);
  } finally {
    btn.disabled = false;
    btn.textContent = "导入";
  }
}

// ---- sticker editor ----

let editing = null; // {id, members}

function openStickerDialog(id) {
  const s = server.stickers.find((s) => s.id === id);
  if (!s) return;
  editing = { id, members: [...s.members] };
  $("#sd-label").value = s.label;
  $("#sd-desc").value = s.description;
  $("#sd-enabled").checked = s.enabled;
  renderStickerDialog(s);
  $("#sticker-dialog").showModal();
}

function renderStickerDialog(s) {
  $("#sd-preview").replaceChildren(stickerThumb(s));
  $("#sd-kind").textContent = { sticker: "以贴纸发送", animation: "以动图发送", photo: "以图片发送" }[s.kind] + `（${s.ext}）`;
  const draw = () => $("#sd-members").replaceChildren(memberPicker(editing.members, (next) => { editing.members = next; draw(); }));
  draw();
}

function setupStickerDialog() {
  const dlg = $("#sticker-dialog");
  $("#sd-cancel").onclick = () => dlg.close();
  $("#sticker-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    const ok = await stickerUpdate(editing.id, {
      label: $("#sd-label").value, description: $("#sd-desc").value,
      enabled: $("#sd-enabled").checked, members: editing.members,
    });
    if (ok) { dlg.close(); toast("已保存"); }
  });
  $("#sd-delete").onclick = async () => {
    const s = server.stickers.find((s) => s.id === editing.id);
    if (!s || !confirm(`删除表情「${s.label}」？`)) return;
    try {
      applyStickerResponse(await api("DELETE", `/api/stickers/${s.id}`));
      dlg.close();
    } catch (err) {
      toast(err.message, true);
    }
  };
  const file = $("#sd-file");
  $("#sd-replace").onclick = () => file.click();
  file.onchange = async () => {
    const f = file.files[0];
    file.value = "";
    if (!f) return;
    try {
      applyStickerResponse(await postFiles("PUT", `/api/stickers/${editing.id}/file`, [f]));
      renderStickerDialog(server.stickers.find((s) => s.id === editing.id));
      toast("图片已替换");
    } catch (err) {
      toast(err.message, true);
    }
  };
}

// ---- kaomoji ----

let kaoNewMembers = [];

function renderKaomoji() {
  const list = server.kaomoji || [];
  $("#sub-count-kaomoji").textContent = list.length || "";
  const drawNew = () => $("#kao-members").replaceChildren(memberPicker(kaoNewMembers, (next) => { kaoNewMembers = next; drawNew(); }));
  drawNew();

  const sel = $("#kao-filter");
  const current = sel.value;
  sel.replaceChildren(h("option", { value: "" }, "全部"),
    ...draft.participants.map((p) => h("option", { value: p.id, selected: p.id === current }, `${p.name || p.id} 能用的`)));
  const shown = sel.value ? list.filter((k) => allows(k, sel.value)) : list;

  const box = $("#kao-list");
  if (!list.length) {
    box.replaceChildren(h("div", { class: "empty" }, "还没有颜文字。可以先点「添加一组推荐的」，再按成员分配。"));
    return;
  }
  box.replaceChildren(...shown.map((k) => {
    const face = h("input", { class: "face", value: k.text, title: "颜文字" });
    const mood = h("input", { value: k.mood, placeholder: "什么时候用（可选）" });
    const commit = () => {
      if (face.value.trim() !== k.text || mood.value.trim() !== k.mood) kaoUpdate(k.id, { text: face.value, mood: mood.value });
    };
    for (const el of [face, mood]) {
      el.addEventListener("change", commit);
      el.addEventListener("keydown", (e) => e.key === "Enter" && el.blur());
    }
    return h("div", { class: "kao-row" + (k.enabled ? "" : " off") },
      face, mood,
      memberPicker(k.members, (next) => kaoUpdate(k.id, { members: next })),
      h("label", {}, h("input", { type: "checkbox", checked: k.enabled, onchange: (e) => kaoUpdate(k.id, { enabled: e.target.checked }) }), "启用"),
      h("button", { class: "danger", onclick: () => kaoDelete(k) }, "删除"));
  }));
}

function applyKaomojiResponse(r) {
  server.kaomoji = r.kaomoji;
  renderKaomoji();
  renderByMember();
  renderPreview();
}

async function kaoUpdate(id, changes) {
  try {
    applyKaomojiResponse(await api("PUT", `/api/kaomoji/${id}`, changes));
  } catch (e) {
    toast(e.message, true);
    renderKaomoji();
    renderByMember();
  }
}

async function kaoDelete(k) {
  if (!confirm(`删除颜文字 ${k.text}？`)) return;
  try {
    applyKaomojiResponse(await api("DELETE", `/api/kaomoji/${k.id}`));
  } catch (e) {
    toast(e.message, true);
  }
}

async function kaoAdd(body, after) {
  try {
    const r = await api("POST", "/api/kaomoji", body);
    applyKaomojiResponse(r);
    if (after) after();
    toast(r.added ? `已添加 ${r.added} 个颜文字` : "都已经在列表里了");
  } catch (e) {
    toast(e.message, true);
  }
}

// ---- by member ----

async function renderByMember() {
  const sel = $("#bm-member");
  if (!sel || $("[data-subpanel='by-member']").hidden) return;
  const current = sel.value;
  sel.replaceChildren(...draft.participants.map((p) => h("option", { value: p.id, selected: p.id === current }, p.name || p.id)));
  const id = sel.value;
  if (!id) {
    $("#bm-summary").textContent = "还没有成员";
    return;
  }
  const stickers = server.stickers || [];
  const kaomoji = server.kaomoji || [];
  const nS = stickers.filter((s) => s.enabled && allows(s, id)).length;
  const nK = kaomoji.filter((k) => k.enabled && allows(k, id)).length;
  $("#bm-summary").textContent = `${memberName(id)} 能用 ${nS} 张表情包、${nK} 个颜文字`;

  $("#bm-stickers").replaceChildren(...(stickers.length ? stickers.map((s) => {
    const on = allows(s, id);
    return h("div", { class: "sticker " + (on ? "picked" : "unpicked") + (s.enabled ? "" : " off"),
      title: on ? "点击取消" : "点击分给它", onclick: () => stickerUpdate(s.id, { members: toggledMembers(s.members, id, !on) }) },
      h("div", { class: "thumb" }, stickerThumb(s)),
      h("div", { class: "name" }, s.label),
      h("div", { class: "tick" }, on ? "✓ 能用" : "不能用", s.enabled ? "" : "（已停用）"));
  }) : [h("div", { class: "empty", style: "grid-column:1/-1" }, "表情库是空的")]));

  $("#bm-kaomoji").replaceChildren(...(kaomoji.length ? kaomoji.map((k) => h("label", { title: k.enabled ? "" : "已停用" },
    h("input", { type: "checkbox", checked: allows(k, id), onchange: (e) => kaoUpdate(k.id, { members: toggledMembers(k.members, id, e.target.checked) }) }),
    h("span", { class: "face" }, k.text),
    k.mood ? h("span", { class: "mood" }, k.mood) : null)) : [h("div", { class: "empty" }, "还没有颜文字")]));

  const out = $("#bm-preview");
  try {
    const r = await api("POST", "/api/preview", { settings: draft, member: id });
    const parts = r.prompt.split(/\n{2,}/).filter((p) => p.startsWith("表情包：") || p.startsWith("颜文字："));
    out.textContent = parts.join("\n\n") || "（它现在没有可用的表情包和颜文字，提示词里不会提到）";
  } catch (e) {
    out.textContent = "预览失败：" + e.message;
  }
}

function setupStickerInputs() {
  const input = $("#file-input");
  const zone = $("#dropzone");
  $("#pick-files").onclick = () => input.click();
  input.onchange = () => { uploadStickers([...input.files]); input.value = ""; };
  zone.addEventListener("dragover", (e) => { e.preventDefault(); zone.classList.add("over"); });
  zone.addEventListener("dragleave", () => zone.classList.remove("over"));
  zone.addEventListener("drop", (e) => {
    e.preventDefault();
    zone.classList.remove("over");
    uploadStickers([...e.dataTransfer.files]);
  });
  $("#import-set").onclick = importStickerSet;
  $("#sticker-filter").oninput = renderStickers;
  for (const b of document.querySelectorAll("#sticker-subtabs button")) b.onclick = () => showSubtab(b.dataset.sub);
  setupStickerDialog();

  $("#kao-add").onclick = () => kaoAdd({ text: $("#kao-text").value, mood: $("#kao-mood").value, members: kaoNewMembers }, () => {
    $("#kao-text").value = "";
    $("#kao-mood").value = "";
  });
  $("#kao-text").addEventListener("keydown", (e) => e.key === "Enter" && $("#kao-add").click());
  $("#kao-add-lines").onclick = () => kaoAdd({ lines: $("#kao-lines").value }, () => ($("#kao-lines").value = ""));
  $("#kao-starter").onclick = () => kaoAdd({ starter: true });
  $("#kao-filter").onchange = renderKaomoji;
  $("#bm-member").onchange = renderByMember;
}

// ---------- memory ----------

function memberName(id) {
  if (id === "all") return "全员";
  const p = draft.participants.find((p) => p.id === id);
  return p ? p.name || p.id : id;
}

function scopeSelect(value, onchange) {
  return h("select", { onchange },
    h("option", { value: "all", selected: value === "all" }, "全员"),
    ...draft.participants.map((p) => h("option", { value: p.id, selected: value === p.id }, "只给 " + (p.name || p.id))));
}

function factRow(f) {
  const text = h("input", { value: f.text });
  text.addEventListener("change", () => text.value.trim() !== f.text && memoryCall("PUT", `/api/memory/${f.id}`, { text: text.value }));
  text.addEventListener("keydown", (e) => e.key === "Enter" && text.blur());
  if (f.status === "pending") {
    return h("div", { class: "fact" },
      text,
      h("span", { class: "src" }, `${memberName(f.source)} 提议`),
      h("button", { class: "primary", onclick: () => memoryCall("PUT", `/api/memory/${f.id}`, { text: text.value, status: "active" }) }, "记住"),
      h("button", { onclick: () => memoryCall("DELETE", `/api/memory/${f.id}`) }, "忽略"));
  }
  return h("div", { class: "fact" + (f.enabled ? "" : " off") },
    h("input", { type: "checkbox", checked: f.enabled, title: "启用", onchange: (e) => memoryCall("PUT", `/api/memory/${f.id}`, { enabled: e.target.checked }) }),
    text,
    scopeSelect(f.scope, (e) => memoryCall("PUT", `/api/memory/${f.id}`, { scope: e.target.value })),
    f.source !== "manual" ? h("span", { class: "src" }, `来自 ${memberName(f.source)}`) : null,
    h("button", { class: "danger", onclick: () => confirm("删除这条记忆？") && memoryCall("DELETE", `/api/memory/${f.id}`) }, "删除"));
}

function renderMemory() {
  const learn = $("#memory-learn");
  learn.value = draft.memory.learn;
  learn.onchange = (e) => { draft.memory.learn = e.target.value; changed(); renderPreview(); };

  const scope = $("#memory-scope");
  const keep = scope.value || "all";
  scope.replaceChildren(...scopeSelect(keep, null).children);
  scope.value = keep;

  const items = server.memory || [];
  const pending = items.filter((f) => f.status === "pending");
  $("#memory-pending").replaceChildren(...(pending.length ? [h("div", { class: "pending-box" },
    h("div", { class: "small", style: "margin-bottom:4px" }, `模型提议记住 ${pending.length} 条，确认后才会生效：`),
    ...pending.map(factRow))] : []));
  const active = items.filter((f) => f.status === "active");
  $("#memory-list").replaceChildren(...(active.length ? active.map(factRow) :
    [h("div", { class: "muted small" }, "还没有长期记忆。")]));
}

function renderMemes() {
  const list = server.memes || [];
  $("#meme-count").textContent = list.length ? `${list.length} 条` : "";
  $("#meme-list").replaceChildren(...(list.length ? [...list].reverse().map((m) => {
    const text = h("input", { value: m.text });
    text.addEventListener("change", () => text.value.trim() !== m.text && memeCall("PUT", `/api/memes/${m.id}`, { text: text.value }));
    text.addEventListener("keydown", (e) => e.key === "Enter" && text.blur());
    return h("div", { class: "fact" + (m.enabled ? "" : " off") },
      h("input", { type: "checkbox", checked: m.enabled, title: "启用", onchange: (e) => memeCall("PUT", `/api/memes/${m.id}`, { enabled: e.target.checked }) }),
      text,
      m.url ? h("a", { class: "src", href: m.url, target: "_blank", rel: "noreferrer" }, "来源") : null,
      h("span", { class: "src" }, m.added_by ? `${m.added_by} 记的` : ""),
      h("button", { class: "danger", onclick: () => confirm("删除这条梗？") && memeCall("DELETE", `/api/memes/${m.id}`) }, "删除"));
  }) : [h("div", { class: "muted small" }, "梗库还是空的。")]));
}

async function memeCall(method, path, body) {
  try {
    server.memes = (await api(method, path, body)).memes;
    renderMemes();
    renderPreview();
    return true;
  } catch (e) {
    toast(e.message, true);
    renderMemes();
    return false;
  }
}

async function addMeme() {
  const text = $("#meme-text");
  if (!text.value.trim()) return;
  if (await memeCall("POST", "/api/memes", { text: text.value, url: $("#meme-url").value })) {
    text.value = "";
    $("#meme-url").value = "";
  }
}

async function memoryCall(method, path, body) {
  try {
    const r = await api(method, path, body);
    server.memory = r.memory;
    renderMemory();
    renderStatus(r.status);
    renderPreview();
    return true;
  } catch (e) {
    toast(e.message, true);
    renderMemory();
    return false;
  }
}

async function addMemory() {
  const input = $("#memory-text");
  if (!input.value.trim()) return;
  if (await memoryCall("POST", "/api/memory", { text: input.value, scope: $("#memory-scope").value })) input.value = "";
}

// ---------- journal ----------

let journalData = null;

async function loadJournal() {
  try {
    journalData = await api("GET", "/api/journal");
    renderJournal();
  } catch (e) {
    toast(e.message, true);
  }
}

function renderJournal() {
  const box = $("#journal");
  if (!journalData) return;
  const fmt = (ts) => {
    const d = new Date(ts * 1000);
    return `${d.getMonth() + 1}-${String(d.getDate()).padStart(2, "0")} ${d.getHours()}:${String(d.getMinutes()).padStart(2, "0")}`;
  };
  const members = journalData.members.filter((m) => m.entries.length || draft.participants.find((p) => p.id === m.id)?.browse);
  if (!members.length) {
    box.replaceChildren(h("div", { class: "card empty" }, "还没有见闻。在「成员」里为角色打开「日常浏览」，或者点「现在去刷一次」。"));
    return;
  }
  box.replaceChildren(...members.map((m) => h("div", { class: "card" },
    h("div", { class: "row-between" },
      h("h2", {}, m.name, " ", h("span", { class: "muted small" }, `${m.entries.length} 条`))),
    ...(m.entries.length ? m.entries.map((e) => h("div", { class: "entry" },
      h("div", { class: "entry-head" },
        h("span", { class: "stars", title: `印象 ${e.interest}/5` }, "★".repeat(e.interest) + "☆".repeat(5 - e.interest)),
        h("b", {}, e.title),
        e.shared_ts ? h("span", { class: "pill ok" }, "已分享") : null,
        h("span", { class: "spacer" }),
        h("button", { class: "danger", onclick: () => deleteEntry(e) }, "删除")),
      h("div", { class: "entry-body" }, e.summary),
      e.take ? h("div", { class: "entry-take" }, "它的看法：", e.take) : null,
      h("div", { class: "entry-meta" },
        `${fmt(e.ts)} · ${e.source === "x" ? "X" : "网页"} · ${fmt(e.expires_at)} 忘掉 · `,
        h("a", { href: e.url, target: "_blank", rel: "noopener noreferrer" }, e.url)))) :
      [h("div", { class: "muted small" }, "还没有见闻。")]))));
}

async function deleteEntry(e) {
  if (!confirm(`删除这条见闻？\n${e.title}`)) return;
  try {
    journalData = await api("DELETE", `/api/journal/${e.id}`);
    renderJournal();
  } catch (err) {
    toast(err.message, true);
  }
}

// ---------- chats ----------

let chats = [];
let currentChat = "";

const fmtTime = (ts) => {
  if (!ts) return "";
  const d = new Date(ts * 1000);
  const pad = (n) => String(n).padStart(2, "0");
  return `${d.getMonth() + 1}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}`;
};

async function loadChats() {
  try {
    chats = (await api("GET", "/api/chats")).chats;
  } catch (e) {
    toast(e.message, true);
    return;
  }
  const sel = $("#chat-select");
  if (!chats.some((c) => c.key === currentChat)) currentChat = chats[0]?.key || "";
  sel.replaceChildren(...chats.map((c) => h("option", { value: c.key, selected: c.key === currentChat }, `群 ${c.key}（${c.message_count} 条）`)));
  sel.hidden = !chats.length;
  sel.onchange = (e) => { currentChat = e.target.value; loadChat(); };
  if (currentChat) loadChat();
  else $("#chat-detail").replaceChildren(h("div", { class: "muted" }, "还没有群聊记录。"));
}

async function loadChat() {
  try {
    renderChat(await api("GET", `/api/chats/${encodeURIComponent(currentChat)}`));
  } catch (e) {
    toast(e.message, true);
  }
}

async function chatCall(method, path, confirmText) {
  if (confirmText && !confirm(confirmText)) return;
  try {
    renderChat(await api(method, path));
    toast("已完成");
  } catch (e) {
    toast(e.message, true);
  }
}

function renderChat(d) {
  const base = `/api/chats/${encodeURIComponent(d.key)}`;
  const transcript = h("div", { class: "transcript" },
    ...(d.messages.length ? d.messages.map((m) =>
      h("div", { class: "line" },
        h("span", { class: "time" }, fmtTime(m.ts)),
        h("span", { class: "who" + (m.speaker_id === "user" ? " user" : "") }, m.speaker_name + ":"),
        m.text)) : [h("div", { class: "muted" }, "这个群还没有记录（重置后会清空）。")]));

  const contexts = d.members.map((m) => {
    const isApi = Array.isArray(m.history);
    const summary = isApi ? `本程序保存的上下文：${m.history.length} 条` :
      m.session_id ? `CLI 会话 ${m.session_id.slice(0, 8)}…` : "还没有会话";
    const list = isApi && m.history.length ? h("details", {},
      h("summary", {}, "查看上下文"),
      h("div", { class: "ctx-list" }, ...m.history.map((item, i) =>
        h("div", { class: "ctx-item" },
          h("span", { class: "role" }, item.role === "user" ? "发给它" : "它的回复"),
          h("span", { class: "content" }, item.content),
          h("button", { class: "danger", onclick: () => chatCall("DELETE", `${base}/members/${m.id}/history/${i}`) }, "删除"))))) : null;
    return h("div", { class: "ctx" },
      h("div", { class: "ctx-head" },
        h("b", {}, m.name),
        h("span", { class: "muted small" }, summary),
        h("span", { class: "spacer" }),
        h("button", {
          onclick: () => chatCall("POST", `${base}/members/${m.id}/reset`,
            `清空 ${m.name} 在这个群的上下文？它下次发言时会从最近的群聊记录重新了解情况。`),
        }, "清空它的上下文")),
      list);
  });

  $("#chat-detail").replaceChildren(
    transcript,
    h("div", { class: "row-between" },
      h("h2", {}, "各成员的上下文"),
      h("button", { class: "danger", onclick: () => chatCall("POST", `${base}/reset`, "清空这个群的群聊记录和所有成员的上下文？长期记忆不受影响。") }, "清空这个群的全部记录")),
    h("p", { class: "muted small" }, "订阅 CLI 的上下文保存在各 CLI 自己的会话里，只能整体清空；API 成员的上下文可以逐条删除。"),
    ...contexts);
}

// ---------- status ----------

function renderStatus(s) {
  server.status = s;
  const tg = s.telegram;
  const pill = $("#tg-pill");
  const running = tg.state === "运行中";
  pill.textContent = running ? "Telegram 运行中" : tg.state.startsWith("未配置") ? "Telegram 未配置" : "Telegram 异常";
  pill.className = "pill " + (running ? "ok" : tg.state.startsWith("未配置") ? "warn" : "err");
  const badge = $("#memory-badge");
  badge.hidden = !s.pending_memory;
  badge.textContent = s.pending_memory || "";

  $("#notices").replaceChildren(...(s.notices || []).map((n) => h("div", { class: "notice" }, n)));

  $("#status-telegram").replaceChildren(h("div", { class: "kv" },
    h("div", {}, "状态"), h("div", {}, tg.state),
    h("div", {}, "总控 bot"), h("div", {}, tg.controller ? "@" + tg.controller : "—"),
    h("div", {}, "数据目录"), h("div", { style: "word-break:break-all" }, server.meta.data_dir),
    tg.privacy_warning ? h("div", {}, "注意") : null,
    tg.privacy_warning ? h("div", { style: "color:var(--warn)" }, "总控 bot 开着 privacy mode，收不到群里的普通消息。请在 @BotFather 执行 /setprivacy → Disable，再把它移出群重新拉入。") : null));

  const members = s.members || [];
  $("#status-members").replaceChildren(...(members.length ? members.map((m) =>
    h("div", { class: "status-member" },
      h("span", { class: "dot " + m.state }),
      h("b", {}, m.name),
      m.bot_username ? h("span", { class: "muted small" }, "@" + m.bot_username) : null,
      h("span", { class: "msg" }, m.state === "disabled" ? "已停用" : m.message))) : [h("div", { class: "muted" }, "还没有成员")]));

  const rooms = s.rooms || [];
  $("#status-rooms").replaceChildren(...(rooms.length ? rooms.map((r) =>
    h("div", { class: "room" },
      h("div", { class: "row-between" },
        h("div", {}, h("b", {}, `群 ${r.key}`), " ",
          h("span", { class: "muted small" }, `${r.busy ? "讨论中" : "空闲"} · 记忆中 ${r.message_count} 条消息`)),
        h("div", {},
          h("button", { disabled: !r.busy, onclick: () => roomAction(r.key, "stop") }, "中止本轮"), " ",
          h("button", { class: "danger", onclick: () => roomAction(r.key, "reset") }, "清空记忆"))),
      h("ul", { class: "room-msgs" }, ...r.recent.map((m) => h("li", {}, h("b", {}, m.speaker + ":"), m.text))))) :
    [h("div", { class: "muted" }, "重启后还没有群里的消息。")]));

  const logs = $("#status-logs");
  const atBottom = logs.scrollHeight - logs.scrollTop - logs.clientHeight < 30;
  logs.textContent = (s.logs || []).join("\n");
  if (atBottom) logs.scrollTop = logs.scrollHeight;
}

async function roomAction(key, action) {
  if (action === "reset" && !confirm("清空这个群里所有模型的记忆？")) return;
  try {
    renderStatus(await api("POST", `/api/rooms/${encodeURIComponent(key)}/${action}`));
  } catch (e) {
    toast(e.message, true);
  }
}

async function pollStatus() {
  try {
    renderStatus(await api("GET", "/api/status"));
  } catch { /* server restarting; try again next tick */ }
}

// ---------- boot ----------

function showTab(name) {
  if (!document.getElementById("tab-" + name)) name = "members";
  for (const b of document.querySelectorAll(".tabs button")) b.classList.toggle("active", b.dataset.tab === name);
  for (const p of document.querySelectorAll(".tab-panel")) p.classList.toggle("active", p.id === "tab-" + name);
  history.replaceState(null, "", "#" + name);
  window.scrollTo(0, 0);
  if (name === "memory" && server) loadChats();
  if (name === "journal" && server) loadJournal();
}

document.querySelectorAll(".tabs button").forEach((b) => (b.onclick = () => showTab(b.dataset.tab)));
$("#save-btn").onclick = save;
$("#discard-btn").onclick = () => resetDraft();
$("#add-member-btn").onclick = (e) => {
  e.stopPropagation();
  const menu = $("#add-member-menu");
  menu.hidden = !menu.hidden;
};
document.addEventListener("click", (e) => {
  const menu = $("#add-member-menu");
  if (!menu.hidden && !menu.contains(e.target)) menu.hidden = true;
});
window.addEventListener("beforeunload", (e) => {
  if (draft && (settingsDirty() || hasPendingSecrets())) e.preventDefault();
});

renderAddMenu();
setupStickerInputs();
$("#memory-add").onclick = addMemory;
$("#meme-add").onclick = addMeme;
$("#meme-text").addEventListener("keydown", (e) => e.key === "Enter" && addMeme());
$("#memory-text").addEventListener("keydown", (e) => e.key === "Enter" && addMemory());
showTab(location.hash.slice(1) || "members");
load()
  .then(() => {
    if (location.hash === "#memory") loadChats();
    if (location.hash === "#journal") loadJournal();
  })
  .catch((e) => toast(e.message, true));
setInterval(pollStatus, 4000);
