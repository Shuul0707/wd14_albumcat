"""The little web page that lets a human look at the classifiers.

Served by ``albumcat serve`` at ``/ui``; the page itself posts images to
``/classify`` and draws the *unfiltered* class scores, which the phone never
sees (it only ever receives the winning tag).  It exists because "梗图基本都被
分成了插画" is impossible to argue about from a tag list that has already thrown
the runner-up away.

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
  }
  * { box-sizing: border-box; }
  body {
    margin: 0; padding: 24px;
    background: var(--bg); color: var(--text);
    font: 14px/1.5 -apple-system, "Segoe UI", "Microsoft YaHei", sans-serif;
  }
  header { display: flex; gap: 16px; align-items: baseline; flex-wrap: wrap; }
  h1 { font-size: 18px; margin: 0 0 4px; }
  .sub { color: var(--muted); font-size: 12px; }
  .row { display: flex; gap: 8px; align-items: center; flex-wrap: wrap; }
  .grow { flex: 1; }
  .drop {
    margin: 18px 0 10px; padding: 26px; text-align: center;
    border: 2px dashed var(--line); border-radius: 12px;
    background: var(--panel); color: var(--muted); cursor: pointer;
    transition: border-color .15s, background .15s;
  }
  .drop.hot { border-color: var(--accent); background: #1b2333; color: var(--text); }
  .drop b { color: var(--text); }
  .toolbar { display: flex; gap: 14px; align-items: center; flex-wrap: wrap; margin: 12px 0; }
  .chips { display: flex; gap: 8px; flex-wrap: wrap; }
  .chip {
    padding: 4px 10px; border-radius: 999px; border: 1px solid var(--line);
    background: var(--panel); color: var(--muted); cursor: pointer; font-size: 12px;
  }
  .chip.on { border-color: var(--accent); color: var(--text); background: #1b2333; }
  .chip b { color: var(--text); font-weight: 600; }
  input[type=password], input[type=text], select {
    background: var(--panel-2); color: var(--text);
    border: 1px solid var(--line); border-radius: 8px; padding: 5px 9px; font: inherit;
  }
  input[type=password] { width: 150px; }
  .grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(268px, 1fr)); gap: 14px; }
  .card {
    background: var(--panel); border: 1px solid var(--line); border-radius: 12px;
    overflow: hidden; display: flex; flex-direction: column;
  }
  .thumb { width: 100%; height: 172px; background: #0b0d11; object-fit: contain; }
  .body { padding: 10px 12px 12px; display: flex; flex-direction: column; gap: 8px; }
  .badge {
    display: inline-flex; align-items: center; gap: 6px;
    font-weight: 600; font-size: 13px;
  }
  .badge .dot { width: 8px; height: 8px; border-radius: 50%; background: var(--muted); }
  .bars { display: flex; flex-direction: column; gap: 5px; }
  .bar { display: grid; grid-template-columns: 44px 1fr 44px; gap: 8px; align-items: center; font-size: 12px; }
  .bar .label { color: var(--muted); }
  .bar .track { height: 8px; border-radius: 999px; background: #0b0d11; overflow: hidden; }
  .bar .fill { height: 100%; width: 0; background: #2f3a4d; transition: width .25s; }
  .bar.win .label { color: var(--text); }
  .bar.win .fill { background: var(--accent); }
  .bar .num { text-align: right; font-variant-numeric: tabular-nums; color: var(--muted); }
  .bar.win .num { color: var(--text); }
  .tags { font-size: 12px; color: var(--muted); word-break: break-word; }
  .chars { font-size: 12px; color: var(--ok); }
  .meta { font-size: 11px; color: #6c7688; display: flex; justify-content: space-between; gap: 8px; }
  .err { color: var(--bad); font-size: 12px; }
  .empty { color: var(--muted); padding: 30px 0; text-align: center; }
  .hint { color: #6c7688; font-size: 12px; margin-top: 6px; }
  .spinner {
    width: 12px; height: 12px; border-radius: 50%;
    border: 2px solid var(--line); border-top-color: var(--accent);
    display: inline-block; animation: spin .7s linear infinite;
  }
  @keyframes spin { to { transform: rotate(360deg); } }
</style>
</head>
<body>
<header>
  <div class="grow">
    <h1>分类效果</h1>
    <div class="sub">
      四个类别是互斥的 softmax：<code>截图 / 插画 / 梗图 / 照片</code>。
      这里显示<b>全部</b>分数（手机端只收到冠军那一个），低于阈值就弃权。
    </div>
  </div>
  <div class="row">
    <label class="sub" for="token">令牌</label>
    <input id="token" type="password" placeholder="服务端设了才要填" autocomplete="off">
  </div>
</header>

<div class="drop" id="drop">
  <b>把图片拖进来</b>，或者点这里选文件，也可以直接 <b>Ctrl+V</b> 粘贴一张
  <div class="hint">只发给本机的 albumcat 服务；上传前会在浏览器里缩到长边 1024，不落盘</div>
</div>
<input id="file" type="file" accept="image/*" multiple hidden>

<div class="toolbar">
  <div class="chips" id="chips"></div>
  <div class="grow"></div>
  <div class="row">
    <label class="sub" for="sort">排序</label>
    <select id="sort">
      <option value="order">上传顺序</option>
      <option value="unsure">最拿不准的在前</option>
    </select>
    <button class="chip" id="clear">清空</button>
  </div>
</div>

<div class="grid" id="grid"></div>
<div class="empty" id="empty">还没有图片。丢一张进来看看它被判成什么。</div>

<script>
"use strict";

const MAX_EDGE = 1024;
const BATCH = 4;
const items = [];
let filter = "";

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

async function toDataUrl(file) {
  let bitmap = null;
  try {
    bitmap = await createImageBitmap(file);
  } catch (err) {
    bitmap = await new Promise((resolve, reject) => {
      const img = new Image();
      img.onload = () => resolve(img);
      img.onerror = () => reject(new Error("这张图浏览器解不开"));
      img.src = URL.createObjectURL(file);
    });
  }
  const w0 = bitmap.width || bitmap.naturalWidth;
  const h0 = bitmap.height || bitmap.naturalHeight;
  const scale = Math.min(1, MAX_EDGE / Math.max(w0, h0));
  const w = Math.max(1, Math.round(w0 * scale));
  const h = Math.max(1, Math.round(h0 * scale));
  const canvas = document.createElement("canvas");
  canvas.width = w;
  canvas.height = h;
  canvas.getContext("2d").drawImage(bitmap, 0, 0, w, h);
  if (bitmap.close) bitmap.close();
  return canvas.toDataURL("image/jpeg", 0.85);
}

function addFiles(files) {
  const list = Array.from(files || []).filter((f) => f && f.type.startsWith("image/"));
  if (!list.length) return;
  for (const file of list) {
    const id = (crypto.randomUUID ? crypto.randomUUID() : String(Math.random()).slice(2));
    items.push({ id, name: file.name || "pasted.jpg", file, busy: true, result: null, error: "" });
  }
  $("empty").style.display = "none";
  render();
  pump();
}

let pumping = false;
async function pump() {
  if (pumping) return;
  pumping = true;
  try {
    while (true) {
      const batch = items.filter((it) => it.busy && !it.sent).slice(0, BATCH);
      if (!batch.length) break;
      batch.forEach((it) => { it.sent = true; });
      const payload = [];
      for (const it of batch) {
        try {
          it.thumb = await toDataUrl(it.file);
          payload.push({ id: it.id, name: it.name, image: it.thumb });
        } catch (err) {
          it.busy = false;
          it.error = String(err.message || err);
        }
      }
      if (payload.length) await send(payload);
      render();
    }
  } finally {
    pumping = false;
  }
  // a drop that landed while we were finishing would otherwise never be sent
  if (items.some((it) => it.busy && !it.sent)) pump();
}

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
      if (it) { it.busy = false; it.error = String(err.message || err); }
    }
    return;
  }
  for (const hit of data.items || []) {
    const it = items.find((x) => x.id === hit.id);
    if (!it) continue;
    it.busy = false;
    it.result = hit;
    it.error = hit.ok === false ? (hit.error || "服务端没认下这张图") : "";
  }
}

function winner(result) {
  if (!result || !result.classifiers || !result.classifiers.length) return null;
  const cl = result.classifiers[0];
  let best = 0;
  cl.scores.forEach((s, i) => { if (s > cl.scores[best]) best = i; });
  return { cl, index: best, label: cl.labels[best], score: cl.scores[best] };
}

function classOf(it) {
  const w = winner(it.result);
  if (!w) return "未判";
  return w.score >= w.cl.threshold ? w.label : "弃权";
}

function render() {
  const grid = $("grid");
  grid.textContent = "";
  const shown = items.filter((it) => !filter || classOf(it) === filter);
  $("empty").style.display = items.length ? "none" : "block";
  $("empty").textContent = items.length && !shown.length ? "这个筛选下没有图片。" : "还没有图片。丢一张进来看看它被判成什么。";

  const order = $("sort").value;
  const sorted = shown.slice();
  if (order === "unsure") {
    sorted.sort((a, b) => {
      const sa = winner(a.result), sb = winner(b.result);
      const va = sa ? Math.abs(sa.score - sa.cl.threshold) : 9;
      const vb = sb ? Math.abs(sb.score - sb.cl.threshold) : 9;
      return va - vb;
    });
  }

  for (const it of sorted) grid.appendChild(card(it));
  renderChips();
}

function renderChips() {
  const counts = new Map();
  for (const it of items) {
    const key = classOf(it);
    counts.set(key, (counts.get(key) || 0) + 1);
  }
  const chips = $("chips");
  chips.textContent = "";
  const mk = (label, value, n) => {
    const el = document.createElement("button");
    el.className = "chip" + (filter === value ? " on" : "");
    el.innerHTML = label + " <b>" + n + "</b>";
    el.onclick = () => { filter = filter === value ? "" : value; render(); };
    chips.appendChild(el);
  };
  mk("全部", "", items.length);
  for (const [key, n] of counts) mk(key === "弃权" ? "弃权" : key, key, n);
}

function card(it) {
  const el = document.createElement("div");
  el.className = "card";
  const thumb = document.createElement("img");
  thumb.className = "thumb";
  thumb.alt = it.name;
  if (it.thumb) thumb.src = it.thumb;
  el.appendChild(thumb);

  const body = document.createElement("div");
  body.className = "body";
  el.appendChild(body);

  const badge = document.createElement("div");
  badge.className = "badge";
  const dot = document.createElement("span");
  dot.className = "dot";
  const w = winner(it.result);
  if (it.busy) {
    const sp = document.createElement("span");
    sp.className = "spinner";
    badge.appendChild(sp);
    badge.appendChild(document.createTextNode("推理中…"));
  } else if (it.error) {
    dot.style.background = "var(--bad)";
    badge.appendChild(dot);
    badge.appendChild(document.createTextNode("出错"));
  } else if (w) {
    const yes = w.score >= w.cl.threshold;
    dot.style.background = yes ? "var(--ok)" : "var(--warn)";
    badge.appendChild(dot);
    const name = yes ? w.label : "弃权（最高 " + w.label + " " + pct(w.score) + "%）";
    badge.appendChild(document.createTextNode(name));
  } else {
    badge.appendChild(document.createTextNode("没有分类插件"));
  }
  body.appendChild(badge);

  if (it.error) {
    const err = document.createElement("div");
    err.className = "err";
    err.textContent = it.error;
    body.appendChild(err);
  }

  if (w) {
    const bars = document.createElement("div");
    bars.className = "bars";
    const cl = w.cl;
    cl.labels.forEach((label, i) => {
      const row = document.createElement("div");
      row.className = "bar" + (i === w.index && cl.scores[i] >= cl.threshold ? " win" : "");
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
    body.appendChild(bars);

    const meta = document.createElement("div");
    meta.className = "meta";
    const left = document.createElement("span");
    left.textContent = cl.id + " · 阈值 " + cl.threshold.toFixed(2);
    const right = document.createElement("span");
    right.textContent = it.result && it.result.ms ? it.result.ms + " ms" : "";
    meta.appendChild(left);
    meta.appendChild(right);
    body.appendChild(meta);

    if (it.result.characters && it.result.characters.length) {
      const chars = document.createElement("div");
      chars.className = "chars";
      chars.textContent = it.result.characters.slice(0, 4).map((c) => c.label + " " + pct(c.score) + "%").join(" · ");
      body.appendChild(chars);
    }
    if (it.result.tags && it.result.tags.length) {
      const tags = document.createElement("div");
      tags.className = "tags";
      tags.textContent = it.result.tags.slice(0, 14).join(" · ");
      body.appendChild(tags);
    }
  }
  return el;
}

function pct(x) { return (x * 100).toFixed(1); }

const drop = $("drop");
drop.addEventListener("click", () => $("file").click());
$("file").addEventListener("change", (e) => { addFiles(e.target.files); e.target.value = ""; });
["dragenter", "dragover"].forEach((name) => drop.addEventListener(name, (e) => {
  e.preventDefault(); drop.classList.add("hot");
}));
["dragleave", "drop"].forEach((name) => drop.addEventListener(name, (e) => {
  e.preventDefault(); drop.classList.remove("hot");
}));
drop.addEventListener("drop", (e) => {
  if (e.dataTransfer) addFiles(e.dataTransfer.files);
});
document.addEventListener("paste", (e) => {
  const files = Array.from((e.clipboardData || {}).files || []);
  if (files.length) { e.preventDefault(); addFiles(files); }
});
$("sort").addEventListener("change", render);
$("clear").addEventListener("click", () => { items.length = 0; render(); });
render();
</script>
</body>
</html>
"""

__all__ = ["INDEX_HTML"]
