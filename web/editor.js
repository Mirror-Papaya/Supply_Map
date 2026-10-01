"use strict";
/* 库存地图编辑器：网格填格编辑。
 * 数据：T 格子类型(0未定义 1功能 2库存 3走廊) / B 所属地图区块 id / Z 仓库区块字母(字符码，0=无)
 * 仓库区块（字母）是比地图区块高一级的图层，以半透明覆盖显示。
 */
const $ = (s) => document.querySelector(s);
const $$ = (s) => [...document.querySelectorAll(s)];

const TYPE_COLOR = ["#f8f9fa", "#ffecb3", "#cee0fc", "#e4e7eb"];
const TYPE_NAME = ["未定义", "功能区块", "库存区块（货架）", "走廊"];
const PALETTE = ["#2f6fdb", "#d9480f", "#2b8a3e", "#ae3ec9", "#e67700", "#0c8599", "#c2255c", "#5c940d",
  "#1864ab", "#a61e4d", "#087f5b", "#862e9c", "#d6336c", "#364fc7", "#f08c00", "#495057"];
const LETTERS = Array.from({ length: 26 }, (_, i) => String.fromCharCode(65 + i));

let M = null;            // 地图元数据（不含格子数组）
let T, B, Z;             // 格子数组（Uint8Array / Int32Array / Uint8Array）
let binfo = {};          // 区块统计：id -> {count, r0,c0,r1,c1, zone, zoneProblem, dup}
let zinfo = {};          // 仓库区块统计：letter -> {count, sr, sc}
let selected = 0;
let dirty = false;
let undoStack = [], redoStack = [];
let issueBlocks = new Set();
let view = { scale: 1, ox: 20, oy: 20 };
let layer = "blocks";
let tool = "rect";
let brushType = 2;
let zoneLetter = "A", zoneErase = false;
let selectedZone = "";   // 仓库区块图层里用选择工具点中的字母
let show = { zones: true, grid: true, bg: true };
let bgImg = null;
let lastLayers = { min: 1, max: 4 };
let lastRegions = 1;      // 新建货架时沿用上一次选的"每层区域数"
let createdInStroke = 0;
let drag = null;         // 当前拖拽：{kind: 'paint'|'rect'|'pan', ...}
let spaceDown = false;
let preEditSnapshot = null;

const cv = $("#cv"), ctx = cv.getContext("2d"), stage = $("#stage");

// ---------------- 通用 ----------------
async function api(method, url, body) {
  const opt = { method, headers: {} };
  if (body !== undefined) { opt.headers["Content-Type"] = "application/json"; opt.body = JSON.stringify(body); }
  const res = await fetch(url, opt);
  let data = null;
  try { data = await res.json(); } catch { /* 非 JSON */ }
  if (!res.ok && res.status !== 422) throw new Error((data && data.detail) || res.statusText);
  return { status: res.status, data };
}
let toastTimer = null;
function toast(msg, isErr) {
  const t = $("#toast");
  t.textContent = msg; t.className = "toast" + (isErr ? " err" : ""); t.hidden = false;
  clearTimeout(toastTimer); toastTimer = setTimeout(() => (t.hidden = true), isErr ? 5000 : 2600);
}
const pad2 = (n) => String(n).padStart(2, "0");
const esc = (s) => String(s ?? "").replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
function zoneColor(letter) {
  return (M.zone_meta[letter] && M.zone_meta[letter].color) || PALETTE[(letter.charCodeAt(0) - 65) % PALETTE.length];
}
function setDirty(v) { dirty = v; $("#dirtyMark").hidden = !v; }

// ---------------- 载入 / 序列化 ----------------
async function loadWarehouses(selectWh) {
  const { data } = await api("GET", "/api/warehouses");
  const sel = $("#whSelect");
  sel.innerHTML = data.map((w) => `<option value="${w.wh_no}">${w.wh_no} ${esc(w.name)}</option>`).join("");
  $("#emptyState").hidden = data.length > 0;
  if (!data.length) { M = null; draw(); renderPanel(); return; }
  let wh = selectWh || localStorage.getItem("wm.lastWh");
  if (!data.some((w) => w.wh_no === wh)) wh = data[0].wh_no;
  sel.value = wh;
  await loadMap(wh);
}

async function loadMap(wh) {
  const { data: m } = await api("GET", `/api/maps/${wh}`);
  const n = m.rows * m.cols;
  T = new Uint8Array(n); B = new Int32Array(n); Z = new Uint8Array(n);
  for (let r = 0; r < m.rows; r++) {
    const tr = m.types[r], br = m.bids[r], zr = m.zones[r];
    for (let c = 0; c < m.cols; c++) {
      const i = r * m.cols + c;
      T[i] = tr.charCodeAt(c) - 48; B[i] = br[c];
      const ch = zr.charCodeAt(c); Z[i] = ch === 46 ? 0 : ch;
    }
  }
  delete m.types; delete m.bids; delete m.zones;
  m.blocks = m.blocks || {}; m.zone_meta = m.zone_meta || {};
  m.bg = Object.assign({ file: "", opacity: 0.5, show: true }, m.bg || {});
  M = m;
  selected = 0; undoStack = []; redoStack = []; issueBlocks = new Set();
  $("#whName").value = M.name || "";
  $("#zoneColor").value = zoneColor(zoneLetter);
  $("#bgOpacity").value = M.bg.opacity;
  $("#showBg").checked = M.bg.show !== false;
  show.bg = $("#showBg").checked;
  $("#issues").innerHTML = '<li class="muted">点"检查"或"保存"后显示</li>';
  $("#issueCount").textContent = "";
  try { localStorage.setItem("wm.lastWh", wh); } catch { /* 忽略 */ }
  loadBg();
  recompute();
  setDirty(false);
  fitView();
  renderPanel();
}

function loadBg() {
  bgImg = null;
  if (!M || !M.bg.file) { draw(); return; }
  const img = new Image();
  img.onload = () => { bgImg = img; draw(); };
  img.src = `/api/maps/${M.wh_no}/bg?t=${Date.now()}`;
}

function serialize() {
  const types = [], bids = [], zones = [];
  for (let r = 0; r < M.rows; r++) {
    let ts = "", zs = "";
    const br = new Array(M.cols);
    for (let c = 0; c < M.cols; c++) {
      const i = r * M.cols + c;
      ts += T[i];
      zs += Z[i] ? String.fromCharCode(Z[i]) : ".";
      br[c] = B[i];
    }
    types.push(ts); bids.push(br); zones.push(zs);
  }
  return { ...M, name: $("#whName").value.trim(), types, bids, zones };
}

// ---------------- 统计 ----------------
function recompute() {
  binfo = {}; zinfo = {};
  const cols = M.cols;
  for (let i = 0; i < T.length; i++) {
    const z = Z[i];
    const r = (i / cols) | 0, c = i - r * cols;
    if (z) {
      const zi = zinfo[z] || (zinfo[z] = { count: 0, sr: 0, sc: 0 });
      zi.count++; zi.sr += r; zi.sc += c;
    }
    const b = B[i];
    if (!b) continue;
    const meta = M.blocks[b];
    const want = meta && (meta.type === "storage" ? 2 : meta.type === "function" ? 1 : -1);
    if (!meta || T[i] !== want) { B[i] = 0; continue; }
    let bi = binfo[b];
    if (!bi) bi = binfo[b] = { count: 0, r0: r, c0: c, r1: r, c1: c, zones: new Set() };
    bi.count++;
    if (r < bi.r0) bi.r0 = r; if (r > bi.r1) bi.r1 = r;
    if (c < bi.c0) bi.c0 = c; if (c > bi.c1) bi.c1 = c;
    bi.zones.add(z);
  }
  for (const id of Object.keys(M.blocks)) {
    if (!binfo[id]) { delete M.blocks[id]; if (+id === selected) selected = 0; }
  }
  const seen = {};
  for (const [id, bi] of Object.entries(binfo)) {
    const meta = M.blocks[id];
    const real = [...bi.zones].filter((z) => z);
    bi.zone = real.length === 1 ? String.fromCharCode(real[0]) : "";
    bi.zoneProblem = meta.type !== "storage" ? "" :
      real.length === 0 ? "没有落在任何仓库区块内" :
      real.length > 1 ? `跨越多个仓库区块：${real.map((z) => String.fromCharCode(z)).join("、")}` :
      bi.zones.has(0) ? `部分格子不在区块 ${bi.zone} 内` : "";
    bi.dup = false;
    if (meta.type === "storage" && bi.zone && /^\d{2}$/.test(meta.shelf || "")) {
      const key = bi.zone + "-" + meta.shelf;
      if (seen[key]) { bi.dup = true; binfo[seen[key]].dup = true; } else seen[key] = id;
    }
  }
}

// ---------------- 撤销 ----------------
function snapshot() {
  return {
    T: T.slice(), B: B.slice(), Z: Z.slice(),
    blocks: JSON.parse(JSON.stringify(M.blocks)), zone_meta: JSON.parse(JSON.stringify(M.zone_meta)),
    next_id: M.next_id, dims: { width: M.width, height: M.height, cell: M.cell, cols: M.cols, rows: M.rows },
    selected,
  };
}
function restore(s) {
  T = s.T; B = s.B; Z = s.Z; M.blocks = s.blocks; M.zone_meta = s.zone_meta; M.next_id = s.next_id;
  Object.assign(M, s.dims); selected = s.selected;
  recompute(); setDirty(true); renderPanel(); draw();
}
function pushUndo(s) {
  undoStack.push(s || snapshot());
  if (undoStack.length > 80) undoStack.shift();
  redoStack = [];
}
function undo() { if (!M || !undoStack.length) return; redoStack.push(snapshot()); restore(undoStack.pop()); }
function redo() { if (!M || !redoStack.length) return; undoStack.push(snapshot()); restore(redoStack.pop()); }

// ---------------- 视图 ----------------
let dpr = window.devicePixelRatio || 1;
function resizeCanvas() {
  dpr = window.devicePixelRatio || 1;
  cv.width = Math.round(stage.clientWidth * dpr);
  cv.height = Math.round(stage.clientHeight * dpr);
  draw();
}
new ResizeObserver(resizeCanvas).observe(stage);

function fitView() {
  if (!M) return;
  const w = stage.clientWidth, h = stage.clientHeight;
  const W = M.cols * M.cell, H = M.rows * M.cell;
  view.scale = Math.max(0.05, Math.min((w - 40) / W, (h - 40) / H));
  view.ox = (w - W * view.scale) / 2;
  view.oy = (h - H * view.scale) / 2;
  draw();
}
function centerOn(id) {
  const bi = binfo[id];
  if (!bi) return;
  const cs = M.cell;
  const cx = ((bi.c0 + bi.c1 + 1) / 2) * cs, cy = ((bi.r0 + bi.r1 + 1) / 2) * cs;
  view.ox = stage.clientWidth / 2 - cx * view.scale;
  view.oy = stage.clientHeight / 2 - cy * view.scale;
  draw();
}

let rafPending = false;
function draw() {
  if (rafPending) return;
  rafPending = true;
  requestAnimationFrame(() => { rafPending = false; realDraw(); });
}

function realDraw() {
  const w = stage.clientWidth, h = stage.clientHeight;
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  ctx.fillStyle = "#dee2e6";
  ctx.fillRect(0, 0, w, h);
  if (!M) return;
  const cs = M.cell, cols = M.cols, rows = M.rows, W = cols * cs, H = rows * cs, s = view.scale;
  ctx.save();
  ctx.translate(view.ox, view.oy);
  ctx.scale(s, s);
  ctx.fillStyle = "#ffffff";
  ctx.fillRect(0, 0, W, H);
  const useBg = show.bg && bgImg && M.bg.file;
  if (useBg) {
    ctx.globalAlpha = +M.bg.opacity;
    ctx.drawImage(bgImg, 0, 0, W, H);
    ctx.globalAlpha = 1;
  }
  const c0 = Math.max(0, Math.floor(-view.ox / s / cs)), c1 = Math.min(cols - 1, Math.floor((w - view.ox) / s / cs));
  const r0 = Math.max(0, Math.floor(-view.oy / s / cs)), r1 = Math.min(rows - 1, Math.floor((h - view.oy) / s / cs));

  // 格子类型（按行合并同色）
  if (useBg) ctx.globalAlpha = 0.8;
  for (let r = r0; r <= r1; r++) {
    let c = c0;
    while (c <= c1) {
      const t = T[r * cols + c];
      let e = c;
      while (e + 1 <= c1 && T[r * cols + e + 1] === t) e++;
      if (t !== 0) { ctx.fillStyle = TYPE_COLOR[t]; ctx.fillRect(c * cs, r * cs, (e - c + 1) * cs, cs); }
      c = e + 1;
    }
  }
  ctx.globalAlpha = 1;

  // 仓库区块覆盖
  const zonesOn = show.zones || layer === "zones";
  if (zonesOn) {
    ctx.globalAlpha = layer === "zones" ? 0.38 : 0.26;
    for (let r = r0; r <= r1; r++) {
      let c = c0;
      while (c <= c1) {
        const z = Z[r * cols + c];
        let e = c;
        while (e + 1 <= c1 && Z[r * cols + e + 1] === z) e++;
        if (z) { ctx.fillStyle = zoneColor(String.fromCharCode(z)); ctx.fillRect(c * cs, r * cs, (e - c + 1) * cs, cs); }
        c = e + 1;
      }
    }
    ctx.globalAlpha = 1;
    // 区块边界（按颜色分组）
    const segs = {};
    const add = (z, x1, y1, x2, y2) => { if (!z) return; (segs[z] = segs[z] || []).push(x1, y1, x2, y2); };
    for (let r = r0; r <= r1; r++) for (let c = c0; c <= c1; c++) {
      const i = r * cols + c, z = Z[i];
      const zr = c + 1 < cols ? Z[i + 1] : 0, zb = r + 1 < rows ? Z[i + cols] : 0;
      if (zr !== z) { add(z, (c + 1) * cs, r * cs, (c + 1) * cs, (r + 1) * cs); add(zr, (c + 1) * cs, r * cs, (c + 1) * cs, (r + 1) * cs); }
      if (zb !== z) { add(z, c * cs, (r + 1) * cs, (c + 1) * cs, (r + 1) * cs); add(zb, c * cs, (r + 1) * cs, (c + 1) * cs, (r + 1) * cs); }
      if (c === 0 && z) add(z, 0, r * cs, 0, (r + 1) * cs);
      if (r === 0 && z) add(z, c * cs, 0, (c + 1) * cs, 0);
    }
    ctx.lineWidth = 2 / s;
    for (const [z, arr] of Object.entries(segs)) {
      ctx.strokeStyle = zoneColor(String.fromCharCode(+z));
      ctx.beginPath();
      for (let k = 0; k < arr.length; k += 4) { ctx.moveTo(arr[k], arr[k + 1]); ctx.lineTo(arr[k + 2], arr[k + 3]); }
      ctx.stroke();
    }
  }

  // 网格线
  if (show.grid && cs * s >= 5) {
    ctx.strokeStyle = "rgba(0,0,0,0.09)";
    ctx.lineWidth = 1 / s;
    ctx.beginPath();
    for (let c = c0; c <= c1 + 1; c++) { ctx.moveTo(c * cs, r0 * cs); ctx.lineTo(c * cs, (r1 + 1) * cs); }
    for (let r = r0; r <= r1 + 1; r++) { ctx.moveTo(c0 * cs, r * cs); ctx.lineTo((c1 + 1) * cs, r * cs); }
    ctx.stroke();
  }

  // 地图区块边界
  ctx.strokeStyle = "#495057";
  ctx.lineWidth = 2 / s;
  ctx.beginPath();
  for (let r = r0; r <= r1; r++) for (let c = c0; c <= c1; c++) {
    const i = r * cols + c, b = B[i];
    const br = c + 1 < cols ? B[i + 1] : 0, bb = r + 1 < rows ? B[i + cols] : 0;
    if (br !== b && (b || br)) { ctx.moveTo((c + 1) * cs, r * cs); ctx.lineTo((c + 1) * cs, (r + 1) * cs); }
    if (bb !== b && (b || bb)) { ctx.moveTo(c * cs, (r + 1) * cs); ctx.lineTo((c + 1) * cs, (r + 1) * cs); }
    if (c === 0 && b) { ctx.moveTo(0, r * cs); ctx.lineTo(0, (r + 1) * cs); }
    if (r === 0 && b) { ctx.moveTo(c * cs, 0); ctx.lineTo((c + 1) * cs, 0); }
  }
  ctx.stroke();
  ctx.strokeStyle = "#868e96";
  ctx.strokeRect(0, 0, W, H);

  // 选中区块描边
  if (selected && binfo[selected]) {
    const bi = binfo[selected];
    ctx.strokeStyle = "#1c7ed6";
    ctx.lineWidth = 3 / s;
    ctx.beginPath();
    for (let r = bi.r0; r <= bi.r1; r++) for (let c = bi.c0; c <= bi.c1; c++) {
      const i = r * cols + c;
      if (B[i] !== selected) continue;
      if (c + 1 >= cols || B[i + 1] !== selected) { ctx.moveTo((c + 1) * cs, r * cs); ctx.lineTo((c + 1) * cs, (r + 1) * cs); }
      if (c === 0 || B[i - 1] !== selected) { ctx.moveTo(c * cs, r * cs); ctx.lineTo(c * cs, (r + 1) * cs); }
      if (r + 1 >= rows || B[i + cols] !== selected) { ctx.moveTo(c * cs, (r + 1) * cs); ctx.lineTo((c + 1) * cs, (r + 1) * cs); }
      if (r === 0 || B[i - cols] !== selected) { ctx.moveTo(c * cs, r * cs); ctx.lineTo((c + 1) * cs, r * cs); }
    }
    ctx.stroke();
  }

  // 选中的仓库区块：描出它的所有格子
  if (layer === "zones" && selectedZone) {
    const zc = selectedZone.charCodeAt(0);
    ctx.strokeStyle = "#1c7ed6"; ctx.lineWidth = 3 / s;
    ctx.beginPath();
    for (let r = r0; r <= r1; r++) for (let c = c0; c <= c1; c++) {
      const i = r * cols + c;
      if (Z[i] !== zc) continue;
      if (c + 1 >= cols || Z[i + 1] !== zc) { ctx.moveTo((c + 1) * cs, r * cs); ctx.lineTo((c + 1) * cs, (r + 1) * cs); }
      if (c === 0 || Z[i - 1] !== zc) { ctx.moveTo(c * cs, r * cs); ctx.lineTo(c * cs, (r + 1) * cs); }
      if (r + 1 >= rows || Z[i + cols] !== zc) { ctx.moveTo(c * cs, (r + 1) * cs); ctx.lineTo((c + 1) * cs, (r + 1) * cs); }
      if (r === 0 || Z[i - cols] !== zc) { ctx.moveTo(c * cs, r * cs); ctx.lineTo((c + 1) * cs, r * cs); }
    }
    ctx.stroke();
  }

  // 缩放手柄（仅矩形区块）
  {
    const rc = drag && drag.kind === "resize" ? drag.cur : resizableRect();
    if (rc) {
      if (drag && drag.kind === "resize") {
        ctx.strokeStyle = "#1c7ed6"; ctx.lineWidth = 2 / s; ctx.setLineDash([6 / s, 4 / s]);
        ctx.strokeRect(rc.c0 * cs, rc.r0 * cs, (rc.c1 - rc.c0 + 1) * cs, (rc.r1 - rc.r0 + 1) * cs);
        ctx.setLineDash([]);
      }
      const hs = 8 / s;
      ctx.fillStyle = "#fff"; ctx.strokeStyle = "#1c7ed6"; ctx.lineWidth = 1.5 / s;
      for (const h of HANDLES) {
        const p = handlePos(rc, h);
        ctx.fillRect(p.x - hs / 2, p.y - hs / 2, hs, hs);
        ctx.strokeRect(p.x - hs / 2, p.y - hs / 2, hs, hs);
      }
    }
  }

  // 文字
  ctx.textAlign = "center";
  ctx.textBaseline = "middle";
  for (const [id, bi] of Object.entries(binfo)) {
    const meta = M.blocks[id];
    const bw = (bi.c1 - bi.c0 + 1) * cs, bh = (bi.r1 - bi.r0 + 1) * cs;
    let text, bad = false;
    if (meta.type === "storage") {
      text = `${bi.zone || "?"}-${meta.shelf || "??"}`;
      bad = bi.dup || !!bi.zoneProblem || !/^\d{2}$/.test(meta.shelf || "") || issueBlocks.has(+id);
    } else {
      text = meta.label || "";
      if (!text) continue;
    }
    let size = Math.min(cs * 3, bh * 0.72);
    ctx.font = `600 ${size}px "Microsoft YaHei", sans-serif`;
    const tw = ctx.measureText(text).width;
    if (tw > bw * 0.92) { size = size * (bw * 0.92) / tw; ctx.font = `600 ${size}px "Microsoft YaHei", sans-serif`; }
    if (size * s < 5) continue;
    ctx.fillStyle = bad ? "#e03131" : "#212529";
    ctx.fillText(text, bi.c0 * cs + bw / 2, bi.r0 * cs + bh / 2);
  }
  if (zonesOn) {
    for (const [z, zi] of Object.entries(zinfo)) {
      const letter = String.fromCharCode(+z);
      const size = Math.max(cs * 2, Math.min(cs * 5, Math.sqrt(zi.count) * cs * 0.35));
      ctx.font = `700 ${size}px "Microsoft YaHei", sans-serif`;
      ctx.globalAlpha = 0.55;
      ctx.fillStyle = zoneColor(letter);
      ctx.fillText(letter, (zi.sc / zi.count + 0.5) * cs, (zi.sr / zi.count + 0.5) * cs);
      ctx.globalAlpha = 1;
    }
  }

  // 矩形预览
  if (drag && drag.kind === "rect" && drag.cur) {
    const a = drag.start, b = drag.cur;
    const rr0 = Math.min(a.r, b.r), rr1 = Math.max(a.r, b.r), cc0 = Math.min(a.c, b.c), cc1 = Math.max(a.c, b.c);
    const color = layer === "zones" ? (zoneErase ? "#868e96" : zoneColor(zoneLetter)) : (brushType === 0 ? "#868e96" : "#1c7ed6");
    ctx.fillStyle = color; ctx.globalAlpha = 0.25;
    ctx.fillRect(cc0 * cs, rr0 * cs, (cc1 - cc0 + 1) * cs, (rr1 - rr0 + 1) * cs);
    ctx.globalAlpha = 1; ctx.strokeStyle = color; ctx.lineWidth = 2 / s;
    ctx.strokeRect(cc0 * cs, rr0 * cs, (cc1 - cc0 + 1) * cs, (rr1 - rr0 + 1) * cs);
  }
  ctx.restore();
}

// ---------------- 编辑 ----------------
function cellAt(ev) {
  const rect = cv.getBoundingClientRect();
  const x = (ev.clientX - rect.left - view.ox) / view.scale, y = (ev.clientY - rect.top - view.oy) / view.scale;
  const c = Math.floor(x / M.cell), r = Math.floor(y / M.cell);
  return { r, c, inside: r >= 0 && c >= 0 && r < M.rows && c < M.cols };
}

// ---- 矩形区块的缩放手柄 ----
const HANDLES = [
  { dx: -1, dy: -1, cur: "nwse-resize" }, { dx: 0, dy: -1, cur: "ns-resize" }, { dx: 1, dy: -1, cur: "nesw-resize" },
  { dx: -1, dy: 0, cur: "ew-resize" }, { dx: 1, dy: 0, cur: "ew-resize" },
  { dx: -1, dy: 1, cur: "nesw-resize" }, { dx: 0, dy: 1, cur: "ns-resize" }, { dx: 1, dy: 1, cur: "nwse-resize" },
];

// 选中的是一个完整矩形的货架/功能区块时，返回它的范围；画笔涂出的不规则区块不支持缩放
function resizableRect() {
  if (layer !== "blocks" || !selected) return null;
  const bi = binfo[selected];
  if (!bi || bi.count !== (bi.r1 - bi.r0 + 1) * (bi.c1 - bi.c0 + 1)) return null;
  return { r0: bi.r0, r1: bi.r1, c0: bi.c0, c1: bi.c1 };
}

function handlePos(rc, h) {
  const cs = M.cell;
  return {
    x: h.dx < 0 ? rc.c0 * cs : h.dx > 0 ? (rc.c1 + 1) * cs : (rc.c0 + rc.c1 + 1) * cs / 2,
    y: h.dy < 0 ? rc.r0 * cs : h.dy > 0 ? (rc.r1 + 1) * cs : (rc.r0 + rc.r1 + 1) * cs / 2,
  };
}

function handleAt(ev) {
  const rc = resizableRect();
  if (!rc) return null;
  const rect = cv.getBoundingClientRect();
  const px = ev.clientX - rect.left, py = ev.clientY - rect.top;
  for (const h of HANDLES) {
    const p = handlePos(rc, h);
    if (Math.abs(px - (p.x * view.scale + view.ox)) <= 7 && Math.abs(py - (p.y * view.scale + view.oy)) <= 7) return { h, rc };
  }
  return null;
}

// 把区块改成新范围。新范围里属于其他区块的格子保持不动（不会吞掉别的区块）
function applyResize(id, nr) {
  const type = M.blocks[id].type === "storage" ? 2 : 1;
  pushUndo();
  for (let i = 0; i < B.length; i++) if (B[i] === id) { B[i] = 0; T[i] = 0; }
  for (let r = nr.r0; r <= nr.r1; r++) for (let c = nr.c0; c <= nr.c1; c++) {
    const i = r * M.cols + c;
    if (B[i] && B[i] !== id) continue;
    T[i] = type; B[i] = id;
  }
  recompute(); setDirty(true); renderPanel(); draw();
}

function newBlock(type) {
  const id = M.next_id++;
  M.blocks[id] = type === 2
    ? { type: "storage", shelf: "", layer_min: lastLayers.min, layer_max: lastLayers.max, regions: lastRegions }
    : { type: "function", label: "" };
  return id;
}

function strokeTarget(shift) {
  if (layer !== "blocks" || (brushType !== 1 && brushType !== 2)) return 0;
  const want = brushType === 2 ? "storage" : "function";
  let append = document.querySelector('input[name="strokeMode"]:checked').value === "append";
  if (shift) append = !append;
  if (append && selected && M.blocks[selected] && M.blocks[selected].type === want) return selected;
  const id = newBlock(brushType);
  createdInStroke = id;
  selected = id;
  return id;
}

function paintCell(r, c, target) {
  if (r < 0 || c < 0 || r >= M.rows || c >= M.cols) return;
  const i = r * M.cols + c;
  if (layer === "zones") {
    Z[i] = zoneErase ? 0 : zoneLetter.charCodeAt(0);
    return;
  }
  T[i] = brushType;
  B[i] = brushType === 1 || brushType === 2 ? target : 0;
}

function paintLine(a, b, target) {
  let x0 = a.c, y0 = a.r;
  const x1 = b.c, y1 = b.r;
  const dx = Math.abs(x1 - x0), dy = -Math.abs(y1 - y0), sx = x0 < x1 ? 1 : -1, sy = y0 < y1 ? 1 : -1;
  let err = dx + dy;
  for (;;) {
    paintCell(y0, x0, target);
    if (x0 === x1 && y0 === y1) break;
    const e2 = 2 * err;
    if (e2 >= dy) { err += dy; x0 += sx; }
    if (e2 <= dx) { err += dx; y0 += sy; }
  }
}

function ensureZoneMeta() {
  if (layer === "zones" && !zoneErase && !M.zone_meta[zoneLetter]) {
    M.zone_meta[zoneLetter] = { color: $("#zoneColor").value || zoneColor(zoneLetter) };
  }
}

function endStroke() {
  recompute();
  const id = createdInStroke;
  createdInStroke = 0;
  if (id && M.blocks[id] && M.blocks[id].type === "storage" && !M.blocks[id].shelf) autoNumber(id);
  setDirty(true);
  renderPanel();
  draw();
}

function autoNumber(id) {
  const bi = binfo[id];
  if (!bi || !bi.zone) return;
  const used = new Set(Object.entries(M.blocks)
    .filter(([k, b]) => b.type === "storage" && +k !== +id && binfo[k] && binfo[k].zone === bi.zone)
    .map(([, b]) => b.shelf));
  for (let n = 1; n <= 99; n++) if (!used.has(pad2(n))) { M.blocks[id].shelf = pad2(n); break; }
  recompute();
}

function autoNumberAll() {
  const todo = Object.keys(binfo).filter((id) => M.blocks[id].type === "storage" && !M.blocks[id].shelf && binfo[id].zone)
    .sort((a, b) => binfo[a].r0 - binfo[b].r0 || binfo[a].c0 - binfo[b].c0);
  if (!todo.length) { toast("没有需要编号的货架（未编号且已落在仓库区块内）"); return; }
  pushUndo();
  todo.forEach(autoNumber);
  setDirty(true); renderPanel(); draw();
  toast(`已为 ${todo.length} 个货架自动编号`);
}

function deleteBlock(id) {
  if (!M.blocks[id]) return;
  pushUndo();
  for (let i = 0; i < B.length; i++) if (B[i] === +id) { B[i] = 0; T[i] = 0; }
  delete M.blocks[id];
  if (selected === +id) selected = 0;
  recompute(); setDirty(true); renderPanel(); draw();
}

// ---------------- 鼠标 ----------------
cv.addEventListener("contextmenu", (e) => e.preventDefault());
cv.addEventListener("pointerdown", (ev) => {
  if (!M) return;
  cv.setPointerCapture(ev.pointerId);
  if (ev.button === 1 || ev.button === 2 || spaceDown) {
    drag = { kind: "pan", x: ev.clientX, y: ev.clientY, ox: view.ox, oy: view.oy };
    cv.style.cursor = "grabbing";
    return;
  }
  if (ev.button !== 0) return;
  const hit = handleAt(ev);
  if (hit) {
    drag = { kind: "resize", id: selected, h: hit.h, orig: hit.rc, cur: { ...hit.rc } };
    return;
  }
  const p = cellAt(ev);
  if (tool === "select") {
    if (layer === "zones") {
      const z = p.inside ? Z[p.r * M.cols + p.c] : 0;
      selectedZone = z ? String.fromCharCode(z) : "";
      selected = 0;
    } else selected = p.inside ? B[p.r * M.cols + p.c] : 0;
    renderPanel(); draw();
    return;
  }
  if (!p.inside) return;
  if (tool === "rect") {
    drag = { kind: "rect", start: p, cur: p, shift: ev.shiftKey };
    draw();
    return;
  }
  pushUndo();
  ensureZoneMeta();
  const target = strokeTarget(ev.shiftKey);
  drag = { kind: "paint", last: p, target };
  paintCell(p.r, p.c, target);
  recompute(); draw();
});

cv.addEventListener("pointermove", (ev) => {
  if (!M) return;
  const p = cellAt(ev);
  const coord = $("#coord");
  if (p.inside) {
    const i = p.r * M.cols + p.c, b = B[i], z = Z[i];
    let s = `行 ${p.r + 1} · 列 ${p.c + 1} · ${TYPE_NAME[T[i]]}`;
    if (z) s += ` · 区块 ${String.fromCharCode(z)}`;
    if (b && M.blocks[b]) s += M.blocks[b].type === "storage"
      ? ` · 货架 ${binfo[b]?.zone || "?"}-${M.blocks[b].shelf || "??"}` : ` · ${M.blocks[b].label || "功能区块"}`;
    coord.textContent = s;
  } else coord.textContent = "";
  if (!drag) {
    const hit = handleAt(ev);
    cv.style.cursor = hit ? hit.h.cur : (tool === "select" ? "pointer" : "crosshair");
    return;
  }
  if (drag.kind === "resize") {
    const rr = Math.max(0, Math.min(M.rows - 1, p.r)), cc = Math.max(0, Math.min(M.cols - 1, p.c));
    const o = drag.orig, h = drag.h, n = { ...o };
    if (h.dx < 0) n.c0 = Math.min(cc, o.c1); else if (h.dx > 0) n.c1 = Math.max(cc, o.c0);
    if (h.dy < 0) n.r0 = Math.min(rr, o.r1); else if (h.dy > 0) n.r1 = Math.max(rr, o.r0);
    drag.cur = n;
    draw();
    return;
  }
  if (drag.kind === "pan") {
    view.ox = drag.ox + ev.clientX - drag.x;
    view.oy = drag.oy + ev.clientY - drag.y;
    draw();
  } else if (drag.kind === "paint") {
    if (p.r !== drag.last.r || p.c !== drag.last.c) {
      paintLine(drag.last, p, drag.target);
      drag.last = p;
      recompute(); draw();
    }
  } else if (drag.kind === "rect") {
    drag.cur = { r: Math.max(0, Math.min(M.rows - 1, p.r)), c: Math.max(0, Math.min(M.cols - 1, p.c)) };
    draw();
  }
});

function finishDrag(ev) {
  if (!drag) return;
  const d = drag;
  drag = null;
  cv.style.cursor = "";
  if (d.kind === "paint") endStroke();
  else if (d.kind === "resize") {
    const o = d.orig, n = d.cur;
    if (n.r0 !== o.r0 || n.r1 !== o.r1 || n.c0 !== o.c0 || n.c1 !== o.c1) applyResize(d.id, n);
    else draw();
  } else if (d.kind === "rect") {
    pushUndo();
    ensureZoneMeta();
    const target = strokeTarget(d.shift || (ev && ev.shiftKey));
    const r0 = Math.min(d.start.r, d.cur.r), r1 = Math.max(d.start.r, d.cur.r);
    const c0 = Math.min(d.start.c, d.cur.c), c1 = Math.max(d.start.c, d.cur.c);
    for (let r = r0; r <= r1; r++) for (let c = c0; c <= c1; c++) paintCell(r, c, target);
    endStroke();
  } else draw();
}
cv.addEventListener("pointerup", finishDrag);
cv.addEventListener("pointercancel", () => {
  if (drag && drag.kind === "resize") { drag = null; draw(); return; }
  if (drag && drag.kind !== "pan") finishDrag();
  drag = null;
});

cv.addEventListener("wheel", (ev) => {
  if (!M) return;
  ev.preventDefault();
  const rect = cv.getBoundingClientRect();
  const mx = ev.clientX - rect.left, my = ev.clientY - rect.top;
  const k = Math.exp(-ev.deltaY * 0.0015);
  const ns = Math.max(0.05, Math.min(10, view.scale * k));
  view.ox = mx - (mx - view.ox) * (ns / view.scale);
  view.oy = my - (my - view.oy) * (ns / view.scale);
  view.scale = ns;
  draw();
}, { passive: false });

// ---------------- 键盘 ----------------
window.addEventListener("keydown", (ev) => {
  const typing = ["INPUT", "SELECT", "TEXTAREA"].includes(document.activeElement.tagName);
  if ((ev.ctrlKey || ev.metaKey) && ev.key.toLowerCase() === "s") { ev.preventDefault(); save(); return; }
  if (typing) return;
  if ((ev.ctrlKey || ev.metaKey) && ev.key.toLowerCase() === "z") { ev.preventDefault(); ev.shiftKey ? redo() : undo(); return; }
  if ((ev.ctrlKey || ev.metaKey) && ev.key.toLowerCase() === "y") { ev.preventDefault(); redo(); return; }
  if (ev.key === " ") { spaceDown = true; cv.style.cursor = "grab"; ev.preventDefault(); return; }
  if (ev.key === "Escape") { selected = 0; selectedZone = ""; renderPanel(); draw(); return; }
  if ((ev.key === "Delete" || ev.key === "Backspace") && selected) { deleteBlock(selected); return; }
  const k = ev.key.toLowerCase();
  if (k === "r") setTool("rect");
  else if (k === "b") setTool("brush");
  else if (k === "v") setTool("select");
  else if (["1", "2", "3", "4"].includes(k) && layer === "blocks") {
    const v = { 1: 2, 2: 1, 3: 3, 4: 0 }[k];
    document.querySelector(`input[name="btype"][value="${v}"]`).checked = true;
    brushType = v; updateHint();
  }
});
window.addEventListener("keyup", (ev) => { if (ev.key === " ") { spaceDown = false; cv.style.cursor = ""; } });
window.addEventListener("beforeunload", (ev) => { if (dirty) { ev.preventDefault(); ev.returnValue = ""; } });

// ---------------- 工具栏 ----------------
function setTool(t) {
  tool = t;
  $$("#toolSeg button").forEach((b) => b.classList.toggle("on", b.dataset.tool === t));
  cv.style.cursor = t === "select" ? "pointer" : "crosshair";
  updateHint();
}
function setLayer(l) {
  layer = l;
  selectedZone = "";
  $$("#layerSeg button").forEach((b) => b.classList.toggle("on", b.dataset.layer === l));
  $("#blockPalette").hidden = l !== "blocks";
  $("#zonePalette").hidden = l !== "zones";
  updateHint(); renderPanel(); draw();
}
function updateHint() {
  const h = $("#hint");
  if (layer === "zones") {
    h.innerHTML = "选择字母后在地图上涂抹，划分仓库区块（库位码第 2 段）。<br>货架属于哪个区块，由它所在的位置自动决定。<br>用<b>选择</b>工具点一个区块，可在右侧修改它的字母、颜色，或清除。<br>右键拖动平移，滚轮缩放。";
  } else {
    h.innerHTML = "<b>矩形</b>适合画货架，<b>画笔</b>适合画不规则区域。<br>每个新笔画默认新建一个区块，按住 Shift 追加到选中区块。<br>用<b>选择</b>工具点区块，在右侧填写货架编码、层号或文字标注。<br>右键或空格+拖动平移，滚轮缩放，Delete 删除选中区块。";
  }
}
$$("#toolSeg button").forEach((b) => b.addEventListener("click", () => setTool(b.dataset.tool)));
$$("#layerSeg button").forEach((b) => b.addEventListener("click", () => setLayer(b.dataset.layer)));
$$('input[name="btype"]').forEach((r) => r.addEventListener("change", () => { brushType = +r.value; updateHint(); }));
$("#zoneLetter").innerHTML = LETTERS.map((l) => `<option>${l}</option>`).join("");
$("#zoneLetter").addEventListener("change", () => {
  zoneLetter = $("#zoneLetter").value;
  if (M) $("#zoneColor").value = zoneColor(zoneLetter);
});
$("#zoneColor").addEventListener("change", () => {
  if (!M) return;
  pushUndo();
  M.zone_meta[zoneLetter] = { color: $("#zoneColor").value };
  setDirty(true); draw();
});
$("#zoneErase").addEventListener("change", () => (zoneErase = $("#zoneErase").checked));
$("#showZones").addEventListener("change", () => { show.zones = $("#showZones").checked; draw(); });
$("#showGrid").addEventListener("change", () => { show.grid = $("#showGrid").checked; draw(); });
$("#showBg").addEventListener("change", () => {
  show.bg = $("#showBg").checked;
  if (M) { M.bg.show = show.bg; setDirty(true); }
  draw();
});
$("#bgOpacity").addEventListener("input", () => { if (M) { M.bg.opacity = +$("#bgOpacity").value; setDirty(true); draw(); } });
$("#btnFit").addEventListener("click", fitView);
$("#btnBg").addEventListener("click", () => { if (M) $("#bgFile").click(); });
$("#bgFile").addEventListener("change", async () => {
  const f = $("#bgFile").files[0];
  if (!f) return;
  const fd = new FormData();
  fd.append("file", f);
  const res = await fetch(`/api/maps/${M.wh_no}/bg`, { method: "POST", body: fd });
  const data = await res.json();
  if (!res.ok) { toast(data.detail || "上传失败", true); return; }
  M.bg.file = data.file; M.bg.show = true; show.bg = true; $("#showBg").checked = true;
  setDirty(true); loadBg();
  $("#bgFile").value = "";
  toast("底图已导入，保存后生效");
});
$("#whName").addEventListener("input", () => M && setDirty(true));
$("#whSelect").addEventListener("change", async () => {
  const wh = $("#whSelect").value;
  if (dirty && !confirm("当前地图还没保存，确定切换吗？")) { $("#whSelect").value = M.wh_no; return; }
  await loadMap(wh);
});
$("#btnUndo").addEventListener("click", undo);
$("#btnRedo").addEventListener("click", redo);
$("#btnSave").addEventListener("click", save);
$("#btnCheck").addEventListener("click", check);
$("#btnExport").addEventListener("click", () => {
  if (!M) return;
  if (dirty) { toast("请先保存，再导出图片", true); return; }
  window.open(`/api/maps/${M.wh_no}/render.png?zones=${show.zones ? 1 : 0}`, "_blank");
});

// ---------------- 右侧属性面板 ----------------
function renderPanel() {
  const p = $("#propPanel");
  if (!M) { p.innerHTML = ""; return; }
  if (layer === "zones" && selectedZone) {
    if (zinfo[selectedZone.charCodeAt(0)]) { renderZonePanel(p); return; }
    selectedZone = "";
  }
  const meta = selected && M.blocks[selected];
  if (!meta) { renderSummary(p); return; }
  const bi = binfo[selected];
  if (meta.type === "function") {
    p.innerHTML = `
      <div class="prop-title"><i class="sw t1"></i><strong>功能区块</strong><span class="muted">#${selected} · ${bi.count} 格</span></div>
      <div class="kv"><span>文字标注</span><input id="pLabel" value="${esc(meta.label)}" placeholder="如：打包台 / 收货区"></div>
      <button type="button" class="danger" id="pDelete">删除此区块</button>`;
    bindEdit("#pLabel", (v) => (meta.label = v));
  } else {
    const zone = bi.zone || "?";
    const layerOpts = (v) => Array.from({ length: 31 }, (_, i) => `<option ${i === +v ? "selected" : ""}>${i}</option>`).join("");
    const problems = [];
    if (bi.zoneProblem) problems.push(bi.zoneProblem);
    if (!/^\d{2}$/.test(meta.shelf || "")) problems.push("货架编码需为两位数字");
    if (bi.dup) problems.push(`货架编码 ${zone}-${meta.shelf} 重复`);
    p.innerHTML = `
      <div class="prop-title"><i class="sw t2"></i><strong>货架</strong><span class="muted">#${selected} · ${bi.count} 格</span></div>
      <div class="kv">
        <span>仓库区块</span><b>${bi.zone || '<span class="warn-text">未确定</span>'}</b>
        <span>货架编码</span><input id="pShelf" class="shelf" maxlength="2" value="${esc(meta.shelf)}" placeholder="01">
        <span>层号范围</span><div><select id="pLmin">${layerOpts(meta.layer_min)}</select> 至 <select id="pLmax">${layerOpts(meta.layer_max)}</select></div>
        <span>每层区域数</span><div><select id="pRegions">${Array.from({ length: 30 }, (_, i) => `<option ${i + 1 === (+meta.regions || 1) ? "selected" : ""}>${i + 1}</option>`).join("")}</select> 个 <span class="muted">（每一层都分成这么多个大区域）</span></div>
      </div>
      <div class="code-preview">${M.wh_no}-${zone}-${meta.shelf || "??"}-<span class="muted">层</span>-<span class="muted">区域</span>/${meta.regions || 1}</div>
      ${problems.map((t) => `<div class="warn-text">⚠ ${esc(t)}</div>`).join("")}
      <div id="pStock" class="stock-list"></div>
      <button type="button" class="danger" id="pDelete">删除此货架</button>`;
    bindEdit("#pShelf", (v) => (meta.shelf = v.replace(/\D/g, "").slice(0, 2)), (el) => {
      if (/^\d$/.test(el.value)) { el.value = pad2(el.value); meta.shelf = el.value; }
    });
    const onLayer = () => {
      let lo = +$("#pLmin").value, hi = +$("#pLmax").value;
      if (lo > hi) { hi = lo; $("#pLmax").value = hi; }
      meta.layer_min = lo; meta.layer_max = hi; lastLayers = { min: lo, max: hi };
    };
    bindEdit("#pLmin", onLayer); bindEdit("#pLmax", onLayer);
    bindEdit("#pRegions", (v) => { meta.regions = +v; lastRegions = +v; });
    if (bi.zone && /^\d{2}$/.test(meta.shelf || "")) loadShelfStock(M.wh_no, bi.zone, meta.shelf);
  }
  $("#pDelete").addEventListener("click", () => deleteBlock(selected));
}

function renderZonePanel(p) {
  const letter = selectedZone, code = letter.charCodeAt(0);
  const shelves = Object.values(binfo).filter((b) => b.zone === letter).length;
  p.innerHTML = `
    <div class="prop-title"><i class="sw" style="background:${zoneColor(letter)}"></i><strong>仓库区块 ${letter}</strong><span class="muted">${zinfo[code].count} 格 · ${shelves} 个货架</span></div>
    <div class="kv">
      <span>区块字母</span><select id="pZone">${LETTERS.map((l) => `<option ${l === letter ? "selected" : ""}>${l}</option>`).join("")}</select>
      <span>覆盖颜色</span><input type="color" id="pZoneColor" value="${zoneColor(letter)}">
    </div>
    <p class="note">改字母会同步改变区块内所有货架的库位码（如 ${M.wh_no}-${letter}-01 → ${M.wh_no}-新字母-01）。选已有的字母会与那个区块合并。若这些货架已有库存登记，保存时会被拒绝。</p>
    <button type="button" class="danger" id="pZoneDelete">清除此区块字母</button>`;
  $("#pZone").addEventListener("change", () => renameZone(letter, $("#pZone").value));
  $("#pZoneColor").addEventListener("change", () => {
    pushUndo();
    M.zone_meta[letter] = { ...(M.zone_meta[letter] || {}), color: $("#pZoneColor").value };
    setDirty(true); renderPanel(); draw();
  });
  $("#pZoneDelete").addEventListener("click", () => {
    pushUndo();
    for (let i = 0; i < Z.length; i++) if (Z[i] === code) Z[i] = 0;
    delete M.zone_meta[letter];
    selectedZone = "";
    recompute(); setDirty(true); renderPanel(); draw();
  });
}

function renameZone(from, to) {
  if (from === to) return;
  const fc = from.charCodeAt(0), tc = to.charCodeAt(0);
  const merge = !!zinfo[tc];
  if (merge && !confirm(`区块 ${to} 已存在，改名会把 ${from} 合并进 ${to}。继续吗？`)) { renderPanel(); return; }
  pushUndo();
  for (let i = 0; i < Z.length; i++) if (Z[i] === fc) Z[i] = tc;
  if (!merge && M.zone_meta[from]) M.zone_meta[to] = M.zone_meta[from];
  delete M.zone_meta[from];
  selectedZone = to;
  recompute(); setDirty(true); renderPanel(); draw();
}

function bindEdit(sel, apply, onBlur) {
  const el = $(sel);
  el.addEventListener("focus", () => (preEditSnapshot = snapshot()));
  const handler = () => {
    if (preEditSnapshot) { pushUndo(preEditSnapshot); preEditSnapshot = null; }
    apply(el.value);
    recompute(); setDirty(true); draw();
  };
  el.addEventListener(el.tagName === "SELECT" ? "change" : "input", handler);
  el.addEventListener("blur", () => {
    preEditSnapshot = null;
    if (onBlur) { onBlur(el); recompute(); draw(); }
    renderPanelSoon();
  });
}
let panelTimer = null;
function renderPanelSoon() {
  clearTimeout(panelTimer);
  panelTimer = setTimeout(() => {
    if (!["INPUT", "SELECT"].includes(document.activeElement.tagName) || !$("#propPanel").contains(document.activeElement)) renderPanel();
  }, 50);
}

async function loadShelfStock(wh, zone, shelf) {
  try {
    const { data } = await api("GET", `/api/shelf/${wh}/${zone}/${shelf}`);
    const box = $("#pStock");
    if (!box) return;
    if (!data.length) { box.innerHTML = '<p class="note">该货架暂无库存登记（或尚未保存）</p>'; return; }
    box.innerHTML = `<h3>当前登记 ${data.length} 条</h3><table><tr><th>层</th><th>区域</th><th>SKU</th><th>品名</th><th>余量</th></tr>${
      data.map((r) => `<tr><td>${r.layer}</td><td>${r.loc_code.split("-").pop()}</td><td>${esc(r.sku)}</td><td>${esc(r.name)}</td><td>${r.qty ?? "—"}</td></tr>`).join("")}</table>`;
  } catch { /* 忽略 */ }
}

function renderSummary(p) {
  const st = Object.entries(M.blocks).filter(([, b]) => b.type === "storage");
  const fn = Object.entries(M.blocks).filter(([, b]) => b.type === "function");
  const zl = Object.keys(zinfo).map((z) => String.fromCharCode(+z)).sort();
  const unnumbered = st.filter(([id, b]) => !b.shelf).length;
  p.innerHTML = `
    <h3>仓库 ${M.wh_no} 概况</h3>
    <div class="summary-grid">
      <div><b>${st.length}</b><span>货架</span></div>
      <div><b>${fn.length}</b><span>功能区块</span></div>
      <div><b>${zl.length}</b><span>仓库区块 ${zl.join(" ")}</span></div>
      <div><b>${M.cols}×${M.rows}</b><span>网格（格子 ${M.cell}px）</span></div>
    </div>
    ${unnumbered ? `<button type="button" id="btnAutoNum">为 ${unnumbered} 个未编号货架自动编号</button>` : ""}
    <p class="note">用"选择"工具（V）点击区块查看和编辑属性。</p>`;
  const b = $("#btnAutoNum");
  if (b) b.addEventListener("click", autoNumberAll);
}

// ---------------- 检查 / 保存 ----------------
function showIssues(errors, warnings, okText) {
  const ul = $("#issues");
  issueBlocks = new Set(errors.filter((e) => e.block).map((e) => e.block));
  const li = (cls, e) => `<li class="${cls}" ${e.block ? `data-block="${e.block}"` : ""}>${esc(e.msg)}</li>`;
  ul.innerHTML = errors.map((e) => li("err", e)).join("") + warnings.map((e) => li("warn", e)).join("") +
    (!errors.length ? `<li class="ok">${esc(okText || "没有发现错误")}</li>` : "");
  const cnt = $("#issueCount");
  cnt.textContent = errors.length ? errors.length : "";
  cnt.className = "badge" + (errors.length ? " err" : "");
  ul.querySelectorAll("li[data-block]").forEach((el) => el.addEventListener("click", () => {
    selected = +el.dataset.block;
    setTool("select"); centerOn(selected); renderPanel(); draw();
  }));
  draw();
}

async function check() {
  if (!M) return;
  try {
    const { data } = await api("POST", `/api/maps/${M.wh_no}/validate`, serialize());
    showIssues(data.errors, data.warnings, "检查通过，可以保存");
  } catch (e) { toast(e.message, true); }
}

async function save() {
  if (!M) return;
  const body = serialize();
  try {
    const { status, data } = await api("PUT", `/api/maps/${M.wh_no}`, body);
    if (status === 422) {
      showIssues(data.errors, data.warnings);
      toast(`有 ${data.errors.length} 个问题，未保存`, true);
      return;
    }
    M.name = body.name;
    setDirty(false);
    showIssues([], data.warnings, `已保存：${data.shelves} 个货架，${data.zones} 个仓库区块`);
    toast("已保存");
    const opt = $(`#whSelect option[value="${M.wh_no}"]`);
    if (opt) opt.textContent = `${M.wh_no} ${M.name}`;
    renderPanel();
  } catch (e) { toast("保存失败：" + e.message, true); }
}

// ---------------- 对话框 ----------------
function gridNote(form) {
  const w = +form.width.value, h = +form.height.value, c = +form.cell.value;
  const note = form.querySelector("[data-grid-note]");
  if (c > 0) note.textContent = `网格 ${Math.floor(w / c)} 列 × ${Math.floor(h / c)} 行，共 ${Math.floor(w / c) * Math.floor(h / c)} 格`;
}

function openNewWh() {
  const dlg = $("#dlgNewWh"), form = dlg.querySelector("form");
  form.reset();
  form.querySelector("[data-error]").textContent = "";
  const used = new Set([...$("#whSelect").options].map((o) => o.value));
  for (let n = 1; n <= 99; n++) if (!used.has(pad2(n))) { form.wh_no.value = pad2(n); break; }
  gridNote(form);
  dlg.showModal();
}
$("#btnNewWh").addEventListener("click", openNewWh);
$("#btnNewWh2").addEventListener("click", openNewWh);
["#dlgNewWh", "#dlgCanvas"].forEach((id) => $(id).querySelector("form").addEventListener("input", (e) => gridNote(e.currentTarget)));
$("#dlgNewWh").querySelector("form").addEventListener("submit", async (ev) => {
  if (ev.submitter && ev.submitter.value !== "ok") return;
  ev.preventDefault();
  const f = ev.currentTarget;
  if (dirty && !confirm("当前地图还没保存，确定继续吗？")) return;
  try {
    await api("POST", "/api/warehouses", {
      wh_no: f.wh_no.value.trim(), name: f.name.value.trim(),
      width: +f.width.value, height: +f.height.value, cell: +f.cell.value,
    });
    $("#dlgNewWh").close();
    setDirty(false);
    await loadWarehouses(f.wh_no.value.trim());
    toast("仓库已创建");
  } catch (e) { f.querySelector("[data-error]").textContent = e.message; }
});

$("#btnCanvas").addEventListener("click", () => {
  if (!M) return;
  const dlg = $("#dlgCanvas"), f = dlg.querySelector("form");
  f.width.value = M.width; f.height.value = M.height; f.cell.value = M.cell;
  gridNote(f);
  dlg.showModal();
});
$("#dlgCanvas").querySelector("form").addEventListener("submit", async (ev) => {
  const f = ev.currentTarget, v = ev.submitter && ev.submitter.value;
  if (v === "delete") {
    ev.preventDefault();
    if (!confirm(`确定删除仓库 ${M.wh_no} 的地图吗？此操作不能撤销。`)) return;
    const res = await fetch(`/api/warehouses/${M.wh_no}`, { method: "DELETE" });
    const data = await res.json();
    if (!res.ok) { toast(data.errors ? data.errors[0].msg : "删除失败", true); return; }
    $("#dlgCanvas").close(); setDirty(false);
    await loadWarehouses();
    return;
  }
  if (v !== "ok") return;
  const w = +f.width.value, h = +f.height.value, cell = +f.cell.value;
  if (!(cell >= 4 && w >= cell && h >= cell)) { ev.preventDefault(); toast("尺寸不合理", true); return; }
  if (Math.floor(w / cell) * Math.floor(h / cell) > 250000) { ev.preventDefault(); toast("网格太多（超过 25 万格），请加大格子尺寸", true); return; }
  resizeGrid(w, h, cell);
});

function resizeGrid(width, height, cell) {
  const cols = Math.floor(width / cell), rows = Math.floor(height / cell);
  if (width === M.width && height === M.height && cell === M.cell) return;
  pushUndo();
  const oc = M.cols, or = M.rows, ocell = M.cell;
  const nT = new Uint8Array(rows * cols), nB = new Int32Array(rows * cols), nZ = new Uint8Array(rows * cols);
  for (let r = 0; r < rows; r++) {
    const orr = Math.floor(((r + 0.5) * cell) / ocell);
    if (orr >= or) continue;
    for (let c = 0; c < cols; c++) {
      const occ = Math.floor(((c + 0.5) * cell) / ocell);
      if (occ >= oc) continue;
      const i = r * cols + c, j = orr * oc + occ;
      nT[i] = T[j]; nB[i] = B[j]; nZ[i] = Z[j];
    }
  }
  T = nT; B = nB; Z = nZ;
  Object.assign(M, { width, height, cell, cols, rows });
  recompute(); setDirty(true); fitView(); renderPanel();
  toast(`画布已调整为 ${cols}×${rows}，保存后生效`);
}

// ---------------- 启动 ----------------
setTool("rect");
setLayer("blocks");
loadWarehouses().catch((e) => toast("载入失败：" + e.message, true));
