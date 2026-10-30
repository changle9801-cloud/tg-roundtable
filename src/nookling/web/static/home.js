// The 「家」 tab: an isometric 3D house where members walk to the room of what they are really doing.
// Positions come only from /api/home; walking between rooms is drawn here and never stored.

import * as THREE from "three";
import { OrbitControls } from "/static/vendor/OrbitControls.js";
import { GLTFLoader } from "/static/vendor/GLTFLoader.js";
import { RoomEnvironment } from "/static/vendor/RoomEnvironment.js";
import { openTelegram, renderEditor, say, send } from "/static/home-edit.js";

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

// ---------- layout: rooms where the floor plan puts them (x to the right, z towards the viewer) ----------
// Every room, hallways included, has a position and size in grid cells; a cell is half a unit.
// Doors sit on grid lines between two rooms, or between a room and the outside (docs/floorplan.md).

const CELL = 0.5;
const near = (a, b) => Math.abs(a - b) < 1e-6;

function layout(data, level = 0) {
  const rects = {};
  for (const room of data.rooms) {
    if (!room.at || (room.level ?? 0) !== level) continue;
    rects[room.id] = { x: room.at[0] * CELL, z: room.at[1] * CELL, w: room.size[0] * CELL, d: room.size[1] * CELL, room };
  }
  const all = Object.values(rects);
  const X0 = Math.min(...all.map((r) => r.x)), Z0 = Math.min(...all.map((r) => r.z));
  const W = Math.max(...all.map((r) => r.x + r.w)), D = Math.max(...all.map((r) => r.z + r.d));
  const doors = (data.doors || []).filter((d) => (d.level ?? 0) === level).map((d) => {
    const w = (d.width ?? 3) * CELL, x = d.at[0] * CELL, z = d.at[1] * CELL;
    return { ...d, w, x: d.line === "z" ? x + w / 2 : x, z: d.line === "z" ? z : z + w / 2, rooms: d.rooms || [] };
  });
  const wallOf = (r, d) => (d.line === "z" ? (near(d.z, r.z) ? "back" : "front") : (near(d.x, r.x) ? "left" : "right"));
  for (const r of all) {
    r.doors = doors.filter((d) => d.rooms.includes(r.room.id)).map((d) => ({ ...d, wall: wallOf(r, d) }));
  }
  const frontDoor = doors.find((d) => d.outside);
  const frontRoom = frontDoor ? frontDoor.rooms[0] : (rects.living ? "living" : all[0]?.room.id);
  const fr = rects[frontRoom];
  const front = frontDoor ? { x: frontDoor.x, z: frontDoor.z } : { x: fr.x + fr.w / 2, z: fr.z + fr.d };
  const out = frontDoor ? { front: [0, 1], back: [0, -1], left: [-1, 0], right: [1, 0] }[wallOf(fr, frontDoor)] : [0, 1];
  return { W, D, X0, Z0, rects, doors, front, frontRoom, frontInside: frontDoor && fr ? inFront(fr, frontDoor) : front,
           outside: { x: front.x + out[0] * 3, z: front.z + out[1] * 3 } };
}

// Walking from room to room: through doors only. Rooms are rectangles, so a straight line
// between two points in the same room never crosses a wall.
function route(L, fromRoom, toRoom, from, to) {
  if (fromRoom === toRoom || !L.rects[fromRoom] || !L.rects[toRoom]) return [to];
  const prev = { [fromRoom]: null };
  const todo = [fromRoom];
  while (todo.length && !(toRoom in prev)) {
    const here = todo.shift();
    for (const d of L.rects[here].doors) {
      if (d.outside || d.rooms.length !== 2) continue;
      const next = d.rooms[0] === here ? d.rooms[1] : d.rooms[0];
      if (next in prev || !L.rects[next]) continue;
      prev[next] = { room: here, door: d };
      todo.push(next);
    }
  }
  if (!(toRoom in prev)) return [to]; // no way in: she just arrives (the floor plan editor will say why)
  const steps = [];
  for (let at = toRoom; prev[at]; at = prev[at].room) steps.unshift({ ...prev[at], into: at });
  const pts = [];
  for (const { room, door, into } of steps) pts.push(inFront(L.rects[room], door), { x: door.x, z: door.z }, inFront(L.rects[into], door));
  pts.push(to);
  return pts;
}

// ---------- walking around furniture: A* on the grid of cells ----------
// A cell is walkable if it is inside a room and not under furniture that blocks the way. Walls
// between rooms are crossed only where a door is. A walk may move across the piece it starts or
// ends on (a seat inside a sofa, the middle of a bed).

const cellKey = (x, z) => `${x},${z}`;

function walkGrid(L, data) {
  const rooms = new Map(), blocked = new Map(), passes = new Set();
  for (const r of Object.values(L.rects)) {
    const [ax, az] = r.room.at;
    for (let i = 0; i < r.room.size[0]; i++) for (let j = 0; j < r.room.size[1]; j++) rooms.set(cellKey(ax + i, az + j), r.room.id);
  }
  for (const piece of data.pieces) {
    const type = data.types[piece.type], r = L.rects[piece.room];
    if (!type || !type.blocks || !r) continue;
    const [tw, td] = turnedSize(type.size, piece.turn);
    const [ax, az] = r.room.at;
    for (let i = 0; i < tw; i++) for (let j = 0; j < td; j++) blocked.set(cellKey(ax + piece.at[0] + i, az + piece.at[1] + j), piece.id);
  }
  for (const d of L.doors) {
    if (d.outside || d.rooms.length !== 2) continue;
    const [x, z] = d.at;
    for (let i = 0; i < (d.width ?? 3); i++) {
      const [a, b] = d.line === "z" ? [cellKey(x + i, z - 1), cellKey(x + i, z)] : [cellKey(x - 1, z + i), cellKey(x, z + i)];
      passes.add(`${a}|${b}`).add(`${b}|${a}`);
    }
  }
  return { rooms, blocked, passes };
}

function walkPath(G, from, to) {
  if (!G) return null;
  const cell = (p) => [Math.floor(p.x / CELL + 1e-9), Math.floor(p.z / CELL + 1e-9)];
  const [sx, sz] = cell(from), [gx, gz] = cell(to);
  const start = cellKey(sx, sz), goal = cellKey(gx, gz);
  if (!G.rooms.has(start) || !G.rooms.has(goal)) return null;
  const own = new Set([G.blocked.get(start), G.blocked.get(goal)].filter(Boolean)); // the pieces she gets off or onto
  const open = (k) => G.rooms.has(k) && (!G.blocked.has(k) || own.has(G.blocked.get(k)));
  const cross = (a, b) => G.rooms.get(a) === G.rooms.get(b) || G.passes.has(`${a}|${b}`);
  const h = (x, z) => Math.hypot(x - gx, z - gz);
  const cost = new Map([[start, 0]]), came = new Map(), frontier = [[h(sx, sz), sx, sz]];
  const done = new Set();
  let found = false;
  for (let guard = 0; frontier.length && guard < 20000; guard++) {
    let best = 0;
    for (let i = 1; i < frontier.length; i++) if (frontier[i][0] < frontier[best][0]) best = i;
    const [, x, z] = frontier.splice(best, 1)[0];
    const here = cellKey(x, z);
    if (here === goal) { found = true; break; }
    if (done.has(here)) continue;
    done.add(here);
    for (const [dx, dz] of [[1, 0], [-1, 0], [0, 1], [0, -1], [1, 1], [1, -1], [-1, 1], [-1, -1]]) {
      const next = cellKey(x + dx, z + dz);
      if (!open(next) || !cross(here, next)) continue;
      if (dx && dz) { // diagonal: inside one room, not cutting a corner
        const a = cellKey(x + dx, z), b = cellKey(x, z + dz);
        if (!open(a) || !open(b) || G.rooms.get(a) !== G.rooms.get(here) || G.rooms.get(b) !== G.rooms.get(here)
            || G.rooms.get(next) !== G.rooms.get(here)) continue;
      }
      const c = cost.get(here) + (dx && dz ? Math.SQRT2 : 1);
      if (c < (cost.get(next) ?? Infinity)) {
        cost.set(next, c);
        came.set(next, here);
        frontier.push([c + h(x + dx, z + dz), x + dx, z + dz]);
      }
    }
  }
  if (!found) return null;
  const cells = [];
  for (let k = goal; k !== start; k = came.get(k)) cells.unshift(k.split(",").map(Number));
  // Cell centres, keeping only the turns.
  const pts = [];
  for (let i = 0; i < cells.length - 1; i++) {
    const [x, z] = cells[i], prev = i ? cells[i - 1] : [sx, sz], next = cells[i + 1];
    if (x - prev[0] !== next[0] - x || z - prev[1] !== next[1] - z) pts.push({ x: (x + 0.5) * CELL, z: (z + 0.5) * CELL });
  }
  pts.push(to);
  return pts;
}

// A point just inside a room, in front of one of its doors.
function inFront(r, door) {
  const k = 0.6;
  if (door.line === "z") return { x: door.x, z: door.z + (near(door.z, r.z) ? k : -k) };
  return { x: door.x + (near(door.x, r.x) ? k : -k), z: door.z };
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

const textureLoader = new THREE.TextureLoader();
const pictures = new Map(); // uploaded picture -> its texture, loaded once

// A material showing an uploaded picture, repeated rx × ry times (floor tiles, wallpaper), or a colour.
function surfaceMaterial(look, fallback, rx = 1, ry = 1) {
  if (!look || !look.image) return new THREE.MeshLambertMaterial({ color: look?.color ?? fallback });
  if (!pictures.has(look.image)) {
    const t = textureLoader.load(`/api/home/images/${look.image}`);
    t.colorSpace = THREE.SRGBColorSpace;
    pictures.set(look.image, t);
  }
  const map = pictures.get(look.image).clone();
  map.wrapS = map.wrapT = THREE.RepeatWrapping;
  map.repeat.set(Math.max(rx, 0.01), Math.max(ry, 0.01));
  map.needsUpdate = true;
  return new THREE.MeshLambertMaterial({ map });
}

// The coloured part of a picture-holding piece (a frame's canvas, a poster, a rug) shows its picture.
function picture(mesh, piece) {
  if (piece.image) mesh.material = surfaceMaterial({ image: piece.image }, 0xffffff);
  return mesh;
}

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
  rug: (g, W, D, H, c, piece) => g.add(picture(box(W, 0.02, D, c, 0, 0, 0), piece)),
  "computer-desk": (g, W, D, H, c, piece, clickable) => {
    g.add(box(W, H, 0.7, c, 0, 0, -D / 2 + 0.35));
    const monitor = box(0.8, 0.5, 0.06, 0x263238, 0, H + 0.06, -D / 2 + 0.2);
    const apps = (piece.functions || []).some((f) => f.startsWith("chat."));
    if (apps) { monitor.userData = { computer: piece.id }; clickable.push(monitor); }
    g.add(monitor);
    g.add(box(0.75, 0.42, 0.02, apps ? 0x81c784 : 0x4fc3f7, 0, H + 0.1, -D / 2 + 0.24));
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
  armchair: (g, W, D, H, c) => {
    g.add(box(W * 0.9, 0.42, D * 0.9, c, 0, 0, 0.02));
    g.add(box(W * 0.9, H - 0.42, 0.22, shade(c, 0.8), 0, 0.42, -D * 0.45 + 0.11));
    for (const side of [-1, 1]) g.add(box(0.16, 0.25, D * 0.8, shade(c, 0.85), side * (W * 0.45 - 0.08), 0.42, 0.05));
  },
  chair: (g, W, D, H, c) => {
    g.add(box(W * 0.8, 0.06, D * 0.8, c, 0, H - 0.06, 0));
    for (const [dx, dz] of [[-1, -1], [1, -1], [-1, 1], [1, 1]]) g.add(box(0.05, H - 0.06, 0.05, shade(c, 0.7), dx * W * 0.35, 0, dz * D * 0.35));
    g.add(box(W * 0.8, 0.45, 0.05, c, 0, H, -D * 0.38));
  },
  wardrobe: (g, W, D, H, c) => {
    g.add(box(W, H, D, c, 0, 0, 0));
    g.add(box(0.02, H * 0.9, 0.02, shade(c, 0.6), 0, H * 0.05, D / 2 + 0.01));
    for (const side of [-1, 1]) g.add(box(0.04, 0.25, 0.04, 0xd4af37, side * 0.12, H * 0.5, D / 2 + 0.03));
  },
  poster: (g, W, D, H, c, piece) => g.add(picture(box(W * 0.8, H, 0.03, c, 0, 0.9, -D / 2 + 0.05), piece)),
  table: (g, W, D, H, c) => g.add(box(W, H, D, c, 0, 0, 0)),
  frame: (g, W, D, H, c, piece) => {
    g.add(box(W * 0.8, H, 0.05, 0xffffff, 0, 0.95, -D / 2 + 0.05));
    g.add(picture(box(W * 0.66, H - 0.14, 0.02, c, 0, 1.02, -D / 2 + 0.08), piece));
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
  parcel: (g, W, D, H, c, piece, clickable) => {
    const body = box(W * 0.8, H, D * 0.8, c, 0, 0, 0);
    body.userData = { parcel: piece.id };
    clickable.push(body);
    g.add(body);
    g.add(box(W * 0.82, 0.02, 0.12, 0xe8d7b0, 0, H, 0));
    g.add(box(0.12, 0.02, D * 0.82, 0xe8d7b0, 0, H, 0));
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

const loader = new GLTFLoader();
const models = new Map(); // file -> Promise of its scene, loaded once

function loadModel(file) {
  if (!models.has(file)) {
    models.set(file, new Promise((ok, fail) => loader.load(`/api/home/models/${file}`, (gltf) => ok(gltf.scene), undefined, fail)));
  }
  return models.get(file);
}

// A model scaled to fit the footprint and height (keeping its proportions), on the floor, centred.
function fitModel(scene, W, D, H, model) {
  const inner = scene.clone(true);
  inner.rotation.y = (-(model.turn || 0) * Math.PI) / 180;
  const pivot = new THREE.Group();
  pivot.add(inner);
  pivot.updateMatrixWorld(true);
  let box = new THREE.Box3().setFromObject(pivot);
  const size = box.getSize(new THREE.Vector3());
  const k = Math.min(W / (size.x || 1), D / (size.z || 1), H > 0.05 ? H / (size.y || 1) : Infinity) * (model.scale || 1);
  pivot.scale.setScalar(Number.isFinite(k) ? k : 1);
  pivot.updateMatrixWorld(true);
  box = new THREE.Box3().setFromObject(pivot);
  const centre = box.getCenter(new THREE.Vector3());
  pivot.position.set(-centre.x, -box.min.y, -centre.z);
  return pivot;
}

function greyOut(g) {
  g.traverse((o) => {
    if (!o.material) return;
    o.material = Array.isArray(o.material) ? o.material.map((m) => m.clone()) : o.material.clone();
    for (const m of [].concat(o.material)) {
      if (!m.color) continue;
      const c = m.color, grey = c.r * 0.3 + c.g * 0.59 + c.b * 0.11;
      c.setRGB(grey, grey, grey);
    }
  });
}

function drawPiece(parent, rect, piece, type, clickable, available) {
  const g = new THREE.Group();
  g.userData.pieceId = piece.id;
  const [w, d] = type.size;
  const draw = TEMPLATES[type.template] || TEMPLATES.table;
  draw(g, w * CELL, d * CELL, type.height, piece.color || type.color, piece, clickable);
  // Furniture whose functions nothing can do right now (its tool is off) is drawn grey.
  const fns = piece.functions || [];
  const grey = fns.length && !fns.some((f) => available.has(f));
  if (grey) greyOut(g);
  if (type.model) {
    loadModel(type.model.file).then((scene) => {
      const keep = g.children.filter((o) => o.userData.diary || o.userData.parcel); // still clickable
      g.clear();
      g.add(fitModel(scene, w * CELL, d * CELL, type.height, type.model));
      for (const o of keep) { o.visible = false; g.add(o); }
      if (grey) greyOut(g);
    }).catch(() => { /* a model that won't load: the template stays */ });
  }
  const [tw, td] = turnedSize(type.size, piece.turn);
  g.rotation.y = (-piece.turn * Math.PI) / 180;
  g.position.set(rect.x + (piece.at[0] + tw / 2) * CELL, 0, rect.z + (piece.at[1] + td / 2) * CELL);
  parent.add(g);
  return g;
}

const FLOORS = { hall: 0xe9e4dc, bed: 0xf3e5d8, study: 0xe3ecef, fun: 0xece4f5, living: 0xf6ead7, kitchen: 0xe8f1e4, storage: 0xe6e2dd };
const WALL = 0xd9d4cc, WALL_H = 0.7;

function walls(g, r, look) {
  const segment = (w, h, d, x, y, z, length) => {
    const mesh = box(w, h, d, WALL, x, y, z);
    if (look) mesh.material = surfaceMaterial(look, WALL, length / (look.tile || 1), h / (look.tile || 1));
    return mesh;
  };
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
    const gaps = r.doors.filter((dp) => dp.wall === wall).map((dp) => [horizontal ? dp.x : dp.z, dp.w]).sort((a, b) => a[0] - b[0]);
    let start = horizontal ? x1 : z1;
    const end = horizontal ? x2 : z2;
    for (const gap of [...gaps, null]) {
      const gc = gap && gap[0], half = gap ? gap[1] / 2 : 0;
      const stop = gap === null ? end : gc - half;
      if (stop - start > 0.05) {
        const len = stop - start, mid = start + len / 2;
        g.add(horizontal ? segment(len, WALL_H, t, mid, 0, z1, len) : segment(t, WALL_H, len, x1, 0, mid, len));
      }
      if (gap !== null) start = gc + half;
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
    this.arrangeBtn = el("button", "home-arrange", "布置家具");
    this.arrangeBtn.type = "button";
    this.arrangeBtn.onclick = () => this.toggleArrange();
    this.arranging = false;
    this.selectedPiece = "";
    this.stage.append(this.labelLayer, this.banner, this.arrangeBtn);
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
    // Something soft to reflect: uploaded models' metal (glTF's default) would otherwise look black.
    this.scene.environment = new THREE.PMREMGenerator(this.renderer).fromScene(new RoomEnvironment(), 0.04).texture;
    this.scene.environmentIntensity = 0.6;
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
    this.furniture = new THREE.Group(); // one group per placed piece, by piece id
    this.scene.add(this.furniture);
    this.grid = new THREE.Group(); // the cells, shown while arranging
    this.grid.visible = false;
    this.scene.add(this.grid);
    this.highlight = null;
    this.people = new THREE.Group();
    this.scene.add(this.people);
    this.ray = new THREE.Raycaster();
    let down = null;
    const canvas = this.renderer.domElement;
    canvas.addEventListener("pointerdown", (e) => {
      down = [e.clientX, e.clientY];
      if (this.arranging) this.startDrag(e);
    });
    canvas.addEventListener("pointermove", (e) => this.drag && this.moveDrag(e));
    canvas.addEventListener("pointerup", (e) => {
      if (this.drag) this.endDrag();
      else if (down && Math.hypot(e.clientX - down[0], e.clientY - down[1]) < 5 && !this.arranging) this.click(e);
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
    let data;
    try {
      const res = await fetch("/api/home", { cache: "no-store" });
      if (!res.ok) throw new Error(String(res.status));
      data = await res.json();
    } catch {
      this.offline(true); // the service is not answering
      return;
    }
    this.offline(false);
    try {
      this.apply(data);
    } catch (e) {
      console.error("家没画出来：", e); // a drawing bug, not the service being down
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
    this.furniture.clear();
    this.grid.clear();
    this.clickable = [];
    this.L = layout(data);
    const L = this.L;
    const ground = new THREE.Mesh(new THREE.BoxGeometry(L.W - L.X0 + 6, 0.05, L.D - L.Z0 + 8), new THREE.MeshLambertMaterial({ color: 0xc8d5b9 }));
    ground.position.set((L.X0 + L.W) / 2, -0.09, (L.Z0 + L.D) / 2 + 1);
    this.house.add(ground);
    this.roomLabels = [];
    for (const room of data.rooms) {
      const r = L.rects[room.id];
      if (!r) continue;
      const hall = room.kind === "hall";
      const kind = room.id.startsWith("bed-") ? "bed" : hall ? "hall" : room.id;
      const tile = room.floor?.tile || 1;
      const look = surfaceMaterial(room.floor, FLOORS[kind] ?? 0xeeeeee, r.w / tile, r.d / tile);
      if ((data.unreachable || []).includes(room.id)) look.color.lerp(new THREE.Color(0xe57373), look.map ? 0.5 : 0.6);
      const floor = new THREE.Mesh(new THREE.BoxGeometry(r.w, hall ? 0.06 : 0.08, r.d), look);
      floor.position.set(r.x + r.w / 2, hall ? -0.03 : -0.04, r.z + r.d / 2);
      this.house.add(floor);
      if (!hall || room.walls) walls(this.house, r, room.walls); // hallways are open, like the old corridors
      const lines = [];
      for (let i = 0; i <= room.size[0]; i++) lines.push(r.x + i * CELL, 0.012, r.z, r.x + i * CELL, 0.012, r.z + r.d);
      for (let j = 0; j <= room.size[1]; j++) lines.push(r.x, 0.012, r.z + j * CELL, r.x + r.w, 0.012, r.z + j * CELL);
      const geo = new THREE.BufferGeometry();
      geo.setAttribute("position", new THREE.Float32BufferAttribute(lines, 3));
      this.grid.add(new THREE.LineSegments(geo, new THREE.LineBasicMaterial({ color: 0x9e9384, transparent: true, opacity: 0.45 })));
      if (!hall) this.roomLabels.push({ id: room.id, pos: new THREE.Vector3(r.x + r.w / 2, 0.2, r.z + r.d - 0.25) });
    }
    // Seats come from the furniture: each use of each placed piece is a spot someone can take.
    this.seats = {};
    this.screens = {};
    this.pieceAt = {}; // piece id -> its centre and the way its front faces, for standing in front of it
    for (const piece of data.pieces) {
      const type = data.types[piece.type], r = L.rects[piece.room];
      if (!type || !r) continue;
      drawPiece(this.furniture, r, piece, type, this.clickable, new Set(data.available || []));
      (type.uses || []).forEach((use, i) => {
        (this.seats[piece.room] ||= []).push({ key: `${piece.id}:${i}`, piece, type, ...useSpot(r, piece, type, use) });
      });
      const [tw, td] = turnedSize(type.size, piece.turn);
      const centre = { id: piece.id, room: piece.room, x: r.x + (piece.at[0] + tw / 2) * CELL, z: r.z + (piece.at[1] + td / 2) * CELL,
                       turn: piece.turn, reach: ((piece.turn % 180 ? tw : td) / 2) * CELL };
      this.pieceAt[piece.id] = centre;
      if (type.screen) (this.screens[piece.room] ||= []).push(centre);
    }
    this.walk = walkGrid(L, data);
    this.center = new THREE.Vector3((L.X0 + L.W) / 2, 0, (L.Z0 + L.D) / 2);
    if (!this.aimed) this.aimCamera(); // keep the viewer's own angle and zoom across redraws
    else this.resize(); // the house may have grown: fit it again
    this.showHighlight();
  }

  // ---- arranging: top-down, on the grid ----

  aimCamera() {
    if (!this.L) return;
    const span = Math.max(this.L.W - this.L.X0, this.L.D - this.L.Z0);
    this.controls.target.copy(this.center);
    if (this.arranging) {
      this.controls.minPolarAngle = 0;
      this.controls.maxPolarAngle = 0;
      this.controls.enableRotate = false;
      this.camera.position.set(this.center.x, span * 2, this.center.z + 0.001);
    } else {
      this.controls.minPolarAngle = Math.PI * 0.12;
      this.controls.maxPolarAngle = Math.PI * 0.42;
      this.controls.enableRotate = true;
      this.camera.position.set(this.center.x + span, span * 0.95, this.center.z + span);
    }
    this.camera.zoom = 1;
    this.camera.updateProjectionMatrix();
    this.aimed = true;
    this.resize();
  }

  toggleArrange() {
    this.arranging = !this.arranging;
    this.selectedPiece = "";
    this.selected = "";
    this.arrangeBtn.textContent = this.arranging ? "布置好了" : "布置家具";
    this.arrangeBtn.classList.toggle("on", this.arranging);
    this.grid.visible = this.arranging;
    this.aimCamera();
    this.showHighlight();
    this.renderPanel();
  }

  groundAt(e) {
    const rect = this.renderer.domElement.getBoundingClientRect();
    const mouse = new THREE.Vector2(((e.clientX - rect.left) / rect.width) * 2 - 1, -((e.clientY - rect.top) / rect.height) * 2 + 1);
    this.ray.setFromCamera(mouse, this.camera);
    return { mouse, point: this.ray.ray.intersectPlane(new THREE.Plane(new THREE.Vector3(0, 1, 0), 0), new THREE.Vector3()) };
  }

  startDrag(e) {
    this.groundAt(e);
    const hit = this.ray.intersectObjects(this.furniture.children, true)[0];
    let g = hit && hit.object;
    while (g && !g.userData.pieceId) g = g.parent;
    if (!g) return;
    const piece = this.data.pieces.find((p) => p.id === g.userData.pieceId);
    const type = piece && this.data.types[piece.type];
    if (!type) return;
    this.controls.enabled = false;
    this.drag = { group: g, piece, type, room: piece.room, at: [...piece.at] };
    this.selectedPiece = piece.id;
    this.showHighlight();
    this.renderPanel();
  }

  moveDrag(e) {
    const { point } = this.groundAt(e);
    if (!point) return;
    const r = Object.values(this.L.rects).find((q) => point.x >= q.x && point.x <= q.x + q.w && point.z >= q.z && point.z <= q.z + q.d);
    if (!r) return;
    const [tw, td] = turnedSize(this.drag.type.size, this.drag.piece.turn);
    const cols = r.room.size[0], rows = r.room.size[1];
    const at = [
      Math.max(0, Math.min(cols - tw, Math.round((point.x - r.x) / CELL - tw / 2))),
      Math.max(0, Math.min(rows - td, Math.round((point.z - r.z) / CELL - td / 2))),
    ];
    this.drag.room = r.room.id;
    this.drag.at = at;
    this.drag.group.position.set(r.x + (at[0] + tw / 2) * CELL, 0, r.z + (at[1] + td / 2) * CELL);
    this.showHighlight();
  }

  async endDrag() {
    const { piece, room, at } = this.drag;
    this.drag = null;
    this.controls.enabled = true;
    if (room !== piece.room || at[0] !== piece.at[0] || at[1] !== piece.at[1]) {
      try { await send("PUT", `/api/home/pieces/${piece.id}`, { room, at }); } catch (err) { say(err.message, true); }
      this.layoutKey = ""; // redraw from what was saved, which puts a refused piece back
    }
    await this.poll();
  }

  showHighlight() {
    if (this.highlight) { this.scene.remove(this.highlight); this.highlight = null; }
    const g = this.arranging && this.furniture.children.find((c) => c.userData.pieceId === this.selectedPiece);
    if (!g) return;
    this.highlight = new THREE.BoxHelper(g, 0xf0a020);
    this.scene.add(this.highlight);
  }

  // A small turning picture of a kind of furniture, for the editor.
  showPreview(box, type) {
    if (!this.preview) {
      const renderer = new THREE.WebGLRenderer({ antialias: true, alpha: true });
      renderer.setPixelRatio(Math.min(window.devicePixelRatio || 1, 2));
      const scene = new THREE.Scene();
      scene.add(new THREE.HemisphereLight(0xffffff, 0xb0a99f, 1.6));
      const sun = new THREE.DirectionalLight(0xffffff, 1.2);
      sun.position.set(-4, 8, 6);
      scene.add(sun);
      const camera = new THREE.OrthographicCamera(-2, 2, 1.5, -1.5, -50, 50);
      camera.position.set(4, 3.6, 4);
      camera.lookAt(0, 0.4, 0);
      const holder = new THREE.Group();
      scene.add(holder);
      this.preview = { renderer, scene, camera, holder };
      const spin = () => {
        const shown = this.preview.renderer.domElement.isConnected;
        if (!shown && this.preview.seen) { this.preview.spinning = this.preview.seen = false; return; } // dialog closed
        this.preview.seen ||= shown; // not on the page yet: the dialog opens right after this
        holder.rotation.y += 0.01;
        renderer.render(scene, camera);
        requestAnimationFrame(spin);
      };
      this.preview.spin = spin;
    }
    const { renderer, camera, holder } = this.preview;
    box.replaceChildren(renderer.domElement);
    renderer.setSize(240, 160, false);
    renderer.domElement.style.width = "240px";
    renderer.domElement.style.height = "160px";
    holder.clear();
    const t = { ...type, size: type.size.map((v) => Math.max(1, Number(v) || 1)), height: Number(type.height) || 0.5 };
    const g = drawPiece(holder, { x: 0, z: 0 }, { id: "preview", at: [0, 0], turn: 0, color: null, functions: [] }, t, [], new Set());
    const [w, d] = t.size;
    g.position.set(0, 0, 0);
    const reach = Math.max(w * CELL, d * CELL, t.height) * 0.9 + 0.3;
    camera.left = -reach * 1.5; camera.right = reach * 1.5; camera.top = reach; camera.bottom = -reach;
    camera.updateProjectionMatrix();
    if (!this.preview.spinning) { this.preview.spinning = true; this.preview.spin(); }
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
    if (this.drag) return; // the redraw would snatch the piece from under the pointer
    this.data = data;
    const key = JSON.stringify([data.rooms, data.doors, data.unreachable, data.pieces, data.available, data.types]);
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
        const go = (a, ra, rb, b) => walkPath(this.walk, a, b) || route(L, ra, rb, a, b);
        if (out) pts = [...go(fig.root.position, from, L.frontRoom, L.frontInside), L.front, L.outside];
        else if (from === "out") {
          fig.root.visible = true;
          fig.root.position.set(L.outside.x, 0, L.outside.z);
          pts = [L.front, ...go(L.frontInside, L.frontRoom, person.room, spot)];
        } else pts = go(fig.root.position, from, person.room, spot);
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
      for (const x of [this.L.X0 - 1, this.L.W + 1]) for (const z of [this.L.Z0 - 1, this.L.D + 3]) for (const y of [0, 1.5]) {
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
    const shut = new Set(this.data.unreachable || []);
    const names = Object.fromEntries(this.data.rooms.map((r) => [r.id, shut.has(r.id) ? `${r.name}（进不去）` : r.name]));
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
    const hit = hits.find((h) => h.object.userData.diary || h.object.userData.person || h.object.userData.parcel
      || h.object.userData.computer);
    if (!hit) return;
    if (hit.object.userData.computer) { openTelegram(this); return; }
    if (hit.object.userData.parcel) { this.selectedPiece = hit.object.userData.parcel; this.selected = ""; this.renderPanel(); return; }
    if (hit.object.userData.diary) this.openDiary(hit.object.userData.diary);
    else { this.selected = hit.object.userData.person; this.renderPanel(); }
  }

  // ---- the side panel ----

  renderPanel() {
    const d = this.data;
    if (!d) return;
    const focus = document.activeElement;
    if (focus && this.panel.contains(focus) && /INPUT|SELECT/.test(focus.tagName)) return; // editing a look
    if (this.arranging) {
      // Only when the house changed: a rebuild every poll would drop what is picked in the panel.
      const key = JSON.stringify([d.rooms, d.pieces, d.types, d.available, this.selectedPiece]);
      if (key !== this.editorKey) {
        this.editorKey = key;
        this.panel.replaceChildren(...renderEditor(this));
      }
      return;
    }
    this.editorKey = "";
    const rows = d.people.map((p) => {
      const row = el("button", "home-row" + (this.selected === p.id ? " selected" : ""));
      row.type = "button";
      row.onclick = () => { this.selected = this.selected === p.id ? "" : p.id; this.selectedPiece = ""; this.renderPanel(); };
      const dot = el("span", "home-dot");
      dot.style.background = p.look.color;
      const stale = !p.active && !["sleep", "out"].includes(p.verb) && d.now - p.since > STALE_SECONDS;
      const text = el("span", "home-row-text");
      text.append(el("b", "", p.name), el("span", "muted", ` ${p.room_name} · ${stale ? "好久没动了" : p.words}`));
      const when = el("span", "home-when muted", (p.active ? "从 " : "") + clock(p.since));
      row.append(dot, text, when);
      return row;
    });
    const shut = (d.unreachable || []).map((id) => d.rooms.find((r) => r.id === id)?.name || id);
    const warning = shut.length ? el("p", "home-warn small",
      `${shut.join("、")}没有门通到大门，成员进不去，也不会被安排去那里。要给它开一扇门。`) : null;
    const list = el("div", "home-list");
    list.append(...rows);
    const detail = el("div", "home-detail");
    const p = d.people.find((x) => x.id === this.selected);
    const parcel = !p && d.pieces.find((x) => x.id === this.selectedPiece);
    if (parcel) {
      detail.append(el("h3", "", parcel.name || "纸箱"),
        el("p", "muted small", "新接的工具还没连到家里的功能。到「工具」页面，在这个工具的「家里的功能」里选一个，纸箱就拆开了：成员用它时会走到挂着那个功能的家具跟前。"));
      const go = el("button", "", "去工具页");
      go.onclick = () => { location.hash = "#tools"; document.querySelector('.tabs button[data-tab="tools"]')?.click(); };
      detail.append(go);
    } else if (p) {
      detail.append(el("h3", "", p.name));
      detail.append(el("p", "", `${p.room_name}，${p.active ? "正在" : "最近"}：${p.detail || p.words}`));
      if (p.peer) detail.append(el("p", "muted small", `和 ${d.people.find((x) => x.id === p.peer)?.name || p.peer} 有关`));
      const diary = el("button", "", "翻她的日记本");
      diary.onclick = () => this.openDiary(p.id);
      detail.append(diary, this.lookEditor(p));
    } else {
      detail.append(el("p", "muted small", "点一个人看她在干什么；点卧室桌上的红本子翻日记。拖动旋转，滚轮缩放。"));
    }
    this.panel.replaceChildren(...[el("h2", "", "家里"), warning, list, detail].filter(Boolean));
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

export { layout, walkGrid, walkPath, route }; // for checking walks from the browser console
