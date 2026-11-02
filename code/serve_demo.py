# -*- coding: utf-8 -*-
"""起一个局域网服务用于演示/截图（阻塞运行，Ctrl+C 结束）。

用法: python serve_demo.py [端口]
"""
import sys
import time
from pathlib import Path

sys.path.insert(0, r'D:\dsh\软件设计2-课程设计\code')

from nanjingtrip.app import AppContext
from nanjingtrip.lan_server import LanServer, lan_ip

port = int(sys.argv[1]) if len(sys.argv) > 1 else 8765
data_dir = sys.argv[2] if len(sys.argv) > 2 else None
ctx = AppContext(data_dir)
print('模式:', ctx.degrade.mode_label())
srv = LanServer(ctx, port=port)
url = srv.start()
print(f'桌面端可打开: http://127.0.0.1:{port}')
print(f'手机端可打开: {url}   （同一局域网内，手机浏览器输入）')
try:
    while True:
        time.sleep(3600)
finally:
    srv.stop()
    ctx.close()
