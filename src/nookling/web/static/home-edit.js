// Arranging the house (docs/furniture.md, 2d): the side panel while 「布置家具」 is on — the selected
// piece, adding furniture, the owner's own kinds of furniture, and rooms. Every change goes to the
// server, which checks it ("放不下", "挡住门了"); the house then redraws from what was saved.

const el = (tag, cls, text) => {
  const node = document.createElement(tag);
  if (cls) node.className = cls;
  if (text !== undefined) node.textContent = text;
  return node;
};

export async function send(method, path, body, raw = false) {
  const res = await fetch(path, {
    method,
    headers: body === undefined ? {} : { "Content-Type": raw ? "application/octet-stream" : "application/json" },
    body: body === undefined ? undefined : raw ? body : JSON.stringify(body),
  });
  let data = {};
  try { data = await res.json(); } catch { /* empty */ }
  if (!res.ok) throw new Error(data.error || `请求失败（HTTP ${res.status}）`);
  return data;
}

export function say(text, isError = false) {
  if (typeof window.toast === "function") window.toast(text, isError);
  else if (isError) alert(text);
}

// Every function name the house knows: built-in ones, and any used on furniture or by tools.
function functionNames(d) {
  const names = { ...d.function_names };
  for (const f of d.available || []) names[f] ||= f.startsWith("tool.") ? `还没拆箱的工具（${f.slice(5)}）` : f;
  for (const p of d.pieces) for (const f of p.functions || []) names[f] ||= f;
  for (const t of Object.values(d.types)) for (const f of t.functions || []) names[f] ||= f;
  return names;
}

function checkboxes(names, chosen, onChange) {
  const box = el("div", "home-fns");
  for (const [f, label] of Object.entries(names)) {
    if (f.startsWith("tool.")) continue; // an unlinked tool belongs to its box only
    const input = el("input");
    input.type = "checkbox";
    input.checked = chosen.includes(f);
    input.onchange = () => onChange(input.checked ? [...chosen, f] : chosen.filter((x) => x !== f));
    const row = el("label", "home-fn");
    row.append(input, el("span", "", label), el("code", "", f));
    box.append(row);
  }
  return box;
}

function section(title, ...children) {
  const s = el("div", "home-edit-section");
  s.append(el("h3", "", title), ...children.filter(Boolean));
  return s;
}

export function renderEditor(app) {
  const d = app.data;
  const rooms = Object.fromEntries(d.rooms.map((r) => [r.id, r]));
  const out = [el("h2", "", "布置"), el("p", "muted small", "拖动家具换位置，点一下选中它。放不下、挡门、叠在一起的会被退回原处。")];
  const act = async (fn, done) => {
    try { await fn(); if (done) say(done); } catch (e) { say(e.message, true); }
    await app.poll();
  };

  // ---- the selected piece ----
  const piece = d.pieces.find((p) => p.id === app.selectedPiece);
  if (piece) {
    const type = d.types[piece.type] || {};
    const name = el("input");
    name.value = piece.name || "";
    name.placeholder = type.name || "";
    name.maxLength = 16;
    name.onchange = () => act(() => send("PUT", `/api/home/pieces/${piece.id}`, { name: name.value }));
    const color = el("input");
    color.type = "color";
    color.value = piece.color || type.color || "#888888";
    color.onchange = () => act(() => send("PUT", `/api/home/pieces/${piece.id}`, { color: color.value }));
    const turn = el("button", "", "转 90°");
    turn.onclick = () => act(() => send("PUT", `/api/home/pieces/${piece.id}`, { turn: (piece.turn + 90) % 360 }));
    const remove = el("button", "danger", "删掉");
    remove.onclick = () => act(async () => { await send("DELETE", `/api/home/pieces/${piece.id}`); app.selectedPiece = ""; });
    const row = el("div", "home-look");
    row.append(color, turn, remove);
    let pic = null; // a frame, poster or rug can show a picture
    if (["frame", "poster", "rug"].includes(type.template)) {
      pic = el("div", "home-look");
      const file = pickImage(async (name) => act(() => send("PUT", `/api/home/pieces/${piece.id}`, { image: name })));
      pic.append(el("span", "muted small", piece.image ? "有图片" : "放一张图片"), file);
      if (piece.image) {
        const off = el("button", "", "去掉图片");
        off.onclick = () => act(() => send("PUT", `/api/home/pieces/${piece.id}`, { image: null }));
        pic.append(off);
      }
    }
    const fns = piece.type === "base/parcel"
      ? el("p", "muted small", "纸箱的功能跟着它的工具走：到「工具」页面给工具选一个功能，纸箱就拆开了。")
      : checkboxes(functionNames(d), piece.functions || [],
        (next) => act(() => send("PUT", `/api/home/pieces/${piece.id}`, { functions: next })));
    let apps = null; // the computer: its chat apps' settings
    if ((piece.functions || []).includes("chat.telegram")) {
      apps = el("button", "", "Telegram 设置");
      apps.onclick = () => openTelegram(app);
    }
    out.push(section(`选中：${piece.name || type.name}`, apps, el("p", "muted small", `${type.name} · ${rooms[piece.room]?.name || piece.room}`),
      name, row, pic, el("p", "muted small", "挂的功能（成员用这些功能时会走到这件家具跟前）："), fns));
  }

  // ---- adding a piece ----
  const kind = el("select");
  const groups = { 内置: [], 我做的: [] };
  for (const t of Object.values(d.types)) if (t.id !== "base/parcel") groups[t.id.startsWith("mine/") ? "我做的" : "内置"].push(t);
  for (const [label, list] of Object.entries(groups)) {
    if (!list.length) continue;
    const g = el("optgroup");
    g.label = label;
    for (const t of list) { const o = el("option", "", t.name); o.value = t.id; g.append(o); }
    kind.append(g);
  }
  if (app.addKind) kind.value = app.addKind;
  kind.onchange = () => (app.addKind = kind.value);
  const into = el("select");
  for (const r of d.rooms) { const o = el("option", "", r.name); o.value = r.id; into.append(o); }
  if (app.addRoom) into.value = app.addRoom;
  into.onchange = () => (app.addRoom = into.value);
  const add = el("button", "", "放进去");
  add.onclick = () => act(async () => { app.selectedPiece = (await send("POST", "/api/home/pieces", { type: kind.value, room: into.value })).id; });
  const addRow = el("div", "home-look");
  addRow.append(kind, into, add);
  out.push(section("添加家具", addRow));

  // ---- the owner's own kinds ----
  const mine = Object.values(d.types).filter((t) => t.id.startsWith("mine/"));
  const list = el("div", "home-kinds");
  for (const t of mine) {
    const row = el("div", "home-kind");
    const edit = el("button", "", "改");
    edit.onclick = () => openKind(app, t);
    const del = el("button", "danger", "删");
    del.onclick = () => act(() => send("DELETE", `/api/home/types/${t.id}`));
    row.append(el("span", "", `${t.name}${t.model ? " · 模型" : ""}`), edit, del);
    list.append(row);
  }
  if (!mine.length) list.append(el("p", "muted small", "还没有自己做的家具。"));
  const make = el("button", "", "新做一种");
  make.onclick = () => openKind(app, null);
  const copyOf = el("select");
  for (const t of Object.values(d.types)) if (t.id.startsWith("base/") && t.id !== "base/parcel") {
    const o = el("option", "", t.name); o.value = t.id; copyOf.append(o);
  }
  const copy = el("button", "", "复制一份");
  copy.onclick = () => act(async () => openKind(app, await send("POST", "/api/home/types", { copy_of: copyOf.value })));
  const kindRow = el("div", "home-look");
  kindRow.append(make, copyOf, copy);
  out.push(section("我做的家具", list, kindRow, el("p", "muted small", "内置的家具不能直接改：复制一份，改成自己的。")));

  // ---- rooms ----
  const roomList = el("div", "home-kinds");
  for (const r of d.rooms) {
    const row = el("div", "home-kind");
    const n = el("input");
    n.value = r.name;
    n.maxLength = 8;
    n.onchange = () => act(() => send("PUT", `/api/home/rooms/${r.id}`, { name: n.value }));
    const dress = el("button", "", "外观");
    dress.onclick = () => openRoomLook(app, r);
    row.append(n, dress);
    if (!r.id.startsWith("bed-") && r.id !== "living") {
      const del = el("button", "danger", "删");
      del.onclick = () => act(() => send("DELETE", `/api/home/rooms/${r.id}`));
      row.append(del);
    }
    roomList.append(row);
  }
  const roomName = el("input");
  roomName.placeholder = "新房间的名字";
  roomName.maxLength = 8;
  const size = el("select");
  for (const [k, label] of [["small", "小"], ["medium", "中"], ["large", "大"]]) {
    const o = el("option", "", `${label}（${d.room_sizes[k][0] / 2}×${d.room_sizes[k][1] / 2}）`); o.value = k; size.append(o);
  }
  const addRoom = el("button", "", "加一间");
  addRoom.onclick = () => act(() => send("POST", "/api/home/rooms", { name: roomName.value, size: size.value }));
  const roomRow = el("div", "home-look");
  roomRow.append(roomName, size, addRoom);
  out.push(section("房间", roomList, roomRow, el("p", "muted small", "只能删空的、不是卧室的房间；客厅有大门，不能删。")));
  return out;
}

// ---- the computer's Telegram: who may talk, which bots, the controller ----
// Tokens are never shown: a field left empty keeps what is saved, typing replaces it.

export async function openTelegram(app) {
  let st;
  try { st = await send("GET", "/api/state"); } catch (e) { say(e.message, true); return; }
  const tg = JSON.parse(JSON.stringify(st.settings.apps.telegram));
  const members = st.settings.participants;
  const status = st.status.telegram;
  const secrets = { bots: {} };
  const body = el("div", "home-kind-editor");
  const field = (label, input, hint) => {
    const f = el("label", "home-field");
    f.append(el("span", "muted small", label), input);
    if (hint) f.append(el("span", "muted small", hint));
    return f;
  };
  const tokenInput = (saved, set) => {
    const i = el("input");
    i.type = "password";
    i.autocomplete = "off";
    i.placeholder = saved ? `已设置（${saved}），留空不改` : "没设置";
    i.oninput = () => set(i.value.trim());
    return i;
  };
  const controller = el("select");
  for (const m of members) { const o = el("option", "", `兼任：用「${m.name || m.id}」的 bot`); o.value = m.id; o.selected = tg.controller_participant === m.id; controller.append(o); }
  const own = el("option", "", "单独的总控 bot");
  own.value = "";
  own.selected = !tg.controller_participant;
  controller.append(own);
  const controllerToken = field("总控 bot 的 token", tokenInput(st.secrets.controller_token, (v) => (secrets.controller_token = v)));
  controllerToken.hidden = !!tg.controller_participant;
  controller.onchange = () => { tg.controller_participant = controller.value; controllerToken.hidden = !!controller.value; };
  const ids = (value) => value.split(/[\s,，]+/).filter(Boolean).map(Number);
  const users = el("input");
  users.value = tg.allowed_user_ids.join(", ");
  const chats = el("input");
  chats.value = tg.allowed_chat_ids.join(", ");
  const bots = el("div", "home-edit-section");
  bots.append(el("h3", "", "每个成员自己的 bot"));
  for (const m of members) bots.append(field(m.name || m.id, tokenInput(st.secrets.bots[m.id], (v) => { if (v) secrets.bots[m.id] = v; else delete secrets.bots[m.id]; })));
  const save = el("button", "primary", "保存");
  save.onclick = async () => {
    try {
      const nextUsers = ids(users.value), nextChats = ids(chats.value);
      if ([...nextUsers, ...nextChats].some((n) => !Number.isInteger(n))) throw new Error("ID 要写成数字，用逗号隔开");
      const next = { ...tg, allowed_user_ids: nextUsers, allowed_chat_ids: nextChats };
      if (JSON.stringify(next) !== JSON.stringify(st.settings.apps.telegram)) {
        await send("PUT", "/api/settings", { ...st.settings, apps: { ...st.settings.apps, telegram: next } });
      }
      const changes = {};
      if (secrets.controller_token) changes.controller_token = secrets.controller_token;
      if (Object.keys(secrets.bots).length) changes.bots = secrets.bots;
      if (Object.keys(changes).length) await send("PUT", "/api/secrets", changes);
      app.modal.hidden = true;
      say("Telegram 设置已保存");
    } catch (e) { say(e.message, true); }
  };
  const warn = [status.privacy_warning ? "总控 bot 开着 privacy mode，收不到群里的普通消息。" : "",
                status.reaction_warning ? "总控 bot 不是群管理员，收不到点赞和踩。" : ""].filter(Boolean).join(" ");
  body.append(el("h2", "", "家里的电脑 · Telegram"),
    el("p", "muted small", `状态：${status.state}${status.controller ? `（@${status.controller}）` : ""}${warn ? "。" + warn : ""}`),
    field("谁来收消息", controller, "总控 bot 负责收群里的消息；成员各自的 bot 负责发。"), controllerToken,
    field("允许的用户 ID", users, "第一个是主人，后面的是群友；逗号隔开"), field("允许的群 ID", chats),
    bots, el("p", "muted small", "token 存在 secrets.json 里，不进 git。这些设置在「群聊规则」和「成员」里也能改。"), save);
  app.showModal(body);
}

// A file picker that uploads a picture (PNG, JPG, WebP) and hands back its saved name.
function pickImage(done) {
  const file = el("input");
  file.type = "file";
  file.accept = ".png,.jpg,.jpeg,.webp";
  file.onchange = async () => {
    const f = file.files[0];
    if (!f) return;
    try { done((await send("POST", "/api/home/images", await f.arrayBuffer(), true)).file); }
    catch (e) { say(e.message, true); }
  };
  return file;
}

// ---- a room's floor and walls: a colour, or a picture repeated like tiles or wallpaper ----

function openRoomLook(app, room) {
  const draft = { floor: room.floor || null, walls: room.walls || null };
  const body = el("div", "home-kind-editor");
  const part = (key, label) => {
    const box = el("div", "home-edit-section");
    const render = () => {
      const look = draft[key];
      const color = el("input");
      color.type = "color";
      color.value = look?.color || (key === "floor" ? "#efe6da" : "#d9d4cc");
      color.oninput = () => { draft[key] = { color: color.value }; render(); };
      const reset = el("button", "", "用默认的");
      reset.onclick = () => { draft[key] = null; render(); };
      const row = el("div", "home-look");
      row.append(el("span", "muted small", "颜色"), color, el("span", "muted small", "或图片"),
        pickImage((name) => { draft[key] = { image: name, tile: look?.tile || 1 }; render(); }), reset);
      const now = look?.image ? `图片 ${look.image}` : look?.color ? `颜色 ${look.color}` : "默认";
      box.replaceChildren(el("h3", "", label), el("p", "muted small", `现在：${now}`), row);
      if (look?.image) {
        const tile = el("input");
        tile.type = "number"; tile.min = 0.25; tile.max = 5; tile.step = 0.25; tile.value = look.tile || 1;
        tile.onchange = () => (draft[key] = { ...look, tile: Number(tile.value) });
        const t = el("div", "home-look");
        t.append(el("span", "muted small", "一张图铺多大（米）"), tile);
        box.append(t);
      }
    };
    render();
    return box;
  };
  const save = el("button", "primary", "保存");
  save.onclick = async () => {
    try {
      await send("PUT", `/api/home/rooms/${room.id}`, draft);
      app.modal.hidden = true;
      await app.poll();
    } catch (e) { say(e.message, true); }
  };
  body.append(el("h2", "", `${room.name}的外观`), part("floor", "地面（地砖、地板）"), part("walls", "墙面（墙纸、颜色）"),
    el("p", "muted small", "图片会像地砖、墙纸一样重复铺开。只收 PNG、JPG、WebP，最大 5 MB。"), save);
  app.showModal(body);
}

// ---- making or changing a kind of furniture ----

function openKind(app, existing) {
  const d = app.data;
  const draft = existing ? JSON.parse(JSON.stringify(existing))
    : { template: "sofa", name: "", size: [...d.templates.sofa.size], height: d.templates.sofa.height, color: d.templates.sofa.color, functions: [], model: null };
  const body = el("div", "home-kind-editor");
  const preview = el("div", "home-preview");
  const render = () => {
    body.replaceChildren();
    const field = (label, input) => { const f = el("label", "home-field"); f.append(el("span", "muted small", label), input); return f; };
    const name = el("input");
    name.value = draft.name;
    name.placeholder = d.templates[draft.template]?.name || "";
    name.maxLength = 16;
    name.oninput = () => (draft.name = name.value);
    const template = el("select");
    for (const [k, t] of Object.entries(d.templates)) { const o = el("option", "", t.name); o.value = k; o.selected = k === draft.template; template.append(o); }
    template.onchange = () => {
      const t = d.templates[template.value];
      Object.assign(draft, { template: template.value, size: [...t.size], height: t.height, color: t.color });
      render();
    };
    const num = (value, min, max, step, set) => {
      const i = el("input");
      i.type = "number"; i.min = min; i.max = max; i.step = step; i.value = value;
      i.onchange = () => { set(Number(i.value)); app.showPreview(preview, draft); };
      return i;
    };
    const color = el("input");
    color.type = "color";
    color.value = draft.color;
    color.oninput = () => { draft.color = color.value; app.showPreview(preview, draft); };
    const sizes = el("div", "home-look");
    sizes.append(num(draft.size[0], 1, 24, 1, (v) => (draft.size[0] = v)), el("span", "", "×"),
      num(draft.size[1], 1, 24, 1, (v) => (draft.size[1] = v)), el("span", "muted small", "格（一格半米）"));
    const model = el("div", "home-model");
    const file = el("input");
    file.type = "file";
    file.accept = ".glb";
    const info = el("p", "muted small", draft.model ? `模型：${draft.model.file}` : "可以上传 Tripo 等导出的 .glb，替换上面的样子。");
    file.onchange = async () => {
      const f = file.files[0];
      if (!f) return;
      info.textContent = "上传中…";
      try {
        const got = await send("POST", "/api/home/models", await f.arrayBuffer(), true);
        draft.model = { file: got.file, turn: 0, scale: 1 };
        info.textContent = `${(got.bytes / 1048576).toFixed(1)} MB，约 ${got.triangles} 个三角面${got.animations.length ? `，动画：${got.animations.join("、")}` : ""}`
          + (got.triangles > 100000 ? "。面数有点多，手机上可能会卡。" : "");
        render();
      } catch (e) { info.textContent = e.message; }
    };
    model.append(file, info);
    if (draft.model) {
      const turn = el("select");
      for (const a of [0, 90, 180, 270]) { const o = el("option", "", `转 ${a}°`); o.value = a; o.selected = draft.model.turn === a; turn.append(o); }
      turn.onchange = () => { draft.model.turn = Number(turn.value); app.showPreview(preview, draft); };
      const drop = el("button", "danger", "不用模型");
      drop.onclick = () => { draft.model = null; render(); };
      const row = el("div", "home-look");
      row.append(turn, el("span", "muted small", "大小"), num(draft.model.scale, 0.2, 3, 0.1, (v) => (draft.model.scale = v)), drop);
      model.append(row);
    }
    const save = el("button", "primary", existing ? "保存" : "做好了");
    save.onclick = async () => {
      try {
        const payload = { name: draft.name, template: draft.template, size: draft.size, height: draft.height,
                          color: draft.color, functions: draft.functions, model: draft.model };
        if (existing) await send("PUT", `/api/home/types/${existing.id}`, payload);
        else await send("POST", "/api/home/types", payload);
        app.modal.hidden = true;
        await app.poll();
      } catch (e) { say(e.message, true); }
    };
    body.append(el("h2", "", existing ? `改：${existing.name}` : "新做一种家具"),
      field("名字", name), field("样子", template), field("占地", sizes),
      field("高度（米）", num(draft.height, 0.01, 3, 0.05, (v) => (draft.height = v))), field("颜色", color),
      preview, field("模型", model),
      field("默认挂的功能（摆出来时带上，之后每件可以改）", checkboxes(functionNames(d), draft.functions, (next) => { draft.functions = next; render(); })),
      el("p", "muted small", "坐、躺的位置按样子和大小自动算：沙发每两格一个座位，床一个躺的位置。"), save);
    app.showPreview(preview, draft);
  };
  render();
  app.showModal(body);
}
