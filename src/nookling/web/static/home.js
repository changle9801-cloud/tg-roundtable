// The 「家」 tab: an isometric 3D house where members walk to the room of what they are really doing.
// Positions come only from /api/home; walking between rooms is drawn here and never stored.

import * as THREE from "three";
import { OrbitControls } from "/static/vendor/OrbitControls.js";

const POLL_MS = 5000;
const STALE_SECONDS = 3 * 3600; // awake but nothing happened for this long: she is dimmed, not faked busy
const PHONE_SECONDS = 45; // spoke in the group this recently: phone in hand
const WALK_SPEED = 2.4; // units per second
const SKIN = 0xf1d2b6;

const el = (tag, cls, text) => {
  const node = document.createElement(tag);
  if (cls) node.className = cls;
  if (text !== undefined) node.textContent = text;
  return node;
};
const clock = (ts) => new Date(ts * 1000).toLocaleTimeString("zh-CN", { hour: "2-digit", minute: "2-digit" });

let app = null; // the one running scene

export function open(root) {
  if (!app) app = new HomeApp(root);
  app.start();
}

export function close() {
  if (app) app.stop();
}

// ---------- layout: rooms as rectangles on the floor (x to the right, z towards the viewer) ----------
// Rooms come with a size in grid cells; a cell is half a unit. Row A: bedrooms. Row B: study, a hall,
// the games room. Row C: kitchen, living room, storage, then any other room.

const CELL = 0.5, HALL = 1.5;

function doorAt(rect, door) {
  const along = door.wall === "front" || door.wall === "back" ? rect.w : rect.d;
  const at = door.at !== undefined ? door.at * CELL : along / 2;
  const spot = {
    front: { x: rect.x + at, z: rect.z + rect.d }, back: { x: rect.x + at, z: rect.z },
    left: { x: rect.x, z: rect.z + at }, right: { x: rect.x + rect.w, z: rect.z + at },
  }[door.wall];
  return { ...spot, wall: door.wall, outside: !!door.outside };
}

function layout(rooms) {
  const rects = {};
  const put = (room, x, z) => {
    const r = { x, z, w: room.size[0] * CELL, d: room.size[1] * CELL, room };
    r.doors = (room.doors || []).map((d) => doorAt(r, d));
    rects[room.id] = r;
    return r;
  };
  const byId = Object.fromEntries(rooms.map((r) => [r.id, r]));
  const beds = rooms.filter((r) => r.id.startsWith("bed-"));
  let x = 0;
  for (const room of beds) x += put(room, x, 0).w;
  const depthA = Math.max(5, ...beds.map((r) => r.size[1] * CELL));
  const zB = depthA + HALL;
  let depthB = 0, hallX = 0;
  if (byId.study) { const r = put(byId.study, 0, zB); hallX = r.w + HALL / 2; depthB = r.d; }
  if (byId.fun) depthB = Math.max(depthB, put(byId.fun, hallX + HALL / 2, zB).d);
  const zC = zB + depthB + HALL;
  let xc = 0, depthC = 0;
  const rowC = ["kitchen", "living", "storage"].map((id) => byId[id]).filter(Boolean);
  for (const room of rooms) if (!rects[room.id] && !rowC.includes(room)) rowC.push(room);
  for (const room of rowC) { const r = put(room, xc, zC); xc += r.w; depthC = Math.max(depthC, r.d); }
  const W = Math.max(x, hallX + HALL / 2 + (byId.fun ? byId.fun.size[0] * CELL : 0), xc);
  const living = rects.living || Object.values(rects)[0];
  const front = living.doors.find((d) => d.outside) || { x: living.x + living.w / 2, z: living.z + living.d };
  return {
    W, D: zC + depthC, rects, front, hallX, outside: { x: front.x, z: front.z + 3 },
    corridors: [depthA + HALL / 2, zB + depthB + HALL / 2], spans: { A: depthA, B: [zB, depthB], C: zC },
  };
}

// The corridor in front of a door: the nearest one on the side the door opens to.
function corridorFor(L, door) {
  const side = door.wall === "back" ? -1 : 1;
  const ahead = L.corridors.filter((c) => (c - door.z) * side > 0);
  return ahead.sort((a, b) => Math.abs(a - door.z) - Math.abs(b - door.z))[0] ?? L.corridors[0];
}

function route(L, fromRoom, toRoom, from, to) {
  const a = L.rects[fromRoom], b = L.rects[toRoom];
  if (fromRoom === toRoom || !a || !b) return [to];
  const da = a.doors.find((d) => !d.outside) || a.doors[0], db = b.doors.find((d) => !d.outside) || b.doors[0];
  if (!da || !db) return [to];
  const ca = corridorFor(L, da), cb = corridorFor(L, db);
  const pts = [{ x: da.x, z: da.z }, { x: da.x, z: ca }];
  if (ca !== cb) pts.push({ x: L.hallX, z: ca }, { x: L.hallX, z: cb });
  pts.push({ x: db.x, z: cb }, { x: db.x, z: db.z }, to);
  return pts;
}

// ---------- furniture: each template draws one type, sized and coloured, around its footprint's centre ----------
// Local axes: x to the right, z towards the front (the viewer when turn is 0).

function box(w, h, d, color, x, y, z) {
  const mesh = new THREE.Mesh(new THREE.BoxGeometry(w, h, d), new THREE.MeshLambertMaterial({ color }));
  mesh.position.set(x, y + h / 2, z);
  return mesh;
}

function cylinder(r, h, color, x, y, z) {
  const mesh = new THREE.Mesh(new THREE.CylinderGeometry(r, r, h, 20), new THREE.MeshLambertMaterial({ color }));
  mesh.position.set(x, y + h / 2, z);
  return mesh;
}

function ball(r, color, x, y, z) {
  const mesh = new THREE.Mesh(new THREE.SphereGeometry(r, 16, 12), new THREE.MeshLambertMaterial({ color }));
  mesh.position.set(x, y, z);
  return mesh;
}

const shade = (color, k) => new THREE.Color(color).multiplyScalar(k);

const TEMPLATES = {
  bed: (g, W, D, H, c) => {
    g.add(box(W, 0.35, D, c, 0, 0, 0));
    g.add(box(W, 0.55, 0.12, shade(c, 0.75), 0, 0, -D / 2 + 0.06));
    g.add(box(W - 0.1, 0.12, D - 0.2, 0xeceff1, 0, 0.35, 0.05));
    g.add(box(W * 0.6, 0.12, 0.45, 0xffffff, 0, 0.47, -D / 2 + 0.4));
  },
  "writing-desk": (g, W, D, H, c, piece, clickable) => {
    g.add(box(W, H, D, c, 0, 0, 0));
    const book = box(0.36, 0.08, 0.28, 0xc0392b, 0, H, 0);
    if (piece.owner) { book.userData = { diary: piece.owner }; clickable.push(book); }
    g.add(book);
  },
  "floor-lamp": (g, W, D, H, c) => {
    g.add(cylinder(0.06, H - 0.2, 0x555555, 0, 0, 0));
    g.add(cylinder(0.2, 0.18, c, 0, H - 0.2, 0));
  },
  rug: (g, W, D, H, c) => g.add(box(W, 0.02, D, c, 0, 0, 0)),
  "computer-desk": (g, W, D, H, c) => {
    g.add(box(W, H, 0.7, c, 0, 0, -D / 2 + 0.35));
    g.add(box(0.8, 0.5, 0.06, 0x263238, 0, H + 0.06, -D / 2 + 0.2));
    g.add(box(0.75, 0.42, 0.02, 0x4fc3f7, 0, H + 0.1, -D / 2 + 0.24));
    g.add(box(0.5, 0.45, 0.5, 0x607d8b, 0, 0, D / 2 - 0.4));
  },
  bookshelf: (g, W, D, H, c) => {
    g.add(box(W, H, D, c, 0, 0, 0));
    for (let s = 0; s < 4; s++) g.add(box(W - 0.1, 0.04, D - 0.08, shade(c, 0.8), 0, 0.45 + s * 0.45, 0.04));
    ["#e57373", "#64b5f6", "#81c784", "#ffd54f", "#ba68c8"].forEach((col, i) =>
      g.add(box(0.14, 0.36, 0.3, new THREE.Color(col), -W / 2 + 0.35 + i * 0.4, 0.49 + (i % 3) * 0.45, D / 2 - 0.15)));
  },
  plant: (g, W, D, H, c) => {
    const r = Math.min(W, D);
    g.add(cylinder(r * 0.5, H * 0.45, 0x8d6e63, 0, 0, 0));
    g.add(ball(r * 0.9, c, 0, H * 0.75, 0));
  },
  tv: (g, W, D, H, c, piece) => {
    g.add(box(W * 0.87, 0.5, D, 0x5d4037, 0, 0, 0));
    g.add(box(W * 0.8, 1.3, 0.1, c, 0, 0.55, -0.05));
    g.add(box(W * 0.75, 1.15, 0.02, 0x7e57c2, 0, 0.62, 0.01));
    if ((piece.functions || []).includes("game.play")) g.add(box(0.5, 0.12, 0.3, 0xeeeeee, W * 0.3, 0.5, 0.05));
  },
  sofa: (g, W, D, H, c) => {
    g.add(box(W, 0.45, D, c, 0, 0, 0));
    g.add(box(W, 0.6, 0.3, shade(c, 0.8), 0, 0.3, -D / 2 + 0.15));
  },
  beanbag: (g, W, D, H, c) => g.add(cylinder(Math.min(W, D) / 2, H, c, 0, 0, 0)),
  table: (g, W, D, H, c) => g.add(box(W, H, D, c, 0, 0, 0)),
  frame: (g, W, D, H, c) => {
    g.add(box(W * 0.8, H, 0.05, 0xffffff, 0, 0.95, -D / 2 + 0.05));
    g.add(box(W * 0.66, H - 0.14, 0.02, c, 0, 1.02, -D / 2 + 0.08));
  },
  fridge: (g, W, D, H, c) => {
    g.add(box(W * 0.9, H, D * 0.8, c, 0, 0, 0));
    g.add(box(0.02, H * 0.6, 0.02, 0x90a4ae, W * 0.3, H * 0.25, D * 0.4 + 0.01));
  },
  counter: (g, W, D, H, c) => g.add(box(W, H, D, c, 0, 0, 0)),
  "dining-table": (g, W, D, H, c) => {
    g.add(box(W * 0.8, H, D * 0.5, c, 0, 0, 0));
    for (const [dx, dz] of [[-1, -1], [1, -1], [-1, 1], [1, 1]]) g.add(box(0.42, 0.45, 0.42, 0x8d6e63, dx * W / 4, 0, dz * (D / 2 - 0.25)));
  },
  boxes: (g, W, D, H, c) => {
    [[-0.4, -0.4, 0.6], [0.4, -0.4, 0.7], [-0.4, 0.4, 0.8]].forEach(([dx, dz, h]) => g.add(box(0.7, h, 0.7, c, dx, 0, dz)));
    g.add(box(0.7, 0.5, 0.7, shade(c, 0.85), -0.4, 0.6, -0.4));
  },
};

const turnedSize = (size, turn) => (turn === 90 || turn === 270 ? [size[1], size[0]] : [size[0], size[1]]);

// Where a use puts a person, in house units, and the way she faces (degrees clockwise; 0 = +z).
function useSpot(rect, piece, type, use) {
  const [w, d] = type.size;
  const cx = (use.at[0] - w / 2) * CELL, cz = (use.at[1] - d / 2) * CELL;
  const a = (piece.turn * Math.PI) / 180;
  const [tw, td] = turnedSize(type.size, piece.turn);
  return {
    x: rect.x + (piece.at[0] + tw / 2) * CELL + cx * Math.cos(a) - cz * Math.sin(a),
    z: rect.z + (piece.at[1] + td / 2) * CELL + cx * Math.sin(a) + cz * Math.cos(a),
    face: (piece.turn + (use.face || 0)) % 360, pose: use.pose, y: use.y || 0,
  };
}

function drawPiece(parent, rect, piece, type, clickable) {
  const g = new THREE.Group();
  const [w, d] = type.size;
  const draw = TEMPLATES[type.template] || TEMPLATES.table;
  draw(g, w * CELL, d * CELL, type.height, piece.color || type.color, piece, clickable);
  const [tw, td] = turnedSize(type.size, piece.turn);
  g.rotation.y = (-piece.turn * Math.PI) / 180;
  g.position.set(rect.x + (piece.at[0] + tw / 2) * CELL, 0, rect.z + (piece.at[1] + td / 2) * CELL);
  parent.add(g);
}

const FLOORS = { bed: 0xf3e5d8, study: 0xe3ecef, fun: 0xece4f5, living: 0xf6ead7, kitchen: 0xe8f1e4, storage: 0xe6e2dd };
const WALL = 0xd9d4cc, WALL_H = 0.7, DOOR_W = 1.5;

function walls(g, r) {
  // Low walls a hair inside the room, with door gaps; neighbours' walls never share a face (no flicker).
  const inset = 0.06, t = 0.12;
  const edges = [
    ["back", r.x + inset, r.z + inset, r.x + r.w - inset, r.z + inset],
    ["front", r.x + inset, r.z + r.d - inset, r.x + r.w - inset, r.z + r.d - inset],
    ["left", r.x + inset, r.z + inset, r.x + inset, r.z + r.d - inset],
    ["right", r.x + r.w - inset, r.z + inset, r.x + r.w - inset, r.z + r.d - inset],
  ];
  for (const [wall, x1, z1, x2, z2] of edges) {
    const horizontal = z1 === z2;
    const gaps = r.doors.filter((dp) => dp.wall === wall).map((dp) => (horizontal ? dp.x : dp.z)).sort((a, b) => a - b);
    let start = horizontal ? x1 : z1;
    const end = horizontal ? x2 : z2;
    for (const gc of [...gaps, null]) {
      const stop = gc === null ? end : gc - DOOR_W / 2;
      if (stop - start > 0.05) {
        const len = stop - start, mid = start + len / 2;
        g.add(horizontal ? box(len, WALL_H, t, WALL, mid, 0, z1) : box(t, WALL_H, len, WALL, x1, 0, mid));
      }
      if (gc !== null) start = gc + DOOR_W / 2;
    }
  }
  for (const door of r.doors.filter((dp) => dp.outside)) g.add(box(1.2, 1.9, 0.12, 0x6d4c41, door.x, 0, door.z));
}

// ---------- a blocky member ----------

function accessoryMesh(kind, color) {
  const g = new THREE.Group();
  const mat = (c) => new THREE.MeshLambertMaterial({ color: c });
  const add = (geo, c, x, y, z, rx = 0, rz = 0) => { const m = new THREE.Mesh(geo, mat(c)); m.position.set(x, y, z); m.rotation.set(rx, 0, rz); g.add(m); return m; };
  const contrast = new THREE.Color(color).offsetHSL(0.5, 0, 0.15);
  if (kind === "scarf") {
    add(new THREE.BoxGeometry(0.5, 0.1, 0.36), contrast, 0, 1.06, 0);
    add(new THREE.BoxGeometry(0.1, 0.3, 0.06), contrast, 0.12, 0.92, 0.19);
  } else if (kind === "headphones") {
    add(new THREE.TorusGeometry(0.25, 0.035, 8, 20, Math.PI), 0x222222, 0, 1.3, 0);
    add(new THREE.BoxGeometry(0.08, 0.16, 0.16), 0x222222, -0.25, 1.28, 0);
    add(new THREE.BoxGeometry(0.08, 0.16, 0.16), 0x222222, 0.25, 1.28, 0);
  } else if (kind === "sunglasses") {
    add(new THREE.BoxGeometry(0.38, 0.09, 0.03), 0x111111, 0, 1.33, 0.215);
  } else if (kind === "glasses") {
    add(new THREE.TorusGeometry(0.06, 0.012, 6, 16), 0x333333, -0.09, 1.33, 0.215);
    add(new THREE.TorusGeometry(0.06, 0.012, 6, 16), 0x333333, 0.09, 1.33, 0.215);
  } else if (kind === "whale-clip") {
    add(new THREE.SphereGeometry(0.07, 12, 8), 0x4fc3f7, 0.2, 1.47, 0.08).scale.set(1.5, 0.8, 0.8);
    add(new THREE.ConeGeometry(0.04, 0.08, 6), 0x4fc3f7, 0.31, 1.49, 0.08, 0, -Math.PI / 2);
  } else if (kind === "hat") {
    add(new THREE.CylinderGeometry(0.3, 0.3, 0.04, 20), contrast, 0, 1.51, 0);
    add(new THREE.CylinderGeometry(0.2, 0.2, 0.18, 20), contrast, 0, 1.6, 0);
  } else if (kind === "bow") {
    add(new THREE.ConeGeometry(0.07, 0.14, 4), 0xec407a, -0.07, 1.54, 0, 0, Math.PI / 2);
    add(new THREE.ConeGeometry(0.07, 0.14, 4), 0xec407a, 0.07, 1.54, 0, 0, -Math.PI / 2);
  } else if (kind === "backpack") {
    add(new THREE.BoxGeometry(0.36, 0.42, 0.16), contrast, 0, 0.78, -0.24);
  }
  return g;
}

class Figure {
  constructor(person) {
    this.id = person.id;
    this.root = new THREE.Group();
    this.body = new THREE.Group(); // tilts when lying down
    this.root.add(this.body);
    const color = new THREE.Color(person.look.color || "#888888");
    const dark = color.clone().multiplyScalar(0.6);
    this.mats = [];
    const part = (w, h, d, c, x, y, z) => {
      const mat = new THREE.MeshLambertMaterial({ color: c, transparent: true });
      this.mats.push(mat);
      const m = new THREE.Mesh(new THREE.BoxGeometry(w, h, d), mat);
      m.position.set(x, y, z);
      m.userData = { person: person.id };
      return m;
    };
    const limb = (w, h, c, x, y) => { // pivot at the top, so it swings like a leg or arm
      const pivot = new THREE.Group();
      pivot.position.set(x, y, 0);
      pivot.add(part(w, h, w, c, 0, -h / 2, 0));
      this.body.add(pivot);
      return pivot;
    };
    this.legL = limb(0.16, 0.5, dark, -0.1, 0.5);
    this.legR = limb(0.16, 0.5, dark, 0.1, 0.5);
    this.body.add(part(0.46, 0.56, 0.3, color, 0, 0.78, 0));
    this.armL = limb(0.12, 0.48, color, -0.3, 1.02);
    this.armR = limb(0.12, 0.48, color, 0.3, 1.02);
    this.body.add(part(0.42, 0.4, 0.4, SKIN, 0, 1.28, 0));
    this.body.add(part(0.06, 0.07, 0.02, 0x2b2b2b, -0.09, 1.3, 0.205));
    this.body.add(part(0.06, 0.07, 0.02, 0x2b2b2b, 0.09, 1.3, 0.205));
    this.body.add(part(0.44, 0.12, 0.42, dark, 0, 1.49, -0.01)); // hair
    this.phone = part(0.06, 0.12, 0.02, 0x222222, 0, -0.48, 0.08);
    this.armR.add(this.phone);
    this.extra = accessoryMesh(person.look.accessory, color);
    this.body.add(this.extra);
    this.look = `${person.look.color}|${person.look.accessory}`;
    this.path = [];
    this.pose = "stand";
    this.face = 0;
    this.y = 0;
    this.phase = Math.random() * 6;
  }

  walkTo(points) {
    this.path = points.map((p) => new THREE.Vector3(p.x, 0, p.z));
  }

  update(dt, t) {
    const pos = this.root.position;
    let walking = false;
    if (this.path.length) {
      const target = this.path[0];
      const delta = target.clone().sub(pos).setY(0);
      const dist = delta.length();
      const step = WALK_SPEED * dt;
      if (dist <= step) {
        pos.set(target.x, 0, target.z);
        this.path.shift();
      } else {
        pos.add(delta.multiplyScalar(step / dist));
        this.root.rotation.y = Math.atan2(delta.x, delta.z);
      }
      walking = this.path.length > 0 || dist > step;
    }
    const pose = walking ? "walk" : this.pose;
    const swing = walking ? Math.sin(t * 9 + this.phase) * 0.6 : 0;
    this.legL.rotation.x = pose === "sit" ? -Math.PI / 2 : swing;
    this.legR.rotation.x = pose === "sit" ? -Math.PI / 2 : -swing;
    this.armL.rotation.x = -swing * 0.8;
    this.armR.rotation.x = this.phoning && !walking ? -2.5 : swing * 0.8;
    this.phone.visible = !!this.phoning;
    this.body.rotation.x = pose === "lie" ? -Math.PI / 2 : 0;
    this.body.position.y = pose === "sit" ? -0.05 : pose === "lie" ? 0.15 : 0;
    this.body.position.z = pose === "lie" ? 0.2 : 0;
    if (!walking) {
      this.root.rotation.y = this.face;
      pos.y = this.y + (pose === "stand" ? Math.abs(Math.sin(t * 1.5 + this.phase)) * 0.015 : 0);
    } else {
      pos.y = 0;
    }
  }

  dim(on) {
    for (const m of this.mats) m.opacity = on ? 0.4 : 1;
  }
}

// ---------- the scene ----------

class HomeApp {
  constructor(root) {
    this.root = root;
    this.running = false;
    this.figures = new Map();
    this.labels = new Map();
    this.data = null;
    this.layoutKey = "";
    this.clickable = [];
    this.selected = "";
    root.replaceChildren();
    this.stage = el("div", "home-stage");
    this.labelLayer = el("div", "home-labels");
    this.banner = el("div", "home-banner");
    this.banner.hidden = true;
    this.panel = el("div", "home-panel card");
    this.modal = el("div", "home-modal");
    this.modal.hidden = true;
    this.stage.append(this.labelLayer, this.banner);
    const wrap = el("div", "home-wrap");
    wrap.append(this.stage, this.panel);
    root.append(wrap, this.modal);
    try {
      this.renderer = new THREE.WebGLRenderer({ antialias: true, alpha: true });
    } catch {
      this.renderer = null;
      this.stage.append(el("p", "muted", "这个浏览器不支持 WebGL，只能看右边的列表。"));
      return;
    }
    this.renderer.setPixelRatio(Math.min(window.devicePixelRatio || 1, 2));
    this.stage.prepend(this.renderer.domElement);
    this.scene = new THREE.Scene();
    this.scene.add(new THREE.HemisphereLight(0xffffff, 0xb0a99f, 1.6));
    const sun = new THREE.DirectionalLight(0xffffff, 1.2);
    sun.position.set(-8, 14, 10);
    this.scene.add(sun);
    this.camera = new THREE.OrthographicCamera(-10, 10, 10, -10, -200, 400);
    this.controls = new OrbitControls(this.camera, this.renderer.domElement);
    this.controls.enableDamping = true;
    this.controls.maxPolarAngle = Math.PI * 0.42;
    this.controls.minPolarAngle = Math.PI * 0.12;
    this.controls.minZoom = 0.6;
    this.controls.maxZoom = 4;
    this.controls.enablePan = true;
    this.house = new THREE.Group();
    this.scene.add(this.house);
    this.people = new THREE.Group();
    this.scene.add(this.people);
    this.ray = new THREE.Raycaster();
    let down = null;
    this.renderer.domElement.addEventListener("pointerdown", (e) => (down = [e.clientX, e.clientY]));
    this.renderer.domElement.addEventListener("pointerup", (e) => {
      if (down && Math.hypot(e.clientX - down[0], e.clientY - down[1]) < 5) this.click(e);
      down = null;
    });
    new ResizeObserver(() => this.resize()).observe(this.stage);
    this.lastFrame = performance.now();
  }

  start() {
    if (this.running) return;
    this.running = true;
    this.poll();
    this.timer = setInterval(() => this.poll(), POLL_MS);
    if (this.renderer) this.frame();
  }

  stop() {
    this.running = false;
    clearInterval(this.timer);
  }

  async poll() {
    try {
      const res = await fetch("/api/home", { cache: "no-store" });
      if (!res.ok) throw new Error(String(res.status));
      this.apply(await res.json());
      this.offline(false);
    } catch {
      this.offline(true);
    }
  }

  offline(on) {
    this.stage.classList.toggle("home-offline", on);
    this.banner.hidden = !on;
    this.banner.textContent = "服务没在运行，房子停在最后看到的样子";
  }

  // ---- building ----

  build(data) {
    this.house.clear();
    this.clickable = [];
    this.L = layout(data.rooms);
    const L = this.L;
    const corridor = new THREE.MeshLambertMaterial({ color: 0xe9e4dc });
    const strip = (w, d, x, z) => { const m = new THREE.Mesh(new THREE.BoxGeometry(w, 0.06, d), corridor); m.position.set(x, -0.03, z); this.house.add(m); };
    for (const c of L.corridors) strip(L.W, HALL, L.W / 2, c);
    strip(HALL, L.spans.B[1], L.hallX, L.spans.B[0] + L.spans.B[1] / 2);
    const ground = new THREE.Mesh(new THREE.BoxGeometry(L.W + 6, 0.05, L.D + 8), new THREE.MeshLambertMaterial({ color: 0xc8d5b9 }));
    ground.position.set(L.W / 2, -0.09, L.D / 2 + 1);
    this.house.add(ground);
    this.roomLabels = [];
    for (const room of data.rooms) {
      const r = L.rects[room.id];
      if (!r) continue;
      const kind = room.id.startsWith("bed-") ? "bed" : room.id;
      const floor = new THREE.Mesh(new THREE.BoxGeometry(r.w, 0.08, r.d), new THREE.MeshLambertMaterial({ color: FLOORS[kind] ?? 0xeeeeee }));
      floor.position.set(r.x + r.w / 2, -0.04, r.z + r.d / 2);
      this.house.add(floor);
      walls(this.house, r);
      this.roomLabels.push({ id: room.id, pos: new THREE.Vector3(r.x + r.w / 2, 0.2, r.z + r.d - 0.25) });
    }
    // Seats come from the furniture: each use of each placed piece is a spot someone can take.
    this.seats = {};
    this.screens = {};
    this.pieceAt = {}; // piece id -> its centre and the way its front faces, for standing in front of it
    for (const piece of data.pieces) {
      const type = data.types[piece.type], r = L.rects[piece.room];
      if (!type || !r) continue;
      drawPiece(this.house, r, piece, type, this.clickable);
      (type.uses || []).forEach((use, i) => {
        (this.seats[piece.room] ||= []).push({ key: `${piece.id}:${i}`, piece, type, ...useSpot(r, piece, type, use) });
      });
      const [tw, td] = turnedSize(type.size, piece.turn);
      const centre = { id: piece.id, room: piece.room, x: r.x + (piece.at[0] + tw / 2) * CELL, z: r.z + (piece.at[1] + td / 2) * CELL,
                       turn: piece.turn, reach: ((piece.turn % 180 ? tw : td) / 2) * CELL };
      this.pieceAt[piece.id] = centre;
      if (type.screen) (this.screens[piece.room] ||= []).push(centre);
    }
    const span = Math.max(L.W, L.D);
    this.center = new THREE.Vector3(L.W / 2, 0, L.D / 2);
    this.camera.position.set(this.center.x + span, span * 0.95, this.center.z + span);
    this.controls.target.copy(this.center);
    this.view = span * 0.62;
    this.resize();
  }

  // A seat looking at a screen in the same room, not too far: where you watch TV from.
  facesScreen(seat, only = null) {
    const dir = { x: -Math.sin((seat.face * Math.PI) / 180), z: Math.cos((seat.face * Math.PI) / 180) };
    return (this.screens[seat.piece.room] || []).filter((sc) => !only || sc.id === only).some((sc) => {
      const vx = sc.x - seat.x, vz = sc.z - seat.z, len = Math.hypot(vx, vz);
      return len > 0.3 && len <= 4 && (dir.x * vx + dir.z * vz) / len > 0.7;
    });
  }

  spotFor(person, taken) {
    if (person.verb === "visit" && this.figures.has(person.peer)) {
      const p = this.figures.get(person.peer).root.position;
      return { x: p.x + 0.8, z: p.z + 0.5, pose: "stand", face: 90 };
    }
    const seats = (this.seats[person.room] || []).filter((s) => !taken.has(s.key));
    const sit = seats.filter((s) => s.pose === "sit");
    const using = person.piece && this.pieceAt[person.piece];
    if (using) {
      // The piece she went to for this: its own seat, a seat facing it if it is a screen, else in front of it.
      const own = seats.filter((s) => s.piece.id === using.id);
      const facing = (this.screens[using.room] || []).some((sc) => sc.id === using.id) ? sit.filter((s) => this.facesScreen(s, using.id)) : [];
      const choice = own[0] || facing[0];
      if (choice) {
        taken.add(choice.key);
        return choice;
      }
      const a = (using.turn * Math.PI) / 180, n = taken.size;
      taken.add(`front:${using.id}:${n}`);
      const side = ((n % 2) - 0.5) * 0.6; // two people at one thing stand side by side
      return { x: using.x - Math.sin(a) * (using.reach + 0.6) + side * Math.cos(a),
               z: using.z + Math.cos(a) * (using.reach + 0.6) + side * Math.sin(a), pose: "stand", face: (using.turn + 180) % 360 };
    }
    const pick = {
      sleep: () => seats.filter((s) => s.pose === "lie" && s.piece.owner === person.id).concat(seats.filter((s) => s.pose === "lie")),
      read: () => sit.filter((s) => (s.piece.functions || []).includes("web.read")).concat(sit),
      play: () => sit.filter((s) => this.facesScreen(s)).concat(sit),
      watch: () => sit.filter((s) => this.facesScreen(s)).concat(sit),
      rest: () => sit.filter((s) => !(s.piece.functions || []).includes("web.read")).concat(sit),
    };
    pick.browse = pick.write = pick.read;
    const options = pick[person.verb] ? pick[person.verb]() : [];
    if (options.length) {
      taken.add(options[0].key);
      return options[0];
    }
    // Nowhere to sit for this: stand in the room, spread out a little.
    const r = this.L.rects[person.room] || this.L.rects.living;
    const n = taken.size;
    taken.add(`stand:${n}`);
    return { x: r.x + r.w / 2 + ((n % 3) - 1) * 0.8, z: r.z + r.d * 0.6 + Math.floor(n / 3) * 0.6, pose: "stand", face: 0 };
  }

  apply(data) {
    this.data = data;
    const key = JSON.stringify([data.rooms, data.pieces]);
    if (key !== this.layoutKey && this.renderer) {
      this.layoutKey = key;
      this.build(data);
    }
    if (this.renderer) this.place(data);
    this.renderPanel();
  }

  place(data) {
    const L = this.L;
    const seen = new Set();
    const taken = new Set(); // seats already given to someone this time
    const people = [...data.people].sort((a, b) => (a.verb === "visit") - (b.verb === "visit") || a.id.localeCompare(b.id));
    for (const person of people) {
      seen.add(person.id);
      let fig = this.figures.get(person.id);
      if (fig && fig.look !== `${person.look.color}|${person.look.accessory}`) {
        this.people.remove(fig.root);
        this.figures.delete(person.id);
        fig = null;
      }
      const out = person.verb === "out";
      const spot = out ? L.outside : this.spotFor(person, taken);
      if (!fig) {
        fig = new Figure(person);
        this.figures.set(person.id, fig);
        this.people.add(fig.root);
        fig.root.position.set(spot.x, 0, spot.z);
        fig.room = person.room;
      } else if (fig.room !== person.room || fig.spotKey !== `${spot.x},${spot.z}`) {
        const from = fig.room;
        let pts;
        if (out) pts = [...route(L, from, "living", fig.root.position, L.front), L.front, L.outside];
        else if (from === "out") {
          fig.root.visible = true;
          fig.root.position.set(L.outside.x, 0, L.outside.z);
          pts = [L.front, ...route(L, "living", person.room, L.front, spot)];
        } else pts = route(L, from, person.room, fig.root.position, spot);
        fig.walkTo(pts);
        fig.room = person.room;
      }
      fig.spotKey = `${spot.x},${spot.z}`;
      fig.pose = spot.pose || "stand";
      fig.face = (-(spot.face ?? 0) * Math.PI) / 180;
      fig.y = spot.y || 0;
      fig.out = out;
      fig.phoning = person.verb === "phone" || person.verb === "share"
        || (data.now - person.phone_at < PHONE_SECONDS && !["sleep", "out"].includes(person.verb));
      fig.stale = !person.active && !["sleep", "out"].includes(person.verb) && data.now - person.since > STALE_SECONDS;
      fig.dim(fig.stale);
      fig.person = person;
    }
    for (const [id, fig] of this.figures) {
      if (!seen.has(id)) { this.people.remove(fig.root); this.figures.delete(id); }
    }
  }

  // ---- every frame ----

  resize() {
    if (!this.renderer) return;
    const w = this.stage.clientWidth, h = this.stage.clientHeight;
    if (!w || !h) return;
    this.renderer.setSize(w, h, false);
    const aspect = w / h;
    let view = this.view || 14;
    if (this.L) {
      // Fit the whole house: the corners of the ground, seen from the camera, decide the frame.
      this.camera.lookAt(this.controls.target);
      this.camera.updateMatrixWorld();
      const inv = this.camera.matrixWorldInverse;
      let halfX = 0, halfY = 0;
      for (const x of [-1, this.L.W + 1]) for (const z of [-1, this.L.D + 3]) for (const y of [0, 1.5]) {
        const p = new THREE.Vector3(x, y, z).applyMatrix4(inv);
        halfX = Math.max(halfX, Math.abs(p.x));
        halfY = Math.max(halfY, Math.abs(p.y));
      }
      view = Math.max(halfY, halfX / aspect) * 1.04;
    }
    this.camera.left = -view * aspect; this.camera.right = view * aspect;
    this.camera.top = view; this.camera.bottom = -view;
    this.camera.updateProjectionMatrix();
  }

  frame() {
    if (!this.running) return;
    requestAnimationFrame(() => this.frame());
    if (document.hidden) return;
    const now = performance.now();
    const dt = Math.min(0.05, (now - this.lastFrame) / 1000);
    this.lastFrame = now;
    const t = now / 1000;
    for (const fig of this.figures.values()) {
      fig.update(dt, t);
      fig.root.visible = !(fig.out && !fig.path.length);
    }
    this.controls.update();
    this.renderer.render(this.scene, this.camera);
    this.drawLabels();
  }

  screen(v) {
    const p = v.clone().project(this.camera);
    return [(p.x + 1) / 2 * this.stage.clientWidth, (1 - p.y) / 2 * this.stage.clientHeight];
  }

  drawLabels() {
    if (!this.data || !this.roomLabels) return;
    const want = new Set();
    const put = (key, cls, text, v, title) => {
      want.add(key);
      let node = this.labels.get(key);
      if (!node) {
        node = el("div", cls);
        this.labels.set(key, node);
        this.labelLayer.append(node);
      }
      if (node.textContent !== text) node.textContent = text;
      node.className = cls;
      if (title !== undefined) node.title = title;
      let [x, y] = this.screen(v);
      if (key.startsWith("who:")) { // people standing together: lift a tag until it no longer covers another
        const w = node.offsetWidth || 90, hgt = node.offsetHeight || 20;
        for (let moved = true, n = 0; moved && n < 6; n++) {
          moved = false;
          for (const [ox, oy, ow] of placed) {
            if (Math.abs(ox - x) < (ow + w) / 2 && Math.abs(oy - y) < hgt) { y = oy - hgt - 2; moved = true; }
          }
        }
        placed.push([x, y, w]);
      }
      node.style.transform = `translate(${Math.round(x)}px, ${Math.round(y)}px) translate(-50%, -100%)`;
    };
    const placed = [];
    const names = Object.fromEntries(this.data.rooms.map((r) => [r.id, r.name]));
    for (const r of this.roomLabels) put("room:" + r.id, "home-room-label", names[r.id] || r.id, r.pos);
    for (const fig of this.figures.values()) {
      if (!fig.root.visible) continue;
      const p = fig.person;
      const head = fig.root.position.clone().add(new THREE.Vector3(0, fig.pose === "lie" ? 0.9 : fig.pose === "sit" ? 1.6 : 1.9, 0));
      const words = fig.path.length ? "走过去…" : fig.stale ? "好久没动了" : p.verb === "sleep" ? "💤" : fig.phoning && p.verb !== "phone" ? "在看手机" : p.words;
      put("who:" + p.id, `home-tag${this.selected === p.id ? " selected" : ""}${fig.stale ? " stale" : ""}`, `${p.name} · ${words}`, head, p.detail || "");
    }
    for (const [key, node] of this.labels) {
      if (!want.has(key)) { node.remove(); this.labels.delete(key); }
    }
  }

  // ---- clicking ----

  click(e) {
    const rect = this.renderer.domElement.getBoundingClientRect();
    const mouse = new THREE.Vector2(((e.clientX - rect.left) / rect.width) * 2 - 1, -((e.clientY - rect.top) / rect.height) * 2 + 1);
    this.ray.setFromCamera(mouse, this.camera);
    const hits = this.ray.intersectObjects([...this.clickable, ...this.people.children], true);
    const hit = hits.find((h) => h.object.userData.diary || h.object.userData.person);
    if (!hit) return;
    if (hit.object.userData.diary) this.openDiary(hit.object.userData.diary);
    else { this.selected = hit.object.userData.person; this.renderPanel(); }
  }

  // ---- the side panel ----

  renderPanel() {
    const d = this.data;
    if (!d) return;
    const focus = document.activeElement;
    if (focus && this.panel.contains(focus) && /INPUT|SELECT/.test(focus.tagName)) return; // editing a look
    const rows = d.people.map((p) => {
      const row = el("button", "home-row" + (this.selected === p.id ? " selected" : ""));
      row.type = "button";
      row.onclick = () => { this.selected = this.selected === p.id ? "" : p.id; this.renderPanel(); };
      const dot = el("span", "home-dot");
      dot.style.background = p.look.color;
      const stale = !p.active && !["sleep", "out"].includes(p.verb) && d.now - p.since > STALE_SECONDS;
      const text = el("span", "home-row-text");
      text.append(el("b", "", p.name), el("span", "muted", ` ${p.room_name} · ${stale ? "好久没动了" : p.words}`));
      const when = el("span", "home-when muted", (p.active ? "从 " : "") + clock(p.since));
      row.append(dot, text, when);
      return row;
    });
    const list = el("div", "home-list");
    list.append(...rows);
    const detail = el("div", "home-detail");
    const p = d.people.find((x) => x.id === this.selected);
    if (p) {
      detail.append(el("h3", "", p.name));
      detail.append(el("p", "", `${p.room_name}，${p.active ? "正在" : "最近"}：${p.detail || p.words}`));
      if (p.peer) detail.append(el("p", "muted small", `和 ${d.people.find((x) => x.id === p.peer)?.name || p.peer} 有关`));
      const diary = el("button", "", "翻她的日记本");
      diary.onclick = () => this.openDiary(p.id);
      detail.append(diary, this.lookEditor(p));
    } else {
      detail.append(el("p", "muted small", "点一个人看她在干什么；点卧室桌上的红本子翻日记。拖动旋转，滚轮缩放。"));
    }
    this.panel.replaceChildren(el("h2", "", "家里"), list, detail);
  }

  lookEditor(p) {
    const box = el("div", "home-look");
    const color = el("input");
    color.type = "color";
    color.value = p.look.color;
    const acc = el("select");
    for (const [key, name] of Object.entries(this.data.accessories)) {
      const o = el("option", "", name);
      o.value = key;
      o.selected = key === p.look.accessory;
      acc.append(o);
    }
    const save = el("button", "", "换外形");
    save.onclick = async () => {
      try {
        await sendJSON("PUT", `/api/home/looks/${encodeURIComponent(p.id)}`, { color: color.value, accessory: acc.value });
        this.poll();
      } catch (e) { alert(e.message); }
    };
    const choose = el("button", "", "让她自己挑");
    choose.onclick = async () => {
      choose.disabled = true;
      choose.textContent = "她在挑…";
      try {
        await sendJSON("POST", `/api/home/looks/${encodeURIComponent(p.id)}/choose`, {});
        this.poll();
      } catch (e) { alert(e.message); }
      choose.disabled = false;
      choose.textContent = "让她自己挑";
    };
    box.append(el("span", "muted small", "外形"), color, acc, save, choose);
    return box;
  }

  // ---- the diary book ----

  async openDiary(pid) {
    const person = this.data?.people.find((p) => p.id === pid);
    const name = person?.name || pid;
    const showCover = async () => {
      const cover = await sendJSON("GET", `/api/home/diary/${encodeURIComponent(pid)}`);
      const body = el("div", "home-diary");
      body.append(el("h2", "", `${name}的日记本`),
        el("p", "muted small", "日记是她的隐私。翻开哪一天，她都会知道。"));
      if (!cover.days.length) body.append(el("p", "muted", "还没写过日记。"));
      for (const day of cover.days) {
        const b = el("button", "home-day", `${day.day}（${day.count} 篇）`);
        b.onclick = () => showDay(day.day);
        body.append(b);
      }
      this.showModal(body);
    };
    const showDay = async (day) => {
      const page = await sendJSON("GET", `/api/home/diary/${encodeURIComponent(pid)}?day=${encodeURIComponent(day)}`);
      const body = el("div", "home-diary");
      const back = el("button", "", "← 目录");
      back.onclick = showCover;
      body.append(back, el("h2", "", `${name} · ${day}`));
      for (const e of page.entries) {
        const card = el("div", "home-entry");
        const feelings = Object.keys(e.feelings || {}).join("、");
        card.append(el("b", "", e.title || "（无题）"), el("p", "", e.thoughts || e.event || ""));
        if (feelings) card.append(el("p", "muted small", feelings));
        body.append(card);
      }
      const told = page.reads.filter((r) => r.day === day).at(-1);
      body.append(el("p", "muted small", told?.told_at ? `她 ${clock(told.told_at)} 知道你翻过这一天。` : "她下次活动或说话时会知道你翻了这一天。"));
      this.showModal(body);
    };
    try { await showCover(); } catch (e) { alert(e.message); }
  }

  showModal(body) {
    const sheet = el("div", "home-sheet card");
    const closeBtn = el("button", "home-close", "×");
    closeBtn.onclick = () => (this.modal.hidden = true);
    sheet.append(closeBtn, body);
    this.modal.replaceChildren(sheet);
    this.modal.onclick = (e) => { if (e.target === this.modal) this.modal.hidden = true; };
    this.modal.hidden = false;
  }
}

async function sendJSON(method, path, body) {
  const res = await fetch(path, {
    method,
    headers: body === undefined ? {} : { "Content-Type": "application/json" },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  let data = {};
  try { data = await res.json(); } catch { /* empty */ }
  if (!res.ok) throw new Error(data.error || `请求失败（HTTP ${res.status}）`);
  return data;
}
