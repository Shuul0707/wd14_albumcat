"""The little web page that lets a human look at the classifiers.

Served by ``albumcat serve`` at ``/ui``; the page itself posts images to
``/classify`` and draws the *unfiltered* class scores, which the phone never
sees (it only ever receives the winning tag).  It exists because "梗图基本都被
分成了插画" is impossible to argue about from a tag list that has already thrown
the runner-up away.

The usual way in is a folder: pick one (or drop it on the page), every image is
tiled, then one button classifies the lot and re-tiles them under their four
classes.  Nothing is uploaded until that button is pressed.

No dependencies, no build step, no CDN: one HTML string with inline CSS and JS,
so it works on a laptop that is offline except for the LAN.
"""

from __future__ import annotations

INDEX_HTML = r"""<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>albumcat · 分类效果</title>
<style>
  :root {
    --bg: #0f1115;
    --panel: #171a21;
    --panel-2: #1e222b;
    --line: #2b313c;
    --text: #e7eaf0;
    --muted: #929cad;
    --accent: #7aa2f7;
    --ok: #7ee787;
    --warn: #f0b429;
    --bad: #f7768e;
    --draw: #7aa2f7;
    --meme: #f0b429;
    --photo: #7ee787;
    --shot: #c792ea;
    --none: #6c7688;
  }
  * { box-sizing: border-box; }
  body {
    margin: 0; padding: 0 24px 60px;
    background: var(--bg); color: var(--text);
    font: 14px/1.5 -apple-system, "Segoe UI", "Microsoft YaHei", sans-serif;
  }
  header { padding-top: 22px; display: flex; gap: 16px; align-items: baseline; flex-wrap: wrap; }
  h1 { font-size: 18px; margin: 0 0 4px; }
  .sub { color: var(--muted); font-size: 12px; }
  .row { display: flex; gap: 8px; align-items: center; flex-wrap: wrap; }
  .grow { flex: 1; }
  code { background: var(--panel-2); padding: 0 4px; border-radius: 4px; }

  /* the toolbar sticks: a folder of 500 tiles is a lot of scrolling */
  .bar {
    position: sticky; top: 0; z-index: 10;
    display: flex; gap: 10px; align-items: center; flex-wrap: wrap;
    margin: 14px 0 10px; padding: 10px 0;
    background: linear-gradient(var(--bg) 82%, rgba(15, 17, 21, 0));
  }
  button {
    background: var(--panel-2); color: var(--text);
    border: 1px solid var(--line); border-radius: 8px;
    padding: 6px 12px; font: inherit; cursor: pointer;
  }
  button:hover:not(:disabled) { border-color: var(--accent); }
  button:disabled { color: #5b6474; cursor: default; }
  button.primary { background: #1d2b47; border-color: #365b9e; font-weight: 600; }
  button.primary:hover:not(:disabled) { border-color: var(--accent); background: #223353; }
  button.on { border-color: var(--accent); background: #1b2333; }
  input[type=password] {
    background: var(--panel-2); color: var(--text);
    border: 1px solid var(--line); border-radius: 8px; padding: 5px 9px; font: inherit; width: 140px;
  }
  .drop {
    margin: 14px 0; padding: 20px; text-align: center;
    border: 2px dashed var(--line); border-radius: 12px;
    background: var(--panel); color: var(--muted); cursor: pointer;
    transition: border-color .15s, background .15s;
  }
  .drop.hot { border-color: var(--accent); background: #1b2333; color: var(--text); }
  .drop b { color: var(--text); }
  /* once there are tiles, the dropzone stops being the hero and becomes a strip */
  .drop.slim { margin: 10px 0; padding: 8px 12px; font-size: 12px; border-width: 1px; }
  .drop.slim .hint { display: none; }
  .hint { color: #6c7688; font-size: 12px; margin-top: 6px; }

  .chips { display: flex; gap: 8px; flex-wrap: wrap; }
  .chip {
    padding: 3px 10px; border-radius: 999px; border: 1px solid var(--line);
    background: var(--panel); color: var(--muted); cursor: pointer; font-size: 12px;
  }
  .chip.on { border-color: var(--accent); color: var(--text); background: #1b2333; }
  .chip b { color: var(--text); font-weight: 600; }
  .chip .dot, .section .dot {
    display: inline-block; width: 8px; height: 8px; border-radius: 50%;
    margin-right: 5px; vertical-align: 0;
  }

  .progress { flex: 1 1 220px; min-width: 160px; }
  .progress .track { height: 6px; border-radius: 999px; background: #0b0d11; overflow: hidden; }
  .progress .fill { height: 100%; width: 0; background: var(--accent); transition: width .2s; }
  .progress .text { font-size: 12px; color: var(--muted); margin-bottom: 3px; }

  .tiles { display: grid; grid-template-columns: repeat(auto-fill, minmax(128px, 1fr)); gap: 8px; }
  .tile {
    position: relative; border-radius: 10px; overflow: hidden;
    background: var(--panel); border: 1px solid var(--line);
  }
  .tile.ready { cursor: zoom-in; }
  .tile.ready:hover { border-color: var(--accent); }
  .tile img {
    display: block; width: 100%; aspect-ratio: 1 / 1; object-fit: cover; background: #0b0d11;
  }
  .tile .tag {
    position: absolute; top: 5px; left: 5px; max-width: calc(100% - 10px);
    font-size: 10px; padding: 1px 6px; border-radius: 999px;
    background: rgba(0, 0, 0, .62); color: var(--text);
    white-space: nowrap; overflow: hidden; text-overflow: ellipsis;
  }
  .tile .cap {
    position: absolute; left: 0; right: 0; bottom: 0; padding: 12px 6px 3px;
    font-size: 10px; color: #cfd6e4;
    background: linear-gradient(transparent, rgba(0, 0, 0, .78));
    white-space: nowrap; overflow: hidden; text-overflow: ellipsis;
  }
  .tile .spin {
    position: absolute; inset: 0; display: flex; align-items: center; justify-content: center;
    background: rgba(8, 10, 14, .45);
  }
  .spinner {
    width: 14px; height: 14px; border-radius: 50%;
    border: 2px solid var(--line); border-top-color: var(--accent);
    display: inline-block; animation: spin .7s linear infinite;
  }
  @keyframes spin { to { transform: rotate(360deg); } }

  .section { margin: 20px 0 6px; }
  .section h2 {
    font-size: 14px; margin: 0 0 8px;
    display: flex; gap: 8px; align-items: baseline;
    position: sticky; top: 52px; background: var(--bg); padding: 4px 0; z-index: 5;
  }
  .section h2 .n { color: var(--muted); font-weight: 400; font-size: 12px; }
  .section h2 .rule { flex: 1; height: 1px; background: var(--line); }
  .empty { color: var(--muted); padding: 40px 0; text-align: center; }

  /* detail sheet: the four bars live here now, one click away from the tile */
  #detail {
    position: fixed; inset: 0; z-index: 30; display: none;
    background: rgba(6, 8, 12, .8); padding: 24px; overflow: auto;
  }
  #detail.on { display: block; }
  #detail .panel {
    max-width: 700px; margin: 0 auto;
    background: var(--panel); border: 1px solid var(--line); border-radius: 14px; overflow: hidden;
  }
  #detail .pic { background: #0b0d11; display: flex; justify-content: center; }
  #detail .pic img { max-width: 100%; max-height: 46vh; object-fit: contain; }
  #detail .body { padding: 12px 16px 16px; display: flex; flex-direction: column; gap: 10px; }
  #detail .head { display: flex; gap: 10px; align-items: baseline; flex-wrap: wrap; }
  #detail .name { font-weight: 600; word-break: break-all; }
  #detail .close { margin-left: auto; }
  /* scoped: an unscoped .bar would also hit the toolbar below */
  #detail .bars { display: flex; flex-direction: column; gap: 6px; }
  #detail .bar { display: grid; grid-template-columns: 46px 1fr 52px; gap: 8px; align-items: center; font-size: 12px; }
  #detail .bar .label { color: var(--muted); }
  #detail .bar .track { height: 8px; border-radius: 999px; background: #0b0d11; overflow: hidden; }
  #detail .bar .fill { height: 100%; width: 0; background: #2f3a4d; }
  #detail .bar.win .label { color: var(--text); }
  #detail .bar.win .fill { background: var(--accent); }
  #detail .bar .num { text-align: right; font-variant-numeric: tabular-nums; color: var(--muted); }
  #detail .bar.win .num { color: var(--text); }
  .tags { font-size: 12px; color: var(--muted); word-break: break-word; }
  .chars { font-size: 12px; color: var(--ok); }
  .meta { font-size: 11px; color: #6c7688; display: flex; justify-content: space-between; gap: 8px; }
  .prior { font-size: 11px; color: var(--warn); }
  .err { color: var(--bad); font-size: 12px; }
</style>
</head>
<body>
<header>
  <div class="grow">
    <h1>分类效果 · 整理一个文件夹</h1>
    <div class="sub">
      四个类别是互斥的 softmax：<code>截图 / 插画 / 梗图 / 照片</code>。选一个文件夹、按下「开始分类」，
      图片会按四类重新铺开；点开任何一张能看到<b>全部</b>四类的分数（手机端只收到冠军那一个）。
      低于阈值就弃权，页面把它归到「未识别」。上传前会在浏览器里缩到长边 1024，原图尺寸单独一起发，
      因为分类器只看得到 448 的方图，否则小图会被当成大图。
    </div>
  </div>
  <div class="row">
    <label class="sub" for="token">令牌</label>
    <input id="token" type="password" placeholder="服务端设了才要填" autocomplete="off">
  </div>
</header>

<div class="bar">
  <button class="primary" id="run">开始分类</button>
  <button id="stop" disabled>停止</button>
  <button id="pickDir">选文件夹</button>
  <button id="pickFiles">选图片</button>
  <div class="progress">
    <div class="text" id="ptext">还没有图片</div>
    <div class="track"><div class="fill" id="pfill"></div></div>
  </div>
  <div class="row">
    <button class="chip on" id="viewPool">全部平铺</button>
    <button class="chip" id="viewGroups">按分类看</button>
    <button class="chip" id="clear">清空</button>
  </div>
</div>

<input id="dir" type="file" webkitdirectory directory multiple hidden>
<input id="files" type="file" accept="image/*" multiple hidden>

<div class="drop" id="drop">
  <b>把整个文件夹拖到这里</b>，或者点上面「选文件夹」；也可以拖单张图片、<b>Ctrl+V</b> 粘贴
  <div class="hint">图片只在你的浏览器里排队，按下「开始分类」才会上传；不落盘、不联网</div>
</div>

<div class="chips" id="chips"></div>

<div class="tiles" id="pool"></div>
<div id="groups"></div>
<div class="empty" id="empty">还没有图片。选一个文件夹，或者把图片拖进来。</div>

<div id="detail">
  <div class="panel">
    <div class="pic"><img id="dimg" alt=""></div>
    <div class="body">
      <div class="head">
        <span class="name" id="dname"></span>
        <span class="sub" id="dmeta"></span>
        <button class="chip close" id="dclose">关闭</button>
      </div>
      <div class="bars" id="dbars"></div>
      <div class="prior" id="dprior"></div>
      <div class="chars" id="dchars"></div>
      <div class="tags" id="dtags"></div>
      <div class="err" id="derr"></div>
    </div>
  </div>
</div>

<script>
"use strict";

const MAX_EDGE = 1024;        // what gets uploaded
const THUMB_EDGE = 340;       // what gets tiled
const BATCH = 4;              // /classify takes 24, but 4 in flight keeps it responsive
const MAX_ITEMS = 1500;       // a browser tab, not a server
const THUMB_WORKERS = 3;
const CLASS_FALLBACK = ["插画", "梗图", "照片", "截图"];
const CLASS_COLOR = { "插画": "var(--draw)", "梗图": "var(--meme)", "照片": "var(--photo)", "截图": "var(--shot)" };
const NONE_LABEL = "未识别";

const items = [];
let view = "pool";
let filter = "";
let running = false;
let stopFlag = false;
let doneCount = 0;
let totalCount = 0;
let renderQueued = false;

const $ = (id) => document.getElementById(id);
const tokenInput = $("token");
tokenInput.value = localStorage.getItem("albumcat-token") || "";
tokenInput.addEventListener("change", () => localStorage.setItem("albumcat-token", tokenInput.value));

function head() {
  const headers = { "Content-Type": "application/json" };
  const token = tokenInput.value.trim();
  if (token) headers["X-Albumcat-Token"] = token;
  return headers;
}

function pct(x) { return (x * 100).toFixed(1); }
function colorOf(label) { return CLASS_COLOR[label] || "var(--none)"; }
function newId() { return (crypto.randomUUID ? crypto.randomUUID() : String(Math.random()).slice(2)); }

async function decode(file) {
  if (window.createImageBitmap) {
    try { return await createImageBitmap(file); } catch (err) { /* fall back to <img> */ }
  }
  return await new Promise((resolve, reject) => {
    const img = new Image();
    img.onload = () => resolve(img);
    img.onerror = () => reject(new Error("浏览器打不开这张图（可能是 HEIC / AVIF）"));
    img.src = URL.createObjectURL(file);
  });
}

async function toDataUrl(file, edge, quality) {
  const bitmap = await decode(file);
  const w0 = bitmap.width || bitmap.naturalWidth;
  const h0 = bitmap.height || bitmap.naturalHeight;
  const scale = Math.min(1, edge / Math.max(w0, h0));
  const w = Math.max(1, Math.round(w0 * scale));
  const h = Math.max(1, Math.round(h0 * scale));
  const canvas = document.createElement("canvas");
  canvas.width = w;
  canvas.height = h;
  canvas.getContext("2d").drawImage(bitmap, 0, 0, w, h);
  if (bitmap.close) bitmap.close();
  return { url: canvas.toDataURL("image/jpeg", quality), width: w0, height: h0 };
}

// ---------------------------------------------------------------- adding files

function isImage(file) {
  return !!file && (file.type.startsWith("image/") || /\.(jpe?g|png|webp|gif|bmp|avif|heic|heif)$/i.test(file.name || ""));
}

function addFiles(files) {
  const list = Array.from(files || []).filter(isImage);
  if (!list.length) return 0;
  const seen = new Set(items.map((it) => it.key));
  let added = 0;
  for (const file of list) {
    const path = file.webkitRelativePath || file.name || "pasted.jpg";
    const key = path + "|" + (file.size || 0);
    if (seen.has(key)) continue;
    if (items.length >= MAX_ITEMS) break;
    seen.add(key);
    items.push({
      id: newId(), key,
      name: file.name || "pasted.jpg",
      path,
      file,
      thumb: "",
      result: null,
      state: "pending",   // pending -> idle -> queued -> busy -> done | error
      error: "",
    });
    added++;
  }
  if (added) { schedule(); buildThumbs(); }
  return added;
}

// Dropping a folder gives us entries, not files; walk them.  ``items`` has to be
// read synchronously -- the DataTransfer is emptied as soon as we await.
function entriesFrom(dt) {
  const entries = [];
  const plain = [];
  for (const item of Array.from(dt.items || [])) {
    const entry = item.webkitGetAsEntry && item.webkitGetAsEntry();
    if (entry) entries.push(entry);
    else if (item.kind === "file") {
      const file = item.getAsFile();
      if (file) plain.push(file);
    }
  }
  return { entries, plain };
}

async function walkEntry(entry, out, depth) {
  if (depth > 6 || out.length >= MAX_ITEMS) return;
  if (entry.isFile) {
    const file = await new Promise((resolve, reject) => entry.file(resolve, reject));
    out.push(file);
    return;
  }
  if (!entry.isDirectory) return;
  const reader = entry.createReader();
  while (true) {
    const batch = await new Promise((resolve, reject) => reader.readEntries(resolve, reject));
    if (!batch.length) break;
    for (const child of batch) await walkEntry(child, out, depth + 1);
  }
}

async function filesFromDrop(dt) {
  const { entries, plain } = entriesFrom(dt);
  if (!entries.length) return plain;
  const out = [];
  for (const entry of entries) await walkEntry(entry, out, 0);
  return out;
}

let thumbing = false;
async function buildThumbs() {
  if (thumbing) return;
  thumbing = true;
  try {
    while (true) {
      const next = items.filter((it) => it.state === "pending");
      if (!next.length) break;
      const batch = next.slice(0, THUMB_WORKERS);
      batch.forEach((it) => { it.state = "building"; });
      await Promise.all(batch.map(async (it) => {
        try {
          const shot = await toDataUrl(it.file, THUMB_EDGE, 0.72);
          it.thumb = shot.url;
          it.size = { width: shot.width, height: shot.height };
          it.state = "idle";
        } catch (err) {
          it.state = "error";
          it.error = String(err.message || err);
        }
      }));
      schedule();
    }
  } finally {
    thumbing = false;
  }
  updateControls();
  schedule();
  // anything that arrived while we were finishing
  if (items.some((it) => it.state === "pending")) await buildThumbs();
}

// ---------------------------------------------------------------- classifying

async function send(payload) {
  let data = null;
  try {
    const res = await fetch("/classify", { method: "POST", headers: head(), body: JSON.stringify({ images: payload }) });
    data = await res.json().catch(() => null);
    if (!res.ok || !data || data.ok === false) {
      const why = (data && (data.error || data.detail)) || ("HTTP " + res.status);
      throw new Error(res.status === 401 ? "令牌不对（服务端启动时带了 --token）" : why);
    }
  } catch (err) {
    for (const p of payload) {
      const it = items.find((x) => x.id === p.id);
      if (it) { it.state = "error"; it.error = String(err.message || err); }
    }
    return;
  }
  for (const hit of data.items || []) {
    const it = items.find((x) => x.id === hit.id);
    if (!it) continue;
    it.result = hit;
    if (hit.ok === false) { it.state = "error"; it.error = hit.error || "服务端没认下这张图"; }
    else { it.state = "done"; it.error = ""; }
  }
}

async function classifyAll() {
  const todo = items.filter((it) => it.state === "idle" || it.state === "queued" || it.state === "error");
  if (!todo.length || running) return;
  running = true;
  stopFlag = false;
  doneCount = 0;
  totalCount = todo.length;
  todo.forEach((it) => { it.state = "queued"; it.error = ""; it.result = null; });
  updateControls();
  schedule();

  while (!stopFlag) {
    const batch = items.filter((it) => it.state === "queued").slice(0, BATCH);
    if (!batch.length) break;
    batch.forEach((it) => { it.state = "busy"; });
    schedule();
    const payload = [];
    const prepared = await Promise.all(batch.map(async (it) => {
      try {
        const { url, width: w0, height: h0 } = await toDataUrl(it.file, MAX_EDGE, 0.85);
        return { id: it.id, name: it.name, image: url, width: w0, height: h0 };
      } catch (err) {
        it.state = "error";
        it.error = String(err.message || err);
        return null;
      }
    }));
    for (const p of prepared) if (p) payload.push(p);
    if (payload.length) await send(payload);
    doneCount = Math.min(totalCount, doneCount + batch.length);
    updateControls();
    schedule();
  }

  // a stop can land between decoding and sending; those must not stay spinning
  items.forEach((it) => { if (it.state === "queued" || it.state === "busy") it.state = "idle"; });
  running = false;
  updateControls();
  const stopped = stopFlag;
  stopFlag = false;
  if (!stopped) view = "groups";   // the point of the button: show the four piles
  schedule();
}

// ---------------------------------------------------------------- verdicts

function winner(result) {
  if (!result || !result.classifiers || !result.classifiers.length) return null;
  const cl = result.classifiers[0];
  let best = 0;
  cl.scores.forEach((s, i) => { if (s > cl.scores[best]) best = i; });
  return { cl, index: best, label: cl.labels[best], score: cl.scores[best] };
}

function verdict(it) {
  if (it.state === "error") return { key: NONE_LABEL, label: "读不了", score: null, win: null };
  const win = winner(it.result);
  if (!win) return { key: NONE_LABEL, label: "未识别", score: null, win: null };
  if (win.score >= win.cl.threshold) return { key: win.label, label: win.label, score: win.score, win };
  return { key: NONE_LABEL, label: "弃权", score: win.score, win };
}

function classLabels() {
  for (const it of items) {
    const win = winner(it.result);
    if (win && win.cl.labels && win.cl.labels.length) return win.cl.labels.slice();
  }
  return CLASS_FALLBACK.slice();
}

// ---------------------------------------------------------------- rendering

function schedule() {
  if (renderQueued) return;
  renderQueued = true;
  requestAnimationFrame(render);
}

function render() {
  renderQueued = false;
  const showPool = view === "pool";
  $("pool").style.display = showPool ? "" : "none";
  $("groups").style.display = showPool ? "none" : "";
  $("viewPool").classList.toggle("on", showPool);
  $("viewGroups").classList.toggle("on", !showPool);
  // the dropzone is the hero until there are tiles to look at
  $("drop").classList.toggle("slim", items.length > 0);

  if (!items.length) {
    $("pool").textContent = "";
    $("groups").textContent = "";
    $("empty").style.display = "block";
    $("chips").textContent = "";
    updateControls();
    return;
  }
  $("empty").style.display = "none";
  if (showPool) renderPool(); else renderGroups();
  renderChips();
  updateControls();
}

function renderPool() {
  const pool = $("pool");
  pool.textContent = "";
  for (const it of items) {
    if (filter && verdict(it).key !== filter) continue;
    pool.appendChild(tile(it));
  }
}

function renderGroups() {
  const groups = $("groups");
  groups.textContent = "";
  const by = new Map();
  for (const it of items) {
    const key = verdict(it).key;
    if (!by.has(key)) by.set(key, []);
    by.get(key).push(it);
  }
  const labels = classLabels();
  const order = labels.concat([NONE_LABEL]);
  for (const key of order) {
    const list = (by.get(key) || []).slice();
    if (!list.length) continue;
    if (filter && filter !== key) continue;
    list.sort((a, b) => {
      const sa = verdict(a).score, sb = verdict(b).score;
      if (sa === null && sb === null) return 0;
      if (sa === null) return 1;
      if (sb === null) return -1;
      return sb - sa;
    });
    const section = document.createElement("section");
    section.className = "section";
    const h2 = document.createElement("h2");
    const dot = document.createElement("span");
    dot.className = "dot";
    dot.style.background = colorOf(key);
    h2.appendChild(dot);
    h2.appendChild(document.createTextNode(key));
    const n = document.createElement("span");
    n.className = "n";
    n.textContent = list.length + " 张 · " + Math.round((list.length / items.length) * 100) + "%";
    h2.appendChild(n);
    const rule = document.createElement("span");
    rule.className = "rule";
    h2.appendChild(rule);
    section.appendChild(h2);
    const grid = document.createElement("div");
    grid.className = "tiles";
    for (const it of list) grid.appendChild(tile(it));
    section.appendChild(grid);
    groups.appendChild(section);
  }
}

function tile(it) {
  const v = verdict(it);
  const el = document.createElement("div");
  el.className = "tile" + (it.state === "done" ? " ready" : "");
  el.title = it.path || it.name;

  const img = document.createElement("img");
  img.alt = it.name;
  img.loading = "lazy";
  if (it.thumb) img.src = it.thumb;
  el.appendChild(img);

  if (it.state === "busy" || it.state === "building" || it.state === "pending") {
    const spin = document.createElement("div");
    spin.className = "spin";
    const dot = document.createElement("span");
    dot.className = "spinner";
    spin.appendChild(dot);
    el.appendChild(spin);
  } else {
    const tag = document.createElement("span");
    tag.className = "tag";
    tag.textContent = v.score === null ? v.label : v.label + " " + pct(v.score) + "%";
    el.appendChild(tag);
  }

  const cap = document.createElement("span");
  cap.className = "cap";
  cap.textContent = it.name;
  el.appendChild(cap);

  el.addEventListener("click", () => openDetail(it));
  return el;
}

function renderChips() {
  const counts = new Map();
  for (const it of items) {
    const key = verdict(it).key;
    counts.set(key, (counts.get(key) || 0) + 1);
  }
  const chips = $("chips");
  chips.textContent = "";
  const mk = (label, value, n, color) => {
    const el = document.createElement("button");
    el.className = "chip" + (filter === value ? " on" : "");
    if (color) {
      const dot = document.createElement("span");
      dot.className = "dot";
      dot.style.background = color;
      el.appendChild(dot);
    }
    el.appendChild(document.createTextNode(label + " "));
    const b = document.createElement("b");
    b.textContent = n;
    el.appendChild(b);
    el.onclick = () => { filter = filter === value ? "" : value; schedule(); };
    chips.appendChild(el);
  };
  mk("全部", "", items.length, "");
  for (const label of classLabels().concat([NONE_LABEL])) {
    const n = counts.get(label);
    if (n) mk(label, label, n, colorOf(label));
  }
}

function updateControls() {
  const pending = items.filter((it) => it.state === "pending" || it.state === "building").length;
  const idle = items.filter((it) => it.state === "idle").length;
  const failed = items.filter((it) => it.state === "error").length;
  const done = items.filter((it) => it.state === "done").length;
  const busy = items.filter((it) => it.state === "busy" || it.state === "queued").length;

  const run = $("run");
  run.disabled = running || !(idle || failed) || !!pending;
  run.textContent = idle + failed ? "开始分类（" + (idle + failed) + " 张）" : "开始分类";
  $("stop").disabled = !running;

  const total = items.length;
  let text, frac;
  if (running) {
    text = "分类中 " + doneCount + " / " + totalCount + (busy ? "，在飞 " + busy : "");
    frac = totalCount ? doneCount / totalCount : 0;
  } else if (pending) {
    text = "读取中，还剩 " + pending + " 张（共 " + total + " 张）";
    frac = total ? (total - pending) / total : 0;
  } else if (total) {
    text = done + " / " + total + " 张已分类" + (failed ? "（" + failed + " 张读不了）" : "");
    frac = total ? done / total : 0;
  } else {
    text = "还没有图片";
    frac = 0;
  }
  $("ptext").textContent = text;
  $("pfill").style.width = Math.round(frac * 100) + "%";
}

// ---------------------------------------------------------------- detail sheet

function openDetail(it) {
  const v = verdict(it);
  $("dimg").src = it.thumb || "";
  $("dname").textContent = it.name;
  const size = it.result && it.result.size;
  const px = size && size.width && size.height ? size.width + "×" + size.height
    : (it.size ? it.size.width + "×" + it.size.height : "尺寸未知");
  $("dmeta").textContent = (it.path !== it.name ? it.path : "") + " · " + px;

  const bars = $("dbars");
  bars.textContent = "";
  const win = v.win;
  if (win) {
    const cl = win.cl;
    cl.labels.forEach((label, i) => {
      const row = document.createElement("div");
      row.className = "bar" + (i === win.index && cl.scores[i] >= cl.threshold ? " win" : "");
      const l = document.createElement("div");
      l.className = "label";
      l.textContent = label;
      const track = document.createElement("div");
      track.className = "track";
      const fill = document.createElement("div");
      fill.className = "fill";
      fill.style.width = Math.max(1, Math.round(cl.scores[i] * 100)) + "%";
      track.appendChild(fill);
      const num = document.createElement("div");
      num.className = "num";
      num.textContent = pct(cl.scores[i]) + "%";
      row.appendChild(l);
      row.appendChild(track);
      row.appendChild(num);
      bars.appendChild(row);
    });
    const meta = document.createElement("div");
    meta.className = "meta";
    const left = document.createElement("span");
    left.textContent = cl.id + " · 阈值 " + cl.threshold.toFixed(2)
      + (win.score >= cl.threshold ? "" : " · 冠军没过阈值，弃权")
      + (it.result.ms ? " · " + it.result.ms + " ms" : "");
    meta.appendChild(left);
    bars.appendChild(meta);
  } else {
    const note = document.createElement("div");
    note.className = "sub";
    note.textContent = it.state === "error" ? "这张图没能分类" : "还没有分类结果";
    bars.appendChild(note);
  }

  // Say it out loud when resolution moved the answer: this page exists to make
  // the classifier's reasoning contestable, and an invisible adjustment is
  // exactly the kind of thing that is impossible to argue about.
  const prior = win && win.cl.size_prior;
  if (prior && prior.delta) {
    const raw = win.cl.raw_scores && win.index >= 0 ? win.cl.raw_scores[win.index] : null;
    const moved = raw !== null && Math.abs(raw - win.cl.scores[win.index]) > 0.0005
      ? "（" + prior.label + " " + pct(raw) + "% → " + pct(win.cl.scores[win.index]) + "%）"
      : "";
    const sign = prior.delta > 0 ? "+" : "";
    $("dprior").textContent = "尺寸先验 " + prior.label + " " + sign + prior.delta.toFixed(2) + moved;
    $("dprior").style.display = "block";
  } else {
    $("dprior").style.display = "none";
  }

  if (it.result && it.result.characters && it.result.characters.length) {
    $("dchars").textContent = it.result.characters.slice(0, 5).map((c) => c.label + " " + pct(c.score) + "%").join(" · ");
    $("dchars").style.display = "block";
  } else {
    $("dchars").style.display = "none";
  }
  if (it.result && it.result.tags && it.result.tags.length) {
    $("dtags").textContent = it.result.tags.slice(0, 30).join(" · ");
    $("dtags").style.display = "block";
  } else {
    $("dtags").style.display = "none";
  }
  $("derr").textContent = it.error || "";
  $("derr").style.display = it.error ? "block" : "none";

  $("detail").classList.add("on");
}

function closeDetail() { $("detail").classList.remove("on"); }

// ---------------------------------------------------------------- wiring

const drop = $("drop");
$("pickDir").addEventListener("click", () => $("dir").click());
$("pickFiles").addEventListener("click", () => $("files").click());
drop.addEventListener("click", () => $("files").click());
$("dir").addEventListener("change", (e) => { addFiles(e.target.files); e.target.value = ""; });
$("files").addEventListener("change", (e) => { addFiles(e.target.files); e.target.value = ""; });

["dragenter", "dragover"].forEach((name) => drop.addEventListener(name, (e) => {
  e.preventDefault(); drop.classList.add("hot");
}));
["dragleave", "drop"].forEach((name) => drop.addEventListener(name, (e) => {
  e.preventDefault(); drop.classList.remove("hot");
}));
drop.addEventListener("drop", (e) => {
  if (!e.dataTransfer) return;
  filesFromDrop(e.dataTransfer).then(addFiles).catch(() => {});
});
// dropping anywhere on the page works too -- aiming at a dashed box is fiddly
document.addEventListener("dragover", (e) => { if (e.dataTransfer) e.preventDefault(); });
document.addEventListener("drop", (e) => {
  if (!e.dataTransfer || e.target === drop) return;
  e.preventDefault();
  filesFromDrop(e.dataTransfer).then(addFiles).catch(() => {});
});
document.addEventListener("paste", (e) => {
  const files = Array.from((e.clipboardData || {}).files || []);
  if (files.length) { e.preventDefault(); addFiles(files); }
});

$("run").addEventListener("click", classifyAll);
$("stop").addEventListener("click", () => { stopFlag = true; });
$("clear").addEventListener("click", () => {
  if (running) stopFlag = true;
  items.length = 0;
  filter = "";
  doneCount = totalCount = 0;
  view = "pool";
  schedule();
});
$("viewPool").addEventListener("click", () => { view = "pool"; schedule(); });
$("viewGroups").addEventListener("click", () => { view = "groups"; schedule(); });
$("dclose").addEventListener("click", closeDetail);
$("detail").addEventListener("click", (e) => { if (e.target === $("detail")) closeDetail(); });
document.addEventListener("keydown", (e) => { if (e.key === "Escape") closeDetail(); });

render();
</script>
</body>
</html>
"""

__all__ = ["INDEX_HTML"]
