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

const ROW_A = 5, HALL = 1.4, ROW_B = 6, ROW_C = 6;

function layout(rooms) {
  const beds = rooms.filter((r) => r.id.startsWith("bed-"));
  const W = Math.max(20, beds.length * 5);
  const half = W / 2;
  const rects = {};
  const bw = W / Math.max(1, beds.length);
  const zB = ROW_A + HALL, zBC = zB + ROW_B, zC = zBC + HALL;
  beds.forEach((r, i) => {
    rects[r.id] = { x: i * bw, z: 0, w: bw, d: ROW_A, kind: "bed", owner: r.owner, door: { x: i * bw + bw / 2, z: ROW_A } };
  });
  const sideW = half - HALL / 2;
  rects.study = { x: 0, z: zB, w: sideW, d: ROW_B, kind: "study", door: { x: sideW / 2, z: zBC } };
  rects.fun = { x: half + HALL / 2, z: zB, w: sideW, d: ROW_B, kind: "fun", door: { x: half + HALL / 2 + sideW / 2, z: zBC } };
  const kw = W * 0.3, lw = W * 0.45;
  rects.kitchen = { x: 0, z: zC, w: kw, d: ROW_C, kind: "kitchen", door: { x: kw / 2, z: zC } };
  rects.living = { x: kw, z: zC, w: lw, d: ROW_C, kind: "living", door: { x: kw + lw / 2, z: zC } };
  rects.storage = { x: kw + lw, z: zC, w: W - kw - lw, d: ROW_C, kind: "storage", door: { x: kw + lw + (W - kw - lw) / 2, z: zC } };
  const front = { x: kw + lw / 2, z: zC + ROW_C };
  return {
    W, D: zC + ROW_C, half, rects, front,
    outside: { x: front.x, z: front.z + 3 },
    corridor: { A: ROW_A + HALL / 2, C: zBC + HALL / 2 },
  };
}

// The corridor a room's door opens onto: bedrooms open to the back one, the rest to the front one.
const corridorOf = (rect) => (rect.kind === "bed" ? "A" : "C");

function route(L, fromRoom, toRoom, from, to) {
  const a = L.rects[fromRoom], b = L.rects[toRoom];
  if (fromRoom === toRoom || !a || !b) return [to];
  const pts = [{ ...a.door }];
  const ca = L.corridor[corridorOf(a)], cb = L.corridor[corridorOf(b)];
  pts.push({ x: a.door.x, z: ca });
  if (ca !== cb) pts.push({ x: L.half, z: ca }, { x: L.half, z: cb });
  pts.push({ x: b.door.x, z: cb }, { ...b.door }, to);
  return pts;
}

// ---------- furniture and the spots people use ----------

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

// Each furnish() adds meshes to the group and returns spots: {verb group: [{x, z, y, pose, face}]}.
// face is the angle she looks at (0 = towards the viewer, +z).
const FACE_BACK = Math.PI, FACE_FRONT = 0;

function furnishBed(g, r, clickable) {
  const { x, z, w } = r;
  g.add(box(0.12, 0.5, 2.6, 0x8d6e63, x + 0.35, 0, z + 1.6)); // headboard side
  g.add(box(1.6, 0.35, 2.4, 0xa1887f, x + 1.25, 0, z + 1.6));
  g.add(box(1.5, 0.12, 2.3, 0xeceff1, x + 1.25, 0.35, z + 1.6));
  g.add(box(0.9, 0.12, 0.5, 0xffffff, x + 1.25, 0.47, z + 0.65));
  g.add(box(1.2, 0.7, 0.6, 0xbcaaa4, x + w - 1.0, 0, z + 0.55)); // desk
  const diary = box(0.36, 0.08, 0.28, 0xc0392b, x + w - 1.0, 0.7, z + 0.55);
  diary.userData = { diary: r.owner };
  clickable.push(diary);
  g.add(diary);
  g.add(cylinder(0.06, 0.9, 0x555555, x + w - 0.5, 0, z + 0.4));
  g.add(cylinder(0.2, 0.18, 0xffe082, x + w - 0.5, 0.9, z + 0.4));
  g.add(box(1.6, 0.02, 1.2, 0xd7ccc8, x + w / 2 + 0.6, 0, z + 3.4)); // rug
  return {
    sleep: [{ x: x + 1.25, z: z + 1.9, y: 0.5, pose: "lie" }],
    rest: [{ x: x + 2.4, z: z + 2.9, pose: "sit", face: FACE_FRONT, y: 0 }, { x: x + w - 1.0, z: z + 1.25, pose: "sit", face: FACE_BACK }],
    any: [{ x: x + w / 2, z: z + 3.6, pose: "stand", face: FACE_FRONT }, { x: x + w / 2 + 1, z: z + 3.4, pose: "stand", face: FACE_FRONT }],
  };
}

function furnishStudy(g, r) {
  const { x, z, w, d } = r;
  const seats = [];
  const n = 3;
  for (let i = 0; i < n; i++) {
    const cx = x + 1.6 + i * ((w - 3.2) / (n - 1));
    g.add(box(1.5, 0.72, 0.7, 0xd7ccc8, cx, 0, z + 0.7));
    g.add(box(0.8, 0.5, 0.06, 0x263238, cx, 0.78, z + 0.55));
    g.add(box(0.75, 0.42, 0.02, 0x4fc3f7, cx, 0.82, z + 0.59));
    g.add(box(0.5, 0.45, 0.5, 0x607d8b, cx, 0, z + 1.6));
    seats.push({ x: cx, z: z + 1.45, pose: "sit", face: FACE_BACK, y: 0.0 });
  }
  g.add(box(0.5, 2.0, 2.4, 0x8d6e63, x + w - 0.4, 0, z + d - 1.7)); // bookshelf
  for (let s = 0; s < 4; s++) g.add(box(0.42, 0.04, 2.3, 0x6d4c41, x + w - 0.4, 0.45 + s * 0.45, z + d - 1.7));
  ["#e57373", "#64b5f6", "#81c784", "#ffd54f", "#ba68c8"].forEach((c, i) =>
    g.add(box(0.3, 0.36, 0.14, new THREE.Color(c), x + w - 0.45, 0.49 + (i % 3) * 0.45, z + d - 2.6 + i * 0.35)));
  g.add(cylinder(0.25, 0.5, 0x8d6e63, x + 0.5, 0, z + d - 0.6)); // plant
  g.add(new THREE.Mesh(new THREE.SphereGeometry(0.45, 16, 12), new THREE.MeshLambertMaterial({ color: 0x66bb6a })))
    .children.at(-1).position.set(x + 0.5, 0.85, z + d - 0.6);
  return { read: seats, browse: seats, write: seats, any: [{ x: x + w / 2, z: z + d - 1.5, pose: "stand", face: FACE_FRONT }] };
}

function furnishFun(g, r) {
  const { x, z, w, d } = r;
  g.add(box(2.6, 0.5, 0.5, 0x5d4037, x + w / 2, 0, z + 0.45)); // TV stand
  g.add(box(2.4, 1.3, 0.1, 0x212121, x + w / 2, 0.55, z + 0.4));
  g.add(box(2.25, 1.15, 0.02, 0x7e57c2, x + w / 2, 0.62, z + 0.46));
  g.add(box(0.5, 0.12, 0.3, 0xeeeeee, x + w / 2 + 0.9, 0.5, z + 0.5)); // console
  g.add(box(3.2, 0.45, 1.0, 0x5c6bc0, x + w / 2, 0, z + 3.3)); // sofa
  g.add(box(3.2, 0.6, 0.3, 0x3f51b5, x + w / 2, 0.3, z + 3.75));
  g.add(cylinder(0.5, 0.4, 0xff8a65, x + 1.0, 0, z + d - 1.2)); // beanbag
  g.add(box(2.6, 0.02, 2.0, 0xb39ddb, x + w / 2, 0, z + 2.2));
  const sofa = [-1, 0, 1].map((k) => ({ x: x + w / 2 + k, z: z + 3.2, y: 0.05, pose: "sit", face: FACE_BACK }));
  return { play: sofa, rest: [{ x: x + 1.0, z: z + d - 1.2, y: 0.05, pose: "sit", face: FACE_BACK }, ...sofa], any: sofa };
}

function furnishLiving(g, r) {
  const { x, z, w, d } = r;
  g.add(box(3.6, 0.45, 1.1, 0x8d6e63, x + w / 2, 0, z + 1.2)); // sofa against the back wall
  g.add(box(3.6, 0.6, 0.3, 0x6d4c41, x + w / 2, 0.3, z + 0.75));
  g.add(box(1.8, 0.4, 0.9, 0xbcaaa4, x + w / 2, 0, z + 2.7)); // coffee table
  g.add(box(4.2, 0.02, 3.0, 0xffe0b2, x + w / 2, 0, z + 2.3));
  [-1.2, 0, 1.2].forEach((k, i) => { // photo wall (filled in stage 2)
    g.add(box(0.8, 0.6, 0.05, [0xffffff, 0xfafafa, 0xf5f5f5][i], x + w / 2 + k, 1.25, z + 0.18));
    g.add(box(0.66, 0.46, 0.02, [0x90caf9, 0xa5d6a7, 0xffcc80][i], x + w / 2 + k, 1.32, z + 0.21));
  });
  g.add(box(1.2, 1.9, 0.12, 0x6d4c41, x + w / 2, 0, z + d)); // front door
  g.add(cylinder(0.3, 0.6, 0x8d6e63, x + w - 0.6, 0, z + 0.6));
  g.add(new THREE.Mesh(new THREE.SphereGeometry(0.55, 16, 12), new THREE.MeshLambertMaterial({ color: 0x43a047 })))
    .children.at(-1).position.set(x + w - 0.6, 1.0, z + 0.6);
  const sofa = [-1.1, 0, 1.1].map((k) => ({ x: x + w / 2 + k, z: z + 1.3, y: 0.05, pose: "sit", face: FACE_FRONT }));
  return { rest: sofa, share: [{ x: x + w / 2 - 1.6, z: z + 3.6, pose: "stand", face: FACE_FRONT }, { x: x + w / 2 + 1.6, z: z + 3.6, pose: "stand", face: FACE_FRONT }], any: sofa };
}

function furnishKitchen(g, r) {
  const { x, z, w, d } = r;
  g.add(box(0.9, 1.9, 0.8, 0xeceff1, x + 0.6, 0, z + 0.55)); // fridge
  g.add(box(0.02, 1.2, 0.02, 0x90a4ae, x + 0.95, 0.5, z + 0.97));
  g.add(box(w - 2.0, 0.9, 0.7, 0xb0bec5, x + 1.1 + (w - 2.0) / 2, 0, z + 0.45)); // counter
  g.add(box(1.6, 0.72, 1.0, 0xd7ccc8, x + w / 2, 0, z + 3.3)); // table
  const seats = [[-0.5, -0.8, FACE_FRONT], [0.5, -0.8, FACE_FRONT], [-0.5, 0.8, FACE_BACK], [0.5, 0.8, FACE_BACK]].map(([dx, dz, face]) => {
    g.add(box(0.42, 0.45, 0.42, 0x8d6e63, x + w / 2 + dx, 0, z + 3.3 + dz));
    return { x: x + w / 2 + dx, z: z + 3.3 + dz, y: 0, pose: "sit", face };
  });
  return { rest: seats, any: seats };
}

function furnishStorage(g, r) {
  const { x, z, w } = r;
  [[0.6, 0.6], [1.4, 0.6], [0.6, 1.4]].forEach(([dx, dz], i) => g.add(box(0.7, 0.6 + i * 0.1, 0.7, 0xbcaaa4, x + dx, 0, z + dz)));
  g.add(box(0.7, 0.5, 0.7, 0xa1887f, x + 0.6, 0.6, z + 0.6));
  return { any: [{ x: x + w / 2, z: z + 3.5, pose: "stand", face: FACE_FRONT }] };
}

const FURNISH = { bed: furnishBed, study: furnishStudy, fun: furnishFun, living: furnishLiving, kitchen: furnishKitchen, storage: furnishStorage };
const FLOORS = { bed: 0xf3e5d8, study: 0xe3ecef, fun: 0xece4f5, living: 0xf6ead7, kitchen: 0xe8f1e4, storage: 0xe6e2dd };
const WALL = 0xd9d4cc, WALL_H = 0.7, DOOR_W = 1.3;

function walls(g, r, doors) {
  // Low walls a hair inside the room, with door gaps; neighbours' walls never share a face (no flicker).
  const inset = 0.06, t = 0.12;
  const edges = [
    [r.x + inset, r.z + inset, r.x + r.w - inset, r.z + inset],
    [r.x + inset, r.z + r.d - inset, r.x + r.w - inset, r.z + r.d - inset],
    [r.x + inset, r.z + inset, r.x + inset, r.z + r.d - inset],
    [r.x + r.w - inset, r.z + inset, r.x + r.w - inset, r.z + r.d - inset],
  ];
  for (const [x1, z1, x2, z2] of edges) {
    const horizontal = z1 === z2;
    const gaps = doors.filter((dp) => (horizontal ? Math.abs(dp.z - z1) < 0.2 : Math.abs(dp.x - x1) < 0.2))
      .map((dp) => (horizontal ? dp.x : dp.z)).sort((a, b) => a - b);
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
    this.spots = {};
    const corridor = new THREE.MeshLambertMaterial({ color: 0xe9e4dc });
    const strip = (w, d, x, z) => { const m = new THREE.Mesh(new THREE.BoxGeometry(w, 0.06, d), corridor); m.position.set(x, -0.03, z); this.house.add(m); };
    strip(L.W, HALL, L.W / 2, ROW_A + HALL / 2);
    strip(L.W, HALL, L.W / 2, L.corridor.C);
    strip(HALL, ROW_B, L.half, ROW_A + HALL + ROW_B / 2);
    const ground = new THREE.Mesh(new THREE.BoxGeometry(L.W + 6, 0.05, L.D + 8), new THREE.MeshLambertMaterial({ color: 0xc8d5b9 }));
    ground.position.set(L.W / 2, -0.09, L.D / 2 + 1);
    this.house.add(ground);
    this.roomLabels = [];
    for (const room of data.rooms) {
      const r = L.rects[room.id];
      if (!r) continue;
      const floor = new THREE.Mesh(new THREE.BoxGeometry(r.w, 0.08, r.d), new THREE.MeshLambertMaterial({ color: FLOORS[r.kind] }));
      floor.position.set(r.x + r.w / 2, -0.04, r.z + r.d / 2);
      this.house.add(floor);
      const doors = [r.door];
      if (room.id === "living") doors.push(L.front);
      walls(this.house, r, doors);
      this.spots[room.id] = FURNISH[r.kind](this.house, r, this.clickable);
      this.roomLabels.push({ id: room.id, pos: new THREE.Vector3(r.x + r.w / 2, 0.2, r.z + r.d - 0.25) });
    }
    const span = Math.max(L.W, L.D);
    this.center = new THREE.Vector3(L.W / 2, 0, L.D / 2);
    this.camera.position.set(this.center.x + span, span * 0.95, this.center.z + span);
    this.controls.target.copy(this.center);
    this.view = span * 0.62;
    this.resize();
  }

  spotFor(person, index) {
    const room = this.spots[person.room] || this.spots.living;
    if (person.verb === "visit" && this.figures.has(person.peer)) {
      const f = this.figures.get(person.peer);
      const p = f.root.position;
      return { x: p.x + 0.8, z: p.z + 0.5, pose: "stand", face: -Math.PI / 2 };
    }
    const group = person.verb === "sleep" ? room.sleep : room[person.verb] || room.rest || room.any;
    const list = group && group.length ? group : room.any;
    return list[index % list.length];
  }

  apply(data) {
    this.data = data;
    const key = JSON.stringify(data.rooms);
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
    const order = {}; // how many already sit in each room's spot group, for spreading out
    const people = [...data.people].sort((a, b) => (a.verb === "visit") - (b.verb === "visit") || a.id.localeCompare(b.id));
    for (const person of people) {
      seen.add(person.id);
      let fig = this.figures.get(person.id);
      if (fig && fig.look !== `${person.look.color}|${person.look.accessory}`) {
        this.people.remove(fig.root);
        this.figures.delete(person.id);
        fig = null;
      }
      const slot = `${person.room}:${person.verb}`;
      const index = (order[slot] = (order[slot] ?? -1) + 1);
      const out = person.verb === "out";
      const spot = out ? L.outside : this.spotFor(person, index);
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
      fig.face = spot.face ?? 0;
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
