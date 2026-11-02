# -*- coding: utf-8 -*-
"""NanjingTrip Agent —— 启动入口（Web 版）。

大改为 Web 之后，本文件不再是"启动一个 Qt 窗口"，而是"启动一个本地 Web 服务
并打开浏览器"。好处是：

  * 桌面与手机共用同一套界面与同一份数据，不再维护两套 UI；
  * 依赖只有 requests，不需要额外安装任何 GUI 框架；
  * 手机连同一局域网即可访问，数据仍然只存在本机 SQLite 里。

用法
----
    python main.py                     启动并自动打开浏览器（默认端口 8765）
    python main.py --port 8800         指定端口
    python main.py --city 北京 --demo   切到北京并填入演示数据
    python main.py --no-browser        只起服务（服务器/演示用）
    python main.py --host 127.0.0.1    只监听本机（不开放局域网）
"""
from __future__ import annotations

import argparse
import sys
import threading
import time
import webbrowser
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description='NanjingTrip Agent（Web 版）')
    p.add_argument('--port', type=int, default=8765, help='监听端口（默认 8765）')
    p.add_argument('--host', default='0.0.0.0',
                   help='监听地址；0.0.0.0 允许手机访问，127.0.0.1 只本机')
    p.add_argument('--data-dir', default=None,
                   help='数据目录（默认 %%LOCALAPPDATA%%\\NanjingTrip）')
    p.add_argument('--date', default=None, help='定位日期 YYYY-MM-DD')
    p.add_argument('--city', default=None, help='启动时切换城市（南京 / 北京）')
    p.add_argument('--demo', action='store_true', help='启动时写入演示数据')
    p.add_argument('--no-browser', action='store_true', help='不自动打开浏览器')
    p.add_argument('--ui-variant', default='D', choices=['A', 'B', 'C', 'D'],
                   help='预留：视觉方向（当前 Web 版由 app.css 令牌控制）')
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    from nanjingtrip.app import AppContext
    from nanjingtrip.lan_server import LanServer, lan_ip

    ctx = AppContext(args.data_dir)

    if args.city:
        ctx.set_city(args.city)

    start_date = date.today()
    if args.date:
        y, m, d = (int(x) for x in args.date.split('-'))
        start_date = date(y, m, d)

    if args.demo:
        from nanjingtrip.demo import seed_demo
        logs = seed_demo(ctx, start_date)
        print('演示数据：')
        for line in logs:
            print('  ' + line)

    srv = LanServer(ctx, port=args.port, host=args.host)
    try:
        url = srv.start()
    except OSError as exc:
        print(f'[错误] 端口 {args.port} 启动失败：{exc}')
        print('       换个端口试试：python main.py --port 8800')
        ctx.close()
        return 1

    local = f'http://127.0.0.1:{args.port}/?date={start_date.isoformat()}'
    print('=' * 56)
    print('  NanjingTrip Agent  ·  Web 版')
    print('=' * 56)
    print(f'  模式      {ctx.degrade.mode_label()}')
    print(f'  城市      {ctx.city}')
    print(f'  本机打开  {local}')
    if args.host == '0.0.0.0':
        print(f'  手机打开  {url}   （同一局域网内）')
    print('  按 Ctrl+C 结束')
    print('=' * 56)

    if not args.no_browser:
        threading.Timer(0.8, lambda: webbrowser.open(local)).start()

    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        print('\n正在停止…')
    finally:
        srv.stop()
        ctx.close()
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
