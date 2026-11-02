/* NanjingTrip Web · 单页应用（按 Stitch 设计稿 jinling_desktop_precision 实现）
 *
 * 数据全部来自本机服务，不编造：
 *   · 指标 / 时间轴 / 通勤段 / 提示 / 冲突 / 排程说明  ← /api/state
 *   · 出行参数 / 城市 / 方式                          ← /api/options
 *   · 住宿锚点与候选                                  ← /api/lodging
 *   · 推理链                                          ← 最近一次 preview + 当前排程结果
 *   · 本日估算（里程 / 碳排）                          ← 各段 distance_m × 方式系数，标注"估算"
 * 图片位用渐变占位（无 POI 图片数据，不放假照片）。
 */
'use strict';

const MODE_LABEL = { driving: '驾', transit: '公', walking: '步', bicycling: '骑' };
const MODE_NAME = { driving: '驾车', transit: '公交', walking: '步行', bicycling: '骑行' };
const CAT_LABEL = { '医疗': '医', '餐饮': '餐', '展览': '展', '办事': '办' };
// 每公里碳排（kg/km）——公开量级的粗估，仅用于"本日估算"
const CO2 = { driving: 0.21, transit: 0.05, walking: 0, bicycling: 0 };
/** 分钟 → 紧凑显示（设计稿的 HUD 用 146ms / 99.4% 这种短数值） */
const compact = (m) => (!m ? '0m' : m < 60 ? `${m}m` : `${(Math.round(m / 6) / 10).toFixed(1)}h`);

const $ = (s) => document.querySelector(s);
const el = (tag, cls, text) => {
  const n = document.createElement(tag);
  if (cls) n.className = cls;
  if (text != null) n.textContent = text;
  return n;
};
/* 本地日期 → 'YYYY-MM-DD'。
   不能用 toISOString()：它按 UTC 换算，东八区在本地 00:00–08:00 之间会退成前一天，
   于是"今天"按钮会跳到昨天、日历里点某天实际打开前一天（缺陷 D-26）。 */
const isoOf = (d) => `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}-`
  + String(d.getDate()).padStart(2, '0');
const S = {
  day: isoOf(new Date()),
  today: isoOf(new Date()),
  rev: null, data: null, options: null, preview: null, page: 'work', undo: null,
  mapZoom: null, mapCenter: null,
};

/* ---------------- HTTP ---------------- */
async function api(path, params = {}) {
  const r = await fetch(`${path}?${new URLSearchParams({ date: S.day, ...params })}`);
  return r.json();
}
//: 要调模型或逐天排程的动作放宽预算；其余请求必须快速给结论
const SLOW_ACTIONS = new Set(['consult', 'apply_proposal', 'propose_trip',
  'preview_batch', 'preview', 'replan', 'fit_day', 'add_batch', 'alternatives']);

//: 断线横幅：请求失败时出现，任何一次请求成功就收起（不用用户手动关）
function showBanner(text) {
  const box = $('#offlineBanner');
  if (!box) return;
  $('#offlineText').textContent = text || '连不上本地服务';
  box.classList.remove('hidden');
}

function hideBanner() {
  const box = $('#offlineBanner');
  if (box) box.classList.add('hidden');
}

async function cmd(action, payload = {}, timeoutMs) {
  const budget = timeoutMs || (SLOW_ACTIONS.has(action) ? 180000 : 25000);
  // 慢动作（生成草案）期间显示真实阶段进度；快请求 <400ms 不弹条
  if (SLOW_ACTIONS.has(action) && typeof withProgress === 'function') {
    return withProgress(payload.session, () => cmdRaw(action, payload, budget));
  }
  return cmdRaw(action, payload, budget);
}

async function cmdRaw(action, payload = {}, budget = 25000) {
  const ctrl = new AbortController();
  const timer = setTimeout(() => ctrl.abort(), budget);
  try {
    const r = await fetch('/api/command', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ action, date: S.day, ...payload }),
      signal: ctrl.signal,
    });
    const d = await r.json();
    hideBanner();                      // 通了就把横幅收掉
    if (d.message) toast(d.message);
    return d;
  } catch (err) {
    const timeout = !!err && err.name === 'AbortError';
    showBanner(timeout
      ? `等太久了（${Math.round(budget / 1000)} 秒）：服务可能正在重启，点右侧「重试」`
      : '连不上本地服务：确认 python tools/dev.py 还在运行，再点「重试」');
    toast(timeout ? '请求超时，已放弃等待' : '连接不上本地服务');
    return { ok: false, timeout,
             message: timeout ? '请求超时，已放弃等待' : '连接不上本地服务' };
  } finally {
    clearTimeout(timer);
  }
}
let toastTimer = null;
function toast(msg) {
  const t = $('#toast');
  t.textContent = msg;
  t.classList.add('on');
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => t.classList.remove('on'), 2200);
}

/* 时长文案：分钟数 → 中文可读（140 → 2 小时 20 分钟），界面上不再出现裸数字 */
function durText(min) {
  const m = Math.max(0, Math.round(Number(min) || 0));
  if (!m) return '—';
  const h = Math.floor(m / 60);
  const mm = m % 60;
  if (!h) return `${mm} 分钟`;
  return mm ? `${h} 小时 ${mm} 分钟` : `${h} 小时`;
}

/* ---------------- 页面切换 ---------------- */

/* 单页应用的 URL 同步：只改地址栏，不整页重载（刷新/后退仍能回到同一天）
   起始页没有"某一天"的概念，就不带 date，入口保持 /? 干净；
   进工作区/地图后才把当前日期同步上去，刷新仍落在同一天。 */
function syncUrl(forceHome = false) {
  const q = new URLSearchParams(location.search);
  // 以 DOM 实际状态为准：首屏 S.page 可能还没切换完，会导致刚进工作区就把 date 抹掉
  const homeEl = document.querySelector('#pageHome');
  const onHome = forceHome || (homeEl ? !homeEl.classList.contains('hidden')
                                      : S.page === 'home');
  const want = S.day || '';
  const has = q.get('date') || '';
  if (onHome) {
    if (!has) return;                    // 已经是干净的 /? 了
    q.delete('date');
  } else {
    if (has === want) return;
    q.set('date', want);
  }
  const qs = q.toString();
  history.replaceState(null, '', qs ? `${location.pathname}?${qs}` : location.pathname);
}

function setPage(p, fromUser = true) {
  S.page = p;
  $('#pageHome').classList.toggle('hidden', p !== 'home');
  $('#pageWork').classList.toggle('hidden', p !== 'work');
  const mv = $('#pageMap');
  if (mv) mv.classList.toggle('hidden', p !== 'map');
  $('#addForm').classList.toggle('hidden', p !== 'work');
  [...$('#nav').querySelectorAll('button[data-page]')].forEach((b) => {
    b.classList.toggle('on', b.dataset.page === p);
  });
  if (p === 'home') { renderHome(); setTimeout(() => $('#homeInput').focus(), 50); }
  if (p === 'map') renderMapPage();
  syncUrl(p === 'home' && fromUser);
}

/* ---------------- 日期 ---------------- */
const WEEK = ['周日', '周一', '周二', '周三', '周四', '周五', '周六'];
function shiftDay(n) {
  const d = new Date(S.day + 'T00:00:00');
  d.setDate(d.getDate() + n);
  S.day = isoOf(d);
  S.rev = null;
  syncUrl();
  load(true);
}

/* ---------------- 左栏月历（真实数据：本月每天排了几项） ---------------- */
async function renderMonth() {
  const ym = S.calYM || S.day.slice(0, 7);
  const year = +ym.slice(0, 4), month = +ym.slice(5, 7);
  $('#calMonth').textContent = `${year}.${String(month).padStart(2, '0')}`;
  let days = {};
  try {
    days = (await api('/api/month', { year, month })).days || {};
  } catch (e) { days = {}; }
  const grid = $('#calGrid');
  grid.innerHTML = '';
  const pad = (n) => String(n).padStart(2, '0');
  const lead = (new Date(year, month - 1, 1).getDay() + 6) % 7;   // 周一为一周之首
  const total = new Date(year, month, 0).getDate();
  for (let i = 0; i < lead; i++) grid.append(el('span', 'cal-cell empty'));
  for (let d = 1; d <= total; d++) {
    const iso = `${year}-${pad(month)}-${pad(d)}`;
    let cls = 'cal-cell';
    if (days[iso]) cls += ' has';
    if (iso === S.today) cls += ' today';
    if (iso === S.day) cls += ' on';
    const c = el('button', cls, String(d));
    if (days[iso]) c.append(el('i', 'cdot'));
    c.title = days[iso] ? `${iso} · ${days[iso]} 项` : iso;
    c.onclick = () => { S.calYM = iso.slice(0, 7); S.day = iso; S.rev = null; syncUrl(); load(true); };
    grid.append(c);
  }
}

function shiftMonth(n) {
  const ym = S.calYM || S.day.slice(0, 7);
  const base = new Date(+ym.slice(0, 4), +ym.slice(5, 7) - 1 + n, 1);
  S.calYM = `${base.getFullYear()}-${String(base.getMonth() + 1).padStart(2, '0')}`;
  renderMonth();
}

/* ---------------- 渲染：工作区 ---------------- */
function render(d) {
  S.data = d;
  S.llmOk = d.llm_ok;
  const dt = new Date(d.date + 'T00:00:00');
  $('#dateLabel').textContent =
    `${dt.getFullYear()}-${String(dt.getMonth() + 1).padStart(2, '0')}-${String(dt.getDate()).padStart(2, '0')} ${d.weekday}`;
  $('#dayTitle').textContent = `${d.city_label}一日 · 时空编排`;
  $('#daySub').textContent = `${d.date} · ${d.mode_short}模式`;
  renderWeather(d);
  $('#modeText').textContent = d.mode_short;
  $('#modePill').classList.toggle('warn', d.mode_short === '半在线');
  $('#verBadge').textContent = `v2.0 · ${d.city_label === '京城' ? '京城' : '金陵'}`;

  // 左栏
  $('#wsMeta').textContent =
    `工作区 ${d.city_label}时空指挥台 · ${d.mode_short}模式 · rev ${d.rev}`;
  $('#calNow').textContent =
    `当前 ${d.weekday} · ${d.stats.count} 项 · 通行 ${d.stats.transit_text}`;
  S.calYM = d.date.slice(0, 7);
  renderMonth();
  $('#anchorFrom').textContent = d.lodging || '未设定';
  $('#anchorTo').textContent = d.lodging || '未设定';
  $('#btnLodging').textContent = d.lodging ? '更换' : '设定';
  $('#pillNoticeCount').textContent = `${(d.notices || []).length} 条`;
  $('#pillConflictCount').textContent = `${(d.conflicts || []).length} 项`;

  // 指标：设计稿是「标签在上、数值在下」，首项做成胶囊（对应「4 项规划」）
  const m = $('#metrics');
  m.innerHTML = '';
  [['项数', `${d.stats.count} 项`], ['事项占用', d.stats.busy_text],
   ['通行', d.stats.transit_text], ['空档', d.stats.idle_text]].forEach(([k, v], i) => {
    const c = el('div', i === 0 ? 'metric chip' : 'metric');
    if (i === 0) { c.append(el('b', null, v)); }
    else { c.append(el('span', null, k), el('b', null, v)); }
    m.append(c);
  });

  $('#pillItemCount').lastChild.textContent = ` ${d.stats.count} 项`;
  renderTimeline(d);
  renderAgent(d);
  renderReport(d);
  renderModeBars(d);
  renderEco(d);
  if (S.page === 'home') renderHome();
  if (S.page === 'map' && S.mapDay !== d.date) { S.mapDay = d.date; renderMapPage(true); }
  prewarmNeighbors();
}

/* 当日天气：只在高德的 4 天预报窗口内才有数；窗口外/离线一律显示"无预报"，不猜 */
function renderWeather(d) {
  const p = $('#wxPill');
  if (!p) return;
  const w = d.weather;
  p.className = 'pill wx';
  if (!w) {
    p.classList.add('muted');
    p.textContent = d.offline ? '离线无天气' : '无预报';
    p.title = d.offline
      ? '离线模式下不取天气（切回在线并配好高德 Key 才有）'
      : '高德天气只提供今天起 4 天的预报，这一天不在窗口内 —— 按"不猜"处理';
    return;
  }
  const day = w.day_weather || '';
  const night = w.night_weather || '';
  const span = (day && night && day !== night) ? `${day}转${night}` : day || w.text || '';
  const t = (w.day_temp || w.night_temp) ? ` ${w.day_temp}/${w.night_temp}℃` : '';
  p.textContent = `${w.wet ? '☂ ' : ''}${span}${t}`;
  p.classList.toggle('warn', !!w.wet);
  p.title = `${w.text || span}${w.wet ? '（有降水：已按雨天先室内排序）' : '（无降水）'}`;
}

function renderTimeline(d) {
  const box = $('#timeline');
  box.innerHTML = '';
  const items = d.items || [];
  if (!items.length) {
    box.append(el('div', 'empty', '今天还没有安排 —— 在底部说一句就行'));
    $('#tlHint').textContent = '';
    return;
  }
  const nowMin = new Date().getHours() * 60 + new Date().getMinutes();
  let primaryId = null;
  if (S.day === S.today) {
    const hit = items.find((x) => !x.done && !x.unscheduled && x.end &&
      (+x.end.slice(0, 2) * 60 + +x.end.slice(3)) >= nowMin);
    if (hit) primaryId = hit.task_id;
  }
  $('#tlHint').textContent = primaryId ? '高亮项为"下一件事"' : '';

  items.forEach((it) => {
    if (it.leg) box.append(legNode(it));
    const row = el('div', 'row' + (it.task_id === primaryId ? ' primary' : '') +
      (it.done ? ' done' : ''));
    const tm = el('div', 'tm');
    tm.append(document.createTextNode(it.time || '--:--'));
    if (it.end) tm.append(el('small', null, it.end));
    const body = el('div');

    const l1 = el('div', 'line1');
    l1.append(el('span', 'title', it.title));
    if (it.category) l1.append(el('span', 'tag', CAT_LABEL[it.category] || it.category));
    if (it.locked) l1.append(el('span', 'tag solid', '已锁定'));
    if (it.conflict) l1.append(el('span', 'tag danger', '冲突'));
    if (it.unscheduled) l1.append(el('span', 'tag warn', '未排入'));
    if (it.done) l1.append(el('span', 'tag', '已完成'));
    body.append(l1);

    const place = it.poi ? `${it.poi}${it.district ? '（' + it.district + '）' : ''}` : '无地点';
    const meta = el('div', 'rmeta');
    meta.append(el('div', 'line2', `${place} · ${it.minutes_text}`));
    if (it.leg && it.leg.distance_m) {
      const k = it.leg.distance_m / 1000;
      const coef = CO2[it.leg.mode] ?? 0;
      // 步行/骑行系数为 0：直接说"不计"，别摆一个 ×0 的乘法式子
      const eco = coef > 0
        ? el('span', 'tag eco', `碳排 ${(k * coef).toFixed(2)} kg`)
        : el('span', 'tag eco', '零碳');
      eco.title = coef > 0
        ? `按 ${k.toFixed(1)} 公里 × ${coef} kg·km⁻¹ 估算，非实测`
        : `${it.leg.mode_cn || '步行/骑行'}不计碳排放`;
      meta.append(eco);
    }
    if (it.poi_brief) {
      const why = el('div', 'why');
      why.append(el('span', null, it.poi_brief));
      meta.append(why);
    }
    if (it.poi) {
      const th = el('div', 'thumb', (it.poi || '?').slice(0, 1));
      th.title = '地点缩略图占位（暂无图片数据）';
      const wrap = el('div', 'rbody');
      wrap.append(th, meta);
      body.append(wrap);
    } else {
      body.append(meta);
    }
    const acts = el('div', 'acts');
    const b1 = el('button', 'btn sm', '改时间');
    b1.onclick = () => moveDialog(it);
    const b2 = el('button', 'btn sm', '出行建议');
    b2.onclick = () => showAdvice(it.task_id);
    const b3 = el('button', 'btn sm', it.done ? '取消完成' : '完成');
    b3.onclick = async () => { await cmd('done', { task_id: it.task_id }); load(true); };
    const b4 = el('button', 'btn sm', '删除');
    b4.onclick = async () => { await cmd('delete', { task_id: it.task_id }); load(true); };
    acts.append(b1, b2, b3, b4);
    body.append(acts);
    row.append(tm, body);
    box.append(row);
  });
}

function legNode(it) {
  const leg = el('div', 'leg');
  const bd = el('span', 'badge', MODE_LABEL[it.leg.mode] || '行');
  leg.append(bd, el('span', null, it.leg.minutes_text), el('span', 'goto', '→ ' + (it.poi || '')));
  if (it.leg.distance_text) leg.append(el('span', 'cap', it.leg.distance_text));
  const meta = el('span', 'meta');
  const a1 = el('a', null, '换方式');
  a1.href = '#';
  a1.onclick = (e) => { e.preventDefault(); modeMenu(it.task_id, a1); };
  const a2 = el('a', null, '路径详情');
  a2.href = '#';
  a2.onclick = (e) => { e.preventDefault(); showLeg(it.task_id, it.leg.mode); };
  const a3 = el('a', null, '公交方案');
  a3.href = '#';
  a3.onclick = (e) => { e.preventDefault(); showPlans(it.task_id); };
  meta.append(a1, a2, a3);
  leg.append(meta);
  return leg;
}

function renderAgent(d) {
  const s = d.stats;
  const span = s.busy + s.transit + s.idle;
  const eff = span ? Math.round((s.busy / span) * 100) : 0;
  const box = $('#agentKpi');
  box.innerHTML = '';
  [['状态', d.mode_short], ['时间利用率', `${eff}%`], ['空档', compact(s.idle)]].forEach(([k, v]) => {
    const c = el('div', 'cell');
    c.append(el('b', null, v), el('span', null, k));
    box.append(c);
  });
  $('#agentState').innerHTML = '';
  $('#agentState').append(el('span', 'dot'), document.createTextNode(d.mode_short));

  // 推理链：用真实的解析/排程/冲突结果拼三步
  const chain = $('#chain');
  chain.innerHTML = '';
  const steps = [];
  if (S.preview && S.preview.draft) {
    const dr = S.preview.draft;
    steps.push(['解析', `输入「${S.preview.text}」→ 意图：${dr.title} / ${dr.poi || '无地点'} / ${dr.duration_text}`]);
  } else {
    const first = ((S.data || {}).items || []).find((x) => !x.unscheduled);
    steps.push(['解析', first
      ? `已排「${first.title}」等 ${s.count} 项（历史录入，最近一轮解析见底部输入）`
      : '等待下一条指令']);
  }
  steps.push(['排程', `按"锁定优先 + 空档扫描"求解：排入 ${s.count} 项，通行合计 ${s.transit_text}`]);
  const nc = (d.notices || []).length, cc = (d.conflicts || []).length;
  steps.push(['校验', `冲突 ${cc} 项、营业提示 ${nc} 条；${cc ? '已给出可执行建议' : '未发现硬冲突'}`]);
  // Key 配了但调不通时，解析已经退回规则：这一步必须说出来，不能装作"模型解析"
  if (d.llm_ok === false) {
    steps[0][1] += '（大模型调用失败，本条走规则兜底解析）';
  }
  steps.forEach(([name, text], i) => {
    const st = el('div', 'step');
    st.append(el('span', 'no', String(i + 1)));
    const tx = el('span', 'txt');
    tx.append(el('b', null, name + '：'), document.createTextNode(text));
    st.append(tx);
    chain.append(st);
  });
}

function renderReport(d) {
  const box = $('#report');
  box.innerHTML = '';
  const rows = [];
  // 放在最前面：这是"看起来在线、实际已降级"，比业务提示更要紧
  if (d.llm_ok === false) {
    const msg = String(d.llm_error || '').slice(0, 80);
    rows.push(['danger', (msg.startsWith('大模型') ? msg : `大模型调用失败：${msg}`)
      + '；解析已回退规则，请检查模型名 / 余额 / 网络']);
  }
  (d.conflicts || []).forEach((c) => rows.push(['danger', `冲突：${c.detail}`]));
  (d.notices || []).forEach((n) => rows.push(['warn', `提示：${n.detail}`]));
  if (!rows.length) { box.textContent = '暂无需要提醒的事项。'; return; }
  rows.forEach(([kind, text]) => {
    const div = el('div', kind === 'warn' ? 'w' : '');
    div.textContent = '· ' + text;
    box.append(div);
  });
  // 排不下时给一条可执行的出路：按优先级取舍（先算、再让用户确认）
  const unscheduled = (d.items || []).filter((i) => i.unscheduled);
  if (unscheduled.length) {
    const bar = el('div', 'cap');
    bar.style.marginTop = '8px';
    const btn = el('button', 'btn ghost sm', `按优先级取舍（${unscheduled.length} 项排不下）`);
    btn.onclick = async () => {
      btn.disabled = true;
      const r = await cmd('fit_day', {}).catch(() => null);
      btn.disabled = false;
      if (!r || !r.ok) return toast((r && r.message) || '无法给出取舍方案');
      const fit = r.fit || {};
      if (!(fit.drop || []).length) return toast(r.message || '当天本来排得下');
      showFitPreview(fit);
    };
    bar.append(btn);
    box.append(bar);
  }
  if (d.explanation) {
    box.append(el('hr', 'hr'));
    box.append(el('div', 'cap', d.explanation));
  }
}

/* 各方式全程耗时：把当天每个换乘段的四方式耗时累加，条长按最慢方式归一。
   数据全部来自 state_payload 的 items[].modes，不做任何猜测。 */
function renderModeBars(d) {
  const box = $('#modeBars');
  if (!box) return;
  box.innerHTML = '';
  const sum = {};
  (d.items || []).forEach((it) => {
    if (!it.modes) return;
    Object.entries(it.modes).forEach(([k, v]) => {
      if (v && v.minutes) sum[k] = (sum[k] || 0) + v.minutes;
    });
  });
  const rows = Object.entries(sum);
  if (!rows.length) {
    // 四方式对比默认不取（切日期要快）；这里给一个按需加载入口
    const btn = el('button', 'btn ghost sm', '载入各方式对比');
    btn.onclick = async () => {
      btn.disabled = true;
      btn.textContent = '载入中…';
      const d2 = await api('/api/state', { date: S.day, modes: 1 }).catch(() => null);
      if (d2) { renderModeBars(d2); return; }
      btn.disabled = false;
      btn.textContent = '载入各方式对比';
    };
    box.append(btn);
    return;
  }
  const max = Math.max(...rows.map(([, v]) => v));
  const fastest = rows.reduce((a, b) => (b[1] < a[1] ? b : a))[0];
  rows.sort((a, b) => a[1] - b[1]).forEach(([k, v]) => {
    const row = el('div', 'bar' + (k === fastest ? ' fast' : ''));
    row.append(el('span', 'nm', MODE_NAME[k] || k));
    const tk = el('div', 'tk');
    const fl = el('div', 'fl');
    fl.style.width = Math.max(6, Math.round((v / max) * 100)) + '%';
    tk.append(fl);
    row.append(tk, el('span', 'vl', compact(v)));
    row.title = `${MODE_NAME[k] || k}：当天各段合计 ${v} 分钟`;
    box.append(row);
  });
  box.append(el('div', 'cap', '各方式按当天每段耗时累加，含段间缓冲不计'));
}

function renderEco(d) {
  const legs = (d.items || []).filter((x) => x.leg && x.leg.distance_m);
  let km = 0, co2 = 0;
  legs.forEach((x) => {
    const k = x.leg.distance_m / 1000;
    km += k;
    co2 += k * (CO2[x.leg.mode] ?? 0);
  });
  const box = $('#ecoStats');
  if (!legs.length) { box.textContent = '当天还没有需要换乘的路段。'; return; }
  box.innerHTML = '';
  box.append(el('div', null, `换乘里程合计约 ${km.toFixed(1)} 公里`));
  if (co2 > 0) {
    box.append(el('div', null, `碳排估算约 ${co2.toFixed(2)} kg CO₂`));
    box.append(el('div', 'cap', '按各段方式系数估算（驾车 0.21 / 公交 0.05 kg·km⁻¹），非实测'));
  } else {
    box.append(el('div', null, '全程步行/骑行，不产生碳排放'));
    box.append(el('div', 'cap', '步行与骑行按 0 kg·km⁻¹ 计'));
  }
}

/* ---------------- 起始页 ---------------- */
function renderHome() {
  const d = S.data;
  const pack = (S.options && S.options.places) || [];
  const hour = new Date().getHours();
  const word = hour < 6 ? '凌晨好' : hour < 11 ? '早上好' : hour < 13 ? '中午好'
    : hour < 18 ? '下午好' : '晚上好';
  $('#helloTitle').textContent = `${word}，${d ? d.city_label : '金陵'}`;
  $('#helloSub').textContent = d && d.stats.count
    ? `今天已排 ${d.stats.count} 项、通行 ${d.stats.transit_text}；可以直接进工作区，或再补一件`
    : '几天、几个人、想玩什么，都丢给我；没头绪也能先给一版可改的行程';

  const strap = $('#homeStrap');
  strap.innerHTML = '';
  if (d) {
    [['安排', `${d.stats.count} 项`], ['占用', d.stats.busy_text],
     ['通行', d.stats.transit_text], ['空档', d.stats.idle_text],
     ['提示', `${(d.notices || []).length} 条`]].forEach(([k, v]) => {
      const it = el('span', 'it');
      it.append(document.createTextNode(k + ' '), el('b', null, v));
      strap.append(it);
    });
    const go = el('button', 'btn sm primary', '进入工作区 →');
    go.style.marginLeft = 'auto';
    go.onclick = () => setPage('work');
    strap.append(go);
  } else {
    strap.append(el('span', 'it', '正在读取…'));
  }

  const used = new Set(((d && d.items) || []).map((x) => x.poi).filter(Boolean));
  const picks = pack.filter((p) => !used.has(p.name)).slice(0, 4);
  $('#recHint').textContent = pack.length ? '按城市包顺序，非热度排名' : '';
  const box = $('#homeRecs');
  box.innerHTML = '';
  if (!picks.length) { box.append(el('div', 'empty', '城市包里没有更多可选地点')); return; }
  picks.forEach((p) => {
    const c = el('button', 'rec');
    c.type = 'button';
    c.append(el('div', 'tag', p.kind || '地点'));
    c.append(el('div', 't', p.name));
    c.append(el('div', 'd', p.brief || p.district || ''));
    const m = el('div', 'm');
    m.append(el('span', null, p.district || ''));
    m.append(el('span', null, p.visit_text || ''));
    c.append(m);
    c.onclick = () => {
      $('#addInput').value = `明天上午九点去${p.name}，大概${p.visit_min ? Math.round(p.visit_min / 60 * 10) / 10 + '小时' : '2小时'}`;
      setPage('work');
    };
    box.append(c);
  });
}

/* ---------------- 左栏交互 ---------------- */
function bindLeft() {
  $('#rngBuffer').oninput = (e) => { $('#valBuffer').textContent = `${e.target.value} 分`; };
  $('#rngBuffer').onchange = async (e) => {
    await cmd('set_settings', { buffer_min: +e.target.value });
    load(true);
  };
  $('#swKeepOrder').onclick = () => toggleStrategy('keep_order');
$('#swAutoMode').onclick = () => toggleStrategy('auto_mode');
$('#swOffline').onclick = async () => {
    await cmd('toggle_offline');
    load(true);
  };
  $('#btnLodging').onclick = async () => {
    const d = await api('/api/lodging');
    const m = $('#ctxMenu');
    m.innerHTML = '';
    if (d.current) {
      const b = el('button', null, `清除当前住宿（${d.current}）`);
      b.onclick = async () => { closeMenus(); await cmd('clear_lodging'); load(true); };
      m.append(b);
    }
    (d.candidates || []).forEach((c) => {
      const b = el('button', null, `${c.name}　${c.star || ''}　${c.price || ''}`);
      b.title = c.reason || '';
      b.onclick = async () => { closeMenus(); await cmd('set_lodging', { name: c.name }); load(true); };
      m.append(b);
    });
    const b2 = el('button', null, '当天往返，不需要住宿');
    b2.onclick = async () => { closeMenus(); await cmd('decline_lodging'); load(true); };
    m.append(b2);
    placeMenu(m, $('#btnLodging'));
  };
  $('#btnClearLodging').onclick = async () => { await cmd('clear_lodging'); load(true); };
  $('#btnReplan').onclick = async () => { await cmd('replan'); load(true); };
}

/* ---------------- 菜单与弹层 ---------------- */
function placeMenu(menu, anchor) {
  const r = anchor.getBoundingClientRect();
  menu.style.left = Math.min(r.left, window.innerWidth - 200) + 'px';
  menu.style.top = (r.bottom + 6) + 'px';
  menu.classList.remove('hidden');
}
function closeMenus() { $('#ctxMenu').classList.add('hidden'); }
document.addEventListener('click', (e) => {
  if (!e.target.closest('#ctxMenu') && !e.target.closest('.btn') &&
      !e.target.closest('.leg a')) closeMenus();
});

/* ---------------- 应用内信息面板（替代 alert / prompt / confirm） ---------------- */

/* 全部规划记录：把库里所有"有安排的日期"列出来，可跳转 / 可删除（应用内，不用脚本） */
async function openPlansIndex() {
  const d = await api('/api/plansindex');
  const days = d.days || [];
  const { body } = openPanel('全部规划记录', `${days.length} 份`);
  if (!days.length) {
    body.append(el('div', 'cap', '还没有任何规划。在起始页说一句话就会生成一份。'));
    return;
  }
  days.forEach((r) => {
    const row = el('div', 'ip-step');
    row.append(el('i', 'no', r.city === '京城' ? '京' : '宁'));
    const t = el('div', 'txt');
    t.append(el('b', null, `${r.date} · ${r.title || '当日行程'}`));
    t.append(el('span', 'cap',
      `${r.city} · ${r.count} 项`
      + (r.first ? ` · ${r.first}-${r.last}` : '')
      + (r.lodging ? ` · 起点 ${r.lodging}` : '')));
    row.append(t);
    const acts = el('div', 'ip-row');
    const open = el('button', 'btn sm', '打开');
    open.onclick = async () => {
      document.getElementById('infoPanel').remove();
      if (r.city !== (S.data ? S.data.city : '')) {
        await cmd('set_city', { city: r.city });
        await boot2();
      }
      S.day = r.date;
      S.rev = null;
      syncUrl();
      setPage('work');
      load(true);
    };
    const del = el('button', 'btn sm', '删除');
    del.onclick = async () => {
      if (del.dataset.armed !== '1') {
        del.dataset.armed = '1';
        del.textContent = '再点确认';
        return;
      }
      const res = await cmd('delete_day', { date: r.date });
      toast(res && res.message ? res.message : '已删除');
      document.getElementById('infoPanel').remove();
      load(true);
    };
    acts.append(open, del);
    row.append(acts);
    body.append(row);
  });
}

function openPanel(title, sub) {
  const old = document.getElementById('infoPanel');
  if (old) old.remove();
  const p = el('div', 'infopanel');
  p.id = 'infoPanel';
  const hd = el('div', 'ip-hd');
  const close = el('button', 'iconbtn', '×');
  close.title = '关闭（Esc）';
  close.onclick = () => p.remove();
  hd.append(el('b', null, title), el('span', 'cap', sub || ''), el('span', 'spacer'), close);
  const bd = el('div', 'ip-bd');
  p.append(hd, bd);
  document.body.append(p);
  return { panel: p, body: bd };
}

function moveDialog(it) {
  const { body } = openPanel('改开始时间', it.title || '');
  const row = el('div', 'ip-row');
  const inp = el('input');
  inp.type = 'time';
  inp.value = it.time || '09:00';
  const ok = el('button', 'btn primary sm', '确定');
  ok.onclick = async () => {
    const v = (inp.value || '').trim();
    if (!/^\d{1,2}:\d{2}$/.test(v)) return toast('时间格式应为 HH:MM');
    await cmd('move', { task_id: it.task_id, start: v });
    document.getElementById('infoPanel').remove();
    load(true);
  };
  row.append(inp, ok);
  body.append(el('div', 'cap', `原时间 ${it.time || '—'}`), row);
  setTimeout(() => inp.focus(), 50);
}
function modeMenu(taskId, anchor) {
  const m = $('#ctxMenu');
  m.innerHTML = '';
  Object.entries(MODE_NAME).forEach(([k, label]) => {
    const b = el('button', null, `改用${label}`);
    b.onclick = async (e) => {
      e.stopPropagation(); closeMenus();
      await cmd('set_mode', { task_id: taskId, mode: k });
      load(true);
    };
    m.append(b);
  });
  placeMenu(m, anchor);
}
async function showLeg(taskId, mode) {
  const d = await api('/api/leg', { task_id: taskId, mode: mode || '' });
  if (!d.ok) return toast(d.message || '无法获取路径详情');
  const det = d.detail || {};
  const { body } = openPanel('路径详情',
    `${det.from || ''} → ${det.to || ''}`.replace(' → ', det.from ? ' → ' : ''));
  const head = el('div', 'ip-sum');
  if (det.minutes || det.distance_m) {
    head.textContent = `合计 ${det.minutes || 0} 分钟 · `
      + `${((det.distance_m || 0) / 1000).toFixed(1)} 公里（${MODE_NAME[det.mode] || ''}）`;
    body.append(head);
  }
  const steps = det.steps || [];
  if (!steps.length) {
    body.append(el('div', 'cap', d.message || '该段没有分步信息'));
  } else {
    steps.forEach((s, i) => {
      const row = el('div', 'ip-step');
      row.append(el('i', 'no', String(i + 1)));
      const t = el('div', 'txt');
      t.append(el('b', null, s.instruction || '继续前行'));
      t.append(el('span', 'cap',
        `${s.distance_m || 0} 米 · ${s.minutes || 0} 分钟`));
      row.append(t);
      body.append(row);
    });
  }
  if (d.message) body.append(el('div', 'cap', d.message));
}
async function showPlans(taskId) {
  const d = await api('/api/plans', { task_id: taskId });
  const plans = d.plans || [];
  const { body } = openPanel('公交方案', plans.length ? `${plans.length} 个方案` : '');
  if (!plans.length) {
    body.append(el('div', 'cap', d.note || '没有可用公交方案'));
    return;
  }
  plans.slice(0, 5).forEach((p, i) => {
    const row = el('div', 'ip-step');
    row.append(el('i', 'no', String(i + 1)));
    const t = el('div', 'txt');
    t.append(el('b', null, p.lines || p.line || '公交线路'));
    t.append(el('span', 'cap',
      `${p.minutes || p.duration_min || '—'} 分钟${p.walk_m ? ' · 步行 ' + p.walk_m + ' 米' : ''}`));
    row.append(t);
    body.append(row);
  });
  if (d.note) body.append(el('div', 'cap', d.note));
}
async function showAdvice(taskId) {
  const d = await api('/api/advice', { task_id: taskId });
  if (!d.ok) return toast(d.message || '无法生成出行建议');
  const a = d.advice || {};
  const { body } = openPanel('出行建议', a.task_title || '');
  if (a.summary) body.append(el('div', 'ip-sum', a.summary));
  (a.options || []).forEach((o) => {
    const row = el('div', 'ip-step');
    row.append(el('i', 'no', '·'));
    const t = el('div', 'txt');
    t.append(el('b', null, `${o.depart_label} 出发`));
    t.append(el('span', 'cap', `${o.minutes} 分钟（${o.factor_label}）`
      + (o.wait_min ? ` · 早到 ${o.wait_min} 分钟` : '')));
    row.append(t);
    body.append(row);
  });
  if (a.caveat) body.append(el('div', 'cap', a.caveat));
}


/* 多日草案卡：按天列出建议行程，确认后逐日写入 */
function showProposal(plan) {
  renderProposal(plan);
  $('#previewSheet').classList.remove('hidden');
}

//: 只渲染、不弹面板（起始页聊天时用：先给一行摘要，用户点开才看细则）
function renderProposal(plan) {
  const body = $('#previewBody');
  body.innerHTML = '';
  body.style.gridTemplateColumns = '1fr';
  const head = el('div', 'cap');
  head.textContent = (plan.message || '行程草案')
    + (plan.wake ? `（可排起点 ${plan.wake}）` : '');
  body.append(head);
  (plan.notes || []).forEach((n) => body.append(el('div', 'cap', '· ' + n)));
  (plan.days || []).forEach((d) => {
    const card = el('div', 'plan-day');
    const head = el('div', 't');
    head.append(el('span', null, `${d.date} 周${d.weekday}`));
    if (d.theme) head.append(el('span', 'tag', d.theme));
    head.append(el('span', 'cap', `${(d.items || []).length} 项`));
    if (d.existing) {                    // 这天原本就有安排：先说清是追加
      head.append(el('span', 'tag', `该日已有 ${d.existing} 项`));
    }
    card.append(head);
    if (d.why) card.append(el('div', 'why', d.why));
    // 头尾交代：从哪出发、结束回哪（没有住宿锚点就说清楚"当天往返"）
    const home = plan.hotel_pick || plan.hotel || '';
    card.append(el('div', 'why', home
      ? `· 起点 ${home}（先放行李/退房）→ 结束回 ${home}`
      : '· 未定住宿：按当天往返算，可在左栏「锚点」里设定酒店'));
    if (d.existing) {
      card.append(el('div', 'why',
        `· 确认后是**追加**：原有 ${d.existing} 项保留，新地点排在其后（时刻会按新路线重算）`));
    }
    card.append(el('div', 'stops', (d.items || [])
      .map((i) => `${i.poi}（${durText(i.duration_min)}）`).join(' → ')));
    (d.items || []).forEach((i) => {
      if (i.open) card.append(el('div', 'why', `· ${i.poi} 营业 ${i.open}`));
      if (i.needs_poi) {
        const row = el('div', 'bp-edit');
        row.append(el('span', 'cap', '待补地点'));
        const inp = el('input');
        inp.value = i.poi || '';
        inp.className = 'bp-poi';
        inp.placeholder = '补个餐厅/地名，或留空则跳过';
        inp.oninput = () => { i.poi = inp.value.trim(); i.needs_poi = !i.poi; };
        row.append(inp);
        if (i.reason) row.append(el('span', 'cap', i.reason));
        card.append(row);
      }
    });
    body.append(card);
  });
  // 住宿锚点：不管是"他自己说的"还是"我替他挑的"，都要能看见、能改、能换成别家
  let hotel = plan.hotel_pick || (plan.hotel && plan.hotel !== 'recommend' ? plan.hotel : '');
  if (plan.hotel || (plan.hotel_options || []).length) {
    const pick = el('div', 'bp-opt');
    pick.append(el('span', 'cap', plan.hotel === 'recommend'
      ? '住宿锚点（我挑的，可改）' : '住宿锚点'));
    const inp = el('input');
    inp.value = hotel;
    inp.className = 'bp-title';
    inp.placeholder = '酒店名，或留空则当天往返';
    inp.oninput = () => { hotel = inp.value.trim(); };
    pick.append(inp);
    body.append(pick);
    const opts = (plan.hotel_options || []).filter((h) => h && h.name);
    if (opts.length) {
      const row = el('div', 'bp-edit');
      row.append(el('span', 'cap', '换成'));
      opts.forEach((h) => {
        const b = el('button', 'btn ghost sm',
          h.price_label ? `${h.name}（${h.price_label}）` : h.name);
        b.type = 'button';
        b.onclick = () => { inp.value = h.name; hotel = h.name; };
        row.append(b);
      });
      body.append(row);
    }
  }
  S.preview = { kind: 'proposal', plan, hotel };
  $('#btnConfirmAdd').onclick = async () => {
    const btn = $('#btnConfirmAdd');
    const days = ((S.preview.plan || {}).days || []).length;
    // 写入要逐天排程并取通行时长（多天通常十几秒到一分钟），必须当场给反馈，
    // 否则用户看到的就是"点了没反应"，还会重复点。
    btn.disabled = true;
    const oldText = btn.textContent;
    btn.textContent = `正在写入 ${days} 天…（约 ${Math.max(10, days * 8)} 秒）`;
    toast(`正在写入 ${days} 天行程，请稍候…`);
    const planNow = S.preview.plan;
    let pending = 0;
    (planNow.days || []).forEach((dd) => {
      dd.items = (dd.items || []).filter((it) => {
        if (it.needs_poi && !it.poi) { pending += 1; return false; }
        return true;
      });
    });
    let r = null;
    try {
      r = await cmd('apply_proposal',
        { plan: planNow, hotel: S.preview.hotel || '' });
    } catch (e) {
      r = { ok: false, message: '写入失败：' + (e.message || e) };
    } finally {
      btn.disabled = false;
      btn.textContent = oldText;
    }
    const daysAll = ((S.preview.plan || {}).days || []);
    const first = daysAll[0] || {};
    const last = daysAll[daysAll.length - 1] || {};
    closePreview();
    // 不再自动跳到第一天：用户可能正在看别的日期，突然被搬走会让人以为"界面自己乱跳"
    let tip = (r && r.message) || '已写入草案';
    if (first.date && last.date) {
      tip += first.date === last.date
        ? `（${first.date}）`
        : `（${first.date} ~ ${last.date}，顶栏「日历」可逐天查看）`;
    }
    if (pending) { tip += `；${pending} 项待补地点已跳过`;
    }
    toast(tip);
    consultAdd('ai', tip);
    S.rev = null;
    load(true);
    $('#btnConfirmAdd').onclick = defaultConfirm;
  };
}

/* 取舍预览：把"建议舍弃哪几项、留下后末项几点结束"摆出来，确认才删 */
function showFitPreview(fit) {
  const body = $('#previewBody');
  body.innerHTML = '';
  body.style.gridTemplateColumns = '1fr';
  const head = el('div', 'cap');
  head.textContent = fit.message || '按优先级取舍';
  body.append(head);
  const drop = el('div');
  drop.append(el('b', null, '建议舍弃：'));
  drop.append(el('span', 'cap', (fit.drop || []).join('、') || '—'));
  body.append(drop);
  (fit.drop || []).forEach((t) => {
    const why = (fit.why || {})[t];
    if (why) body.append(el('div', 'bp-warn', `· ${t}：${why}`));
  });
  const keep = el('div', 'cap');
  keep.textContent = `保留 ${(fit.kept || []).length} 项，末项 ${fit.end || '—'} 结束`;
  body.append(keep);
  $('#previewSheet').classList.remove('hidden');
  S.preview = { kind: 'fit' };
  $('#btnConfirmAdd').onclick = async () => {
    const r = await cmd('fit_day', { apply: true }).catch(() => null);
    closePreview();
    toast((r && r.message) || '已按建议取舍');
    load(true);
    $('#btnConfirmAdd').onclick = defaultConfirm;
  };
}

/* ---------------- 批量多目的地：一次确认多条 ---------------- */
function showBatchPreview(d) {
  const body = $('#previewBody');
  body.innerHTML = '';
  body.style.gridTemplateColumns = '1fr';

  const head = el('div', 'cap');
  const howMany = `解析出 ${d.items.length} 项`;
  const howSplit = d.split_by === 'poi' ? '（按地点名断句）' : '';
  const pending = d.items.filter((x) => x.needs_poi).length;
  head.textContent = howMany + howSplit + (d.origin ? `，起点：${d.origin}` : '')
    + (d.return_by ? `，返程死线 ${d.return_by}` : '')
    + (pending ? `；其中 ${pending} 项还差地点` : '');
  body.append(head);

  d.items.forEach((it, i) => {
    const row = el('div', 'bp-item');
    const line = el('div', 'bp-line');
    line.append(el('i', 'seqno', String(i + 1)));
    const t = el('div', 'bp-txt');
    const tag = it.kind === 'block' ? '（只占时间，无地点）'
      : it.kind === 'todo' ? '（待补地点）' : '';
    t.append(el('b', null, `${it.title || '事项'}${it.poi ? ' · ' + it.poi : ''}${tag}`));
    const durTag = it.duration_label ? `（${it.duration_label}${it.duration_range
      ? ' ' + Math.round(it.duration_range[0] / 60 * 10) / 10 + '–'
        + Math.round(it.duration_range[1] / 60 * 10) / 10 + ' 小时' : ''}）` : '';
    t.append(el('span', 'cap',
      `${it.duration_text || durText(it.duration_min)}${durTag}`
      + (it.desired_start ? ' · ' + it.desired_start + ' 起' : '')
      + (it.poi_brief ? ' · ' + String(it.poi_brief).slice(0, 40) : '')));
    line.append(t);
    row.append(line);

    // 每一项都可以改标题/时长/地点
    const edit = el('div', 'bp-edit');
    const tIn = el('input'); tIn.value = it.title || ''; tIn.className = 'bp-title';
    const dIn = el('input'); dIn.type = 'number'; dIn.min = '10'; dIn.step = '5';
    dIn.value = it.duration_min || 60; dIn.className = 'bp-dur';
    edit.append(el('span', 'cap', '标题'), tIn, el('span', 'cap', '分钟'), dIn);
    if (it.needs_poi) {
      // 认不出地点时不要丢句，给一个输入框让用户补一句
      const pIn = el('input'); pIn.value = it.poi || ''; pIn.className = 'bp-poi';
      pIn.placeholder = '补个地名，或留空跳过';
      edit.append(el('span', 'cap', '地点'), pIn);
      pIn.oninput = () => { d.items[i].poi = pIn.value.trim(); };
    }
    row.append(edit);
    tIn.oninput = () => { d.items[i].title = tIn.value; };
    dIn.oninput = () => { d.items[i].duration_min = +dIn.value || 60; };

    if (it.reason) row.append(el('div', 'bp-warn', '· ' + it.reason));
    (it.notices || []).forEach((nt) => {
      row.append(el('div', 'bp-warn', '· ' + nt));
    });
    body.append(row);
  });

  if ((d.unparsed || []).length) {
    const u = el('div', 'cap');
    u.style.color = 'var(--warn)';
    u.textContent = '未能解析：' + d.unparsed.join(' / ');
    body.append(u);
  }

  // 条件备选（"下雨就改去…"）：不排进今天，但给一个一键切换
  (d.conditional || []).forEach((c, ci) => {
    const box = el('div', 'bp-cond');
    const line = el('div', 'cap');
    line.textContent = `条件备选（${c.trigger}）：${(c.places || []).join('、') || c.text}`;
    box.append(line);
    const btn = el('button', 'btn ghost sm', `改用这套（${c.trigger}）`);
    btn.onclick = async () => {
      const txt = (c.places || []).join('、');
      if (!txt) return toast('这套备选里没认出地名');
      const r = await cmd('preview_batch', { text: txt }).catch(() => null);
      if (r && r.ok) { showBatchPreview(r); toast(`已切到「${c.trigger}」方案，确认后写入`); }
      else toast('这套备选解析失败');
    };
    box.append(btn);
    body.append(box);
  });

  // 顺序策略
  const opt = el('div', 'bp-opt');
  // 批量卡的"按给定顺序"独立于左栏策略开关，默认开
  S.batchKeepOrder = true;
  const sw = el('span', 'sw on');
  sw.onclick = () => {
    S.batchKeepOrder = !S.batchKeepOrder;
    sw.classList.toggle('on', S.batchKeepOrder);
  };
  opt.append(el('span', 'cap', '按这个顺序排（关掉则系统自选最优顺序）'), sw);
  body.append(opt);
}

async function confirmBatch() {
  const p = S.preview;
  if (!p || p.kind !== 'batch') return;
  const ready = p.items.filter((x) => x.poi || x.kind === 'block' || x.kind === 'lodging');
  const pending = p.items.length - ready.length;
  const r = await cmd('add_batch', {
    items: ready.map((x) => ({
      kind: x.kind, title: x.title, poi: x.poi, duration_min: x.duration_min,
      category: x.category, desired_start: x.desired_start,
      raw_text: x.raw_text || x.segment || '',
    })),
    origin: p.origin || null,
    return_by: p.returnBy || '',
    keep_order: S.batchKeepOrder !== false,
  });
  closePreview();
  let msg = r && r.message ? r.message : (r && r.ok ? '已写入' : '写入失败');
  if (pending > 0) msg += `；${pending} 项还没补地点，已跳过`;
  if (p.returnBy) msg += `；返程死线 ${p.returnBy} 已记下`;
  await boot2();          // 刷新左栏策略开关的显示
  setPage('work');
  load(true);
  toast(msg);             // 放到最后：避免被后续请求的提示覆盖掉"跳过 N 项"
}


/* ---------------- 起始页 · 行程顾问（主入口） ----------------
   会话 id 存在 localStorage：服务端保存槽位（日期/人数/兴趣/节奏），
   所以刷新页面、换设备都还能接着聊，不用重头说一遍。 */
function consultSessionId() {
  let sid = localStorage.getItem('consultSession');
  if (!sid) {
    sid = 'c' + Date.now().toString(36) + Math.random().toString(36).slice(2, 6);
    localStorage.setItem('consultSession', sid);
  }
  return sid;
}

function consultAdd(who, text) {
  const log = $('#consultLog');
  if (!log) return null;
  const row = el('div', who === 'me' ? 'consult-me' : 'consult-ai', text);
  log.append(row);
  log.scrollTop = log.scrollHeight;
  return row;
}

function consultChips(items, onClick) {
  const box = $('#consultChips');
  if (!box) return;
  box.innerHTML = '';
  (items || []).forEach((label) => {
    const b = el('button', 'btn ghost sm', label);
    b.onclick = () => onClick(label);
    box.append(b);
  });
}

async function onConsult() {
  const src = $('#homeInput');
  const text = (src.value || '').trim();
  if (!text) return toast('说一句就行：几天、几个人、想玩什么');
  // 聊完想落库：直接说"写入/就这个"就打开确认面板，不用去找按钮
  if (/^(写入|确认|就这个|就这样|可以了|不用改了)[。.!！]?$/.test(text)
      && S.preview && S.preview.kind === 'proposal') {
    src.value = '';
    autoGrow(src);
    consultAdd('me', text);
    consultAdd('ai', '好，我把草案摊开给你确认：');
    $('#previewSheet').classList.remove('hidden');
    return;
  }
  // 明显是"报地名/带时长"的录入请求：仍走批量预览卡（顾问与录入并存）
  const looksLikeList = /(去|到).{0,8}?(分钟|小时)/.test(text)
    || (/[、\s]/.test(text) && !/(推荐|规划|安排|行程|几天|玩|头绪)/.test(text));
  if (looksLikeList) return onPreview();
  consultAdd('me', text);
  src.value = '';
  autoGrow(src);
  const pendingRow = consultAdd('ai', '正在排草案…（约 20–60 秒）');
  const r = await cmd('consult', { text, session: consultSessionId() }).catch(() => null);
  if (pendingRow) pendingRow.remove();     // 结果回来就别把"正在…"留在那儿
  if (!r || r.ok === false) {
    consultAdd('ai', (r && r.message) || '连接不上本地服务，稍后再试');
    return;
  }
  if (r.kind === 'entry') {           // 用户是在报地名：走录入批量卡，不追问日期
    consultAdd('ai', r.message || '按录入处理');
    $('#homeInput').value = text;
    autoGrow($('#homeInput'));
    return onPreview();
  }
  consultAdd('ai', r.message || '（无回复）');
  (r.questions || []).forEach((q) => {
    // 问句已经拼在这条回复里了，就不要再单独说一遍
    if (q.ask && !(r.message || '').includes(q.ask)) consultAdd('ai', q.ask);
  });
  const opts = [];
  (r.questions || []).forEach((q) => (q.options || []).forEach((o) => opts.push(o)));
  if (r.kind === 'ask') {
    consultChips(opts.length ? opts : ['9月10号-9月16号', '随便你安排'], (v) => {
      $('#homeInput').value = v;
      autoGrow($('#homeInput'));
      onConsult();
    });
    return;
  }
  if (r.plan && (r.plan.days || []).length) {
    // 起始页是"边聊边改"的地方：草案只渲染不弹面板，否则每轮对话都被它挡住
    renderProposal(r.plan);
    consultAdd('ai', '草案如下（要改就接着聊，想写入点下面「查看并写入」）：');
    (r.plan.days || []).slice(0, 5).forEach((d) => {
      const stops = (d.items || []).map((i) => i.poi || `［${i.title}］`).join(' → ');
      consultAdd('ai', `${d.date} 周${d.weekday}：${stops}`);
    });
    const quick = ['查看并写入', '换一版', '多加一天', '第二天太满', '换成博物馆为主'];
    if (r.plan.hotel === 'recommend') quick.unshift('住宿我自己定');
    else if (!r.plan.hotel) {
      const ho = (r.plan.hotel_options || []).slice(0, 2).map((h) => h.name);
      quick.unshift(...(ho.length ? ho : ['你帮我选']));
    }
    // 吃哪儿也给出可选：点一个就按它排，不点就留着"就近吃"的时间块
    if (!r.plan.meal) {
      const mo = (r.plan.meal_options || []).slice(0, 2).map((m) => m.name);
      quick.unshift(...(mo.length ? mo : ['就近随便']));
    }
    consultChips(quick.slice(0, 8), (v) => {
      if (v === '查看并写入') {         // 只在用户要看细则/确认时才弹面板
        $('#previewSheet').classList.remove('hidden');
        return;
      }
      $('#homeInput').value = v;
      autoGrow($('#homeInput'));
      onConsult();
    });
  }
}

// 重新开始：旧槽位（日期/住宿/口味/排除项）一并作废，避免"陈年偏好"继续生效
$('#btnConsultReset')?.addEventListener('click', async () => {
  const old = consultSessionId();
  localStorage.removeItem('consultSession');
  localStorage.setItem('consultSession', `c${Date.now().toString(36)}`);
  const log = $('#consultLog');
  if (log) log.innerHTML = '';
  const chips = $('#consultChips');
  if (chips) chips.innerHTML = '';
  consultAdd('ai', '好，从头开始：这次想怎么安排？');
  await cmd('consult_reset', { session: old }).catch(() => null);
});

$('#btnBannerRetry')?.addEventListener('click', () => { load(true); });

/* ---------------- 录入：预览 → 确认 ---------------- */
$('#addForm').addEventListener('submit', onPreview);
$('#homeForm').addEventListener('submit', (e) => { e.preventDefault(); onConsult(); });

async function onPreview(e) {
  if (e) e.preventDefault();
  const src = S.page === 'home' ? $('#homeInput') : $('#addInput');
  const text = src.value.trim();
  if (!text) return toast('先用一句话描述行程安排');
  // 一整段话里可能有好几件事：先按批量解析试一次。
  // 只要解析出 ≥2 项、或存在"待补地点"的条目、或还有没解析的片段，
  // 就走批量预览卡 —— 不能因为只认出 1 项就把其余整句悄悄丢掉。
  // 只有"像在要一份多日行程"时才走提案；纯地名堆/带时长的句子仍走批量预览卡
  const wantsProposal = /(推荐|规划|安排|行程|几天|\d+\s*[月号]|没头绪|没有头绪|怎么玩)/.test(text);
  if (wantsProposal) {
    const prop = await cmd('propose_trip', { text }).catch(() => null);
    if (prop && prop.ok && (prop.plan || {}).days
        && (prop.plan.days || []).length >= 1) {
      showProposal(prop.plan);
      $('#previewSheet').classList.remove('hidden');
      return;
    }
  }
  const b = await cmd('preview_batch', { text }).catch(() => null);
  const bitems = (b && b.items) || [];
  const needBatch = bitems.length >= 2
    || bitems.some((x) => x.needs_poi)
    || (b && (b.unparsed || []).length > 0);
  if (b && b.ok && needBatch) {
    S.preview = { kind: 'batch', text, items: b.items, origin: b.origin || '',
                  returnBy: b.return_by || '' };
    showBatchPreview(b);
    $('#previewSheet').classList.remove('hidden');
    return;
  }
  const d = await cmd('preview', { text });
  if (!d.ok) return toast(d.message || '无法解析这句话');
  if (d.kind === 'lodging') { src.value = ''; setPage('work'); load(true); return; }
  S.preview = { text, draft: d.draft, candidates: d.candidates || [] };
  showPreview(d);
  $('#previewSheet').classList.remove('hidden');
  setTimeout(() => $('#pvTitle') && $('#pvTitle').focus(), 30);
}

function showPreview(d) {
  const body = $('#previewBody');
  body.innerHTML = '';
  const field = (label, node, full) => {
    const w = el('div');
    if (full) w.style.gridColumn = '1 / -1';
    w.append(el('label', null, label), node);
    body.append(w);
  };
  const t = el('input'); t.id = 'pvTitle'; t.value = d.draft.title;
  field('事项', t);
  const time = el('input'); time.id = 'pvTime'; time.value = d.draft.desired_start || '';
  time.placeholder = '留空则由系统择时';
  field('开始时间', time);
  const dur = el('input'); dur.id = 'pvDur'; dur.value = d.draft.duration_min;
  field('耗时（分钟）', dur);
  const poi = el('select'); poi.id = 'pvPoi';
  (d.candidates.length ? d.candidates : [{ name: d.draft.poi, district: d.draft.poi_district }])
    .forEach((c) => {
      const o = el('option', null, `${c.name}${c.district ? '（' + c.district + '）' : ''}`);
      o.value = c.name; poi.append(o);
    });
  field('地点', poi);
  const cat = el('select'); cat.id = 'pvCat';
  ['医疗', '餐饮', '展览', '办事', '其他'].forEach((c) => {
    const o = el('option', null, c); o.value = c; cat.append(o);
  });
  cat.value = d.draft.category || '其他';
  field('类别', cat);
  if (d.draft.poi_brief) {
    const info = el('div', 'cap'); info.style.gridColumn = '1 / -1';
    info.textContent = d.draft.poi_brief; body.append(info);
  }
  if ((d.notices || []).length) {
    const w = el('div'); w.style.gridColumn = '1 / -1'; w.style.color = 'var(--warn)';
    w.style.fontSize = '12.66px';
    w.textContent = '· ' + d.notices.join('\n· ');
    body.append(w);
  }
}
const closePreview = () => $('#previewSheet').classList.add('hidden');
$('#btnCancelPreview').onclick = closePreview;
$('#btnCancelPreview2').onclick = closePreview;
const defaultConfirm = async () => {
  if (!S.preview) return;
  if (S.preview.kind === 'fit') return;          // 由 showFitPreview 自己接管
  if (S.preview.kind === 'proposal') return;     // 由 showProposal 自己接管
  if (S.preview.kind === 'batch') return confirmBatch();
  const patch = {
    title: $('#pvTitle').value.trim(),
    desired_start: $('#pvTime').value.trim(),
    duration_min: parseInt($('#pvDur').value, 10) || 60,
    poi: $('#pvPoi').value,
    category: $('#pvCat').value,
  };
  const r = await cmd('add', { text: S.preview.text, draft: patch });
  if (r.ok) {
    closePreview();
    ['#addInput', '#homeInput'].forEach((s) => {
      const elx = $(s);
      if (elx) { elx.value = ''; autoGrow(elx); }
    });
    S.preview = null;
    setPage('work');
    load(true);
  }
};
$('#btnConfirmAdd').onclick = defaultConfirm;

/* ---------------- 顶栏动作 ---------------- */
$('#prev').onclick = () => shiftDay(-1);
$('#next').onclick = () => shiftDay(1);
$('#today').onclick = () => { S.day = S.today; S.rev = null; syncUrl(); load(true); };
$('#nav').onclick = (e) => {
  const b = e.target.closest('button[data-page]');
  if (b) setPage(b.dataset.page);
};
$('#calPrev').onclick = () => shiftMonth(-1);
$('#calNext').onclick = () => shiftMonth(1);
$('#mvIn').onclick = () => {
  if (S.amap) { S.amap.zoomIn(); return; }
  if ((S.mapZoom || 0) < MAP_MAX_ZOOM) {
    S.mapZoom = Math.min(MAP_MAX_ZOOM, (S.mapZoom || 12) + 1);
  } else {
    S.mapSpan = Math.max(MAP_MIN_SPAN, (S.mapSpan || 1) / 2);
  }
  renderMapPage();
};
$('#mvOut').onclick = () => {
  if (S.amap) { S.amap.zoomOut(); return; }
  if ((S.mapSpan || 1) < 1) {
    S.mapSpan = Math.min(1, (S.mapSpan || 1) * 2);
  } else {
    S.mapZoom = Math.max(3, (S.mapZoom || 12) - 1);
  }
  renderMapPage();
};
$('#mvFit').onclick = () => {
  if (S.amap) { fitAmapView(); return; }
  renderMapPage(true);
};
/* 顶栏「日历」：弹出月历面板（与左栏同一份真实数据，避免两处口径不一致）。
   点某天即跳转；这里用 isoOf 拼日期，不再走 toISOString（否则会差一天）。 */
async function openCalPop(anchor, ym) {
  const m = $('#ctxMenu');
  m.innerHTML = '';
  m.className = 'menu calpop';
  const year = +ym.slice(0, 4), month = +ym.slice(5, 7);
  let days = {};
  try { days = (await api('/api/month', { year, month })).days || {}; } catch (err) { days = {}; }
  const pad = (n) => String(n).padStart(2, '0');

  const hd = el('div', 'cp-hd');
  const prev = el('button', 'cp-nav', '‹');
  const next = el('button', 'cp-nav', '›');
  prev.title = '上个月'; next.title = '下个月';
  prev.onclick = (ev) => { ev.stopPropagation();
    openCalPop(anchor, shiftYm(ym, -1)); };
  next.onclick = (ev) => { ev.stopPropagation();
    openCalPop(anchor, shiftYm(ym, 1)); };
  hd.append(prev, el('b', null, `${year}.${pad(month)}`), next);
  m.append(hd);

  const wk = el('div', 'cp-wk');
  ['一', '二', '三', '四', '五', '六', '日'].forEach((w) => wk.append(el('span', null, w)));
  m.append(wk);

  const grid = el('div', 'cp-grid');
  const lead = (new Date(year, month - 1, 1).getDay() + 6) % 7;
  const total = new Date(year, month, 0).getDate();
  for (let i = 0; i < lead; i++) grid.append(el('span', 'cp-d empty'));
  for (let d = 1; d <= total; d++) {
    const iso = `${year}-${pad(month)}-${pad(d)}`;
    let cls = 'cp-d';
    if (days[iso]) cls += ' has';
    if (iso === S.today) cls += ' today';
    if (iso === S.day) cls += ' on';
    const b = el('button', cls, String(d));
    if (days[iso]) b.append(el('i', 'cdot'));
    b.title = days[iso] ? `${iso} · ${days[iso]} 项` : iso;
    b.onclick = () => { closeMenus(); S.day = iso; S.calYM = iso.slice(0, 7);
      S.rev = null; syncUrl(); load(true); };
    grid.append(b);
  }
  m.append(grid);
  const ft = el('div', 'cp-ft');
  const bt = el('button', 'cp-today', '回到今天');
  bt.onclick = () => { closeMenus(); S.day = S.today; S.calYM = S.today.slice(0, 7);
    S.rev = null; syncUrl(); load(true); };
  ft.append(bt);
  m.append(ft);
  placeMenu(m, anchor);
}

function shiftYm(ym, n) {
  const base = new Date(+ym.slice(0, 4), +ym.slice(5, 7) - 1 + n, 1);
  return `${base.getFullYear()}-${String(base.getMonth() + 1).padStart(2, '0')}`;
}

$('#btnCalendar').onclick = (e) => { openCalPop(e.currentTarget, S.day.slice(0, 7)); };
$('#btnVersions').onclick = async (e) => {
  const anchor = e.currentTarget;      // 异步等待后 currentTarget 会变 null，先抓住
  const m = $('#ctxMenu');
  m.innerHTML = '';
  m.className = 'menu';
  const d = await api('/api/versions');
  if (!d.versions.length) { toast('暂无版本记录'); return; }
  m.append(el('div', 'cp-hd', '版本历史（点两次确认回滚）'));
  d.versions.slice(0, 12).forEach((v) => {
    const label = `#${v.version_id} · ${String(v.created_at).slice(5, 16)} · ${v.note}`;
    const b = el('button', null, label);
    b.dataset.label = label;
    b.onclick = async (ev) => {
      ev.stopPropagation();
      if (b.dataset.armed !== '1') {
        m.querySelectorAll('button[data-label]').forEach((x) => {
          x.dataset.armed = ''; x.classList.remove('danger');
          x.textContent = x.dataset.label;
        });
        b.dataset.armed = '1';
        b.classList.add('danger');
        b.textContent = `确认回滚到 #${v.version_id}？再点一次`;
        return;
      }
      closeMenus();
      await cmd('rollback', { trip_id: S.data.trip_id || null, version_id: v.version_id });
      toast(`已回滚到版本 #${v.version_id}`);
      load(true);
    };
    m.append(b);
  });
  placeMenu(m, anchor);
};
/* ---------------- 位置示意：底图 + SVG 打点连线 ----------------
   底图来自高德静态图（只有路网），点位与折线由前端按 Web Mercator 投影叠上去。
 */
const SVGNS = 'http://www.w3.org/2000/svg';
const svgEl = (tag, attrs) => {
  const n = document.createElementNS(SVGNS, tag);
  Object.entries(attrs || {}).forEach(([k, v]) => n.setAttribute(k, v));
  return n;
};

/* 把「底图 + 打点 + 路段」画进任意容器（浮层与地图页共用）
   legs 是按当前通行方式取到的真实走向；取不到时退回直线并标注估算。 */
function drawMapStage(stage, info, url, legs) {
  stage.innerHTML = '';
  const [W, H] = info.size;
  const img = el('img');
  img.src = url;
  img.alt = '当日地点位置示意';
  // 底图取不到时给出可读提示，而不是留一块黑（切换方式不会重取底图，见缓存头）
  img.onerror = () => {
    if (stage.querySelector('.mp-warn')) return;
    const w = el('div', 'mp-warn', '底图暂时取不到（高德可能限流），路径仍按所选方式绘制');
    stage.append(w);
  };

  // Web Mercator：把经纬度换算成画布像素
  const world = 256 * Math.pow(2, info.zoom);
  const gx = (lng) => ((lng + 180) / 360) * world;
  const gy = (lat) => {
    const s = Math.sin((lat * Math.PI) / 180);
    return (0.5 - Math.log((1 + s) / (1 - s)) / (4 * Math.PI)) * world;
  };
  const cx = gx(info.center[0]), cy = gy(info.center[1]);
  const px = (lng) => gx(lng) - cx + W / 2;
  const py = (lat) => gy(lat) - cy + H / 2;
  const pos = info.points.map((p) => ({ ...p, x: px(p.lng), y: py(p.lat) }));

  const svg = svgEl('svg', { viewBox: `0 0 ${W} ${H}`, class: 'mp-svg' });
  // 路段：有真实折线就画折线，没有就画虚线直连
  (legs || []).forEach((leg) => {
    const real = (leg.lines || []).length > 0;
    (leg.lines || []).forEach((line) => {
      svg.append(svgEl('polyline', {
        points: line.map((q) => `${px(q[0]).toFixed(1)},${py(q[1]).toFixed(1)}`).join(' '),
        fill: 'none', stroke: 'rgba(116,236,187,.92)', 'stroke-width': 4,
        'stroke-linecap': 'round', 'stroke-linejoin': 'round',
      }));
    });
    if (!real) {
      const a = info.points.find((p) => p.name === leg.from);
      const b = info.points.find((p) => p.name === leg.to);
      if (a && b) {
        svg.append(svgEl('polyline', {
          points: `${px(a.lng)},${py(a.lat)} ${px(b.lng)},${py(b.lat)}`,
          fill: 'none', stroke: 'rgba(116,236,187,.55)', 'stroke-width': 2.5,
          'stroke-dasharray': '8 6', 'stroke-linecap': 'round',
        }));
      }
    }
  });
  pos.forEach((p) => {
    const g = svgEl('g', {});
    g.append(svgEl('circle', { cx: p.x, cy: p.y, r: 13,
      fill: 'rgba(17,19,25,.85)', stroke: '#56CFA0', 'stroke-width': 2.5 }));
    const t = svgEl('text', { x: p.x, y: p.y + 4.5, 'text-anchor': 'middle',
      'font-size': 13, 'font-weight': 700, fill: '#E1E2E9' });
    t.textContent = String(p.seq);
    g.append(t);
    const label = svgEl('text', { x: p.x + 18, y: p.y + 5, 'font-size': 13,
      'font-weight': 600, fill: '#E1E2E9',
      stroke: 'rgba(17,19,25,.9)', 'stroke-width': 3.5, 'paint-order': 'stroke' });
    label.textContent = p.name;
    const title = svgEl('title', {});
    title.textContent = `${p.seq}. ${p.time} ${p.name}`;
    g.append(label, title);
    svg.append(g);
  });
  stage.append(img, svg);
  return pos;
}

async function showMapPanel() {
  const old = document.getElementById('mapPanel');
  if (old) { old.remove(); return; }        // 再点一次即关闭
  let info;
  let paths = { legs: [] };
  try {
    info = await api('/api/mapinfo');
    if (info.ok) paths = await api('/api/mappath');
  } catch (err) { info = { ok: false }; }
  if (!info.ok) return toast(info.message || '当日没有可绘制的位置');

  const panel = el('div', 'mappanel');
  panel.id = 'mapPanel';
  const hd = el('div', 'mp-hd');
  const close = el('button', 'iconbtn', '×');
  close.title = '关闭（Esc）';
  close.onclick = () => panel.remove();
  const big = el('button', 'btn sm', '放大查看');
  big.onclick = () => { panel.remove(); setPage('map'); };
  const legs = paths.legs || [];
  const realMode = legs.some((l) => l.source === 'amap');
  hd.append(el('b', null, '位置示意'),
    el('span', 'cap', `${info.points.length} 个地点 · ${MODE_NAME[paths.mode] || S.data?.mode_short || ''}`
      + (realMode ? '真实路径' : '直线估算')),
    el('span', 'spacer'), big, close);
  panel.append(hd);

  const stage = el('div', 'mp-stage');
  drawMapStage(stage, info, `/api/map?date=${S.day}`, legs);
  panel.append(stage);

  const ft = el('div', 'mp-ft');
  info.points.forEach((p) => {
    ft.append(el('span', 'mp-it', `${p.seq}. ${p.time} ${p.name}`));
  });
  panel.append(ft);
  document.body.append(panel);
}

/* ---------------- 地图页：大地图 + 缩放 + 点位居中 ----------------
   底图走 big=1&scale=2（1024×640 → 2048×1280），放大才不糊。
   高德静态图最高 16 级：到顶后靠「缩小取图范围 + 铺满显示」继续放大（span）。
 */
const MAP_MAX_ZOOM = 16;
const MAP_MIN_SPAN = 0.25;

/* ---------------- 高德 JS API：交互式地图（可拖动标记纠正坐标） ----------------
   用 JS API 的收益：标记/路径由高德原生绘制（不再自己算投影）、可拖动标记定位、
   缩放不受静态图 16 级限制、可拿浏览器实时定位。离线或脚本加载失败时退回静态图。
 */
let AMapPromise = null;

function loadAMap(cfg) {
  if (AMapPromise) return AMapPromise;
  AMapPromise = new Promise((resolve, reject) => {
    if (window.AMap) { resolve(window.AMap); return; }
    const s = document.createElement('script');
    s.src = `https://webapi.amap.com/maps?v=${cfg.version || '2.0'}`
      + `&key=${encodeURIComponent(cfg.key)}`
      + `&plugin=${(cfg.plugins || []).join(',')}`;
    s.async = true;
    s.onload = () => (window.AMap ? resolve(window.AMap) : reject(new Error('高德脚本已加载但 AMap 不可用')));
    s.onerror = () => reject(new Error('高德 JS API 加载失败（网络或 Key 受限）'));
    document.head.append(s);
  });
  return AMapPromise;
}

function ensureAmapBox() {
  const stage = $('#mvStage');
  let box = document.getElementById('amapBox');
  if (!box) {
    stage.innerHTML = '';
    box = el('div');
    box.id = 'amapBox';
    stage.append(box);
  }
  return box;
}

function fitAmapView() {
  if (!S.amap || !S.amapOverlays || !S.amapOverlays.length) return;
  S.amap.setFitView(S.amapOverlays, false, [70, 70, 70, 70]);
}

/* 拖动标记 → 解析地址 → 让用户确认后钉住 */
async function onMarkerDragged(marker, point, taskId) {
  const lnglat = marker.getPosition();
  const lng = +lnglat.getLng().toFixed(6);
  const lat = +lnglat.getLat().toFixed(6);
  const { body } = openPanel('确认这个点', point.name || '');
  body.append(el('div', 'ip-sum', `${lng}, ${lat}`));
  const addrLine = el('div', 'cap', '正在解析地址…');
  body.append(addrLine);
  let addr = '';
  try {
    const r = await api('/api/regeo', { lng, lat });
    addr = r.address || (r.message || '');
    addrLine.textContent = r.address ? `解析为：${r.address}` : (r.message || '解析不到地址');
  } catch (err) {
    addrLine.textContent = '解析失败（不影响钉住）';
  }
  const row = el('div', 'ip-row');
  const yes = el('button', 'btn primary sm', '钉住这个位置');
  const no = el('button', 'btn sm', '取消并复位');
  yes.onclick = async () => {
    document.getElementById('infoPanel').remove();
    const r = await cmd('pin_point', { task_id: taskId, lng, lat });
    toast(r && r.ok ? '已钉住并重排' : ((r && r.message) || '钉住失败'));
    renderMapPage(true);
    load(true);
  };
  no.onclick = () => {
    document.getElementById('infoPanel').remove();
    marker.setPosition(new AMap.LngLat(point.lng, point.lat));
  };
  row.append(yes, no);
  body.append(row);
}

async function renderMapAMap(cfg, pts, legsPayload) {
  const box = ensureAmapBox();
  const AMap = await loadAMap(cfg);
  if (!S.amap) {
    S.amap = new AMap.Map(box, {
      zoom: 13, center: [118.79, 32.04], viewMode: '2D',
      resizeEnable: true, mapStyle: 'amap://styles/darkblue',
    });
    S.amap.on('error', (e) => console.warn('AMap error', e));
  }
  S.amap.resize();
  S.amap.clearMap();
  S.amapOverlays = [];

  const points = (pts.points || []);
  const markers = [];
  points.forEach((p, i) => {
    const marker = new AMap.Marker({
      position: [p.lng, p.lat],
      title: `${p.seq}. ${p.name}`,
      label: {
        content: `<div class="amap-mk"><b>${p.seq}</b><span>${esc(p.name)}</span></div>`,
        direction: 'right', offset: new AMap.Pixel(10, -10),
      },
      draggable: true,
      cursor: 'move',
    });
    marker.on('dragend', () => onMarkerDragged(marker, p, p.task_id));
    S.amap.add(marker);
    markers.push(marker);
    S.amapOverlays.push(marker);
  });

  // 选中的方案折线（每段一条，颜色统一）
  const legList = legsPayload.legs || [];
  legList.forEach((leg, li) => {
    const opts = leg.options || [];
    if (!opts.length) return;
    const cur = (S.pick && S.pick[li] != null) ? S.pick[li] : 0;
    const pick = opts[cur] || opts[0];
    const path = [];
    (pick.lines || []).forEach((line) => line.forEach((q) => path.push([q[0], q[1]])));
    if (!path.length) return;
    const pl = new AMap.Polyline({
      path, strokeColor: '#56CFA0', strokeWeight: 6, strokeOpacity: 0.92,
      lineJoin: 'round', lineCap: 'round', showDir: true, zIndex: 60,
    });
    S.amap.add(pl);
    S.amapOverlays.push(pl);
  });

  fitAmapView();
  S.mapZoom = S.amap.getZoom();
  $('#mvZoom').textContent = `z${S.mapZoom.toFixed(0)}`;
  $('#mvIn').disabled = false;
  $('#mvIn').title = '放大（高德交互地图）';
}

function esc(s) {
  return String(s == null ? '' : s).replace(/[&<>"']/g,
    (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
}

async function renderMapPage(reset) {
  if (reset) { S.mapZoom = null; S.mapCenter = null; S.mapSpan = 1; S.pick = []; }
  if (!S.pick) S.pick = [];
  const stage = $('#mvStage');
  const list = $('#mvList');

  // 是否走"高德交互地图"（可拖标记、原生路径、实时定位）
  let cfg = { ready: false };
  try { cfg = await api('/api/mapconfig'); } catch (err) { cfg = { ready: false }; }

  // 按容器实测尺寸取图：**必须让请求的宽高比 = 容器宽高比**，否则底图会被裁切，
  // 而 SVG 标记是按原比例算的，两者就会错位（静态图模式下才需要）。
  const box = stage.getBoundingClientRect();
  let bw = Math.max(240, Math.min(1024, Math.round(box.width)));
  let bh = Math.round(bw * (box.height / Math.max(1, box.width)));
  if (bh > 1024) { bh = 1024; bw = Math.round(bh * (box.width / Math.max(1, box.height))); }
  bh = Math.max(180, bh);
  const span = Math.max(MAP_MIN_SPAN, Math.min(1, S.mapSpan || 1));
  bw = Math.max(200, Math.round(bw * span));
  bh = Math.max(150, Math.round(bh * span));

  let info = { ok: false };
  let pts = { points: [] };
  let legsPayload = { legs: [], mode: S.mapMode || '' };
  let paths = { legs: [] };
  try {
    pts = await api('/api/pointinfo');
    legsPayload = await api('/api/legoptions', { mode: S.mapMode || '', limit: 3 });
    if (!cfg.ready) {
      info = await api('/api/mapinfo', {
        big: 1, w: bw, h: bh, zoom: S.mapZoom || '', center: S.mapCenter || '',
      });
      if (info.ok) paths = await api('/api/mappath', { mode: S.mapMode || '' });
    } else {
      info = { ok: true };
    }
  } catch (err) { info = { ok: false }; }

  // 选中的方案（③ 里挑的那条）
  const legList = (legsPayload.legs || []).map((leg) => ({ ...leg }));
  const drawLegs = legList.map((leg, li) => {
    const opts = leg.options || [];
    if (!opts.length) return null;
    const cur = S.pick[li] == null ? 0 : S.pick[li];
    const pick = opts[cur] || opts[0];
    return { from: leg.from, to: leg.to, mode: leg.mode,
             minutes: pick.minutes, distance_m: pick.distance_m,
             lines: pick.lines, source: 'amap' };
  });
  const drawable = drawLegs.filter(Boolean);
  paths = { legs: drawable.length ? drawable : (paths.legs || []),
            mode: legsPayload.mode };

  if (!info.ok) {
    stage.innerHTML = '';
    stage.append(el('div', 'mv-empty cap', info.message || '当天没有带地点的安排'));
    list.innerHTML = '';
    $('#mvZoom').textContent = 'z—';
    $('#mvSub').textContent = S.day;
    return;
  }

  const realMode = (drawable.length > 0)
    || (paths.legs || []).some((l) => l.source === 'amap');

  // ---------- 画地图 ----------
  if (cfg.ready) {
    try {
      await renderMapAMap(cfg, pts, legsPayload);
    } catch (err) {
      cfg = { ready: false };
      toast('高德交互地图不可用，已退回静态图：' + (err.message || err));
      stage.innerHTML = '';
      info = await api('/api/mapinfo', {
        big: 1, w: bw, h: bh, zoom: '', center: '',
      });
      paths = await api('/api/mappath', { mode: S.mapMode || '' });
    }
  }
  if (!cfg.ready) {
    if (!info.ok) {
      stage.innerHTML = '';
      stage.append(el('div', 'mv-empty cap', '当天没有可绘制的位置'));
      return;
    }
    S.mapZoom = info.zoom;
    const url = `/api/map?date=${S.day}&big=1&scale=2&zoom=${info.zoom}`
      + `&center=${info.center[0]},${info.center[1]}&w=${info.size[0]}&h=${info.size[1]}`;
    drawMapStage(stage, info, url, paths.legs);
    const img = stage.querySelector('img');
    if (img) img.style.cssText = 'width:100%;height:100%;object-fit:fill';
    $('#mvZoom').textContent = span < 1
      ? `z${info.zoom} · ${Math.round(1 / span)}×` : `z${info.zoom}`;
    $('#mvIn').disabled = info.zoom >= MAP_MAX_ZOOM && span <= MAP_MIN_SPAN;
    $('#mvIn').title = info.zoom >= MAP_MAX_ZOOM
      ? `静态图最高 ${MAP_MAX_ZOOM} 级，再放大靠缩小取图范围` : '放大（静态图）';
  }

  // ---------- ② 通行方式（顶栏） ----------
  const seg = $('#mvSeg');
  seg.innerHTML = '';
  Object.entries(MODE_NAME).forEach(([k, label]) => {
    const b = el('button', null, label);
    if (k === (S.mapMode || legsPayload.mode)) b.classList.add('on');
    b.onclick = async () => {
      S.mapMode = k;
      S.pick = [];
      await cmd('set_settings', { transit_mode: k });
      toast(`已切到${label}，正在取该方式的路径`);
      await boot2();
      renderMapPage();
      load(true);
    };
    seg.append(b);
  });

  $('#mvTitle').textContent = `${S.data ? S.data.city_label : ''}当日位置`;
  $('#mvSub').textContent = `${S.day} · ${(pts.points || []).length} 个地点 · `
    + `${MODE_NAME[legsPayload.mode] || ''}`
    + (cfg.ready ? '交互地图（标记可拖动）' : (realMode ? '静态图+精确路径' : '离线直线估算'));

  // ---------- 左侧三步 ----------
  list.innerHTML = '';
  list.append(el('div', 'mv-step', '① 确认点'
    + (cfg.ready ? '（拖标记可纠正）' : '')));
  (pts.points || []).forEach((p) => {
    const row = el('div', 'mv-pt');
    const head = el('button', 'mv-item');
    head.append(el('i', 'seq', String(p.seq)));
    const t = el('span', 'mv-it');
    t.append(el('b', null, `${p.time} ${p.name}`));
    t.append(el('span', 'cap', p.address
      ? `解析为：${p.address}` : `${p.lat.toFixed(4)}, ${p.lng.toFixed(4)}`));
    head.append(t);
    head.onclick = () => {
      if (cfg.ready && S.amap) {
        S.amap.setZoomAndCenter(17, [p.lng, p.lat]);
      } else {
        S.mapCenter = `${p.lng},${p.lat}`;
        S.mapZoom = MAP_MAX_ZOOM;
        S.mapSpan = 0.5;
        renderMapPage();
      }
    };
    row.append(head);

    const near = (p.candidates || [])[0];
    const off = p.address_mismatch
      || (near && p.same_name_m != null && p.same_name_m > 200);
    if (off) {
      row.append(el('div', 'mv-warn', p.address_mismatch
        ? `⚠ 该坐标高德解析为「${(p.address || '').slice(0, 18)}」，与点位名不符，请确认`
        : `⚠ 与同名点相距 ${p.same_name_m} m，可能偏了`));
      const cands = el('div', 'mv-cands');
      (p.candidates || []).slice(0, 3).forEach((c) => {
        const cb = el('button', 'mv-cand');
        cb.append(el('b', null, c.name));
        cb.append(el('span', 'cap', `${c.address || c.district} · 距当前 ${c.distance_m} m`));
        cb.onclick = async () => {
          toast(`正在把「${p.name}」钉到 ${c.name}`);
          const r = await cmd('pin_point', { task_id: p.task_id, lng: c.lng, lat: c.lat });
          if (r && r.ok) { toast('已钉住坐标并重排'); S.pick = []; }
          else toast((r && r.message) || '钉住失败');
          boot2(); renderMapPage(true); load(true);
        };
        cands.append(cb);
      });
      row.append(cands);
    } else if (p.address) {
      row.append(el('div', 'mv-ok', '✓ 地址与该点位一致'));
    }
    list.append(row);
  });

  list.append(el('div', 'mv-step', `② 确认方式：${MODE_NAME[legsPayload.mode] || '—'}`
    + (realMode ? '（顶栏可切换）' : '（离线：无真实路径）')));

  list.append(el('div', 'mv-step', '③ 确认路径'));
  let total = 0;
  let totalM = 0;
  legList.forEach((leg, li) => {
    const legBox = el('div', 'mv-legbox');
    legBox.append(el('div', 'mv-leg-t', `${li + 1}. ${leg.from} → ${leg.to}`));
    const opts = leg.options || [];
    if (!opts.length) {
      legBox.append(el('div', 'cap', '该段没有取到真实方案，地图上以直线示意'));
      const fb = (paths.legs || [])[li] || {};
      total += fb.minutes || 0;
      totalM += fb.distance_m || 0;
    } else {
      const cur = S.pick[li] == null ? 0 : S.pick[li];
      opts.forEach((o, oi) => {
        const b = el('button', 'mv-opt' + (oi === cur ? ' on' : ''));
        b.append(el('span', 't', o.label));
        b.append(el('span', 'm', `${o.minutes} 分钟 · ${(o.distance_m / 1000).toFixed(1)} 公里`));
        b.onclick = () => { S.pick[li] = oi; renderMapPage(); };
        legBox.append(b);
      });
      const pick = opts[cur] || opts[0];
      total += pick.minutes || 0;
      totalM += pick.distance_m || 0;
    }
    list.append(legBox);
  });

  const foot = $('#mvFoot');
  foot.innerHTML = '';
  foot.append(el('span', 'mv-sum',
    `④ 合计：${total ? total + ' 分钟' : '—'} · ${totalM ? (totalM / 1000).toFixed(1) + ' 公里' : '—'}`
    + `（${MODE_NAME[legsPayload.mode] || ''}，${legList.length} 段）`));
  foot.append(el('span', 'cap', realMode
    ? '时间与里程来自高德路径规划，随所选方案变化'
    : '离线估算：无真实路径与用时'));

  // 实时定位按钮（仅交互地图）
  if (cfg.ready && window.AMap && AMap.Geolocation) {
    const loc = el('button', 'btn sm', '用我的当前位置');
    loc.onclick = () => locateMe();
    foot.append(loc);
  }
}

async function locateMe() {
  if (!S.amap || !window.AMap || !AMap.Geolocation) return toast('当前地图不支持定位');
  const geo = new AMap.Geolocation({ enableHighAccuracy: true, timeout: 8000 });
  S.amap.addControl(geo);
  geo.getCurrentPosition((status, result) => {
    if (status === 'complete' && result && result.position) {
      const p = result.position;
      const mk = new AMap.Marker({ position: [p.getLng(), p.getLat()],
        label: { content: '<div class="amap-mk"><b>我</b><span>当前位置</span></div>' } });
      S.amap.add(mk);
      S.amap.setZoomAndCenter(16, [p.getLng(), p.getLat()]);
      toast(`已定位：${result.formattedAddress || (p.getLng().toFixed(5) + ',' + p.getLat().toFixed(5))}`);
    } else {
      toast('定位失败：' + ((result && result.message) || status));
    }
  });
}

$('#btnMore').onclick = (e) => {
  const m = $('#ctxMenu');
  m.innerHTML = '';
  const add = (label, fn) => {
    const b = el('button', null, label);
    b.onclick = async (ev) => { ev.stopPropagation(); closeMenus(); await fn(); };
    m.append(b);
  };
  add('清空当日安排', async () => {
    const ids = ((S.data || {}).items || []).map((x) => x.task_id);
    if (!ids.length) return toast('当日没有安排');
    // 应用内二次确认（不用原生 confirm，风格统一且可键盘操作）
    const { body } = openPanel('清空当日安排', `${ids.length} 项将被删除`);
    body.append(el('div', 'cap', '删除后可在「更多 → 撤销上次批量删除」里恢复。'));
    const row = el('div', 'ip-row');
    const yes = el('button', 'btn primary sm', '确定清空');
    const no = el('button', 'btn sm', '取消');
    yes.onclick = async () => {
      document.getElementById('infoPanel').remove();
      const r = await cmd('delete_many', { task_ids: ids });
      if (r && r.undo_token) S.undo = r.undo_token;
      toast('已清空，可在「更多」里撤销');
      load(true);
    };
    no.onclick = () => document.getElementById('infoPanel').remove();
    row.append(yes, no);
    body.append(row);
  });
  add('撤销上次批量删除', async () => {
    if (!S.undo) return toast('没有可撤销的批量删除');
    const r = await cmd('restore_many', { undo_token: S.undo });
    if (r && r.ok) S.undo = null;
    load(true);
  });
  add('显示位置示意', async () => { await showMapPanel(); });
  add((S.data && S.data.offline) ? '恢复在线（重新联网）' : '强制离线（只用内置数据）',
    async () => {
      const off = !(S.data && S.data.offline);
      await cmd('toggle_offline', { offline: off });
      toast(off ? '已切到离线：只用内置 POI 与规则解析' : '已恢复在线');
      await boot2();
      load(true);
    });
  add('全部规划记录', async () => { await openPlansIndex(); });
  add('回到起始页', async () => setPage('home'));
  placeMenu(m, e.currentTarget);
};
$('#city').onchange = async (e) => {
  await cmd('set_city', { city: e.target.value });
  S.rev = null;
  await boot2();
  load(true);
};

/* ---------------- AI 抽屉 ---------------- */
function openAI() {
  $('#aiMask').classList.remove('hidden');
  $('#aiDrawer').classList.remove('hidden');
  $('#chatInput').focus();
}
function closeAI() {
  $('#aiMask').classList.add('hidden');
  $('#aiDrawer').classList.add('hidden');
}
$('#btnAI').onclick = openAI;
$('#btnCloseAI').onclick = closeAI;
$('#aiMask').onclick = closeAI;
/* 助手：说完就做。写操作一律先给"选项式确认卡"，点确认才落库。 */
$('#chatForm').addEventListener('submit', async (e) => {
  e.preventDefault();
  const q = $('#chatInput').value.trim();
  if (!q) return;
  $('#chatInput').value = '';
  autoGrow($('#chatInput'));
  const log = $('#chatLog');
  log.append(el('div', 'me', q));
  const w = el('div', 'ai', '思考中…');
  log.append(w);
  log.scrollTop = log.scrollHeight;
  let d = {};
  try {
    d = await cmd('assistant_act', { text: q });
  } catch (err) {
    w.textContent = '出错了：' + (err.message || err);
    return;
  }
  w.textContent = d.message || '(无回复)';
  // ① 录入类：复用批量预览卡（本身就是选项式确认）
  if (d.kind === 'batch') {
    S.preview = { kind: 'batch', text: q, items: d.items, origin: d.origin || '' };
    showBatchPreview(d);
    $('#previewSheet').classList.remove('hidden');
    return;
  }
  // ② 变更类：在对话里给选项式确认卡
  if (d.kind === 'confirm') {
    const eff = d.effect || {};
    const card = el('div', 'ai', '');
    card.append(el('div', null,
      `执行后：末项 ${eff.end_before || '—'} → ${eff.end_after || '—'} 结束`
      + (eff.conflicts ? `，仍有 ${eff.conflicts} 处冲突` : '，无冲突')
      + (eff.unscheduled ? `，${eff.unscheduled} 项排不进` : '')));
    (d.options || []).forEach((op) => {
      const b = el('button', 'btn sm' + (op.primary ? '' : ' ghost'), op.label);
      b.style.margin = '6px 6px 0 0';
      b.onclick = async () => {
        b.disabled = true;
        // 用 act 传动作：action 是命令名，不能被覆盖
        const r = await cmd('assistant_act', { apply: true, act: op.action });
        const done = el('div', 'ai', r.message || '已执行');
        if (r.ok && r.undo_version && r.trip_id) {
          const undo = el('button', 'btn ghost sm', '撤销这次改动');
          undo.style.marginLeft = '8px';
          undo.onclick = async () => {
            undo.disabled = true;
            const back = await cmd('rollback', {
              trip_id: r.trip_id, version_id: r.undo_version });
            // 撤销失败必须说出来，不能假装成功
            toast(back && back.ok ? '已撤销' : ('撤销失败：'
              + ((back && back.message) || '未知原因')));
            await load(true);
          };
          done.append(undo);
        }
        card.append(done);
        card.querySelectorAll('button').forEach((x) => { x.disabled = true; });
        log.scrollTop = log.scrollHeight;
        await load(true);
      };
      card.append(b);
    });
    log.append(card);
  }
  log.scrollTop = log.scrollHeight;
});
document.addEventListener('keydown', (e) => {
  if (e.key === 'Escape') {
    closeMenus(); closeAI(); closePreview();
    const mp = document.getElementById('mapPanel');
    if (mp) mp.remove();
    const ip = document.getElementById('infoPanel');
    if (ip) ip.remove();
  }
  if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === 'k') { e.preventDefault(); openAI(); }
  if ((e.ctrlKey || e.metaKey) && e.key === 'Enter') {
    (S.page === 'home' ? $('#homeForm') : $('#addForm')).requestSubmit();
  }
});

/* ---------------- 多行输入：整段话贴进来，Enter 提交 / Shift+Enter 换行 ----------------
   单行 input 塞不下一整段行程描述，回车即提交也让人不敢写长句，
   所以起始页、悬浮录入条、AI 助手三处的输入框都换成自动增高的 textarea。 */
function autoGrow(elx) {
  if (!elx) return;
  elx.style.height = 'auto';
  elx.style.height = Math.min(elx.scrollHeight, 132) + 'px';
}

function bindMultiline(sel) {
  const elx = $(sel);
  if (!elx) return;
  const form = elx.closest('form');
  elx.addEventListener('input', () => autoGrow(elx));
  elx.addEventListener('keydown', (ev) => {
    // 中文输入法组字期间的回车不算提交
    if (ev.key === 'Enter' && !ev.shiftKey && !ev.isComposing) {
      ev.preventDefault();
      if (form) form.requestSubmit();
    }
  });
  autoGrow(elx);
}
['#homeInput', '#addInput', '#chatInput'].forEach(bindMultiline);

/* ---------------- 分段控件与参数 ---------------- */
function buildSegmented() {
  const box = $('#segTransit');
  box.innerHTML = '';
  const cur = S.options ? S.options.transit_mode : 'driving';
  Object.entries(MODE_NAME).forEach(([k, label]) => {
    const b = el('button', null, label);
    if (k === cur) b.classList.add('on');
    b.onclick = async () => {
      await cmd('set_settings', { transit_mode: k });
      load(true);
    };
    box.append(b);
  });
  $('#wsModeDefault').textContent = MODE_NAME[cur] || '—';
}


/* 排程策略开关：保持顺序 / 每段自动择优 */
function syncStrategySwitches(o) {
  S.keepOrder = !!o.keep_order;
  S.autoMode = !!o.auto_mode;
  const k = $('#swKeepOrder'); if (k) k.classList.toggle('on', S.keepOrder);
  const a = $('#swAutoMode'); if (a) a.classList.toggle('on', S.autoMode);
}
async function toggleStrategy(key) {
  S[key === 'keep_order' ? 'keepOrder' : 'autoMode'] = !S[key === 'keep_order' ? 'keepOrder' : 'autoMode'];
  const v = key === 'keep_order' ? S.keepOrder : S.autoMode;
  syncStrategySwitches({ keep_order: S.keepOrder, auto_mode: S.autoMode });
  await cmd('set_settings', { [key]: v });
  toast((key === 'keep_order' ? '按给定顺序排：' : '每段自动择优：') + (v ? '已开启' : '已关闭'));
  load(true);
}

function syncOptions() {
  const o = S.options;
  if (!o) return;
  const sel = $('#city');
  if (sel.options.length !== (o.cities || []).length) {
    sel.innerHTML = '';
    (o.cities || []).forEach((c) => {
      const op = el('option', null, c); op.value = c; sel.append(op);
    });
  }
  sel.value = o.city;
  $('#wsWindow').textContent = `${o.day_start}-${o.day_end}`;
  $('#rngBuffer').value = o.buffer_min;
  $('#valBuffer').textContent = `${o.buffer_min} 分`;
  $('#swOffline').classList.toggle('on', !!o.offline_mode);
  syncStrategySwitches(o);
  buildSegmented();
}

/* ---------------- 加载与轮询 ---------------- */
/* 切日期时顺手预热前后各一天：用户按 ←/→ 连着翻时不至于每天现算 */
function prewarmNeighbors() {
  try {
    const base = new Date(S.day + 'T00:00:00');
    [-1, 1].forEach((delta) => {
      const d = new Date(base.getTime() + delta * 86400000);
      const iso = `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')}`;
      fetch(`/api/state?date=${iso}`).catch(() => {});
    });
  } catch (e) { /* 预热失败不影响主流程 */ }
}

async function load(force = false) {
  try {
    const d = await api('/api/state', S.rev != null && !force ? { rev: S.rev } : {});
    // 大模型"配了但调不通"不改变数据版本号，但必须让界面跟着变——
    // 否则用户会一直看到正常的界面，而解析其实已经退回规则了。
    const llmChanged = S.llmOk !== d.llm_ok;
    hideBanner();
    if (force || d.changed || S.rev === null || llmChanged) {
      S.rev = d.rev;
      render(d);
    }
  } catch (e) {
    // 首次加载就断线时，用户看到的是空界面——必须给出可操作的提示，而不是只改一个角标
    $('#modeText').textContent = '连接断开';
    showBanner('连不上本地服务：请确认 python tools/dev.py 还在运行，然后点「重试」');
  }
}

/* ---------------- 我的偏好：用户档案切换与存档 ---------------- */
const EFFORTS = ['轻松', '标准', '能走能骑'];
const PACES = ['慢', '标准', '快'];
const BUDGETS = ['省', '中', '不设'];
const IDENTITIES = ['', '学生', '上班族', '带长辈', '带小孩', '情侣'];
const MODE_PRIORITIES = ['最快', '公共交通优先', '骑行优先', '步行优先', '驾车优先'];

async function loadProfiles() {
  try {
    const d = await api('/api/profiles');
    S.profiles = d.profiles || [];
    S.profileActive = d.active || '';
    const sel = $('#pfSelect');
    if (sel) {
      sel.innerHTML = '';
      S.profiles.forEach((p) => {
        const o = document.createElement('option');
        o.value = p.name;
        o.textContent = p.name;
        if (p.name === S.profileActive) o.selected = true;
        sel.append(o);
      });
    }
    const cur = S.profiles.find((p) => p.name === S.profileActive) || {};
    const sum = $('#pfSummary');
    if (sum) {
      sum.textContent = (cur.describe || '—')
        + (cur.name ? `　（步行 ≤${cur.max_walk_min ?? '—'} 分 · 骑行 ≤${cur.max_bike_min || '不推荐'} 分）` : '');
    }
  } catch (e) { /* 档案不可用不影响主流程 */ }
}

async function switchProfile(name) {
  const r = await cmd('set_profile', { name });
  toast(r && r.message ? r.message : '已切换');
  await loadProfiles();
  await boot2();            // 缓冲/可排起点会变，左栏要跟着刷新
  await load(true);
}

function openProfileEditor() {
  const cur = (S.profiles || []).find((p) => p.name === S.profileActive) || {};
  const { panel, body } = openPanel('我的偏好', '选完保存即可，只影响默认值与阈值');
  const form = el('div', 'ip-form');
  const field = (label, node) => {
    const w = el('label');
    w.append(document.createTextNode(label), node);
    return w;
  };
  const sel = (opts, val) => {
    const s = el('select');
    opts.forEach((o) => {
      const op = document.createElement('option');
      op.value = o;
      op.textContent = o || '（不限）';
      if (o === val) op.selected = true;
      s.append(op);
    });
    return s;
  };
  const nameIn = el('input');
  nameIn.value = cur.name || '默认';
  nameIn.placeholder = '档案名（如：学生-小李）';
  const idSel = sel(IDENTITIES, cur.identity || '');
  const efSel = sel(EFFORTS, cur.effort || '标准');
  const paSel = sel(PACES, cur.pace || '标准');
  const buSel = sel(BUDGETS, cur.budget || '不设');
  const wakeIn = el('input');
  wakeIn.type = 'time';
  wakeIn.value = cur.wake || '';

  const carSel = sel(['有车', '无车'], cur.has_car === false ? '无车' : '有车');
  const priSel = sel(MODE_PRIORITIES, cur.mode_priority || '最快');
  form.append(field('档案名', nameIn), field('身份', idSel), field('体力', efSel),
              field('节奏', paSel), field('预算', buSel), field('起床时间', wakeIn),
              field('有没有车', carSel), field('出行方式优先', priSel));
  body.append(form);

  const acts = el('div', 'ip-actions');
  const del = el('button', 'btn ghost', '删除此档案');
  del.onclick = async () => {
    if (!cur.name || cur.name === '默认') return toast('默认档案不能删除');
    const r = await cmd('delete_profile', { name: cur.name });
    toast(r && r.message ? r.message : '已删除');
    panel.remove();
    await loadProfiles();
    await boot2();
    await load(true);
  };
  const save = el('button', 'btn', '保存为我的偏好');
  save.onclick = async () => {
    const r = await cmd('save_profile', {
      name: nameIn.value.trim() || '默认',
      identity: idSel.value,
      effort: efSel.value,
      pace: paSel.value,
      budget: buSel.value,
      wake: wakeIn.value || '',
      has_car: carSel.value === '有车',
      mode_priority: priSel.value,
    });
    toast(r && r.message ? r.message : '已保存');
    panel.remove();
    await loadProfiles();
    await boot2();
    await load(true);
  };
  acts.append(del, save);
  body.append(acts);
}

function bindProfile() {
  const sel = $('#pfSelect');
  if (sel && !sel.dataset.bound) {
    sel.dataset.bound = '1';
    sel.onchange = () => switchProfile(sel.value);
  }
  const edit = $('#pfEdit');
  if (edit && !edit.dataset.bound) {
    edit.dataset.bound = '1';
    edit.onclick = openProfileEditor;
  }
}

async function boot2() {
  S.options = await api('/api/options');
  syncOptions();
}

async function boot() {
  const qDay = new URLSearchParams(location.search).get('date');
  if (qDay && /^\d{4}-\d{2}-\d{2}$/.test(qDay)) S.day = qDay;
  await boot2();
  await loadProfiles();
  bindProfile();
  bindLeft();
  await load(true);
  const veil = document.getElementById('boot');
  if (veil) veil.remove();        // 数据到手才掀开载入态
  setPage(S.data && S.data.stats.count ? 'work' : 'home', false);
  setInterval(() => load(false), 3000);
}

boot();

/* ---------------- 设置抽屉（右上角齿轮）----------------
   左栏原来并排摆了「我的偏好 / 时空基准锚点 / 出行参数 / 容错机制」四块，
   信息密度太高。这里把这四块**整块搬进抽屉**（不是重画一遍）：
   控件本身、id、事件绑定全部原样保留，所以既不会出现"抽屉里的值和左栏不一致"，
   也不会因为搬动而丢失已有的 render 逻辑。左栏只留日历 + 当日概览。 */
const MODE_CN = { driving: '驾车', transit: '公交/地铁', walking: '步行', bicycling: '骑行' };
const SD_SLOTS = [
  ['我的偏好', '我的偏好'],
  ['时空基准锚点', '锚点'],
  ['出行参数', '出行'],
  ['容错机制', '容错'],
];

function sdSummaryText(s) {
  const mode = MODE_CN[s.transit_mode] || (S.data && S.data.mode_short) || '默认';
  const span = `${s.day_start || '09:00'}–${s.day_end || '22:00'}`;
  return `当前：${mode} · ${span} · 缓冲 ${s.buffer_min != null ? s.buffer_min : 10} 分`;
}

/* 按标题找一个左栏区块（用标题而不是行号，改版不会失效） */
function sdFindBlock(title) {
  return [...document.querySelectorAll('.block')].find((b) => {
    const hd = b.querySelector('.hd');
    return hd && hd.textContent.replace(/\s+/g, '').startsWith(title);
  });
}

async function openSettingsDrawer() {
  const d = $('#setDrawer');
  if (!d) return;
  d.classList.remove('hidden');
  document.querySelectorAll('#setDrawer .sd-block').forEach((b) => { b.style.display = ''; });   // 让被搬进来的块显示
  try {
    const raw = await api('/api/options');
    const s = (raw && raw.settings) ? raw.settings : (raw || {});
    const sum = $('#sdSummary');
    if (sum) sum.textContent = sdSummaryText(s);
  } catch (e) { /* 拿不到就保留上一次的摘要 */ }
}

function mountSettingsDrawer() {
  const anchor = $('#btnCalendar');
  if (!anchor || $('#btnSettings')) return;
  const btn = el('button', 'btn sm', '⚙');
  btn.id = 'btnSettings';
  btn.title = '设置（偏好 / 锚点 / 出行参数 / 容错）';
  btn.setAttribute('aria-label', '设置');
  btn.onclick = (e) => { e.stopPropagation(); openSettingsDrawer(); };
  anchor.parentElement.insertBefore(btn, anchor.nextSibling);

  const d = document.createElement('div');
  d.id = 'setDrawer';
  d.className = 'drawer hidden';
  d.innerHTML = '<div class="sd-hd"><b>设置</b>'
    + '<button class="sd-x" type="button" aria-label="关闭">×</button></div>'
    + '<div class="sd-sum" id="sdSummary">当前：—</div>'
    + '<div class="sd-body" id="sdBody"></div>';
  document.body.append(d);
  d.querySelector('.sd-x').onclick = () => d.classList.add('hidden');
  document.addEventListener('click', (e) => {
    if (!d.classList.contains('hidden') && !d.contains(e.target)
        && e.target.id !== 'btnSettings') {
      d.classList.add('hidden');
    }
  });

  // 把左栏那四块**整块搬进抽屉**：保留 id 与事件绑定，render 逻辑照旧写到这些节点上
  const body = d.querySelector('#sdBody');
  SD_SLOTS.forEach(([title]) => {
    const block = sdFindBlock(title);
    if (!block) return;
    block.classList.add('sd-block');
    block.removeAttribute('style');
    body.append(block);
  });
}

mountSettingsDrawer();

// ---------------- 生成进度条（B'：主请求不动，另发只读请求轮询） ----------------
// 只有慢到一定程度才显示：离线生成 <400ms，弹个条闪一下反而难看。
let _pgTimer = null;
let _pgShownAt = 0;

function progressBarShow(text, pct) {
  let box = document.getElementById('genProgress');
  if (!box) {
    box = document.createElement('div');
    box.id = 'genProgress';
    box.className = 'genprog';
    box.innerHTML = '<div class="gp-txt"></div><div class="gp-bar"><i></i></div>';
    document.body.append(box);
  }
  box.querySelector('.gp-txt').textContent = text || '正在生成…';
  const bar = box.querySelector('.gp-bar > i');
  bar.style.width = Math.max(4, Math.min(99, pct || 8)) + '%';
  box.classList.remove('hidden');
}

function progressBarHide() {
  const box = document.getElementById('genProgress');
  if (box) box.classList.add('hidden');
}

// 包一层：慢请求期间轮询 /api/progress；<400ms 的快请求整个过程都不显示
async function withProgress(session, work) {
  let shown = false;
  _pgShownAt = Date.now();
  _pgTimer = setInterval(async () => {
    if (!shown) {
      if (Date.now() - _pgShownAt < 400) return;   // 快路径：不弹
      shown = true;
      progressBarShow('正在生成…', 8);
    }
    try {
      const p = await fetch(`/api/progress?session=${encodeURIComponent(session || '')}`).then((r) => r.json());
      if (p && p.stage) progressBarShow(p.stage, p.pct);
    } catch (e) { /* 轮询失败不影响主请求 */ }
  }, 300);
  try {
    return await work();
  } finally {
    clearInterval(_pgTimer);
    _pgTimer = null;
    progressBarHide();
  }
}

// ---------------- 导出入口（精简版方案，一次最多 7 天） ----------------
// 后端 export_plan 返回 markdown + 文件名；这里只负责下载，不重写渲染（同一份数据源）。
function exportDownload(markdown, filename) {
  const blob = new Blob([markdown], { type: 'text/markdown;charset=utf-8' });
  const url = URL.createObjectURL(blob);
  const a = document.createElement('a');
  a.href = url;
  a.download = filename || '行程方案.md';
  document.body.append(a);
  a.click();
  a.remove();
  setTimeout(() => URL.revokeObjectURL(url), 2000);
}

async function doExport(startIso, endIso) {
  try {
    const r = await cmd('export_plan', { start: startIso, end: endIso }, 30000);
    if (!r || !r.ok) { toast((r && r.message) || '导出失败'); return; }
    exportDownload(r.markdown, r.filename);
    toast(r.message || '已导出');
  } catch (e) {
    toast('导出失败：' + ((e && e.message) || e));
  }
}

/* 顶栏「导出」：点开一个小菜单，选"这一天"还是"整趟（最多 7 天）" */
function openExportMenu(anchor) {
  const m = $('#ctxMenu');
  m.innerHTML = '';
  m.className = 'menu';
  m.append(el('div', 'cp-hd', '导出精简版方案'));

  const dayBtn = el('button', 'mi', `这一天（${S.day}）`);
  dayBtn.onclick = () => { m.classList.add('hidden'); doExport(S.day, S.day); };
  m.append(dayBtn);

  // 整趟：用当前行程的首尾日期；超过 7 天就只导最近这一段，并在菜单里说明
  const days = (S.monthDays && S.monthDays[S.day.slice(0, 7)])
    ? Object.keys(S.monthDays).sort() : [];
  const sameMonth = (S.monthDays && S.monthDays[S.day.slice(0, 7)]) || {};
  const keys = Object.keys(sameMonth).filter((k) => k >= S.day.slice(0, 7) + '-01').sort();
  if (keys.length > 1) {
    const first = keys[0];
    const last = keys[keys.length - 1];
    const span = (new Date(last) - new Date(first)) / 86400000 + 1;
    const end = span > 7 ? isoAddDays(first, 6) : last;
    const lbl = el('button', 'mi', span > 7
      ? `整趟前 7 天（${first} ~ ${end}）` : `整趟（${first} ~ ${end}）`);
    lbl.onclick = () => { m.classList.add('hidden'); doExport(first, end); };
    m.append(lbl);
  }
  m.append(el('div', 'cp-hd', '导出内容：时刻 / 地点 / 停留 / 当天费用'));
  placeMenu(m, anchor);
}

function isoAddDays(iso, n) {
  const d = new Date(iso + 'T00:00:00');
  d.setDate(d.getDate() + n);
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')}`;
}

(function mountExportButton() {
  const anchor = $('#btnSettings') || $('#btnCalendar');
  if (!anchor || $('#btnExport')) return;
  const btn = el('button', 'btn sm', '导出');
  btn.id = 'btnExport';
  btn.title = '导出精简版方案（最多 7 天）';
  btn.onclick = (e) => { e.stopPropagation(); openExportMenu(e.currentTarget); };
  anchor.parentElement.insertBefore(btn, anchor);
})();
