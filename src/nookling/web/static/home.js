// The 「家」 tab: an isometric 3D house where members walk to the room of what they are really doing.
// Positions come only from /api/home; walking between rooms is drawn here and never stored.

import * as THREE from "three";
import { OrbitControls } from "/static/vendor/OrbitControls.js";
import { GLTFLoader } from "/static/vendor/GLTFLoader.js";
import { RoomEnvironment } from "/static/vendor/RoomEnvironment.js";
import { openTelegram, renderEditor, renderShelf, say, send } from "/static/home-edit.js";

const POLL_MS = 5000;
const FAST_POLL_MS = 1500; // while a talk at home is going on
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
const STOREY = 3; // units from one floor to the next
// A window this narrow has the side panel below the picture: the furniture shelf is then a strip over the picture.
const NARROW = "(max-width: 860px)";
const near = (a, b) => Math.abs(a - b) < 1e-6;

function layout(data, level = 0) {
  const rects = {};
  for (const room of data.rooms) {
    if (!room.at || (room.level ?? 0) !== level) continue;
    rects[room.id] = { x: room.at[0] * CELL, z: room.at[1] * CELL, w: room.size[0] * CELL, d: room.size[1] * CELL, room };
  }
  const all = Object.values(rects);
  if (!all.length) return null; // a floor with no rooms yet
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
  if (!frontDoor && level > 0) return { W, D, X0, Z0, rects, doors, level }; // the front door is downstairs
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

function walkGrid(L, data, stairwells = []) {
  const rooms = new Map(), blocked = new Map(), passes = new Set();
  for (const k of stairwells) blocked.set(k, "stairwell");
  for (const r of Object.values(L.rects)) {
    const [ax, az] = r.room.at;
    for (let i = 0; i < r.room.size[0]; i++) for (let j = 0; j < r.room.size[1]; j++) rooms.set(cellKey(ax + i, az + j), r.room.id);
  }
  for (const piece of data.pieces) {
    const type = data.types[piece.type], r = L.rects[piece.room];
    if (!type || !type.blocks || !r || piece.wall) continue;
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
    const lift = piece.wall ? 0 : 0.55, z = piece.wall ? -D / 2 + 0.06 : -0.05; // on a wall: the screen alone, flat to it
    if (!piece.wall) g.add(box(W * 0.87, 0.5, D, 0x5d4037, 0, 0, 0));
    g.add(box(W * 0.8, 1.3, 0.1, c, 0, lift, z));
    g.add(box(W * 0.75, 1.15, 0.02, 0x7e57c2, 0, lift + 0.07, z + 0.06));
    if (!piece.wall && (piece.functions || []).includes("game.play")) g.add(box(0.5, 0.12, 0.3, 0xeeeeee, W * 0.3, 0.5, 0.05)); // the console on its stand
  },
  sofa: (g, W, D, H, c) => { // a dark backrest at the back, arms at the sides, cushions in front
    const arm = Math.min(0.18, W * 0.1), back = Math.min(0.28, D * 0.3), inner = W - 2 * arm;
    g.add(box(W, 0.22, D, shade(c, 0.75), 0, 0, 0));
    const seats = Math.max(1, Math.round(inner / 0.9)), sw = inner / seats;
    for (let i = 0; i < seats; i++) g.add(box(sw - 0.05, 0.2, D - back - 0.06, c, -inner / 2 + sw * (i + 0.5), 0.22, back / 2 + 0.01));
    g.add(box(W, H - 0.22, back, shade(c, 0.6), 0, 0.22, -D / 2 + back / 2));
    for (const side of [-1, 1]) g.add(box(arm, 0.42, D - back, shade(c, 0.8), side * (W / 2 - arm / 2), 0.22, back / 2));
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
  poster: (g, W, D, H, c, piece) => g.add(picture(box(W * 0.8, H, 0.03, c, 0, 0, -D / 2 + 0.02), piece)),
  clock: (g, W, D, H, c) => {
    const r = Math.min(W, H) / 2;
    const face = new THREE.Mesh(new THREE.CylinderGeometry(r, r, 0.05, 28), new THREE.MeshLambertMaterial({ color: c }));
    face.rotation.x = Math.PI / 2;
    face.position.set(0, r, -D / 2 + 0.03);
    g.add(face);
    const dial = new THREE.Mesh(new THREE.CylinderGeometry(r * 0.85, r * 0.85, 0.02, 28), new THREE.MeshLambertMaterial({ color: 0xfafafa }));
    dial.rotation.x = Math.PI / 2;
    dial.position.set(0, r, -D / 2 + 0.06);
    g.add(dial);
    g.add(box(0.03, r * 0.6, 0.01, 0x263238, 0, r, -D / 2 + 0.075));
    const minute = box(0.02, r * 0.75, 0.01, 0x263238, 0, 0, 0);
    minute.rotation.z = -Math.PI / 3;
    minute.position.set(r * 0.32, r + r * 0.18, -D / 2 + 0.08);
    g.add(minute);
  },
  "wall-shelf": (g, W, D, H, c) => {
    g.add(box(W, 0.05, D * 0.6, c, 0, 0, -D / 2 + D * 0.3));
    for (const side of [-1, 1]) g.add(box(0.04, 0.12, D * 0.5, shade(c, 0.7), side * (W / 2 - 0.15), -0.12, -D / 2 + D * 0.25));
    const things = [[0x5c6bc0, 0.22], [0xef9a9a, 0.18], [0x81c784, 0.25], [0xffcc80, 0.15]];
    things.forEach(([color, h], i) => g.add(box(0.07, h, D * 0.4, color, -W / 2 + 0.3 + i * 0.09, 0.05, -D / 2 + D * 0.25)));
    g.add(cylinder(0.08, 0.16, 0x8d6e63, W / 2 - 0.4, 0.05, -D / 2 + D * 0.3));
  },
  table: (g, W, D, H, c) => g.add(box(W, H, D, c, 0, 0, 0)),
  frame: (g, W, D, H, c, piece) => {
    g.add(box(W * 0.8, H, 0.05, 0xffffff, 0, 0, -D / 2 + 0.03));
    g.add(picture(box(W * 0.66, H - 0.14, 0.02, c, 0, 0.07, -D / 2 + 0.06), piece));
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
  stairs: (g, W, D, H, c) => {
    const n = 12;
    for (let i = 0; i < n; i++) {
      g.add(box(W, (H * (i + 1)) / n, D / n, i % 2 ? shade(c, 0.9) : c, 0, 0, D / 2 - ((i + 0.5) * D) / n));
    }
    for (const side of [-1, 1]) g.add(box(0.05, 0.9, D, 0x5d4037, side * (W / 2 - 0.03), H * 0.5, 0));
  },
  boxes: (g, W, D, H, c) => {
    [[-0.4, -0.4, 0.6], [0.4, -0.4, 0.7], [-0.4, 0.4, 0.8]].forEach(([dx, dz, h]) => g.add(box(0.7, h, 0.7, c, dx, 0, dz)));
    g.add(box(0.7, 0.5, 0.7, shade(c, 0.85), -0.4, 0.6, -0.4));
  },
};

const turnedSize = (size, turn) => (turn === 90 || turn === 270 ? [size[1], size[0]] : [size[0], size[1]]);

// A piece's cells, counted from its room's corner.
function pieceCells(piece, type) {
  const [w, d] = turnedSize(type.size, piece.turn), out = [];
  for (let i = 0; i < w; i++) for (let j = 0; j < d; j++) out.push([piece.at[0] + i, piece.at[1] + j]);
  return out;
}

// The cells in front of a room's doors that furniture in the way must keep free (as furniture.door_cells).
const DOOR_WIDTH = 3, DOOR_CLEAR = 2;
function doorCells(r) {
  const [w, d] = r.room.size, out = new Set();
  for (const door of r.doors) {
    const along = ["front", "back"].includes(door.wall) ? w : d;
    const centre = ["front", "back"].includes(door.wall) ? (door.x - r.x) / CELL : (door.z - r.z) / CELL;
    for (let i = 0; i < along; i++) {
      if (!(i < centre + DOOR_WIDTH / 2 && i + 1 > centre - DOOR_WIDTH / 2)) continue;
      for (let k = 0; k < DOOR_CLEAR; k++) {
        out.add(cellKey(...{ front: [i, d - 1 - k], back: [i, k], left: [k, i], right: [w - 1 - k, i] }[door.wall]));
      }
    }
  }
  return out;
}

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

// A piece hung on a wall: its middle on the floor plan (against the wall's inner face) and the way it faces.
const WALL_FACE = 0.12; // the walls' inner face, from the room's edge
const WALL_TURN = { back: 0, right: 90, front: 180, left: 270 };
const WALL_Y = { frame: 1.0, poster: 0.9, clock: 1.8, "wall-shelf": 1.4, tv: 0.9 }; // how high, unless said (as furniture.WALL_Y)
function wallSpot(r, piece, type) {
  const along = (piece.along + type.size[0] / 2) * CELL, out = WALL_FACE + (type.size[1] * CELL) / 2;
  const [x, z] = { back: [r.x + along, r.z + out], front: [r.x + along, r.z + r.d - out],
                   left: [r.x + out, r.z + along], right: [r.x + r.w - out, r.z + along] }[piece.wall];
  return { x, z, turn: WALL_TURN[piece.wall] };
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
  if (piece.wall) {
    const spot = wallSpot(rect, piece, type);
    g.rotation.y = (-spot.turn * Math.PI) / 180;
    g.position.set(spot.x, piece.y, spot.z);
  } else {
    const [tw, td] = turnedSize(type.size, piece.turn);
    g.rotation.y = (-piece.turn * Math.PI) / 180;
    g.position.set(rect.x + (piece.at[0] + tw / 2) * CELL, 0, rect.z + (piece.at[1] + td / 2) * CELL);
  }
  parent.add(g);
  return g;
}

const FLOORS = { hall: 0xe9e4dc, bed: 0xf3e5d8, study: 0xe3ecef, fun: 0xece4f5, living: 0xf6ead7, kitchen: 0xe8f1e4, storage: 0xe6e2dd };
const WALL = 0xd9d4cc, WALL_H = 0.7, WALL_TALL = 2.6, LINTEL = 2.1;
const OUTWARD = { back: [0, -1], front: [0, 1], left: [-1, 0], right: [1, 0] };

// A room's walls, a hair inside the room (neighbours' walls never share a face, so no flicker). Each wall is
// a low part, always drawn, and above it the rest of a full-height wall (a lintel over a door), which
// showWalls hides, shows or makes see-through as the camera turns. Without `uppers`, only the low part.
// `rooms` (cell -> room) tells what is behind each stretch of wall: another room, or outside.
function walls(g, r, look, rooms, uppers) {
  const segment = (w, h, d, x, y, z, length) => {
    const mesh = box(w, h, d, WALL, x, y, z);
    if (look) mesh.material = surfaceMaterial(look, WALL, length / (look.tile || 1), h / (look.tile || 1));
    return mesh;
  };
  const inset = 0.06, t = 0.12;
  const [ax, az] = r.room.at, [cols, rows] = r.room.size;
  const sides = { // cells along the wall, and the cell just beyond the i-th of them
    back: [cols, (i) => [ax + i, az - 1]], front: [cols, (i) => [ax + i, az + rows]],
    left: [rows, (i) => [ax - 1, az + i]], right: [rows, (i) => [ax + cols, az + i]],
  };
  for (const [wall, [n, beyond]] of Object.entries(sides)) {
    const horizontal = wall === "back" || wall === "front";
    const gaps = r.doors.filter((dp) => dp.wall === wall).map((dp) => {
      const c = (horizontal ? dp.x - r.x : dp.z - r.z) / CELL, half = dp.w / CELL / 2;
      return [Math.round(c - half), Math.round(c + half)];
    });
    const kind = (i) => `${gaps.some(([a, b]) => i >= a && i < b) ? "door" : "wall"}:${rooms?.get(cellKey(...beyond(i))) ? "inside" : "outside"}`;
    for (let i = 0; i < n;) { // stretches of the wall that are alike: wall or door, a room or outside behind
      const k = kind(i);
      let j = i + 1;
      while (j < n && kind(j) === k) j++;
      const from = Math.max(i * CELL, inset), to = Math.min(j * CELL, n * CELL - inset), len = to - from, mid = (from + to) / 2;
      const [x, z] = horizontal ? [r.x + mid, wall === "back" ? r.z + inset : r.z + r.d - inset]
                                : [wall === "left" ? r.x + inset : r.x + r.w - inset, r.z + mid];
      const part = (y, h) => segment(horizontal ? len : t, h, horizontal ? t : len, x, y, z, len);
      const [what, behind] = k.split(":");
      if (len > 0.05 && what === "wall") g.add(part(0, WALL_H));
      if (len > 0.05 && uppers) {
        const y = what === "door" ? LINTEL : WALL_H, upper = part(y, WALL_TALL - y);
        if (behind === "inside") { // another room behind: see-through, so it never hides that room
          upper.material.transparent = true;
          upper.material.opacity = 0.28;
          upper.material.depthWrite = false;
        }
        upper.userData.outward = OUTWARD[wall];
        uppers.push(upper);
        g.add(upper);
      }
      i = j;
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
    this.path = points.map((p) => new THREE.Vector3(p.x, p.y ?? this.root.position.y, p.z));
  }

  update(dt, t) {
    const pos = this.root.position;
    let walking = false;
    if (this.path.length) {
      const target = this.path[0];
      const delta = target.clone().sub(pos);
      const dist = delta.length();
      const step = WALK_SPEED * dt;
      if (dist <= step) {
        pos.copy(target);
        this.path.shift();
      } else {
        pos.add(delta.multiplyScalar(step / dist));
        if (Math.abs(delta.x) + Math.abs(delta.z) > 1e-6) this.root.rotation.y = Math.atan2(delta.x, delta.z);
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
    this.view = 0; // the floor being looked at
    this.shelf = el("div", "home-shelf"); // the kinds of furniture, to drag into the house while arranging
    this.shelf.hidden = true;
    this.shelfTab = el("button", "home-shelf-tab", "家具 ›");
    this.shelfTab.type = "button";
    this.shelfTab.hidden = true;
    this.shelfTab.onclick = () => this.toggleShelf(true);
    this.shelfOpen = true;
    this.stage.append(this.labelLayer, this.banner, this.arrangeBtn, this.shelf, this.shelfTab);
    // While arranging: R turns what is being dragged, or the selected piece (Shift+R the other way); Esc lets go.
    window.addEventListener("keydown", (e) => {
      if (!this.arranging || /INPUT|SELECT|TEXTAREA/.test(e.target.tagName)) return;
      if (e.key === "Escape") { this.escape(); e.preventDefault(); return; }
      if (e.key !== "r" && e.key !== "R") return;
      const step = e.shiftKey ? -90 : 90;
      const drag = this.cardDrag?.moved ? this.cardDrag : this.drag;
      const selected = this.data?.pieces.find((p) => p.id === this.selectedPiece);
      if (drag && drag.last) {
        if (drag.target || (drag === this.cardDrag && drag.piece?.wall)) return; // on a wall: it always faces the room
        drag.turn = (drag.turn + step + 360) % 360;
        if (drag === this.cardDrag) this.moveCard(drag.last); else this.moveDrag(drag.last);
      } else if (selected && !this.planMode) this.turnPiece(selected, step);
      else return;
      e.preventDefault();
    });
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
    this.planMarks = new THREE.Group(); // doors and the room being shaped, in the floor-plan mode
    this.planMarks.visible = false;
    this.scene.add(this.planMarks);
    this.planMode = false;
    this.highlight = null;
    this.people = new THREE.Group();
    this.scene.add(this.people);
    this.ray = new THREE.Raycaster();
    let down = null, right = null;
    const canvas = this.renderer.domElement;
    canvas.addEventListener("contextmenu", (e) => this.arranging && e.preventDefault()); // right-click turns furniture
    canvas.addEventListener("pointerdown", (e) => {
      down = [e.clientX, e.clientY];
      if (e.button === 2) { right = this.arranging && !this.planMode ? this.pieceUnder(e) : null; return; }
      if (e.button !== 0) return;
      if (this.arranging && this.planMode) this.planDown(e);
      else if (this.arranging && this.armed) this.placeArmed(e);
      else if (this.arranging && !e.shiftKey) this.startDrag(e); // Shift+drag pans the view instead
    });
    canvas.addEventListener("pointermove", (e) => {
      if (this.plan) this.planMove(e);
      else if (this.drag) this.moveDrag(e);
      else if (this.arranging && !this.planMode && !e.buttons) this.hover(e);
    });
    canvas.addEventListener("pointerup", (e) => {
      const still = down && Math.hypot(e.clientX - down[0], e.clientY - down[1]) < 5;
      down = null;
      if (e.button === 2) { // a right-click on a piece: turn it (Shift: the other way)
        if (right && still) {
          this.selectedPiece = right.id;
          this.turnPiece(right, e.shiftKey ? -90 : 90);
        }
        right = null;
        return;
      }
      if (this.plan) this.planUp(e);
      else if (this.drag) this.endDrag();
      else if (still && !this.arranging) this.click(e);
      else if (still && !this.planMode && !this.armed && this.selectedPiece) this.escape(); // a click on empty floor
    });
    canvas.addEventListener("pointerleave", () => this.hover(null));
    new ResizeObserver(() => this.resize()).observe(this.stage);
    this.lastFrame = performance.now();
  }

  start() {
    if (this.running) return;
    this.running = true;
    const tick = () => { // quicker while someone at home is answering, or the owner just spoke
      if (!this.running) return;
      this.poll();
      this.timer = setTimeout(tick, Date.now() < (this.fastUntil || 0) ? FAST_POLL_MS : POLL_MS);
    };
    tick();
    if (this.renderer) this.frame();
  }

  stop() {
    this.running = false;
    clearTimeout(this.timer);
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
    this.planMarks.clear();
    this.clickable = [];
    this.levels = (data.floors || [{ level: 0 }]).map((f) => f.level).sort((a, b) => a - b);
    if (!this.levels.includes(this.view)) this.view = 0;
    this.Ls = {};
    for (const lv of this.levels) { const Lx = layout(data, lv); if (Lx) this.Ls[lv] = Lx; }
    this.L = this.Ls[this.view] || { rects: {}, doors: [] }; // a new floor has no rooms yet
    this.L0 = this.Ls[0];
    this.rects = Object.assign({}, ...Object.values(this.Ls).map((Lx) => Lx.rects));
    this.levelOf = Object.fromEntries(data.rooms.map((r) => [r.id, r.level ?? 0]));
    const all = Object.values(this.Ls);
    this.bounds = { X0: Math.min(...all.map((b) => b.X0)), Z0: Math.min(...all.map((b) => b.Z0)),
                    X1: Math.max(...all.map((b) => b.W)), Z1: Math.max(...all.map((b) => b.D)) };
    const B = this.bounds;
    const ground = new THREE.Mesh(new THREE.BoxGeometry(B.X1 - B.X0 + 6, 0.05, B.Z1 - B.Z0 + 8), new THREE.MeshLambertMaterial({ color: 0xc8d5b9 }));
    ground.position.set((B.X0 + B.X1) / 2, -0.09, (B.Z0 + B.Z1) / 2 + 1);
    this.house.add(ground);
    // Stairs: where she gets on downstairs, the steps, where she gets off upstairs.
    this.stairs = [];
    const wells = {}; // level -> cells where stairs from below come up
    for (const piece of data.pieces) {
      const type = data.types[piece.type], r = this.rects[piece.room];
      if (!type || type.template !== "stairs" || !r) continue;
      const lv = this.levelOf[piece.room];
      const [tw, td] = turnedSize(type.size, piece.turn);
      const c = { x: r.x + (piece.at[0] + tw / 2) * CELL, z: r.z + (piece.at[1] + td / 2) * CELL };
      const a = (piece.turn * Math.PI) / 180, dir = { x: -Math.sin(a), z: Math.cos(a) }, half = (type.size[1] * CELL) / 2;
      const at = (k, level) => ({ x: c.x + dir.x * k, z: c.z + dir.z * k, y: level * STOREY });
      const top = at(-(half + 0.5), lv + 1);
      const upper = Object.values(this.Ls[lv + 1]?.rects || {}).find((q) => top.x > q.x && top.x < q.x + q.w && top.z > q.z && top.z < q.z + q.d);
      this.stairs.push({ lower: lv, room: piece.room, upperRoom: upper?.room.id, bottom: at(half + 0.5, lv), top,
                         climb: [at(half, lv), at(-half, lv + 1)] });
      const [ax, az] = r.room.at;
      for (let i = 0; i < tw; i++) for (let j = 0; j < td; j++) (wells[lv + 1] ||= []).push(cellKey(ax + piece.at[0] + i, az + piece.at[1] + j));
    }
    this.roomLabels = [];
    this.seats = {};
    this.screens = {};
    this.pieceAt = {}; // piece id -> its centre and the way its front faces, for standing in front of it
    this.furnitureLevels = {};
    this.walks = {};
    for (const lv of this.levels) if (this.Ls[lv]) this.walks[lv] = walkGrid(this.Ls[lv], data, wells[lv] || []);
    this.hoverId = "";
    if (this.hoverBox) { this.scene.remove(this.hoverBox); this.hoverBox = null; }
    this.uppers = []; // the tall parts of the walls of the floor being viewed
    this.wallKey = "";
    for (const lv of this.levels) {
      const Lx = this.Ls[lv];
      if (!Lx) continue;
      const base = lv * STOREY, faint = lv < this.view;
      const g = new THREE.Group(), fg = new THREE.Group();
      g.position.y = base;
      fg.position.y = base;
      g.visible = fg.visible = lv <= this.view; // floors above the one being viewed are hidden
      this.house.add(g);
      this.furniture.add(fg);
      this.furnitureLevels[lv] = fg;
      for (const room of data.rooms) {
        const r = Lx.rects[room.id];
        if (!r) continue;
        const hall = room.kind === "hall";
        const kind = room.id.startsWith("bed-") ? "bed" : hall ? "hall" : room.id;
        const tile = room.floor?.tile || 1;
        const look = surfaceMaterial(room.floor, FLOORS[kind] ?? 0xeeeeee, r.w / tile, r.d / tile);
        if ((data.unreachable || []).includes(room.id)) look.color.lerp(new THREE.Color(0xe57373), look.map ? 0.5 : 0.6);
        const floor = new THREE.Mesh(new THREE.BoxGeometry(r.w, hall ? 0.06 : 0.08, r.d), look);
        floor.position.set(r.x + r.w / 2, hall ? -0.03 : -0.04, r.z + r.d / 2);
        g.add(floor);
        if (!hall || room.walls) { // hallways are open, like the old corridors
          walls(g, r, room.walls, this.walks[lv].rooms, lv === this.view ? this.uppers : null); // lower floors: low walls
        }
        if (lv === this.view) {
          const lines = [];
          for (let i = 0; i <= room.size[0]; i++) lines.push(r.x + i * CELL, base + 0.012, r.z, r.x + i * CELL, base + 0.012, r.z + r.d);
          for (let j = 0; j <= room.size[1]; j++) lines.push(r.x, base + 0.012, r.z + j * CELL, r.x + r.w, base + 0.012, r.z + j * CELL);
          const geo = new THREE.BufferGeometry();
          geo.setAttribute("position", new THREE.Float32BufferAttribute(lines, 3));
          this.grid.add(new THREE.LineSegments(geo, new THREE.LineBasicMaterial({ color: 0x9e9384, transparent: true, opacity: 0.45 })));
          if (!hall) this.roomLabels.push({ id: room.id, pos: new THREE.Vector3(r.x + r.w / 2, base + 0.2, r.z + r.d - 0.25) });
        }
      }
      for (const k of wells[lv] || []) { // the stairwell: an opening in the floor
        const [x, z] = k.split(",").map(Number);
        const hole = new THREE.Mesh(new THREE.BoxGeometry(CELL, 0.1, CELL), new THREE.MeshLambertMaterial({ color: 0x5d4f45 }));
        hole.position.set((x + 0.5) * CELL, 0.0, (z + 0.5) * CELL);
        g.add(hole);
      }
      // Seats come from the furniture: each use of each placed piece is a spot someone can take.
      for (const piece of data.pieces) {
        const type = data.types[piece.type], r = Lx.rects[piece.room];
        if (!type || !r) continue;
        drawPiece(fg, r, piece, type, lv === this.view ? this.clickable : [], new Set(data.available || []));
        if (!piece.wall) (type.uses || []).forEach((use, i) => {
          const spot = useSpot(r, piece, type, use);
          (this.seats[piece.room] ||= []).push({ key: `${piece.id}:${i}`, piece, type, ...spot, y: spot.y + base });
        });
        const [tw, td] = piece.wall ? [0, 0] : turnedSize(type.size, piece.turn);
        const centre = piece.wall
          ? { id: piece.id, room: piece.room, ...wallSpot(r, piece, type), y: base, lift: piece.y, reach: (type.size[1] * CELL) / 2 }
          : { id: piece.id, room: piece.room, x: r.x + (piece.at[0] + tw / 2) * CELL, z: r.z + (piece.at[1] + td / 2) * CELL,
              y: base, turn: piece.turn, reach: ((piece.turn % 180 ? tw : td) / 2) * CELL };
        this.pieceAt[piece.id] = centre;
        if (type.screen) (this.screens[piece.room] ||= []).push(centre);
      }
      if (faint) { // the floors below the one being viewed: see-through, behind it
        for (const grp of [g, fg]) grp.traverse((o) => {
          if (!o.material) return;
          o.material = Array.isArray(o.material) ? o.material.map((m) => m.clone()) : o.material.clone();
          for (const m of [].concat(o.material)) { m.transparent = true; m.opacity = 0.18; m.depthWrite = false; }
        });
      }
    }
    this.wells = wells;
    if (this.L) for (const d of this.L.doors) { // a mark on every door of the floor being viewed, for the floor-plan mode
      const along = d.line === "z";
      const mark = new THREE.Mesh(new THREE.BoxGeometry(along ? d.w : 0.3, 0.08, along ? 0.3 : d.w),
        new THREE.MeshBasicMaterial({ color: d.outside ? 0x43a047 : d.id === this.selectedDoor ? 0xf0a020 : 0xff8f00 }));
      mark.position.set(d.x, this.view * STOREY + 0.06, d.z);
      mark.userData = { door: d.id };
      this.planMarks.add(mark);
    }
    this.center = new THREE.Vector3((B.X0 + B.X1) / 2, this.view * STOREY, (B.Z0 + B.Z1) / 2);
    if (!this.aimed) this.aimCamera(); // keep the viewer's own angle and zoom across redraws
    else { // keep the viewer's angle, raised or lowered to the floor being viewed; the house may have grown: fit it again
      const dy = this.center.y - this.controls.target.y;
      this.camera.position.y += dy;
      this.controls.target.y += dy;
      this.resize();
    }
    this.showHighlight();
    this.renderFloors();
    this.showWalls();
  }

  // The tall part of each wall, by where the camera is: a wall facing the camera stays low so the room is seen
  // over it; one facing away is full height (see-through when another room is behind it, from walls()).
  // Seen from above (arranging), all walls are low.
  showWalls() {
    if (!this.uppers) return;
    const v = this.camera.position.clone().sub(this.controls.target).setY(0);
    const len = v.length() || 1, side = (c) => (c / len > 0.2 ? 1 : c / len < -0.2 ? -1 : 0);
    const [sx, sz] = [side(v.x), side(v.z)], top = this.arranging || (!sx && !sz);
    const key = top ? "top" : `${sx},${sz}`;
    if (key === this.wallKey) return;
    this.wallKey = key;
    for (const m of this.uppers) {
      const [nx, nz] = m.userData.outward;
      m.visible = !top && nx * sx + nz * sz < 0;
    }
  }

  // ---- floors: which one is being looked at ----

  renderFloors() {
    if (!this.floorBar) {
      this.floorBar = el("div", "home-floors");
      this.stage.append(this.floorBar);
    }
    const floors = this.data?.floors || [];
    this.floorBar.hidden = floors.length < 2;
    this.floorBar.replaceChildren(...[...floors].sort((a, b) => b.level - a.level).map((f) => {
      const b = el("button", f.level === this.view ? "on" : "", f.name);
      b.type = "button";
      b.onclick = () => this.setView(f.level);
      return b;
    }));
  }

  setView(level) {
    if (level === this.view) return;
    this.view = level;
    this.selectedRoom = this.selectedDoor = this.selectedPiece = "";
    this.layoutKey = "";
    this.editorKey = "";
    this.apply(this.data);
  }

  // Walking from one place to another, up or down stairs when they are on different floors.
  walkBetween(a, la, ra, b, lb, rb) {
    const flat = (level, p, rp, q, rq) => {
      const Lx = this.Ls[level];
      const pts = (Lx && (walkPath(this.walks[level], p, q) || route(Lx, rp, rq, p, q))) || [q];
      return pts.map((pt) => ({ x: pt.x, z: pt.z, y: pt.y ?? level * STOREY }));
    };
    const out = [];
    let pos = a, level = la, room = ra;
    for (let guard = 0; level !== lb && guard < 8; guard++) {
      const up = lb > level;
      const options = this.stairs.filter((st) => (up ? st.lower === level : st.lower === level - 1));
      if (!options.length) break; // no stairs: she just arrives
      const near = (st) => Math.hypot((up ? st.bottom : st.top).x - pos.x, (up ? st.bottom : st.top).z - pos.z);
      const st = options.sort((p, q) => near(p) - near(q))[0];
      out.push(...flat(level, pos, room, up ? st.bottom : st.top, up ? st.room : st.upperRoom));
      out.push(...(up ? st.climb : [...st.climb].reverse()), up ? st.top : st.bottom);
      pos = up ? st.top : st.bottom;
      level += up ? 1 : -1;
      room = up ? st.upperRoom : st.room;
    }
    out.push(...flat(level, pos, room, b, rb));
    return out;
  }

  // ---- arranging: top-down, on the grid ----

  aimCamera() {
    if (!this.bounds) return;
    const span = Math.max(this.bounds.X1 - this.bounds.X0, this.bounds.Z1 - this.bounds.Z0);
    this.controls.target.copy(this.center);
    if (this.arranging && (this.planMode || this.arrangeTop)) {
      this.controls.minPolarAngle = 0;
      this.controls.maxPolarAngle = 0;
      this.controls.enableRotate = false;
      this.camera.position.set(this.center.x, this.center.y + span * 2, this.center.z + 0.001);
    } else {
      this.controls.minPolarAngle = Math.PI * 0.12;
      this.controls.maxPolarAngle = Math.PI * 0.42;
      this.controls.enableRotate = true;
      this.camera.position.set(this.center.x + span, this.center.y + span * 0.95, this.center.z + span);
    }
    this.camera.zoom = 1;
    this.camera.updateProjectionMatrix();
    this.aimed = true;
    this.resize();
  }

  toggleArrange() {
    this.arranging = !this.arranging;
    this.armed = null;
    this.selectedPiece = "";
    this.selected = "";
    this.arrangeBtn.textContent = this.arranging ? "布置好了" : "布置家具";
    this.arrangeBtn.classList.toggle("on", this.arranging);
    this.grid.visible = this.arranging;
    this.planMode = false;
    this.planMarks.visible = false;
    this.hover(null);
    // while arranging the right button turns furniture, so it no longer pans (Shift+drag does)
    this.controls.mouseButtons = { LEFT: THREE.MOUSE.ROTATE, MIDDLE: THREE.MOUSE.DOLLY, RIGHT: this.arranging ? null : THREE.MOUSE.PAN };
    this.aimCamera();
    this.showHighlight();
    this.renderPanel();
    this.renderShelf();
  }

  // ---- the floor-plan mode: move rooms, drag their edges, doors, draw new rooms ----

  setArrangeTop(on) {
    this.arrangeTop = on;
    this.editorKey = "";
    this.aimCamera();
    this.renderPanel();
  }

  setPlanMode(on) {
    this.planMode = on;
    this.armed = null;
    this.hover(null);
    this.aimCamera();
    this.planMarks.visible = on;
    this.selectedPiece = "";
    this.selectedRoom = "";
    this.selectedDoor = "";
    this.showHighlight();
    this.editorKey = "";
    this.renderPanel();
    this.renderShelf();
  }

  // What is under the pointer: a door mark, a room's edge (to resize), inside a room (to move), or empty ground.
  planHit(e) {
    const { point } = this.groundAt(e);
    if (!point) return null;
    const mark = this.ray.intersectObjects(this.planMarks.children, false).find((h) => h.object.userData.door);
    if (mark) return { kind: "door", id: mark.object.userData.door, point };
    const tol = 0.35;
    let edge = null;
    for (const r of Object.values(this.L.rects)) {
      const inX = point.x > r.x - tol && point.x < r.x + r.w + tol, inZ = point.z > r.z - tol && point.z < r.z + r.d + tol;
      for (const [side, dist] of [["left", Math.abs(point.x - r.x)], ["right", Math.abs(point.x - r.x - r.w)],
                                  ["back", Math.abs(point.z - r.z)], ["front", Math.abs(point.z - r.z - r.d)]]) {
        const along = side === "left" || side === "right" ? inZ : inX;
        if (dist < tol && along && (!edge || dist < edge.dist)) edge = { kind: "edge", rect: r, side, dist, point };
      }
    }
    if (edge) return edge;
    const room = Object.values(this.L.rects).find((r) => point.x > r.x && point.x < r.x + r.w && point.z > r.z && point.z < r.z + r.d);
    return room ? { kind: "room", rect: room, point } : { kind: "ground", point };
  }

  planDown(e) {
    const hit = this.planHit(e);
    if (!hit) return;
    this.controls.enabled = false;
    this.plan = { hit, start: hit.point, moved: false };
  }

  planMove(e) {
    const { point } = this.groundAt(e);
    if (!point || !this.plan) return;
    const { hit, start } = this.plan;
    const cell = (v) => Math.round(v / CELL);
    const dx = cell(point.x - start.x), dz = cell(point.z - start.z);
    if (!dx && !dz && !this.plan.moved) return;
    this.plan.moved = true;
    let box = null; // the rectangle being shaped, in cells: [x, z, w, d]
    if (hit.kind === "room") {
      const [ax, az] = hit.rect.room.at, [w, d] = hit.rect.room.size;
      box = [ax + dx, az + dz, w, d];
    } else if (hit.kind === "edge") {
      let [x0, z0] = hit.rect.room.at, [x1, z1] = [x0 + hit.rect.room.size[0], z0 + hit.rect.room.size[1]];
      if (hit.side === "left") x0 = Math.min(x1 - 3, x0 + dx);
      if (hit.side === "right") x1 = Math.max(x0 + 3, x1 + dx);
      if (hit.side === "back") z0 = Math.min(z1 - 3, z0 + dz);
      if (hit.side === "front") z1 = Math.max(z0 + 3, z1 + dz);
      box = [x0, z0, x1 - x0, z1 - z0];
    } else if (hit.kind === "ground") {
      const ax = Math.floor(Math.min(start.x, point.x) / CELL), az = Math.floor(Math.min(start.z, point.z) / CELL);
      const bx = Math.ceil(Math.max(start.x, point.x) / CELL), bz = Math.ceil(Math.max(start.z, point.z) / CELL);
      box = [ax, az, Math.max(1, bx - ax), Math.max(1, bz - az)];
    }
    this.plan.box = box;
    this.showPlanBox(box);
  }

  showPlanBox(box) {
    if (this.planBox) { this.scene.remove(this.planBox); this.planBox = null; }
    if (!box) return;
    const [x, z, w, d] = box;
    this.planBox = new THREE.Mesh(new THREE.BoxGeometry(w * CELL, 0.04, d * CELL),
      new THREE.MeshBasicMaterial({ color: 0x3b6cf6, transparent: true, opacity: 0.35 }));
    this.planBox.position.set((x + w / 2) * CELL, (this.view || 0) * STOREY + 0.08, (z + d / 2) * CELL);
    this.scene.add(this.planBox);
  }

  async planUp(e) {
    const { hit, box, moved } = this.plan;
    this.plan = null;
    this.controls.enabled = true;
    this.showPlanBox(null);
    const done = async (fn) => {
      try { await fn(); } catch (err) { say(err.message, true); }
      this.layoutKey = "";
      await this.poll();
    };
    if (!moved) { // a click
      if (hit.kind === "door") { this.selectedDoor = hit.id; this.selectedRoom = ""; this.editorKey = ""; this.layoutKey = ""; await this.poll(); return; }
      if (hit.kind === "edge") return this.planDoor(hit, done);
      if (hit.kind === "room") { this.selectedRoom = hit.rect.room.id; this.selectedDoor = ""; this.editorKey = ""; this.renderPanel(); }
      return;
    }
    if (!box) return;
    const [x, z, w, d] = box;
    if (hit.kind === "room") return done(() => send("PUT", `/api/home/rooms/${hit.rect.room.id}`, { at: [x, z] }));
    if (hit.kind === "edge") return done(() => send("PUT", `/api/home/rooms/${hit.rect.room.id}`, { at: [x, z], size: [w, d] }));
    if (hit.kind === "ground") {
      if (w < 3 || d < 3) { say("至少拖出 3×3 格（一米半见方）", true); return; }
      const name = prompt("新房间叫什么？（只写「走廊」就是一条走廊）", "");
      if (name === null) return;
      const hall = name.trim() === "走廊" || name.trim() === "";
      return done(() => send("POST", "/api/home/rooms", { name: name.trim(), kind: hall ? "hall" : "room", size: [w, d], at: [x, z],
                                                          level: this.view || 0 }));
    }
  }

  // A click on a room's wall: a door there if another room shares it, or the front door if it is an outer wall.
  planDoor(hit, done) {
    const r = hit.rect, p = hit.point;
    const horizontal = hit.side === "back" || hit.side === "front";
    const along = Math.round((horizontal ? p.x : p.z) / CELL - 1.5);
    const line = horizontal ? "z" : "x";
    const fixed = Math.round((hit.side === "back" ? r.z : hit.side === "front" ? r.z + r.d : hit.side === "left" ? r.x : r.x + r.w) / CELL);
    const at = horizontal ? [along, fixed] : [fixed, along];
    const shared = Object.values(this.L.rects).some((o) => o !== r && (horizontal
      ? (Math.abs(o.z - r.z - (hit.side === "front" ? r.d : 0)) < 1e-6 || Math.abs(o.z + o.d - r.z - (hit.side === "front" ? r.d : 0)) < 1e-6)
        && o.x < (along + 3) * CELL && (along * CELL) < o.x + o.w
      : (Math.abs(o.x - r.x - (hit.side === "right" ? r.w : 0)) < 1e-6 || Math.abs(o.x + o.w - r.x - (hit.side === "right" ? r.w : 0)) < 1e-6)
        && o.z < (along + 3) * CELL && (along * CELL) < o.z + o.d));
    if (shared) return done(() => send("POST", "/api/home/doors", { line, at, level: this.view || 0 }));
    if (this.view) { say("大门只能开在一楼的外墙上", true); return; }
    if (!confirm(`这是${r.room.name}的外墙。把大门挪到这里？`)) return;
    return done(() => send("POST", "/api/home/doors", { line, at, outside: true }));
  }

  groundAt(e) {
    const rect = this.renderer.domElement.getBoundingClientRect();
    const mouse = new THREE.Vector2(((e.clientX - rect.left) / rect.width) * 2 - 1, -((e.clientY - rect.top) / rect.height) * 2 + 1);
    this.ray.setFromCamera(mouse, this.camera);
    const plane = new THREE.Plane(new THREE.Vector3(0, 1, 0), -(this.view || 0) * STOREY);
    return { mouse, point: this.ray.ray.intersectPlane(plane, new THREE.Vector3()) };
  }

  // The piece under the pointer on the floor being viewed: the nearest whose box (what is drawn and the cells
  // it takes, a little bigger all round) the pointer's ray goes through, so a thin frame is as easy to get as a sofa.
  pieceUnder(e) {
    this.groundAt(e);
    const level = this.furnitureLevels?.[this.view || 0];
    if (!level || !this.data) return null;
    const bounds = new THREE.Box3(), hit = new THREE.Vector3(), y = (this.view || 0) * STOREY;
    let best = "", near = Infinity;
    for (const g of level.children) {
      const piece = g.userData.pieceId && this.data.pieces.find((p) => p.id === g.userData.pieceId);
      const type = piece && this.data.types[piece.type], r = piece && this.rects[piece.room];
      if (!type || !r) continue;
      bounds.setFromObject(g);
      if (!piece.wall) {
        const [tw, td] = turnedSize(type.size, piece.turn), x0 = r.x + piece.at[0] * CELL, z0 = r.z + piece.at[1] * CELL;
        bounds.union(new THREE.Box3(new THREE.Vector3(x0, y, z0), new THREE.Vector3(x0 + tw * CELL, y + 0.05, z0 + td * CELL)));
      }
      if (!this.ray.ray.intersectBox(bounds.expandByScalar(0.12), hit)) continue;
      const dist = hit.distanceTo(this.ray.ray.origin);
      if (dist < near) { near = dist; best = g.userData.pieceId; }
    }
    return this.data.pieces.find((p) => p.id === best) || null;
  }

  // The pointer over a piece while arranging: a light outline, its name, and a hand to pick it up with.
  hover(e) {
    const piece = e && !this.cardDrag ? this.pieceUnder(e) : null;
    const id = piece?.id || "";
    if (this.renderer) this.renderer.domElement.style.cursor = id ? "grab" : "";
    if (id === this.hoverId) return;
    this.hoverId = id;
    if (this.hoverBox) { this.scene.remove(this.hoverBox); this.hoverBox = null; }
    const g = id && id !== this.selectedPiece && this.furnitureLevels?.[this.view || 0]?.children.find((c) => c.userData.pieceId === id);
    if (g) {
      this.hoverBox = new THREE.BoxHelper(g, 0xffcc80);
      this.scene.add(this.hoverBox);
    }
  }

  startDrag(e) {
    const piece = this.pieceUnder(e);
    const type = piece && this.data.types[piece.type];
    const g = type && this.furnitureLevels[this.view || 0].children.find((c) => c.userData.pieceId === piece.id);
    if (!g) return;
    const { point } = this.groundAt(e);
    this.controls.enabled = false;
    this.hover(null);
    this.renderer.domElement.style.cursor = "grabbing";
    // where on the piece it was taken hold of, so it does not jump to put its middle under the pointer
    const offset = point && !piece.wall ? { x: point.x - g.position.x, z: point.z - g.position.z } : { x: 0, z: 0 };
    const held = piece.wall && this.wallUnder(e, type.mount === "wall");
    const grab = held && held.wall === piece.wall && held.y != null
      ? { along: held.along - (piece.along + type.size[0] / 2) * CELL, y: held.y - piece.y } : { along: 0, y: type.height / 2 };
    this.drag = { group: g, piece, type, room: piece.room, at: piece.at && [...piece.at], turn: piece.turn ?? 0, reason: "", offset, grab,
                  moved: false, target: null };
    this.selectedPiece = piece.id;
    this.showHighlight();
    this.renderPanel();
    this.renderShelf();
  }

  // The piece follows the pointer smoothly; the plate under it shows the cells it would take, green or red.
  // One that hangs goes along the walls (one that may hang: when the pointer is on a wall rather than the floor).
  moveDrag(e) {
    const drag = this.drag;
    drag.last = e;
    drag.moved = true;
    const hit = drag.type.mount !== "floor" && this.wallUnder(e, drag.type.mount === "wall");
    if (hit) return this.dragOnWall(drag, hit);
    if (drag.type.mount === "wall") return; // off the walls: it stays where it was last on one
    drag.target = null;
    if (this.wallPlate) this.wallPlate.visible = false;
    const { point } = this.groundAt(e);
    if (!point) return;
    const x = point.x - drag.offset.x, z = point.z - drag.offset.z;
    drag.group.position.set(x, 0, z);
    drag.group.rotation.y = (-drag.turn * Math.PI) / 180;
    this.showArrow(x, z, (this.view || 0) * STOREY, drag.turn, (drag.type.size[1] * CELL) / 2);
    const spot = this.spotUnder({ x, z }, drag.type, drag.turn);
    if (this.highlight) { this.scene.remove(this.highlight); this.highlight = null; }
    if (!spot) {
      drag.reason = "放到房间里";
      if (this.ghost) this.ghost.visible = false;
      return;
    }
    const { r, at } = spot;
    drag.room = r.room.id;
    drag.at = at;
    drag.reason = this.fitCheck({ ...drag.piece, room: r.room.id, at, turn: drag.turn }, drag.type);
    this.showGhost(drag.type, { ...drag.piece, room: r.room.id, at, turn: drag.turn }, !drag.reason, false);
  }

  // A hung piece being dragged, at the place on a wall under the pointer (to the cell along, 10 cm up).
  dragOnWall(drag, hit) {
    const t = this.wallTarget(hit, drag.type, drag.grab, drag.piece.y);
    drag.target = t;
    drag.reason = this.wallCheck({ ...drag.piece, ...t }, drag.type);
    const spot = wallSpot(hit.r, t, drag.type);
    drag.group.position.set(spot.x, t.y, spot.z);
    drag.group.rotation.y = (-spot.turn * Math.PI) / 180;
    if (this.highlight) { this.scene.remove(this.highlight); this.highlight = null; }
    if (this.ghost) this.ghost.visible = false;
    this.showWallPlate(hit.r, t, drag.type, !drag.reason);
  }

  // The inner face of a wall under the pointer, on the floor being viewed: the one the pointer's ray reaches first,
  // seen from inside its room, and not behind the floor there. From above the walls are edge-on, so for something
  // that only hangs (`near`), the wall of the room under the pointer that is nearest, within a unit, will do
  // (then its height stays as it was). Returns the room, the wall, how far along it and how high, in units.
  wallUnder(e, near) {
    const { point } = this.groundAt(e);
    const ray = this.ray.ray, base = (this.view || 0) * STOREY, floor = point ? point.distanceTo(ray.origin) : Infinity;
    let best = null;
    for (const r of Object.values(this.L.rects)) {
      if (r.room.kind === "hall" && !r.room.walls) continue;
      for (const [wall, axis, at, inward, a0, a1] of [
        ["back", "z", r.z + WALL_FACE, 1, r.x, r.x + r.w], ["front", "z", r.z + r.d - WALL_FACE, -1, r.x, r.x + r.w],
        ["left", "x", r.x + WALL_FACE, 1, r.z, r.z + r.d], ["right", "x", r.x + r.w - WALL_FACE, -1, r.z, r.z + r.d]]) {
        const dir = ray.direction[axis];
        if (dir * inward >= 0) continue; // seen from behind, or edge-on
        const t = (at - ray.origin[axis]) / dir;
        if (t <= 0 || t > floor + 1e-6 || (best && t >= best.t)) continue;
        const p = ray.at(t, new THREE.Vector3()), along = (axis === "z" ? p.x : p.z) - a0, y = p.y - base;
        if (along < 0 || along > a1 - a0 || y < 0 || y > WALL_TALL) continue;
        best = { t, r, wall, along, y };
      }
    }
    if (best || !near || !point) return best;
    const r = Object.values(this.L.rects).find((q) => point.x >= q.x && point.x <= q.x + q.w && point.z >= q.z && point.z <= q.z + q.d);
    if (!r || (r.room.kind === "hall" && !r.room.walls)) return null;
    const gap = { back: point.z - r.z, front: r.z + r.d - point.z, left: point.x - r.x, right: r.x + r.w - point.x };
    const wall = Object.keys(gap).reduce((a, b) => (gap[b] < gap[a] ? b : a));
    if (gap[wall] > 1) return null;
    return { r, wall, along: wall === "back" || wall === "front" ? point.x - r.x : point.z - r.z, y: null };
  }

  // Where a piece of this kind would hang, for a place on a wall: the cell along it and the height, kept on the wall.
  wallTarget(hit, type, grab = { along: 0, y: type.height / 2 }, height = null) {
    const [cols, rows] = hit.r.room.size, n = hit.wall === "back" || hit.wall === "front" ? cols : rows, w = type.size[0];
    const along = Math.max(0, Math.min(n - w, Math.round((hit.along - grab.along) / CELL - w / 2)));
    const y = hit.y == null ? height ?? WALL_Y[type.template] ?? 1.2
      : Math.max(0, Math.min(Math.max(0, WALL_TALL - type.height), Math.round((hit.y - grab.y) * 10) / 10));
    return { room: hit.r.room.id, wall: hit.wall, along, y: Math.round(y * 10) / 10 };
  }

  // The server's checks for a piece on a wall (furniture.check_wall), for green or red while it is dragged.
  wallCheck(p, type) {
    const r = this.rects[p.room];
    if (!r) return "这里没有房间";
    if (type.mount === "floor") return `${type.name}不能挂在墙上`;
    if (r.room.kind === "hall" && !r.room.walls) return "走廊没有墙，挂不了东西";
    const across = p.wall === "back" || p.wall === "front", n = across ? r.room.size[0] : r.room.size[1];
    const w = type.size[0], h = type.height;
    if (p.along < 0 || p.along + w > n) return `${type.name}挂不下：超出了这面墙`;
    if (p.y < 0 || p.y + h > WALL_TALL + 1e-6) return `${type.name}挂不下：墙只有 ${WALL_TALL} 米高`;
    if (p.y < LINTEL) {
      for (const door of r.doors.filter((d) => d.wall === p.wall)) {
        const centre = (across ? door.x - r.x : door.z - r.z) / CELL;
        for (let i = p.along; i < p.along + w; i++) {
          if (i < centre + DOOR_WIDTH / 2 && i + 1 > centre - DOOR_WIDTH / 2) return `${type.name}挡住门了`;
        }
      }
    }
    for (const o of this.data.pieces) {
      const ot = this.data.types[o.type];
      if (!ot || o.id === p.id || o.room !== p.room || o.wall !== p.wall) continue;
      if (o.along < p.along + w && p.along < o.along + ot.size[0] && o.y < p.y + h && p.y < o.y + ot.height) {
        return `${type.name}和${o.name || ot.name}叠在一起了`;
      }
    }
    return "";
  }

  // A green or red outline on the wall where a piece would hang.
  showWallPlate(r, t, type, ok) {
    if (!this.wallPlate) {
      this.wallPlate = new THREE.Mesh(new THREE.BoxGeometry(1, 1, 0.02), new THREE.MeshBasicMaterial({ transparent: true, opacity: 0.45 }));
      this.scene.add(this.wallPlate);
    }
    const spot = wallSpot(r, t, { ...type, size: [type.size[0], 0] });
    this.wallPlate.scale.set(type.size[0] * CELL, type.height, 1);
    this.wallPlate.position.set(spot.x, (this.view || 0) * STOREY + t.y + type.height / 2, spot.z);
    this.wallPlate.rotation.y = (-spot.turn * Math.PI) / 180;
    this.wallPlate.material.color.set(ok ? 0x43a047 : 0xe53935);
    this.wallPlate.visible = true;
    const front = wallSpot(r, t, type);
    this.showArrow(front.x, front.z, (this.view || 0) * STOREY, front.turn, (type.size[1] * CELL) / 2);
  }

  // Esc: drop what is being dragged or held back where it was, else stop selecting.
  escape() {
    if (this.cardDrag) { this.cardDrag = null; this.hideGhost(); return; }
    if (this.drag) {
      this.drag = null;
      this.controls.enabled = true;
      this.hideGhost();
      this.layoutKey = "";
      this.apply(this.data);
      return;
    }
    if (this.armed) { this.armed = null; this.renderShelf(); return; }
    if (!this.selectedPiece) return;
    this.selectedPiece = "";
    this.editorKey = "";
    this.showHighlight();
    this.renderPanel();
    this.renderShelf();
  }

  // Turning a placed piece about its middle (kept inside its room); one that would not fit stays as it was.
  async turnPiece(piece, step) {
    const type = this.data.types[piece.type], r = this.rects[piece.room];
    if (!type || !r) return;
    if (piece.wall) { say("挂在墙上的东西不用转：它总是朝着房间里面", true); return; }
    const turn = (piece.turn + step + 360) % 360;
    const [w0, d0] = turnedSize(type.size, piece.turn), [w1, d1] = turnedSize(type.size, turn), [cols, rows] = r.room.size;
    const at = [Math.max(0, Math.min(cols - w1, piece.at[0] + Math.trunc((w0 - w1) / 2))),
                Math.max(0, Math.min(rows - d1, piece.at[1] + Math.trunc((d0 - d1) / 2)))];
    const reason = this.fitCheck({ ...piece, at, turn }, type);
    if (reason) { say(reason, true); return; }
    await this.movePiece(piece, { at, turn });
  }

  // A change to a placed piece shows at once; the server then saves it, or refuses it and the piece goes back.
  async movePiece(piece, change) {
    const before = { ...piece };
    for (const k of "wall" in change ? ["at", "turn"] : "at" in change ? ["wall", "along", "y"] : []) delete piece[k];
    Object.assign(piece, change);
    this.layoutKey = "";
    this.editorKey = "";
    this.apply(this.data);
    try {
      await send("PUT", `/api/home/pieces/${piece.id}`, change);
    } catch (err) {
      for (const k of Object.keys(piece)) delete piece[k];
      Object.assign(piece, before);
      say(err.message, true);
      this.layoutKey = "";
      this.editorKey = "";
      this.apply(this.data);
    }
  }

  // A hung piece's height, typed in the side panel (metres, to 10 cm).
  async setHeight(piece, y) {
    const type = this.data.types[piece.type], target = { room: piece.room, wall: piece.wall, along: piece.along, y: Math.round(y * 10) / 10 };
    const reason = this.wallCheck({ ...piece, ...target }, type);
    if (reason) { say(reason, true); this.editorKey = ""; this.renderPanel(); return; }
    await this.movePiece(piece, target);
  }

  // An orange arrow on the floor in front of a piece: the way its front faces (where you sit, what a screen shows).
  showArrow(x, z, y, turn, reach) {
    if (!this.arrow) {
      const s = new THREE.Shape();
      s.moveTo(-0.1, 0); s.lineTo(0.1, 0); s.lineTo(0.1, 0.22); s.lineTo(0.26, 0.22); s.lineTo(0, 0.5); s.lineTo(-0.26, 0.22); s.lineTo(-0.1, 0.22);
      s.closePath();
      const geo = new THREE.ShapeGeometry(s);
      geo.rotateX(Math.PI / 2); // flat on the floor, pointing to +z (a piece's front when it is not turned)
      this.arrow = new THREE.Mesh(geo, new THREE.MeshBasicMaterial({ color: 0xf57c00, side: THREE.DoubleSide }));
      this.scene.add(this.arrow);
    }
    const a = (turn * Math.PI) / 180;
    this.arrow.position.set(x - Math.sin(a) * (reach + 0.08), y + 0.05, z + Math.cos(a) * (reach + 0.08));
    this.arrow.rotation.y = -a;
    this.arrow.visible = true;
  }

  // The room under a point on the floor being viewed, and the cell a piece of this size would start at there.
  spotUnder(point, type, turn) {
    const r = Object.values(this.L.rects).find((q) => point.x >= q.x && point.x <= q.x + q.w && point.z >= q.z && point.z <= q.z + q.d);
    if (!r) return null;
    const [tw, td] = turnedSize(type.size, turn);
    const [cols, rows] = r.room.size;
    return { r, at: [Math.max(0, Math.min(cols - tw, Math.round((point.x - r.x) / CELL - tw / 2))),
                     Math.max(0, Math.min(rows - td, Math.round((point.z - r.z) / CELL - td / 2)))] };
  }

  // The checks the server makes (furniture.check and the stairwell checks), so a piece being dragged
  // can show green or red before it is let go. The server still checks again when it is saved.
  fitCheck(piece, type) {
    const r = this.rects[piece.room];
    if (!r) return "这里没有房间";
    const [tw, td] = turnedSize(type.size, piece.turn), [cols, rows] = r.room.size;
    if (piece.at[0] < 0 || piece.at[1] < 0 || piece.at[0] + tw > cols || piece.at[1] + td > rows) return `${type.name}放不下：超出了${r.room.name}`;
    const lv = this.levelOf[piece.room] ?? 0, [ax, az] = r.room.at;
    const house = (p, t) => pieceCells(p, t).map(([x, z]) => [ax + x, az + z]);
    const mine = house(piece, type);
    if (type.blocks) {
      const doors = doorCells(r);
      if (pieceCells(piece, type).some(([x, z]) => doors.has(cellKey(x, z)))) return `${type.name}挡住门了`;
      const taken = new Set(mine.map(([x, z]) => cellKey(x, z)));
      for (const o of this.data.pieces) {
        const ot = this.data.types[o.type];
        if (o.id === piece.id || o.room !== piece.room || !ot?.blocks || o.wall) continue;
        if (house(o, ot).some(([x, z]) => taken.has(cellKey(x, z)))) return `${type.name}和${o.name || ot.name}叠在一起了`;
      }
      const wells = new Set(lv > 0 ? this.wells?.[lv] || [] : []);
      if (mine.some(([x, z]) => wells.has(cellKey(x, z)))) return "这里是楼梯口，不能摆东西";
    }
    if (piece.type === "base/stairs") {
      if (!this.levels.includes(lv + 1)) return "楼上还没有这一层，先加一层";
      const above = Object.values(this.Ls[lv + 1]?.rects || {});
      const inside = ([x, z]) => above.some((q) => q.room.at[0] <= x && x < q.room.at[0] + q.room.size[0] && q.room.at[1] <= z && z < q.room.at[1] + q.room.size[1]);
      if (!mine.every(inside)) return "楼梯上面要有房间或走廊接着";
      const well = new Set(mine.map(([x, z]) => cellKey(x, z)));
      for (const o of this.data.pieces) {
        const ot = this.data.types[o.type], q = this.Ls[lv + 1]?.rects[o.room];
        if (!q || !ot?.blocks || o.id === piece.id || o.wall) continue;
        if (pieceCells(o, ot).some(([x, z]) => well.has(cellKey(q.room.at[0] + x, q.room.at[1] + z)))) return "楼上楼梯口的位置摆着东西，先挪开";
      }
    }
    return "";
  }

  // What is being dragged, see-through, with a green or red plate under it: green fits, red does not.
  showGhost(type, piece, ok, body) {
    if (!this.ghost) {
      this.ghost = new THREE.Group();
      this.scene.add(this.ghost);
    }
    const r = this.rects[piece.room];
    const key = body ? `${type.id}:${piece.turn}:${type.color}:${JSON.stringify(type.size)}` : `plate:${JSON.stringify(type.size)}:${piece.turn}`;
    if (this.ghost.userData.key !== key) {
      this.ghost.clear();
      this.ghost.userData.key = key;
      const [tw, td] = turnedSize(type.size, piece.turn);
      if (body) {
        const g = drawPiece(this.ghost, { x: 0, z: 0 }, { id: "ghost", at: [0, 0], turn: piece.turn, color: null, functions: [] }, type, [], new Set());
        g.traverse((o) => {
          if (!o.material) return;
          const seeThrough = (m) => Object.assign(m.clone(), { transparent: true, opacity: 0.6 });
          o.material = Array.isArray(o.material) ? o.material.map(seeThrough) : seeThrough(o.material);
        });
      }
      this.ghostPlate = box(tw * CELL, 0.03, td * CELL, 0x43a047, (tw * CELL) / 2, 0.02, (td * CELL) / 2);
      this.ghostPlate.material.transparent = true;
      this.ghostPlate.material.opacity = 0.55;
      this.ghost.add(this.ghostPlate);
    }
    this.ghostPlate.material.color.set(ok ? 0x43a047 : 0xe53935);
    this.ghost.position.set(r.x + piece.at[0] * CELL, (this.levelOf[piece.room] ?? 0) * STOREY, r.z + piece.at[1] * CELL);
    this.ghost.visible = true;
    if (body) {
      const [tw, td] = turnedSize(type.size, piece.turn);
      this.showArrow(this.ghost.position.x + (tw * CELL) / 2, this.ghost.position.z + (td * CELL) / 2, this.ghost.position.y,
        piece.turn, (type.size[1] * CELL) / 2);
    }
  }

  hideGhost() {
    if (this.ghost) this.ghost.visible = false;
    if (this.wallPlate) this.wallPlate.visible = false;
    this.showHighlight(); // the arrow goes back to the selected piece, if any
  }

  // ---- the shelf: the kinds of furniture on the left, dragged into the house ----

  toggleShelf(open) {
    this.shelfOpen = open;
    this.renderShelf();
  }

  // Where the shelf goes: in the side panel beside the picture (renderEditor puts it there), or, when the
  // window is narrow and the panel is below, as a strip along the bottom of the picture.
  shelfInStage() {
    return matchMedia(NARROW).matches;
  }

  renderShelf() {
    const on = this.arranging && !this.planMode && this.data, strip = this.shelfInStage();
    if (strip && this.shelf.parentNode !== this.stage) this.stage.append(this.shelf);
    this.shelf.classList.toggle("in-panel", !strip);
    if (on && !strip && this.shelf.parentNode !== this.panel) {
      this.editorKey = "";
      this.renderPanel();
    }
    this.shelf.hidden = !on || (strip && !this.shelfOpen);
    this.shelfTab.hidden = !on || !strip || this.shelfOpen;
    const covers = strip && !this.shelf.hidden;
    if (this.stage.classList.contains("shelf-open") !== covers) {
      this.stage.classList.toggle("shelf-open", covers);
      this.resize();
    }
    if (!on) return;
    if (!this.shelf.childElementCount) this.shelf.replaceChildren(...renderShelf(this));
    const d = this.data;
    const key = JSON.stringify([d.types, d.pieces, d.rooms.map((r) => [r.id, r.name, r.level]), d.floors,
                                this.selectedPiece, this.armed?.id, this.shelfPage]);
    if (key !== this.shelfKey) {
      this.shelfKey = key;
      this.shelfFill();
    }
  }

  // A row in the shelf's list of placed pieces: show its floor and select it.
  pickPiece(piece) {
    const level = this.levelOf[piece.room] ?? 0;
    if (level !== this.view) this.setView(level);
    this.selectedPiece = piece.id;
    this.editorKey = "";
    this.showHighlight();
    this.renderPanel();
    this.renderShelf();
  }

  // On a phone: a card tapped is held ready, and the next tap on the house puts it there.
  async placeArmed(e) {
    const type = this.armed;
    const hit = type.mount !== "floor" && this.wallUnder(e, false);
    if (hit) {
      const t = this.wallTarget(hit, type), reason = this.wallCheck({ id: "", ...t }, type);
      this.showWallPlate(hit.r, t, type, !reason);
      if (reason) { say(reason, true); setTimeout(() => this.cardDrag || this.hideGhost(), 1200); return; }
      this.armed = null;
      try { this.selectedPiece = (await send("POST", "/api/home/pieces", { type: type.id, ...t })).id; } catch (err) { say(err.message, true); }
      this.hideGhost();
      this.layoutKey = "";
      this.editorKey = "";
      await this.poll();
      return;
    }
    const { point } = this.groundAt(e);
    const spot = point && this.spotUnder(point, type, 0);
    if (!spot) return;
    const piece = type.mount === "wall" ? { type: type.id, room: spot.r.room.id } : { type: type.id, room: spot.r.room.id, at: spot.at, turn: 0 };
    const reason = type.mount === "wall" ? "" : this.fitCheck({ id: "", ...piece }, type);
    if (type.mount !== "wall") this.showGhost(type, piece, !reason, true);
    if (reason) {
      say(reason, true);
      setTimeout(() => this.cardDrag || this.hideGhost(), 1200);
      return;
    }
    this.armed = null;
    try { this.selectedPiece = (await send("POST", "/api/home/pieces", piece)).id; } catch (err) { say(err.message, true); }
    this.hideGhost();
    this.layoutKey = "";
    this.editorKey = "";
    await this.poll();
  }

  // A card pressed: dragged out it becomes a piece where it is let go; just clicked, it goes into a room's first free spot.
  cardDown(e, type) {
    if (e.button) return;
    if (e.pointerType !== "mouse") { // a finger: tap the card, then tap a spot (a swipe just scrolls the shelf)
      const card = e.currentTarget, done = new AbortController();
      card.addEventListener("pointerup", () => {
        done.abort();
        this.armed = this.armed?.id === type.id ? null : type;
        this.renderShelf();
      }, { signal: done.signal });
      card.addEventListener("pointercancel", () => done.abort(), { signal: done.signal });
      return;
    }
    const card = e.currentTarget, start = [e.clientX, e.clientY];
    try { card.setPointerCapture(e.pointerId); } catch { /* a pointer that is already gone */ }
    this.cardDrag = { type, turn: 0, piece: null, reason: "", moved: false, last: null };
    const move = (ev) => {
      const drag = this.cardDrag;
      if (!drag) return;
      if (!drag.moved && Math.hypot(ev.clientX - start[0], ev.clientY - start[1]) < 6) return;
      drag.moved = true;
      this.moveCard(ev);
    };
    const up = async (ev) => {
      card.removeEventListener("pointermove", move);
      card.removeEventListener("pointerup", up);
      card.removeEventListener("pointercancel", up);
      const drag = this.cardDrag;
      this.cardDrag = null;
      this.hideGhost();
      if (!drag) return;
      if (!drag.moved) return this.putKind(type);
      if (ev.type === "pointercancel" || !drag.piece) return;
      if (drag.reason) { say(drag.reason, true); return; }
      try {
        const piece = await send("POST", "/api/home/pieces", drag.piece.wall ? { type: type.id, room: drag.piece.room, wall: drag.piece.wall,
          along: drag.piece.along, y: drag.piece.y } : type.mount === "wall" ? { type: type.id, room: drag.piece.room }
          : { type: type.id, room: drag.piece.room, at: drag.piece.at, turn: drag.piece.turn });
        this.selectedPiece = piece.id;
      } catch (err) { say(err.message, true); }
      this.layoutKey = "";
      this.editorKey = "";
      await this.poll();
    };
    card.addEventListener("pointermove", move);
    card.addEventListener("pointerup", up);
    card.addEventListener("pointercancel", up);
  }

  moveCard(ev) {
    const drag = this.cardDrag;
    drag.last = ev;
    const over = document.elementFromPoint(ev.clientX, ev.clientY);
    const hit = over === this.renderer.domElement && drag.type.mount !== "floor" && this.wallUnder(ev, drag.type.mount === "wall");
    if (hit) { // on a wall (or, seen from above, near one): hung just there
      const t = this.wallTarget(hit, drag.type);
      drag.piece = { type: drag.type.id, ...t };
      drag.reason = this.wallCheck({ id: "", ...drag.piece }, drag.type);
      if (this.ghost) this.ghost.visible = false;
      this.showWallPlate(hit.r, t, drag.type, !drag.reason);
      return;
    }
    if (this.wallPlate) this.wallPlate.visible = false;
    const { point } = over === this.renderer.domElement ? this.groundAt(ev) : {};
    const spot = point && this.spotUnder(point, drag.type, drag.turn);
    if (!spot) { drag.piece = null; this.hideGhost(); return; }
    if (drag.type.mount === "wall") { // over the floor of a room: it goes on the first free place on its walls
      drag.piece = { room: spot.r.room.id };
      drag.reason = "";
      this.hideGhost();
      return;
    }
    drag.piece = { id: "", type: drag.type.id, room: spot.r.room.id, at: spot.at, turn: drag.turn };
    drag.reason = this.fitCheck(drag.piece, drag.type);
    this.showGhost(drag.type, drag.piece, !drag.reason, true);
  }

  // Just clicked: into the selected piece's room, or the living room, or the first room of the floor being viewed.
  async putKind(type) {
    const d = this.data, lv = this.view || 0;
    const sel = d.pieces.find((p) => p.id === this.selectedPiece);
    const onFloor = d.rooms.filter((r) => (r.level ?? 0) === lv);
    const room = (sel && (this.levelOf[sel.room] ?? 0) === lv && sel.room)
      || (lv === 0 && onFloor.some((r) => r.id === "living") && "living")
      || (onFloor.find((r) => r.kind !== "hall") || onFloor[0])?.id;
    if (!room) { say("这一层还没有房间", true); return; }
    try { this.selectedPiece = (await send("POST", "/api/home/pieces", { type: type.id, room })).id; } catch (err) { say(err.message, true); }
    this.layoutKey = "";
    this.editorKey = "";
    await this.poll();
  }

  // A small still picture of a kind of furniture, for the shelf; kept in the browser until the kind's look changes.
  thumb(type) {
    const key = "nookling-thumb:2:" + JSON.stringify([type.template, type.size, type.height, type.color, type.model]);
    this.thumbs ||= new Map();
    if (!this.thumbs.has(key)) {
      let saved = null;
      try { saved = localStorage.getItem(key); } catch { /* no storage here: draw it every time */ }
      this.thumbs.set(key, saved ? Promise.resolve(saved) : (this.thumbQueue = (this.thumbQueue || Promise.resolve()).then(() => this.drawThumb(type)).then((url) => {
        try { localStorage.setItem(key, url); } catch { /* full or blocked: fine */ }
        return url;
      })));
    }
    return this.thumbs.get(key);
  }

  async drawThumb(type) {
    if (!this.thumbScene) {
      const renderer = new THREE.WebGLRenderer({ antialias: true, alpha: true, preserveDrawingBuffer: true });
      renderer.setPixelRatio(2);
      renderer.setSize(120, 90, false);
      const scene = new THREE.Scene();
      scene.add(new THREE.HemisphereLight(0xffffff, 0xb0a99f, 1.6));
      const sun = new THREE.DirectionalLight(0xffffff, 1.2);
      sun.position.set(-4, 8, 6);
      scene.add(sun);
      const holder = new THREE.Group();
      scene.add(holder);
      this.thumbScene = { renderer, scene, holder, camera: new THREE.OrthographicCamera(-1, 1, 1, -1, -50, 50) };
    }
    const { renderer, scene, holder, camera } = this.thumbScene;
    holder.clear();
    const t = { ...type, model: null };
    const W = type.size[0] * CELL, D = type.size[1] * CELL, H = Math.max(0.05, type.height || 0.5);
    if (type.model) {
      try {
        const g = new THREE.Group();
        g.add(fitModel(await loadModel(type.model.file), W, D, H, type.model));
        holder.add(g);
      } catch { drawPiece(holder, { x: 0, z: 0 }, { id: "thumb", at: [0, 0], turn: 0, color: null, functions: [] }, t, [], new Set()); }
    } else {
      drawPiece(holder, { x: 0, z: 0 }, { id: "thumb", at: [0, 0], turn: 0, color: null, functions: [] }, t, [], new Set());
    }
    holder.children[0].position.set(0, 0, 0);
    const reach = Math.max(W, D, H) * 0.62 + 0.15;
    camera.left = -reach * 4 / 3; camera.right = reach * 4 / 3; camera.top = reach; camera.bottom = -reach;
    camera.position.set(4, 3.4 + H / 2, 4);
    camera.lookAt(0, H / 2, 0);
    camera.updateProjectionMatrix();
    renderer.render(scene, camera);
    return renderer.domElement.toDataURL("image/webp", 0.85);
  }

  async endDrag() {
    const { piece, room, at, turn, reason, moved, target } = this.drag;
    this.drag = null;
    this.controls.enabled = true;
    this.renderer.domElement.style.cursor = "";
    this.hideGhost();
    if (!moved) return; // just a click: it is selected
    const same = target ? piece.wall && ["room", "wall", "along", "y"].every((k) => target[k] === piece[k])
      : !piece.wall && !!at && room === piece.room && at[0] === piece.at[0] && at[1] === piece.at[1] && turn === piece.turn;
    if (same || (!target && !at)) {
      this.layoutKey = ""; // back onto its own cells (it followed the pointer freely)
      this.apply(this.data);
      return;
    }
    if (reason) {
      say(reason, true);
      this.layoutKey = ""; // back where it was
      this.apply(this.data);
      return;
    }
    await this.movePiece(piece, target || { room, at, turn });
  }

  showHighlight() {
    if (this.highlight) { this.scene.remove(this.highlight); this.highlight = null; }
    const room = this.arranging && this.planMode && this.L?.rects[this.selectedRoom];
    if (room) {
      const outline = new THREE.Mesh(new THREE.BoxGeometry(room.w, 0.02, room.d), new THREE.MeshBasicMaterial({ color: 0xf0a020, transparent: true, opacity: 0.25 }));
      outline.position.set(room.x + room.w / 2, (this.view || 0) * STOREY + 0.07, room.z + room.d / 2);
      this.highlight = outline;
      this.scene.add(outline);
      return;
    }
    const g = this.arranging && this.furnitureLevels?.[this.view || 0]?.children.find((c) => c.userData.pieceId === this.selectedPiece);
    const at = g && !this.planMode && this.pieceAt[this.selectedPiece];
    if (this.arrow && !this.drag) this.arrow.visible = false;
    if (!g) return;
    this.highlight = new THREE.BoxHelper(g, 0xf0a020);
    this.scene.add(this.highlight);
    if (at && !this.drag && !this.cardDrag) {
      const piece = this.data.pieces.find((p) => p.id === this.selectedPiece);
      this.showArrow(at.x, at.z, at.y, at.turn, ((this.data.types[piece.type]?.size[1] || 1) * CELL) / 2);
    }
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
      return { x: p.x + 0.8, z: p.z + 0.5, pose: "stand", face: 90, y: p.y };
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
               z: using.z + Math.cos(a) * (using.reach + 0.6) + side * Math.sin(a), pose: "stand", face: (using.turn + 180) % 360,
               y: using.y || 0 };
    }
    const pick = {
      sleep: () => seats.filter((s) => s.pose === "lie" && s.piece.owner === person.id).concat(seats.filter((s) => s.pose === "lie")),
      read: () => sit.filter((s) => (s.piece.functions || []).includes("web.read")).concat(sit),
      play: () => sit.filter((s) => this.facesScreen(s)).concat(sit),
      watch: () => sit.filter((s) => this.facesScreen(s)).concat(sit),
      rest: () => sit.filter((s) => !(s.piece.functions || []).includes("web.read")).concat(sit),
      talk: () => sit.filter((s) => !(s.piece.functions || []).length).concat(sit),
    };
    pick.browse = pick.write = pick.read;
    const options = pick[person.verb] ? pick[person.verb]() : [];
    if (options.length) {
      taken.add(options[0].key);
      return options[0];
    }
    // Nowhere to sit for this: stand in the room, spread out a little.
    const r = this.rects[person.room] || this.rects.living;
    const n = taken.size;
    taken.add(`stand:${n}`);
    return { x: r.x + r.w / 2 + ((n % 3) - 1) * 0.8, z: r.z + r.d * 0.6 + Math.floor(n / 3) * 0.6, pose: "stand", face: 0,
             y: (this.levelOf[person.room] ?? 0) * STOREY };
  }

  apply(data) {
    if (this.drag || this.plan) return; // the redraw would snatch the piece or room from under the pointer
    this.data = data;
    if (data.talk?.busy || data.talk?.thinking?.length) this.fastUntil = Math.max(this.fastUntil || 0, Date.now() + 10000);
    this.takeSaid(data);
    const key = JSON.stringify([data.rooms, data.doors, data.floors, data.unreachable, data.pieces, data.available, data.types]);
    if (key !== this.layoutKey && this.renderer) {
      this.layoutKey = key;
      this.build(data);
    }
    if (this.renderer) this.place(data);
    this.renderPanel();
    this.renderShelf();
  }

  place(data) {
    const L = this.L0;
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
        fig.root.position.set(spot.x, spot.y || 0, spot.z);
        fig.room = person.room;
      } else if (fig.room !== person.room || fig.spotKey !== `${spot.x},${spot.z}`) {
        const from = fig.room;
        const lv = (room) => this.levelOf[room] ?? 0;
        const here = { x: fig.root.position.x, z: fig.root.position.z };
        let pts;
        if (out) pts = [...this.walkBetween(here, lv(from), from, L.frontInside, 0, L.frontRoom), L.front, L.outside];
        else if (from === "out") {
          fig.root.visible = true;
          fig.root.position.set(L.outside.x, 0, L.outside.z);
          pts = [L.front, ...this.walkBetween(L.frontInside, 0, L.frontRoom, spot, lv(person.room), person.room)];
        } else pts = this.walkBetween(here, lv(from), from, spot, lv(person.room), person.room);
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
    // On a narrow window the shelf is a strip over the bottom of the picture: fit the house above it.
    const left = 0, below = this.stage.classList.contains("shelf-open") ? Math.min(this.shelf.offsetHeight + 12, h * 0.6) : 0;
    const strip = this.shelfInStage();
    if (strip !== this.wasStrip) { // the window crossed the line: the shelf moves between the panel and the picture
      this.wasStrip = strip;
      queueMicrotask(() => this.renderShelf());
    }
    let view = 14;
    if (this.bounds) {
      // Fit the whole house: the corners of the ground, seen from the camera, decide the frame.
      this.camera.lookAt(this.controls.target);
      this.camera.updateMatrixWorld();
      const inv = this.camera.matrixWorldInverse;
      let halfX = 0, halfY = 0;
      const B = this.bounds, y0 = (this.view || 0) * STOREY;
      for (const x of [B.X0 - 1, B.X1 + 1]) for (const z of [B.Z0 - 1, B.Z1 + 3]) for (const y of [y0, y0 + WALL_TALL]) {
        const p = new THREE.Vector3(x, y, z).applyMatrix4(inv);
        halfX = Math.max(halfX, Math.abs(p.x));
        halfY = Math.max(halfY, Math.abs(p.y));
      }
      view = Math.max((halfY * h) / (h - below), (halfX * h) / (w - left)) * 1.04;
    }
    const sx = (left / h) * view, sy = (below / h) * view; // half the covered width and height, in units
    this.camera.left = -view * aspect - sx; this.camera.right = view * aspect - sx;
    this.camera.top = view - sy; this.camera.bottom = -view - sy;
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
      const lvl = fig.root.position.y / STOREY - (this.view || 0);
      fig.root.visible = !(fig.out && !fig.path.length) && lvl > -0.45 && lvl < 0.55; // on this floor, or half-way up its stairs
    }
    this.controls.update();
    this.showWalls();
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
      return [x, y, node];
    };
    const placed = [];
    const shut = new Set(this.data.unreachable || []);
    const names = Object.fromEntries(this.data.rooms.map((r) => [r.id, shut.has(r.id) ? `${r.name}（进不去）` : r.name]));
    for (const r of this.roomLabels) put("room:" + r.id, "home-room-label", names[r.id] || r.id, r.pos);
    const hovered = this.arranging && !this.drag && this.hoverId && this.data.pieces.find((p) => p.id === this.hoverId);
    if (hovered && this.pieceAt[hovered.id]) {
      const at = this.pieceAt[hovered.id], type = this.data.types[hovered.type] || {};
      put("hover", "home-hover-tag", hovered.name || type.name || "", new THREE.Vector3(at.x, at.y + (at.lift || 0) + (type.height || 0.5) + 0.3, at.z));
    }
    for (const fig of this.figures.values()) {
      if (!fig.root.visible) continue;
      const p = fig.person;
      const head = fig.root.position.clone().add(new THREE.Vector3(0, fig.pose === "lie" ? 0.9 : fig.pose === "sit" ? 1.6 : 1.9, 0));
      const words = fig.path.length ? "走过去…" : fig.stale ? "好久没动了" : p.verb === "sleep" ? "💤" : fig.phoning && p.verb !== "phone" ? "在看手机" : p.words;
      const [x, y, tag] = put("who:" + p.id, `home-tag${this.selected === p.id ? " selected" : ""}${fig.stale ? " stale" : ""}`,
        `${p.name} · ${words}`, head, p.detail || "");
      const said = this.bubbles?.get(p.id);
      if (said && said.until > performance.now()) {
        const people = this.data.people;
        const phone = p.verb === "phone" || (said.kind === "private" && people.find((o) => o.id === said.peer)?.verb === "phone");
        const key = "say:" + p.id;
        want.add(key);
        let node = this.labels.get(key);
        if (!node) {
          node = el("div", "home-bubble");
          node.onclick = () => this.openSaid(this.bubbles.get(p.id));
          this.labels.set(key, node);
          this.labelLayer.append(node);
        }
        const text = (said.kind === "group" || phone ? "📱 " : "") + said.text;
        if (node.textContent !== text) node.textContent = text;
        node.className = `home-bubble ${said.kind}`;
        node.title = said.kind === "group" ? `在群里说：${said.text}` : said.kind === "private" ? `私聊：${said.text}` : said.text;
        node.style.transform = `translate(${Math.round(x)}px, ${Math.round(y - (tag.offsetHeight || 20) - 6)}px) translate(-50%, -100%)`;
      } else if (said) this.bubbles.delete(p.id);
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
    if (!this.arranging && this.panelPage === "talk") { this.renderTalk(); return; } // keeps what is being typed
    if (!this.arranging && this.panelPage === "changes") { this.renderChanges(); return; }
    const focus = document.activeElement;
    if (focus && this.panel.contains(focus) && /INPUT|SELECT/.test(focus.tagName)) return; // editing a look
    if (this.arranging) {
      // Only when the house changed: a rebuild every poll would drop what is picked in the panel.
      const key = JSON.stringify([d.rooms, d.floors, d.doors, d.unreachable, d.pieces, d.types, d.available, this.selectedPiece, this.view,
                                  this.arrangeTop, this.shelfInStage()]);
      if (key !== this.editorKey) {
        this.editorKey = key;
        const list = this.shelf.querySelector(".home-shelf-list"), top = list?.scrollTop; // the shelf is moved, not rebuilt
        this.panel.replaceChildren(...renderEditor(this));
        if (list) list.scrollTop = top;
      }
      return;
    }
    this.editorKey = "";
    const rows = d.people.map((p) => {
      const row = el("button", "home-row" + (this.selected === p.id ? " selected" : ""));
      row.type = "button";
      const level = this.levelOf?.[p.room] ?? 0;
      row.onclick = () => {
        this.selected = this.selected === p.id ? "" : p.id;
        this.selectedPiece = "";
        if (this.selected && p.room !== "out" && level !== this.view) this.setView(level);
        this.renderPanel();
      };
      const dot = el("span", "home-dot");
      dot.style.background = p.look.color;
      const stale = !p.active && !["sleep", "out"].includes(p.verb) && d.now - p.since > STALE_SECONDS;
      const text = el("span", "home-row-text");
      const floor = p.room !== "out" && level !== this.view ? `（${(d.floors || []).find((f) => f.level === level)?.name || ""}）` : "";
      text.append(el("b", "", p.name), el("span", "muted", ` ${p.room_name}${floor} · ${stale ? "好久没动了" : p.words}`));
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
    this.panel.replaceChildren(...[this.panelTabs(), el("h2", "", "家里"), warning, list, detail].filter(Boolean));
  }

  // ---- bubbles: what was just said, over the speaker's head (docs/house-talk.md, 3b) ----

  // New lines from the server become bubbles, each once; what was said before the page opened does not.
  takeSaid(data) {
    this.saidSince ??= (data.now || Date.now() / 1000) - 10;
    this.shownSaid ||= new Set();
    this.bubbles ||= new Map();
    for (const line of data.said || []) {
      const key = `${line.kind}:${line.who}:${line.ts}`;
      if (this.shownSaid.has(key) || line.ts < this.saidSince) continue;
      this.shownSaid.add(key);
      const seconds = Math.min(10, Math.max(4, 2 + line.text.length / 8));
      const bubble = { ...line, until: performance.now() + seconds * 1000 };
      if (line.owner) this.showOwnerLine(bubble);
      else this.bubbles.set(line.who, bubble); // a newer line from the same person takes the old one's place
    }
  }

  showOwnerLine(line) {
    if (!this.ownerLine) {
      this.ownerLine = el("div", "home-owner-line");
      this.ownerLine.onclick = () => this.openSaid(this.ownerLineSaid);
      this.stage.append(this.ownerLine);
    }
    this.ownerLineSaid = line;
    this.ownerLine.textContent = `你：${line.text}`;
    this.ownerLine.title = line.text;
    this.ownerLine.hidden = false;
    clearTimeout(this.ownerLineTimer);
    this.ownerLineTimer = setTimeout(() => (this.ownerLine.hidden = true), line.until - performance.now());
  }

  // A bubble clicked: the line it came from. The talk at home is in 聊天; private chats are in 日常.
  openSaid(line) {
    if (!line) return;
    if (line.kind === "home") {
      this.panelPage = "talk";
      this.renderPanel();
      const row = this.talkUI?.list.querySelector(`[data-ts="${line.ts}"]`);
      if (row) {
        row.scrollIntoView({ block: "nearest" });
        row.classList.add("flash");
        setTimeout(() => row.classList.remove("flash"), 1600);
      }
    } else if (line.kind === "private") {
      document.querySelector('.tabs button[data-tab="life"]')?.click();
    }
  }

  // The side panel's two pages: who is where, and the talk at home (docs/house-talk.md, 3a).
  panelTabs() {
    const talk = this.data.talk || { messages: [] };
    if (this.talkSeen === undefined) this.talkSeen = talk.messages.at(-1)?.ts || 0; // what was said before the page opened is not new
    const fresh = talk.messages.filter((m) => !m.owner && m.ts > this.talkSeen).length;
    const row = el("div", "home-tabs");
    for (const [id, label] of [["home", "家里"], ["talk", fresh && this.panelPage !== "talk" ? `聊天（${fresh}）` : "聊天"],
                               ["changes", "变化"]]) {
      const b = el("button", (this.panelPage || "home") === id ? "on" : "", label);
      b.type = "button";
      b.onclick = () => { this.panelPage = id; this.renderPanel(); if (id === "talk") this.talkUI?.input.focus(); };
      row.append(b);
    }
    return row;
  }

  // 家里的变化: what the family changed at home, newest first; the owner can undo one (docs/redecorate.md, 4b).
  renderChanges() {
    const d = this.data, people = Object.fromEntries(d.people.map((p) => [p.id, p]));
    const key = JSON.stringify(d.changes || []);
    if (key === this.changesKey && this.panel.querySelector(".home-changes")) return;
    this.changesKey = key;
    const list = el("div", "home-changes");
    for (const c of d.changes || []) {
      const row = el("div", "home-change" + (c.undone ? " undone" : ""));
      const who = c.member === "__owner" ? "你" : people[c.member]?.name || c.member;
      row.append(el("span", "home-when muted", new Date(c.ts * 1000).toLocaleString("zh-CN", { month: "numeric", day: "numeric", hour: "2-digit", minute: "2-digit" })),
        el("p", "", `${who}${c.text}`));
      if (c.undone) row.append(el("span", "muted small", "已撤回"));
      else if (!c.why_not) {
        const undo = el("button", "", "撤回");
        undo.onclick = async () => {
          if (!confirm(`撤回这一处改动？\n${who}${c.text}`)) return;
          try { await send("POST", `/api/home/changes/${c.id}/undo`, {}); say("撤回了，她会知道"); } catch (e) { say(e.message, true); }
          this.layoutKey = "";
          await this.poll();
        };
        row.append(undo);
      } else if (c.member !== "__owner") row.append(el("span", "muted small", c.why_not));
      list.append(row);
    }
    if (!(d.changes || []).length) list.append(el("p", "muted small", "家里还没人动过东西。她们在日常里会偶尔收拾自己的卧室、挪挪公共房间的家具。"));
    this.panel.replaceChildren(this.panelTabs(), el("h2", "", "家里的变化"),
      el("p", "muted small", "家人自己动过的东西。撤回会恢复成改之前的样子，改的人会知道。"), list);
  }

  // The talk at home: what was said, who is working on a reply, and a box to say something in the house.
  renderTalk() {
    const d = this.data, talk = d.talk || { messages: [], thinking: [] };
    if (!this.talkUI) {
      const list = el("div", "home-talk-list"), thinking = el("p", "home-talk-thinking muted small");
      const input = el("textarea");
      input.rows = 2;
      input.maxLength = 2000;
      input.placeholder = "在家里说话…（Enter 说出来，Shift+Enter 换行）";
      const speak = el("button", "primary", "说");
      speak.type = "button";
      const go = async () => {
        const text = input.value.trim();
        if (!text || speak.disabled) return;
        speak.disabled = true;
        try {
          this.data.talk = await send("POST", "/api/home/talk", { text });
          input.value = "";
          this.fastUntil = Date.now() + 90000;
          this.renderTalk();
        } catch (e) { say(e.message, true); }
        speak.disabled = false;
        input.focus();
      };
      speak.onclick = go;
      input.onkeydown = (e) => { if (e.key === "Enter" && !e.shiftKey && !e.isComposing) { e.preventDefault(); go(); } };
      const form = el("div", "home-talk-form");
      form.append(input, speak);
      const root = el("div", "home-talk");
      root.append(el("p", "muted small", "在这里说的话，就是在家里说出来的：在家、醒着的家人听得到，接话的人会走到客厅来。"),
        list, thinking, form);
      this.talkUI = { root, list, thinking, input };
    }
    const ui = this.talkUI;
    if (!this.panel.contains(ui.root)) this.panel.replaceChildren(this.panelTabs(), el("h2", "", "聊天"), ui.root);
    else this.panel.firstChild.replaceWith(this.panelTabs());
    const key = JSON.stringify([talk.messages.length, talk.messages.at(-1)?.ts]);
    if (key !== this.talkKey) {
      const first = this.talkKey === undefined, atEnd = ui.list.scrollHeight - ui.list.scrollTop - ui.list.clientHeight < 40;
      this.talkKey = key;
      const people = Object.fromEntries(d.people.map((p) => [p.id, p]));
      ui.list.replaceChildren(...(talk.messages.length ? talk.messages.map((m) => {
        const line = el("div", "home-talk-line" + (m.owner ? " mine" : ""));
        line.dataset.ts = m.ts;
        const who = el("span", "who", m.owner ? "你" : m.name);
        if (!m.owner && people[m.who]) who.style.color = people[m.who].look.color;
        line.append(who, el("span", "when", clock(m.ts)), el("p", "", m.text));
        return line;
      }) : [el("p", "muted small", "还没人在家里说过话。")]));
      if (first || atEnd) ui.list.scrollTop = ui.list.scrollHeight;
    }
    this.talkSeen = Math.max(this.talkSeen || 0, talk.messages.at(-1)?.ts || 0);
    const names = (talk.thinking || []).map((id) => d.people.find((p) => p.id === id)?.name || id);
    ui.thinking.textContent = names.length ? `${names.join("、")} 正在想…` : "";
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

export { layout, walkGrid, walkPath, route, app }; // for checking walks from the browser console
