# -*- coding: utf-8 -*-
"""开发用本地服务：**永不弹浏览器**，改 .py 自动重启（改前端不需要它动）。

为什么需要它
------------
`main.py` 默认会顺手打开浏览器，方便用户双击启动；但改代码时反复重启就会
弹出一个个新页面。开发时用这个包装器：

    python tools/dev.py                 # 8765，改 .py 自动重启
    python tools/dev.py --port 8800     # 换端口
    python tools/dev.py --no-watch      # 只启动，不监视

约定：
* 前端（app.js / app.css / index.html）是每次请求现读的，**改完刷新页面即可**，
  本工具不会因为它们的改动重启服务；
* 只有 `code/**.py` 变化才会重启，重启时关旧进程再起新的，端口不会冲突。
"""
from __future__ import annotations

import argparse
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CODE = ROOT / 'code'


def snapshot() -> dict[str, float]:
    """当前 code/**.py 的修改时间。

    编辑器保存时会在同目录建临时目录/临时文件，扫描的一瞬间它可能已经被删掉，
    直接 rglob+stat 会抛 FileNotFoundError/PermissionError 把监视器打死（实测踩过两次）。
    """
    out: dict[str, float] = {}
    for p in CODE.rglob('*.py'):
        if '__pycache__' in str(p):
            continue
        try:
            out[str(p)] = p.stat().st_mtime
        except OSError:          # 文件刚好被替换/删除：下一次扫描再看
            continue
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description='开发服务（不弹浏览器，改 py 自动重启）')
    ap.add_argument('--port', type=int, default=8765)
    ap.add_argument('--host', default='0.0.0.0')
    ap.add_argument('--data-dir', default=None)
    ap.add_argument('--no-watch', action='store_true', help='只启动，不监视文件')
    a = ap.parse_args(argv)

    cmd = [sys.executable, '-u', str(CODE / 'main.py'),
           '--port', str(a.port), '--host', a.host, '--no-browser']
    if a.data_dir:
        cmd += ['--data-dir', a.data_dir]

    def spawn() -> subprocess.Popen:
        return subprocess.Popen(cmd, cwd=str(CODE))

    proc = spawn()
    print(f'[dev] 已启动：http://127.0.0.1:{a.port}/  （不弹浏览器）', flush=True)
    if a.no_watch:
        return proc.wait()

    seen = snapshot()
    try:
        while True:
            time.sleep(1.0)
            try:
                now = snapshot()
            except OSError:              # 扫描期撞上删除/替换，下一轮再来
                continue
            changed = [p for p, t in now.items() if seen.get(p) != t]
            # 子进程自己挂了（比如改到一半语法错）也要拉起来，否则服务就永久沉默了
            died = proc.poll() is not None
            if not changed and not died:
                seen = now
                continue
            if died:
                print('[dev] 服务已退出（可能是改到一半的语法错），1 秒后自动重启…', flush=True)
                time.sleep(1.0)
            else:
                name = Path(changed[0]).relative_to(CODE)
                print(f'[dev] 检测到 {name} 变更，重启服务…', flush=True)
                proc.terminate()
                try:
                    proc.wait(timeout=8)
                except subprocess.TimeoutExpired:
                    proc.kill()
                time.sleep(1.0)
            proc = spawn()
            seen = snapshot()
            print(f'[dev] 已重启：http://127.0.0.1:{a.port}/', flush=True)
    except KeyboardInterrupt:
        print('[dev] 停止', flush=True)
    finally:
        if proc.poll() is None:
            proc.terminate()
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
