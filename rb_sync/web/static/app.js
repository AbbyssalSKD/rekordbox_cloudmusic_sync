/* rb-sync WebUI 前端：原生 JS，无依赖、无构建 */
"use strict";

const TOKEN = window.__RB_TOKEN__ || "";
const $ = (sel) => document.querySelector(sel);
const $$ = (sel) => Array.from(document.querySelectorAll(sel));

let lastPlan = null;

/* ---------------------------------------------------------------- 基础 */

function esc(value) {
  return String(value ?? "").replace(/[&<>"']/g, (ch) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  }[ch]));
}

function jobStatus(text, kind) {
  const el = $("#job-status");
  el.textContent = text;
  el.className = kind === "err" ? "err" : kind === "ok" ? "ok" : "muted";
}

async function api(method, path, body) {
  const init = { method, headers: { "X-RB-Token": TOKEN } };
  if (body !== undefined) {
    init.headers["Content-Type"] = "application/json";
    init.body = JSON.stringify(body);
  }
  const res = await fetch(path, init);
  let data = {};
  try {
    data = await res.json();
  } catch {
    /* 非 JSON 响应，交给下面的统一报错 */
  }
  if (!res.ok) {
    const err = data.error || {};
    throw new Error(err.message || `HTTP ${res.status}`);
  }
  return data;
}

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

async function runJob(path, body, label) {
  jobStatus(`${label}…`, "run");
  const started = await api("POST", path, body);
  if (!started.job_id) throw new Error("服务没有返回 job_id");
  for (;;) {
    await sleep(600);
    const job = await api("GET", `/api/jobs/${started.job_id}`);
    jobStatus(`${label}… ${job.elapsed}s`, "run");
    if (job.status === "done") {
      jobStatus(`${label}完成（${job.elapsed}s）`, "ok");
      return job.result;
    }
    if (job.status === "error") {
      jobStatus(`${label}失败：${job.error}`, "err");
      throw new Error(job.error);
    }
  }
}

/* ---------------------------------------------------------------- 标签页 */

function switchTab(name) {
  $$(".tab").forEach((b) => b.classList.toggle("active", b.dataset.tab === name));
  $$(".view").forEach((v) => v.classList.toggle("active", v.id === `view-${name}`));
  if (name === "changes") renderChanges();
}

/* ---------------------------------------------------------------- 设置 */

function fillSettings(settings) {
  const rb = settings.rekordbox || {};
  const paths = settings.paths || {};
  $("#db-path").value = rb.db_path || "";
  $("#music-roots").value = (rb.music_roots || []).join("\n");
  $("#previous-roots").value = (paths.previous_roots || []).join("\n");
}

function readRoots(sel) {
  return $(sel).value.split("\n").map((s) => s.trim()).filter(Boolean);
}

async function loadSettings() {
  const data = await api("GET", "/api/settings");
  fillSettings(data.settings || {});
  return data.settings || {};
}

async function saveSettings() {
  const payload = {
    rekordbox: {
      db_path: $("#db-path").value.trim(),
      music_roots: readRoots("#music-roots"),
    },
    paths: { previous_roots: readRoots("#previous-roots") },
  };
  await api("PUT", "/api/settings", payload);
  $("#settings-note").textContent = "已保存";
  await loadStatus();
  setTimeout(() => { $("#settings-note").textContent = ""; }, 2500);
}

async function probe() {
  jobStatus("探测中…", "run");
  const data = await api("POST", "/api/settings/probe", {
    db_path: $("#db-path").value.trim(),
  });
  $("#probe-result").classList.remove("empty");
  const checks = (data.checks || []).map((c) => {
    const kind = c.level === "ok" ? "ok" : c.level === "warn" ? "warn" : "err";
    const icon = c.level === "ok" ? "✔" : c.level === "warn" ? "⚠" : "✖";
    return `<li class="${kind}">${icon} ${esc(c.message)}</li>`;
  }).join("");
  const lib = data.library || {};
  const summary = lib.tracks
    ? `<div class="stats">
         <div class="stat"><div class="k">曲目</div><div class="v">${lib.tracks.toLocaleString()}</div></div>
         <div class="stat"><div class="k">歌单</div><div class="v">${lib.playlists}</div></div>
         <div class="stat"><div class="k">文件在位</div><div class="v">${lib.present_files.toLocaleString()}</div></div>
         <div class="stat"><div class="k">缺失</div><div class="v">${lib.missing_files.toLocaleString()}</div></div>
       </div>`
    : "";
  $("#probe-result").innerHTML = summary + `<ul class="checks">${checks}</ul>`;
  jobStatus("探测完成", "ok");
}

/* ---------------------------------------------------------------- 状态 */

async function loadStatus() {
  const st = await api("GET", "/api/status");
  const chip = $("#chip-version");
  const pageVersion = window.__RB_VERSION__ || "";
  const serverVersion = st.version || "?";
  if (pageVersion && pageVersion !== serverVersion) {
    // 页面是从磁盘读的（新），接口是进程里的（旧）→ 说明服务没重启
    chip.textContent = `服务是旧版 v${serverVersion}`;
    chip.className = "chip err";
    chip.title = `页面版本 ${pageVersion}，后端进程版本 ${serverVersion}。请关闭服务后重新启动。`;
  } else {
    chip.textContent = `v${serverVersion}`;
    chip.className = "chip ok";
    chip.title = "页面与后端版本一致";
  }
  const db = $("#chip-db");
  if (st.db_found) {
    db.textContent = st.configured ? `库：${st.db_path}` : `库：默认位置`;
    db.className = "chip ok";
  } else {
    db.textContent = "库：未找到";
    db.className = "chip err";
  }
  const rb = $("#chip-rb");
  rb.textContent = st.rekordbox_running ? "Rekordbox：运行中" : "Rekordbox：未运行";
  rb.className = st.rekordbox_running ? "chip warn" : "chip ok";
  return st;
}

/* ---------------------------------------------------------------- 歌单 */

async function readPlaylists() {
  const inputs = $("#playlist-input").value
    .split("\n").map((s) => s.trim()).filter(Boolean);
  if (!inputs.length) {
    jobStatus("先填一个歌单链接", "err");
    return;
  }
  $("#btn-plan").disabled = true;
  try {
    const result = await runJob("/api/plan", {
      inputs,
      source: $("#source").value,
      refresh: $("#opt-refresh").checked,
      append: $("#opt-append").checked,
    }, "读取歌单");
    lastPlan = result;
    renderPlan(result);
    renderChanges();
  } catch (err) {
    $("#plan-result").classList.remove("empty");
    $("#plan-result").innerHTML = `<div class="err">${esc(err.message)}</div>`;
  } finally {
    $("#btn-plan").disabled = false;
  }
}

function changeText(c) {
  if (c.first_seen) return `首次读取（${c.added.length} 首）`;
  if (c.unchanged) return "无变化";
  const bits = [];
  if (c.added.length) bits.push(`+${c.added.length}`);
  if (c.removed.length) bits.push(`-${c.removed.length}`);
  if (c.reordered) bits.push(`顺序变动 ${c.moved_count}`);
  if (c.renamed.length) bits.push(`改名 ${c.renamed.length}`);
  if (c.became_unavailable.length) bits.push(`变灰 ${c.became_unavailable.length}`);
  if (c.name_changed) bits.push("歌单改名");
  return bits.join(" ") || "有变化";
}

function renderPlan(result) {
  const box = $("#plan-result");
  box.classList.remove("empty");
  const errors = (result.errors || []).map(
    (e) => `<li class="err">${esc(e.input)}：${esc(e.error)}</li>`
  ).join("");
  const rows = (result.playlists || []).map((p) => {
    const change = (result.changes || []).find(
      (c) => c.playlist_id === p.id && c.source === p.source
    );
    return `<tr>
      <td>${esc(p.name)}</td>
      <td><span class="pill">${esc(p.source)}</span></td>
      <td class="num">${p.track_count.toLocaleString()}</td>
      <td>${change ? esc(changeText(change)) : "-"}</td>
    </tr>`;
  }).join("");

  const extra = result.dropped_previous
    ? `<p class="hint">上次选择里有 ${result.dropped_previous} 个歌单不在本次范围内，已移除。</p>`
    : "";

  box.innerHTML = `
    ${rows ? `<table><thead><tr><th>歌单</th><th>平台</th><th>曲目</th><th>变更</th></tr></thead><tbody>${rows}</tbody></table>` : "<div>没有成功读取的歌单。</div>"}
    ${errors ? `<ul class="checks">${errors}</ul>` : ""}
    ${extra}
    ${result.selection ? `<p class="hint mono">选择配置：${esc(result.selection)}</p>` : ""}`;
}

/* ---------------------------------------------------------------- 变更 */

function renderChanges() {
  const box = $("#changes-result");
  if (!lastPlan) {
    box.className = "panel empty";
    box.textContent = "先在「歌单」里读取一次。";
    return;
  }
  box.className = "panel";
  const blocks = (lastPlan.changes || []).filter((c) => !c.unchanged || c.first_seen);
  if (!blocks.length) {
    box.innerHTML = `<div class="ok">本次没有变更。</div>`;
    return;
  }
  box.innerHTML = blocks.map((c) => {
    const add = (c.added || []).slice(0, 30)
      .map((t) => `<li>+ ${esc(t.name)}${t.artists && t.artists.length ? " — " + esc(t.artists.join(" / ")) : ""}</li>`
      ).join("");
    const del = (c.removed || []).slice(0, 30)
      .map((t) => `<li>- ${esc(t.name)}${t.artists && t.artists.length ? " — " + esc(t.artists.join(" / ")) : ""}</li>`
      ).join("");
    return `<details ${c.first_seen ? "" : "open"}>
      <summary><b>${esc(c.playlist_name)}</b> · ${esc(changeText(c))}</summary>
      ${c.name_changed ? `<p class="hint">改名：${esc(c.name_changed[0])} → ${esc(c.name_changed[1])}</p>` : ""}
      ${add || del ? `<ul class="track-list">${add}${del}</ul>` : ""}
      ${(c.added || []).length > 30 ? `<p class="hint">新增另有 ${c.added.length - 30} 首未显示</p>` : ""}
    </details>`;
  }).join("");
}

/* ---------------------------------------------------------------- 下载清单 */

async function loadFetchPlan() {
  $("#btn-fetch-plan").disabled = true;
  try {
    const result = await runJob("/api/fetch/plan", {}, "计算缺口");
    renderFetchPlan(result);
  } catch (err) {
    $("#fetch-result").classList.remove("empty");
    $("#fetch-result").innerHTML = `<div class="err">${esc(err.message)}</div>`;
  } finally {
    $("#btn-fetch-plan").disabled = false;
  }
}

function renderFetchPlan(data) {
  const box = $("#fetch-result");
  box.classList.remove("empty");
  const counts = data.counts || {};
  const bySource = {};
  (data.missing || []).forEach((item) => {
    bySource[item.source] = bySource[item.source] || [];
    bySource[item.source].push(item);
  });
  const sourceRows = Object.entries(bySource).map(([src, items]) => {
    const manifest = (data.manifest || {})[src];
    return `<tr>
      <td><span class="pill">${esc(src)}</span></td>
      <td class="num">${items.length}</td>
      <td class="mono">${manifest ? esc(manifest) : "—"}</td>
    </tr>`;
  }).join("");

  const sample = (data.missing || []).slice(0, 12).map((item) => `
    <tr>
      <td>${esc(item.title || "(无标题)")}</td>
      <td>${esc((item.artists || []).join(" / "))}</td>
      <td>${esc((item.playlists || []).join("、"))}</td>
    </tr>`).join("");

  box.innerHTML = `
    <div class="stats">
      <div class="stat"><div class="k">歌单成员</div><div class="v">${counts.total || 0}</div></div>
      <div class="stat"><div class="k">本地已有</div><div class="v ok">${counts.present || 0}</div></div>
      <div class="stat"><div class="k">缺失</div><div class="v warn">${counts.missing || 0}</div></div>
      <div class="stat"><div class="k">待解密</div><div class="v warn">${counts.encrypted || 0}</div></div>
      <div class="stat"><div class="k">有歧义</div><div class="v err">${counts.ambiguous || 0}</div></div>
    </div>
    <p class="hint mono">曲库目录：${esc((data.music_roots || []).join(" , ") || "（未设置）")}</p>
    ${counts.encrypted ? `<p class="hint warn">有 ${counts.encrypted} 首只有加密容器（如 .ncm），Rekordbox 放不了。
      配好 <code>fetch.post_command</code> 后跑 <code>rb-sync fetch --run</code>，或自己解密后跑 <code>fetch --verify</code>。</p>` : ""}
    ${sourceRows ? `<table><thead><tr><th>平台</th><th>缺失</th><th>清单文件</th></tr></thead><tbody>${sourceRows}</tbody></table>` : ""}
    ${sample ? `<details open><summary>缺失曲目（前 12 条）</summary>
      <table><thead><tr><th>曲名</th><th>艺人</th><th>所属歌单</th></tr></thead><tbody>${sample}</tbody></table></details>` : ""}
    ${counts.missing ? `<p class="hint">执行下载：<code>rb-sync fetch --run</code>（需先在配置里设置 fetch.commands）</p>` : ""}`;
}

/* ---------------------------------------------------------------- 库体检 */

async function loadLibrary() {
  $("#btn-library").disabled = true;
  jobStatus("读取库…", "run");
  try {
    const data = await api("GET", "/api/library");
    renderLibrary(data);
    jobStatus("体检完成", "ok");
  } catch (err) {
    $("#library-result").classList.remove("empty");
    $("#library-result").innerHTML = `<div class="err">${esc(err.message)}</div>`;
    jobStatus("体检失败", "err");
  } finally {
    $("#btn-library").disabled = false;
  }
}

function renderLibrary(data) {
  const box = $("#library-result");
  box.classList.remove("empty");
  const counts = data.counts || {};
  const stats = `<div class="stats">
    <div class="stat"><div class="k">曲目</div><div class="v">${(counts.tracks || 0).toLocaleString()}</div></div>
    <div class="stat"><div class="k">自带素材</div><div class="v">${counts.factory || 0}</div></div>
    <div class="stat"><div class="k">文件在位</div><div class="v">${(counts.present || 0).toLocaleString()}</div></div>
    <div class="stat"><div class="k">缺失</div><div class="v">${(counts.missing || 0).toLocaleString()}</div></div>
    <div class="stat"><div class="k">完整性</div><div class="v ${data.integrity === "ok" ? "ok" : "err"}">${esc(data.integrity)}</div></div>
  </div>`;

  const folders = {};
  (data.playlists || []).forEach((p) => {
    if (p.is_folder) folders[p.id] = p;
  });
  const groups = Object.values(folders).map((f) => {
    const kids = (data.playlists || []).filter((p) => !p.is_folder && p.parent_id === f.id);
    const rows = kids.map((k) => `<tr><td>${esc(k.name)}</td><td class="num">${k.song_count}</td></tr>`).join("");
    return `<details><summary>[${esc(f.name)}] ${kids.length} 个歌单</summary>
      <table><thead><tr><th>歌单</th><th>曲目</th></tr></thead><tbody>${rows}</tbody></table></details>`;
  }).join("");

  const roots = {};
  (data.missing_tracks || []).forEach((t) => {
    const parts = String(t.path || "").replace(/\\/g, "/").split("/");
    const key = parts.slice(0, 3).join("/") + "/…";
    roots[key] = (roots[key] || 0) + 1;
  });
  const rootRows = Object.entries(roots).sort((a, b) => b[1] - a[1]).slice(0, 10)
    .map(([k, v]) => `<tr><td class="num">${v}</td><td class="mono">${esc(k)}</td></tr>`).join("");

  box.innerHTML = `
    <p class="hint mono">${esc(data.db_path)} · ${(data.db_size || 0).toLocaleString()} 字节 · usn ${data.usn}</p>
    ${stats}
    ${groups || "<p class='hint'>没有歌单。</p>"}
    ${rootRows ? `<h3>缺失文件分布</h3><table><thead><tr><th>数量</th><th>目录</th></tr></thead><tbody>${rootRows}</tbody></table>` : ""}
    ${counts.missing ? `<p class="hint">去「重定位」页可以把这些路径映射到新位置。</p>` : ""}`;
}

/* ---------------------------------------------------------------- 重定位 */

async function previewRelink() {
  $("#btn-relink").disabled = true;
  try {
    const result = await runJob("/api/relink/preview", {
      from_root: $("#from-root").value.trim(),
      to_root: $("#to-root").value.trim(),
      search_roots: readRoots("#search-roots"),
    }, "计算映射");
    renderRelink(result);
  } catch (err) {
    $("#relink-result").classList.remove("empty");
    $("#relink-result").innerHTML = `<div class="err">${esc(err.message)}</div>`;
  } finally {
    $("#btn-relink").disabled = false;
  }
}

function renderRelink(result) {
  const plan = result.plan || {};
  const counts = plan.counts || {};
  const box = $("#relink-result");
  box.classList.remove("empty");

  const strategy = Object.entries(counts.by_strategy || {})
    .map(([k, v]) => `<tr><td class="mono">${esc(k)}</td><td class="num">${v}</td></tr>`).join("");

  const unmatched = (plan.unmatched || []).slice(0, 30).map((u) => `
    <tr><td>${esc(u.title || "(无标题)")}</td><td class="mono">${esc(u.old_path)}</td></tr>`).join("");

  box.innerHTML = `
    <div class="stats">
      <div class="stat"><div class="k">失效</div><div class="v">${counts.total || 0}</div></div>
      <div class="stat"><div class="k">可映射</div><div class="v ok">${counts.matched || 0}</div></div>
      <div class="stat"><div class="k">未匹配</div><div class="v err">${counts.unmatched || 0}</div></div>
      <div class="stat"><div class="k">歧义</div><div class="v warn">${counts.ambiguous || 0}</div></div>
    </div>
    ${strategy ? `<table><thead><tr><th>匹配方式</th><th>数量</th></tr></thead><tbody>${strategy}</tbody></table>` : ""}
    ${unmatched ? `<details><summary>未匹配（前 30 条）</summary>
      <table><thead><tr><th>曲名</th><th>旧路径</th></tr></thead><tbody>${unmatched}</tbody></table></details>` : ""}
    <p class="hint mono">映射表：${esc(result.out)}（dry-run，未修改任何库）</p>`;
}

/* ---------------------------------------------------------------- 启动 */

async function boot() {
  $$(".tab").forEach((btn) => btn.addEventListener("click", () => switchTab(btn.dataset.tab)));
  $("#btn-save").addEventListener("click", () => saveSettings().catch((e) => jobStatus(e.message, "err")));
  $("#btn-probe").addEventListener("click", () => probe().catch((e) => jobStatus(e.message, "err")));
  $("#btn-plan").addEventListener("click", readPlaylists);
  $("#btn-fetch-plan").addEventListener("click", loadFetchPlan);
  $("#btn-library").addEventListener("click", loadLibrary);
  $("#btn-relink").addEventListener("click", previewRelink);

  try {
    const settings = await loadSettings();
    await loadStatus();
    const roots = (settings.rekordbox || {}).music_roots || [];
    const previous = (settings.paths || {}).previous_roots || [];
    if (!$("#to-root").value && roots.length) $("#to-root").value = roots[0];
    if (!$("#from-root").value && previous.length) $("#from-root").value = previous[0];
    if (!$("#search-roots").value && roots.length) $("#search-roots").value = roots.join("\n");
    jobStatus("就绪", "ok");
  } catch (err) {
    jobStatus(`启动失败：${err.message}`, "err");
  }
}

document.addEventListener("DOMContentLoaded", boot);
