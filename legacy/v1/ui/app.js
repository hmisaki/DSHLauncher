'use strict';

const $ = (id) => document.getElementById(id);
let STATUS = null;
let ALL_SLOTS = [];
let pollTimer = null;

// ───────── 格式化 ─────────
function fmtAge(min) {
  if (min === null || min === undefined) return '—';
  if (min < 1) return '刚刚';
  if (min < 60) return `${Math.round(min)} 分钟前`;
  if (min < 1440) return `${(min / 60).toFixed(1)} 小时前`;
  return `${(min / 1440).toFixed(1)} 天前`;
}
function fmtBytes(n) {
  if (!n) return '0 MB';
  if (n >= 1024 ** 3) return (n / 1024 ** 3).toFixed(2) + ' GB';
  return (n / 1024 ** 2).toFixed(0) + ' MB';
}
function fmtMin(min) {
  if (min < 60) return `${Math.round(min)}m`;
  if (min < 1440) return `${(min / 60).toFixed(1)}h`;
  return `${(min / 1440).toFixed(1)}d`;
}

// ───────── 通信 ─────────
async function api(path, opts) {
  const r = await fetch(path, Object.assign({ headers: { 'Content-Type': 'application/json' } }, opts || {}));
  const text = await r.text();
  let body;
  try { body = text ? JSON.parse(text) : {}; } catch { body = { raw: text }; }
  if (!r.ok) {
    const msg = body.detail ? (typeof body.detail === 'string' ? body.detail : JSON.stringify(body.detail)) : `HTTP ${r.status}`;
    throw new Error(msg);
  }
  return body;
}
const post = (path, data) => api(path, { method: 'POST', body: JSON.stringify(data || {}) });

function toast(msg, bad) {
  const t = $('toast');
  t.textContent = msg;
  t.classList.toggle('bad', !!bad);
  t.hidden = false;
  clearTimeout(t._h);
  t._h = setTimeout(() => { t.hidden = true; }, bad ? 6500 : 3800);
}

// ───────── 轮询 ─────────
async function poll() {
  try {
    const s = await api('/api/status');
    STATUS = s;
    renderStatus(s);
  } catch (e) {
    $('healthText').textContent = '服务连不上：' + e.message;
    $('health').querySelector('.dot').className = 'dot down';
  }
}

function renderStatus(s) {
  const up = s.health && s.health.dshRunning;
  $('healthText').textContent = up ? `网关在跑 (pid ${s.health.dshPid || '?'})` : '网关未运行';
  $('health').querySelector('.dot').className = 'dot ' + (up ? 'up' : 'down');

  $('meta').textContent =
    `${s.backupRoot} · 已用 ${fmtBytes(s.usedBytes)} · 余 ${s.diskFreeGb} GB · 共 ${s.totalSlots} 份` +
    (s.paused ? ' · ⏸ 已暂停自动备份' : '');

  const tb = $('layersBody');
  tb.innerHTML = '';
  for (const L of s.layers) {
    const tr = document.createElement('tr');
    const last = L.lastAt ? fmtAge(L.lastAgeMinutes) : '还没有';
    const cov = L.coveredDays !== null ? `${L.coveredDays} 天前` : '—';
    const cnt = L.expected ? `${L.count}/${L.expected}` : L.count;
    tr.innerHTML = `
      <td>${L.label}</td>
      <td>${last}${L.lastAt ? `<span class="muted small"> · ${L.lastAt.slice(5, 16).replace('T', ' ')}</span>` : ''}</td>
      <td class="num">${cnt}</td>
      <td class="num">${fmtBytes(L.bytes)}</td>
      <td>${cov}</td>
      <td class="num"><button data-layer="${L.layer}" class="bkBtn">立即备份</button></td>`;
    tb.appendChild(tr);
  }
  tb.querySelectorAll('.bkBtn').forEach((b) => {
    b.addEventListener('click', () => startBackup(b.dataset.layer, b));
  });

  const p = s.progress;
  const bar = $('progress');
  if (p.busy) {
    bar.hidden = false;
    const pct = p.total ? Math.round((p.done / p.total) * 100) : 0;
    $('progFill').style.width = pct + '%';
    $('progText').textContent = `${p.message || p.phase}${p.total ? ` · ${p.done}/${p.total} (${pct}%)` : ''}`;
  } else if (p.message) {
    bar.hidden = false;
    $('progFill').style.width = '100%';
    $('progText').textContent = p.message;
  } else {
    bar.hidden = true;
  }

  $('btnStart').disabled = up || p.busy;
  $('btnStop').disabled = !up;
  $('btnRestart').disabled = p.busy;
}

async function loadSlots() {
  try {
    const d = await api('/api/slots?limit=200');
    ALL_SLOTS = d.slots;
    const dlg = ALL_SLOTS.filter((s) => s.layer === 'dialog').slice(0, 8);
    renderSlots(dlg);
    renderRestoreOptions(ALL_SLOTS);
  } catch (e) {
    toast('取备份列表失败：' + e.message, true);
  }
}

function renderSlots(rows) {
  const tb = $('slotsBody');
  tb.innerHTML = '';
  if (!rows.length) {
    tb.innerHTML = '<tr><td colspan="5" class="muted">还没有对话层备份</td></tr>';
    return;
  }
  for (const s of rows) {
    const tr = document.createElement('tr');
    const dense = s.bucket === '密集期';
    tr.innerHTML = `
      <td>${fmtAge(s.ageMinutes)}</td>
      <td class="num">${fmtBytes(s.bytes)}</td>
      <td>${s.verifiedOk ? '<span class="badge ok">✓校验</span>' : '<span class="badge bad">✗未校验</span>'}</td>
      <td><span class="badge ${dense ? 'dense' : ''}">${s.bucket}</span></td>
      <td class="num"><button class="link rstBtn" data-slot="${s.slot}">恢复这一份</button></td>`;
    tb.appendChild(tr);
  }
  tb.querySelectorAll('.rstBtn').forEach((b) => {
    b.addEventListener('click', () => openRestore(b.dataset.slot));
  });
}

// ───────── 动作 ─────────
async function startBackup(layer, btn) {
  btn.disabled = true;
  try {
    await post('/api/backup', { layer });
    toast(`${layer} 备份已开始`);
    setTimeout(loadSlots, 1500);
  } catch (e) {
    toast('启动失败：' + e.message, true);
    btn.disabled = false;
  }
}

async function envAction(action) {
  try {
    const r = await post('/api/env/' + action, {});
    toast(r.ok ? `已执行 ${action}` : `${action} 失败：${r.reason || r.stderr || r.rc}`, !r.ok);
  } catch (e) {
    toast(`${action} 失败：` + e.message, true);
  }
}

function renderRestoreOptions(slots) {
  const sel = $('restoreSlot');
  const keep = sel.value;
  sel.innerHTML = '';
  for (const s of slots) {
    const o = document.createElement('option');
    o.value = s.slot;
    o.textContent = `${s.slot}  ·  ${fmtAge(s.ageMinutes)}  ·  ${fmtBytes(s.bytes)}  ·  ${s.verifiedOk ? '✓' : '✗'}`;
    sel.appendChild(o);
  }
  // size>1 的 select 不会自动选第一项，显式补上，否则「开始」会报「先选一份」
  if (keep && slots.some((s) => s.slot === keep)) sel.value = keep;
  else if (!sel.value && sel.options.length) sel.value = sel.options[0].value;
}

async function openRestore(preselect) {
  $('restoreModal').hidden = false;
  if (preselect) $('restoreSlot').value = preselect;
  await precheck();
}

async function precheck() {
  const slot = $('restoreSlot').value;
  const box = $('restorePrecheck');
  if (!slot) { box.textContent = '先选一份'; box.className = 'precheck muted small'; return; }
  box.textContent = '预检中…';
  try {
    const r = await api('/api/restore/check?slot=' + encodeURIComponent(slot));
    const lines = [
      `包状态：${r.ok ? '✓ ' + r.detail : '✗ ' + r.detail}`,
      `内容：${r.files} 个文件 / ${fmtBytes(r.bytes)} / ${r.createdAtIso}`,
      `覆盖块：${(r.blocks || []).join('  ') || '—'}`,
      r.gatewayRunning ? '⚠ 网关正在运行：落位模式会被占用锁挡住，先「关闭环境」；演练模式不受影响' : '网关未运行：可以落位',
    ];
    box.textContent = lines.join('\n');
    box.className = 'precheck ' + (r.ok ? 'good' : 'bad');
  } catch (e) {
    box.textContent = '预检失败：' + e.message;
    box.className = 'precheck bad';
  }
}

async function doRestore() {
  const slot = $('restoreSlot').value;
  if (!slot) { toast('先选一份', true); return; }
  const drill = $('restoreDrill').checked;
  const tgt = $('restoreTargets').value;
  const targets = tgt ? [tgt] : null;
  if (!drill && !confirm(`确定要把 ${slot} 落位到真机路径？旧状态会被改名成 *.pre-restore-* 留下。`)) return;
  try {
    await post('/api/restore', { slot, targets, drill });
    $('restoreModal').hidden = true;
    toast(drill ? '演练开始（解到临时目录，不动真机）' : '恢复开始', false);
  } catch (e) {
    toast('恢复启动失败：' + e.message, true);
  }
}

async function doMilestone() {
  const name = prompt('里程碑名字（例如 装完-blender）：', new Date().toISOString().slice(0, 10));
  if (!name) return;
  try {
    await post('/api/milestone', { name });
    toast(`里程碑 ${name} 开始打包（基线较大，要几分钟）`);
  } catch (e) {
    toast('失败：' + e.message, true);
  }
}

// ───────── 配置 ─────────
async function loadConfig() {
  try {
    const d = await api('/api/config');
    const c = d.config;
    $('portDsh').value = c.ports.dsh;
    $('portApi').value = c.ports.api;
    $('cfgRoot').value = c.paths.backup_root;
    $('cfgEvery').value = c.layers.dialog.every_minutes;
    $('cfgAt').value = c.layers.baseline.at;
    $('cfgPaused').checked = !!c.paused;
  } catch (e) {
    $('cfgMsg').textContent = '读配置失败：' + e.message;
  }
}

async function saveConfig() {
  const patch = {
    paused: $('cfgPaused').checked,
    paths: { backup_root: $('cfgRoot').value },
    ports: { dsh: Number($('portDsh').value), api: Number($('portApi').value) },
    layers: {
      dialog: { every_minutes: Number($('cfgEvery').value) },
      baseline: { at: $('cfgAt').value },
    },
  };
  try {
    await post('/api/config', { config: patch });
    $('cfgMsg').textContent = '已保存（面板端口与路径改动需重启面板生效）';
    toast('配置已保存');
  } catch (e) {
    $('cfgMsg').textContent = '保存失败：' + e.message;
    toast('保存失败：' + e.message, true);
  }
}

async function checkConfig() {
  try {
    const d = await api('/api/config/check');
    $('cfgMsg').textContent = d.errors.length ? '校验不过：\n' + d.errors.join('\n') : '校验通过';
  } catch (e) {
    $('cfgMsg').textContent = '校验失败：' + e.message;
  }
}

// ───────── 绑定 ─────────
$('btnRestore').addEventListener('click', () => openRestore(null));
$('btnMilestone').addEventListener('click', doMilestone);
$('btnStart').addEventListener('click', () => envAction('start'));
$('btnRestart').addEventListener('click', () => envAction('restart'));
$('btnStop').addEventListener('click', () => envAction('stop'));
$('btnAllSlots').addEventListener('click', async () => {
  const d = await api('/api/slots?limit=200');
  ALL_SLOTS = d.slots;
  renderSlots(ALL_SLOTS.filter((s) => s.layer === 'dialog'));
});
$('restoreSlot').addEventListener('change', precheck);
$('btnRestoreCancel').addEventListener('click', () => { $('restoreModal').hidden = true; });
$('btnRestoreGo').addEventListener('click', doRestore);
$('btnSaveCfg').addEventListener('click', saveConfig);
$('btnCheckCfg').addEventListener('click', checkConfig);

// ───────── 启动 ─────────
loadConfig();
loadSlots();
poll();
pollTimer = setInterval(poll, 1500);
setInterval(loadSlots, 12000);
