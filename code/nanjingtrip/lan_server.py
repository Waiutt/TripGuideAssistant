# -*- coding: utf-8 -*-
"""局域网服务：让手机用浏览器访问同一份本地数据（数据不出局域网）。

设计要点
--------
1. **数据主人只有桌面机**：SQLite 仍在本机，服务只是同一进程内的 HTTP 门面，
   不引入云端、不上传任何数据；
2. **多端同步靠 revision**：任何写操作让 `Database.bump_revision()` +1，
   手机与桌面各自带 rev 轮询，发现变了就刷新；
3. **无第三方依赖**：标准库 `http.server` + `ThreadingHTTPServer`，
   数据库连接已允许跨线程并由可重入锁串行化写入。
"""
from __future__ import annotations

import json
import logging
import re
import socket
import threading
from pathlib import Path
from datetime import date
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Optional
from urllib.parse import parse_qs, urlparse

from . import web_api
from .cities import get_city
from .models import TaskStatus, TransitMode

WEB_DIR = Path(__file__).resolve().parent / 'web'

log = logging.getLogger('nanjingtrip.lan')

DEFAULT_PORT = 8765


def lan_ip() -> str:
    """取本机在局域网里的地址（不发起真实连接）"""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(('8.8.8.8', 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except OSError:
        try:
            return socket.gethostbyname(socket.gethostname())
        except OSError:
            return '127.0.0.1'


def _state_payload(ctx, day: date, since_rev: Optional[int]) -> dict:
    """构造给客户端的状态（只含叶子字段，不回传内部对象）"""
    rev = ctx.db.revision()
    trip = ctx.trip_svc.day_view(day)
    result = ctx.trip_svc.compute_day(day)
    stats = ctx.trip_svc.day_stats(trip)

    items = []
    for it in result.ordered():
        leg = it.leg_before
        items.append({
            'task_id': it.task.task_id,
            'title': it.task.title,
            'category': it.task.category,
            'time': it.start_ts.strftime('%H:%M'),
            'end': it.end_ts.strftime('%H:%M'),
            'minutes': it.task.duration_min,
            'poi': it.task.poi.name if it.task.poi else '',
            'district': it.task.poi.district if it.task.poi else '',
            'locked': bool(it.task.is_locked),
            'done': it.task.status == TaskStatus.DONE,
            'mode': (it.task.mode.value if it.task.mode else ''),
            'leg': ({
                'mode': leg.mode.value,
                'mode_cn': leg.mode.cn,
                'minutes': leg.duration_min,
            } if leg else None),
        })
    for t in result.unscheduled:
        items.append({
            'task_id': t.task_id, 'title': t.title, 'time': '', 'end': '',
            'minutes': t.duration_min, 'poi': t.poi.name if t.poi else '',
            'locked': bool(t.is_locked), 'done': False,
            'mode': (t.mode.value if t.mode else ''), 'leg': None,
            'unscheduled': True,
        })
    return {
        'rev': rev,
        'changed': since_rev is None or int(since_rev) != rev,
        'date': day.isoformat(),
        'city': ctx.city,
        'city_label': get_city(ctx.city).title_label,
        'lodging': trip.lodging.name if trip.lodging else '',
        'mode': ctx.degrade.mode_label(),
        'offline': ctx.degrade.is_offline(),
        'explanation': result.explanation,
        'conflicts': [c.detail for c in result.conflicts],
        # 营业时间/闭馆/预约/过夜住宿：手机端只读展示，动作仍在桌面端完成
        'notices': [c.detail for c in result.notices],
        'stats': {'count': stats['count'], 'busy': stats['busy'],
                  'transit': stats['transit'], 'idle': stats['idle']},
        'items': items,
    }


def _apply_command(ctx, body: dict) -> dict:
    """执行手机端发来的写操作，返回 {ok, message, rev}"""
    action = (body.get('action') or '').strip()
    day_raw = body.get('date') or date.today().isoformat()
    try:
        day = date.fromisoformat(day_raw)
    except ValueError:
        return {'ok': False, 'message': '日期格式不对'}

    if action == 'add':
        text = (body.get('text') or '').strip()
        if not text:
            return {'ok': False, 'message': '请先输入一句话'}
        prev = ctx.trip_svc.preview_text(text, day)
        if not prev.ok or prev.draft is None:
            return {'ok': False,
                    'message': prev.error or ('缺少：' + '、'.join(prev.missing))}
        out = ctx.trip_svc.commit_task(day, prev.draft,
                                       note=f'手机端新增：{prev.draft.title}')
        msg = out.explanation or out.error or '已添加'
        return {'ok': out.result is not None, 'message': msg,
                'rev': ctx.db.revision()}

    if action == 'delete':
        tid = body.get('task_id')
        if not tid:
            return {'ok': False, 'message': '缺少任务'}
        out, snaps = ctx.trip_svc.delete_many([int(tid)])
        return {'ok': bool(snaps), 'message': '已删除',
                'rev': ctx.db.revision()}

    if action == 'done':
        tid = body.get('task_id')
        if not tid:
            return {'ok': False, 'message': '缺少任务'}
        out = ctx.trip_svc.mark_done(int(tid))
        return {'ok': out.result is not None, 'message': '已标记完成',
                'rev': ctx.db.revision()}

    if action == 'set_mode':
        tid, mode = body.get('task_id'), body.get('mode') or ''
        if not tid:
            return {'ok': False, 'message': '缺少任务'}
        valid = {m.value for m in TransitMode}
        if mode and mode not in valid:
            return {'ok': False, 'message': '交通方式不合法'}
        out = ctx.trip_svc.adjust(int(tid), {'mode': mode or None})
        return {'ok': out.result is not None, 'message': '已切换方式',
                'rev': ctx.db.revision()}

    # 改开始时间：手机端最常用的"改这一项"，与桌面端"改开始时间"等价（指定时刻即锁定）
    if action == 'move':
        tid, start = body.get('task_id'), (body.get('start') or '').strip()
        if not tid:
            return {'ok': False, 'message': '缺少任务'}
        if not re.match(r'^\d{1,2}:\d{2}$', start):
            return {'ok': False, 'message': '时间格式应为 HH:MM'}
        hh, mm = (int(x) for x in start.split(':'))
        if not (0 <= hh <= 23 and 0 <= mm <= 59):
            return {'ok': False, 'message': '时间超出范围'}
        stamp = f'{hh:02d}:{mm:02d}'
        out = ctx.trip_svc.adjust(int(tid), {'start_ts': stamp})
        if out.result is None:
            return {'ok': False, 'message': out.error or '调整失败',
                    'rev': ctx.db.revision()}
        return {'ok': True, 'message': f'已改到 {stamp}',
                'rev': ctx.db.revision()}

    if action == 'replan':
        out = ctx.trip_svc.replan(day)
        return {'ok': out.result is not None,
                'message': out.explanation or out.error or '已重新规划',
                'rev': ctx.db.revision()}

    return {'ok': False, 'message': f'未知操作：{action}'}


MOBILE_HTML = """<!DOCTYPE html>
<html lang="zh-CN"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,maximum-scale=1">
<title>NanjingTrip · 手机端</title>
<link rel="icon" href="data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 32 32'%3E%3Crect width='32' height='32' rx='7' fill='%232E75B6'/%3E%3Ctext x='16' y='23' font-size='19' text-anchor='middle' fill='%23fff'%3E%E8%A1%8C%3C/text%3E%3C/svg%3E">
<style>
:root{--blue:__ACCENT__;--gray:#5F6368;--line:#E3E8EF;--bg:#F5F7FA;}
*{box-sizing:border-box;-webkit-tap-highlight-color:transparent}
body{margin:0;background:var(--bg);font:15px/1.5 "Microsoft YaHei","PingFang SC",sans-serif;color:#202124}
header{position:sticky;top:0;background:#fff;border-bottom:1px solid var(--line);padding:12px 14px;z-index:5}
h1{margin:0;font-size:17px;display:flex;align-items:center;gap:8px}
.badge{font-size:11px;padding:1px 8px;border-radius:8px;background:__ACCENT_TINT__;color:var(--blue)}
.off{background:#FFF6E5;color:#BF7016}
.sub{margin-top:4px;font-size:12px;color:var(--gray)}
.nav{display:flex;gap:8px;align-items:center;margin-top:8px}
.nav button{padding:5px 10px;font-size:13px}
.nav #dayLabel{font-weight:700;font-size:13px;color:#202124;min-width:96px;text-align:center}
main{padding:12px 14px 92px}
.card{background:#fff;border:1px solid var(--line);border-left:4px solid var(--blue);border-radius:10px;padding:10px 12px;margin-bottom:10px}
.row{display:flex;align-items:baseline;gap:8px;flex-wrap:wrap}
.t{font-weight:700;font-size:15px}
.time{font-variant-numeric:tabular-nums;color:var(--blue);font-weight:700;min-width:44px}
.meta{font-size:12px;color:var(--gray);margin-top:3px}
.leg{font-size:12px;color:#4A6E9E;background:#F2F7FF;border-radius:6px;padding:2px 8px;display:inline-block;margin:6px 0 0}
.acts{margin-top:8px;display:flex;gap:8px;flex-wrap:wrap;align-items:center}
/* 时间控件直接替换时间文字：点时间就是改这一项，符合手机端"改这一项"的定位 */
input.tm{font:inherit;font-size:15px;font-weight:700;font-variant-numeric:tabular-nums;
         color:var(--blue);background:transparent;border:none;padding:0;width:76px}
input.tm::-webkit-calendar-picker-indicator{opacity:.45;cursor:pointer}
.notice{background:#FFF7E6;border:1px solid #F0D9A8;color:#7A5200;border-radius:10px;padding:8px 10px;margin-bottom:10px;font-size:12.5px;line-height:1.55}
button{font:inherit;border-radius:8px;padding:6px 12px;border:1px solid var(--line);background:#fff;color:#202124}
button.p{background:var(--blue);border-color:var(--blue);color:#fff;font-weight:600}
button.d{border-color:#E7B3AE;color:#C0392B}
.tag{font-size:11px;padding:1px 7px;border-radius:8px;background:#E8F5EE;color:#1E7B4B}
.tag.lock{background:#FDEDEC;color:#C0392B}
.tag.done{background:#EEE;color:#777}
.empty{color:var(--gray);text-align:center;padding:28px 0}
form.add{position:fixed;left:0;right:0;bottom:0;background:#fff;border-top:1px solid var(--line);padding:10px 12px calc(10px + env(safe-area-inset-bottom));display:flex;gap:8px}
form.add input{flex:1;font:inherit;padding:10px 12px;border:1px solid var(--line);border-radius:10px}
#msg{position:fixed;left:12px;right:12px;bottom:78px;background:#202124;color:#fff;padding:8px 12px;border-radius:8px;font-size:13px;opacity:0;transition:.25s;pointer-events:none}
#msg.on{opacity:.95}
</style></head><body>
<header>
  <h1>行程 <span class="badge" id="city">__CITY_LABEL__</span> <span class="badge" id="mode">连接中</span></h1>
  <div class="sub" id="sub">正在同步…</div>
  <div class="nav">
    <button onclick="shiftDay(-1)">‹ 前一天</button>
    <span id="dayLabel"></span>
    <button onclick="shiftDay(1)">后一天 ›</button>
    <button onclick="goToday()">今天</button>
  </div>
</header>
<main id="list"><div class="empty">加载中…</div></main>
<form class="add" id="form">
  <input id="text" placeholder="一句话：明天九点去鼓楼医院取报告，半小时">
  <button class="p" type="submit">添加</button>
</form>
<div id="msg"></div>
<script>
const TODAY = "__DATE__";
let rev = null, day = TODAY;
const $ = s => document.querySelector(s);
function toast(t){const m=$('#msg');m.textContent=t;m.classList.add('on');clearTimeout(m._t);m._t=setTimeout(()=>m.classList.remove('on'),2200);}
function esc(s){return (s||'').replace(/[&<>]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;'}[c]));}
function shiftDay(n){
  const dt = new Date(day + 'T00:00:00');
  dt.setDate(dt.getDate() + n);
  day = dt.toLocaleDateString('sv-SE');   // YYYY-MM-DD
  rev = null; sync(true);
}
function goToday(){ day = TODAY; rev = null; sync(true); }
function render(d){
  $('#dayLabel').textContent = d.date;
  $('#city').textContent = d.city_label || d.city || '';
  $('#mode').textContent = d.offline ? '离线' : '在线';
  $('#mode').className = 'badge' + (d.offline ? ' off' : '');
  $('#sub').textContent = d.date + ' · ' + d.stats.count + ' 项 · 通行 ' + d.stats.transit + ' 分钟'
     + (d.lodging ? ' · 住 ' + d.lodging : '')
     + (d.conflicts.length ? ' · ⚠ ' + d.conflicts.length + ' 项冲突' : '');
  const box = $('#list');
  const notices = (d.notices || []);
  const noticeHtml = notices.length
    ? '<div class="notice">' + notices.map(n => '· ' + esc(n)).join('<br>') + '</div>' : '';
  if(!d.items.length){
    box.innerHTML = noticeHtml + '<div class="empty">今天还没有安排<br>用下面输入框说一句就行</div>';
    return;
  }
  box.innerHTML = noticeHtml + d.items.map(it => `
    <div class="card" style="${it.conflict?'border-left-color:#C0392B':''}">
      <div class="row">
        ${it.unscheduled ? '<span class="time">--:--</span>'
          : `<input class="time tm" type="time" value="${it.time||''}"
                    onchange="cmd('move',${it.task_id},null,{start:this.value})">`}
        <span class="t">${esc(it.title)}</span>
        ${it.locked?'<span class="tag lock">已锁定</span>':''}
        ${it.done?'<span class="tag done">已完成</span>':''}
        ${it.unscheduled?'<span class="tag lock">未排入</span>':''}
      </div>
      <div class="meta">${esc(it.poi||'无地点')}${it.district?'（'+esc(it.district)+'）':''} · ${it.minutes} 分钟${it.end?' · 至 '+it.end:''}</div>
      ${it.leg?`<div class="leg">${esc(it.leg.mode_cn)} ${it.leg.minutes} 分钟前往</div>`:''}
      <div class="acts">
        <button onclick="cmd('done',${it.task_id})">完成</button>
        <button onclick="cmd('set_mode',${it.task_id},'transit')">改公交</button>
        <button onclick="cmd('set_mode',${it.task_id},'bicycling')">改骑行</button>
        <button class="d" onclick="cmd('delete',${it.task_id})">删除</button>
      </div>
    </div>`).join('');
}
async function sync(force){
  try{
    const r = await fetch(`/api/state?date=${day}${rev!==null&&!force?'&rev='+rev:''}`);
    const d = await r.json();
    if(force || d.changed || rev === null){ rev = d.rev; render(d); }
  }catch(e){ $('#mode').textContent='连接断开'; $('#mode').className='badge off'; }
}
async function cmd(action, task_id, mode, extra){
  const body = Object.assign({action, date: day, task_id, mode}, extra || {});
  const r = await fetch('/api/command', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify(body)});
  const d = await r.json();
  toast(d.message || (d.ok ? '完成' : '失败'));
  sync(true);
}
$('#form').addEventListener('submit', async e => {
  e.preventDefault();
  const el = $('#text'); const t = el.value.trim(); if(!t) return;
  el.value=''; toast('正在解析…');
  const r = await fetch('/api/command', {method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify({action:'add', date: day, text: t})});
  const d = await r.json();
  toast(d.message || (d.ok?'已添加':'失败'));
  sync(true);
});
sync(true);
setInterval(()=>sync(false), 3000);   // 3 秒轮询：桌面端改了这里自动刷新
</script></body></html>
"""


class _Handler(BaseHTTPRequestHandler):
    ctx = None            # 由 LanServer 注入
    server_version = 'NanjingTripLAN/1.0'

    # ---------- 基础响应 ----------
    def _send(self, code: int, body: bytes, ctype: str,
              cache: str = 'no-store') -> None:
        self.send_response(code)
        self.send_header('Content-Type', ctype)
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', cache)
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _json(self, obj, code: int = 200) -> None:
        self._send(code, json.dumps(obj, ensure_ascii=False).encode('utf-8'),
                   'application/json; charset=utf-8')

    def log_message(self, fmt, *args):      # 安静一点，避免刷屏
        log.debug('%s - %s', self.address_string(), fmt % args)

    # ---------- 路由 ----------
    def do_GET(self):                       # noqa: N802
        parsed = urlparse(self.path)
        path = parsed.path
        q = parse_qs(parsed.query)

        # 静态资源：SPA（nanjingrip/web/ 下的 index.html / app.css / app.js）
        if path in ('/', '/index.html', '/app.css', '/app.js'):
            self._serve_static(path)
            return

        def one(key, default=None):
            return (q.get(key) or [default])[0]

        try:
            day = date.fromisoformat(one('date', date.today().isoformat()))
        except ValueError:
            self._json({'ok': False, 'message': '日期格式不对'}, 400)
            return

        try:
            if path == '/api/state':
                self._json(web_api.state_payload(self.ctx, day, one('rev'),
                                            with_modes=(one('modes') == '1')))
                return
            if path == '/api/progress':
                # 只读：生成期间前端轮询它显示真实阶段（不阻塞主请求）
                from .consult import get_progress
                got = get_progress(one('session', ''))
                self._json({'ok': True, 'rev': self.ctx.db.revision(),
                            'stage': got.get('stage', ''), 'pct': got.get('pct', 0)})
                return
            if path == '/api/health':
                self._json(web_api.health_payload(self.ctx))
                return
            if path == '/api/options':
                self._json(web_api.options_payload(self.ctx, day))
                return
            if path == '/api/versions':
                self._json(web_api.versions_payload(self.ctx, day))
                return
            if path == '/api/month':
                self._json(web_api.month_payload(self.ctx, int(one('year', day.year)),
                                                 int(one('month', day.month))))
                return
            if path == '/api/lodging':
                self._json(web_api.lodging_payload(self.ctx, day))
                return
            if path == '/api/profiles':
                self._json(web_api.profiles_payload(self.ctx))
                return
            if path == '/api/alternatives':
                self._json({'ok': True,
                            'alternatives': self.ctx.trip_svc.alternatives(day)})
                return
            if path == '/api/leg':
                self._json(web_api.leg_payload(
                    self.ctx, int(one('task_id', 0)), one('mode', ''),
                    one('access', 'bicycling'), one('egress', 'walking')))
                return
            if path == '/api/plans':
                self._json(web_api.plans_payload(self.ctx, int(one('task_id', 0))))
                return
            if path == '/api/advice':
                self._json(web_api.advice_payload(self.ctx, int(one('task_id', 0))))
                return
            if path == '/api/modes':
                self._json(web_api.modes_payload(self.ctx, one('from', ''),
                                                 one('to', '')))
                return
            if path == '/api/map':
                data, note = web_api.map_payload(
                    self.ctx, day, one('w'), one('h'), one('zoom'),
                    one('center'), one('big') in ('1', 'true'), one('scale', 1))
                if data is None:
                    self._json({'ok': False, 'message': note}, 503)
                else:
                    # 底图允许短缓存：切换通行方式只重画路径，不该重复取图（高德有限流）
                    self._send(200, data, 'image/png', cache='max-age=120')
                return
            if path == '/api/mapinfo':
                self._json(web_api.mapinfo_payload(
                    self.ctx, day, one('w'), one('h'), one('zoom'),
                    one('center'), one('big') in ('1', 'true')))
                return
            if path == '/api/mappath':
                self._json(web_api.mappath_payload(self.ctx, day, one('mode', '')))
                return
            if path == '/api/legoptions':
                self._json(web_api.legoptions_payload(
                    self.ctx, day, one('mode', ''), one('limit', 3)))
                return
            if path == '/api/plansindex':
                self._json(web_api.plans_index_payload(self.ctx))
                return
            if path == '/api/pointinfo':
                self._json(web_api.pointinfo_payload(self.ctx, day))
                return
            if path == '/api/mapconfig':
                self._json(web_api.mapconfig_payload(self.ctx))
                return
            if path == '/api/regeo':
                self._json(web_api.regeo_payload(self.ctx, one('lng'), one('lat')))
                return
        except Exception as exc:        # noqa: BLE001
            log.exception('GET %s failed', path)
            self._json({'ok': False, 'message': f'读取失败：{exc}'}, 500)
            return

        self._json({'ok': False, 'message': 'not found'}, 404)

    def _serve_static(self, path: str) -> None:
        """从 nanjingrip/web/ 读静态文件；找不到就回退到内置单页。"""
        name = 'index.html' if path in ('/', '/index.html') else path.lstrip('/')
        f = WEB_DIR / name
        if not f.exists():
            pack = get_city(self.ctx.city)
            theme = pack.theme or {}
            html = (MOBILE_HTML
                    .replace('__DATE__', date.today().isoformat())
                    .replace('__ACCENT__', theme.get('accent', '#2E75B6'))
                    .replace('__ACCENT_TINT__', theme.get('accent.tint', '#EAF1FB'))
                    .replace('__CITY_LABEL__', pack.title_label))
            self._send(200, html.encode('utf-8'), 'text/html; charset=utf-8')
            return
        ctype = {'.html': 'text/html; charset=utf-8',
                 '.css': 'text/css; charset=utf-8',
                 '.js': 'application/javascript; charset=utf-8',
                 '.svg': 'image/svg+xml'}.get(f.suffix, 'application/octet-stream')
        self._send(200, f.read_bytes(), ctype)

    def do_POST(self):                      # noqa: N802
        parsed = urlparse(self.path)
        if parsed.path != '/api/command':
            self._json({'ok': False, 'message': 'not found'}, 404)
            return
        length = int(self.headers.get('Content-Length') or 0)
        raw = self.rfile.read(length) if length else b'{}'
        try:
            body = json.loads(raw.decode('utf-8') or '{}')
        except json.JSONDecodeError:
            self._json({'ok': False, 'message': '请求体不是合法 JSON'}, 400)
            return
        try:
            self._json(web_api.command_payload(self.ctx, body))
        except Exception as exc:            # noqa: BLE001
            log.exception('command failed')
            self._json({'ok': False, 'message': f'执行失败：{exc}'}, 500)


class LanServer:
    """局域网服务封装：start()/stop()/url"""

    def __init__(self, ctx, port: int = DEFAULT_PORT, host: str = '0.0.0.0'):
        self.ctx = ctx
        self.port = port
        self.host = host
        self._httpd: Optional[ThreadingHTTPServer] = None
        self._thread: Optional[threading.Thread] = None

    def start(self) -> str:
        handler = type('BoundHandler', (_Handler,), {'ctx': self.ctx})
        self._httpd = ThreadingHTTPServer((self.host, self.port), handler)
        self._httpd.daemon_threads = True
        self._thread = threading.Thread(target=self._httpd.serve_forever,
                                       name='lan-server', daemon=True)
        self._thread.start()
        log.info('局域网服务已启动：%s', self.url())
        return self.url()

    def url(self, lan: bool = True) -> str:
        host = lan_ip() if lan else '127.0.0.1'
        return f'http://{host}:{self.port}'

    def stop(self) -> None:
        if self._httpd is not None:
            self._httpd.shutdown()
            self._httpd.server_close()
            self._httpd = None
        self._thread = None
