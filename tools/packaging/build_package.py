# -*- coding: utf-8 -*-
"""打包交付包：源码 + exe + 报告 + 图表 + 启动脚本，压成一个 zip。

用法: python tools/packaging/build_package.py [--skip-exe]
产出: <上级目录>\\NanjingTrip_交付包_<日期>.zip
"""
from __future__ import annotations

import shutil
import subprocess
import sys
import zipfile
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
PKG_PARENT = ROOT.parent / '_pkg_build'
PKG = PKG_PARENT / 'NanjingTrip_交付包'
STAMP = datetime.now().strftime('%Y%m%d')
ZIP_PATH = ROOT.parent / f'NanjingTrip_交付包_{STAMP}.zip'
SKIP_EXE = '--skip-exe' in sys.argv

# 不进包的东西：缓存、密钥、构建中间产物
EXCLUDE_DIRS = {'__pycache__', '.pytest_cache', '.git', '.idea', 'build', 'dist',
                'pages', 'conv'}
EXCLUDE_FILES = {'secrets.json', '.DS_Store', 'Thumbs.db'}
EXCLUDE_SUFFIX = {'.pyc', '.pyo', '.spec'}

# 要放进包里的顶层条目（相对项目根）
INCLUDE = [
    'code',
    '01_需求分析', '02_系统设计', '03_详细设计与实现', '04_测试',
    '05_设计报告', '06_图表',
    'tools',
    '项目计划与分工.md', '交付说明.md',
]

log: list[str] = []


def say(msg: str) -> None:
    print(msg)
    log.append(msg)


def ignore(dirpath, names):
    out = set()
    for n in names:
        p = Path(dirpath) / n
        if n in EXCLUDE_DIRS or n in EXCLUDE_FILES:
            out.add(n)
        elif p.suffix in EXCLUDE_SUFFIX:
            out.add(n)
    return out


def copy_tree(src: Path, dst: Path) -> None:
    shutil.copytree(src, dst, ignore=ignore, dirs_exist_ok=True)


def build_exe(work_code: Path, out_dir: Path) -> Path | None:
    """用 PyInstaller 打免安装版（onedir）。

    大改为 Web 之后不再需要 Qt：exe 的职责只是"起本地服务 + 打开浏览器"，
    因此打包体积从 ~126 MB 降到几 MB，hidden-import 也只剩标准库与 requests。
    """
    cmd = [
        sys.executable, '-m', 'PyInstaller',
        '--noconfirm', '--clean', '--onedir',
        '--name', 'NanjingTrip',
        '--distpath', str(out_dir),
        '--workpath', str(PKG_PARENT / '_pyi_work'),
        '--specpath', str(PKG_PARENT / '_pyi_spec'),
        # 前端静态文件必须显式带上，否则 exe 起来是空白页
        '--add-data', f'{work_code / "nanjingtrip" / "web"};nanjingtrip/web',
        '--exclude-module', 'tkinter',
        '--exclude-module', 'PySide6',
        '--exclude-module', 'matplotlib',
        '--exclude-module', 'numpy',
        '--exclude-module', 'PIL',
        '--exclude-module', 'pytest',
        str(work_code / 'main.py'),
    ]
    say('打包 exe 中（约 1-3 分钟）...')
    r = subprocess.run(cmd, cwd=str(work_code), capture_output=True, text=True,
                       errors='ignore')
    exe = out_dir / 'NanjingTrip' / 'NanjingTrip.exe'
    if r.returncode != 0 or not exe.exists():
        say('exe 打包失败，改为提供源码版：')
        say((r.stderr or r.stdout or '')[-1500:])
        return None
    size_mb = sum(f.stat().st_size for f in exe.parent.rglob('*')
                  if f.is_file()) / 1024 / 1024
    say(f'exe 完成：{exe}（目录共 {size_mb:.1f} MB）')
    return exe


def main() -> None:
    if PKG_PARENT.exists():
        shutil.rmtree(PKG_PARENT, ignore_errors=True)
    PKG.mkdir(parents=True)

    # 1) 拷贝源码与文档
    for name in INCLUDE:
        src = ROOT / name
        if not src.exists():
            say(f'[跳过] 不存在：{name}')
            continue
        dst = PKG / name
        if src.is_dir():
            copy_tree(src, dst)
        else:
            shutil.copy2(src, dst)
    say(f'已拷贝 {len(INCLUDE)} 个条目')

    # 2) 启动脚本（用 GBK 生成：UTF-8 批处理夹中文再 chcp 会让 cmd 按错字节偏移解析）与说明
    pack_dir = ROOT / 'tools' / 'packaging'
    sys.path.insert(0, str(pack_dir))
    from make_bats import write_all as write_bats
    say('生成启动脚本: ' + '、'.join(write_bats(PKG)))
    shutil.copy2(pack_dir / '换电脑运行说明.md', PKG / '换电脑运行说明.md')
    say('已放入换电脑运行说明')

    # 3) exe
    exe_path = None
    if not SKIP_EXE:
        exe_path = build_exe(PKG / 'code', PKG / 'exe')
        # PyInstaller 构建过程会在源码目录生成 __pycache__，必须在压缩前清掉
        removed = 0
        for junk in list(PKG.rglob('__pycache__')) + list(PKG.rglob('.pytest_cache')):
            shutil.rmtree(junk, ignore_errors=True)
            removed += 1
        for pyc in PKG.rglob('*.pyc'):
            pyc.unlink(missing_ok=True)
            removed += 1
        say(f'已清理构建缓存 {removed} 处')

        # 自证可用：用打包出来的 exe 跑一次离屏渲染
        if exe_path:
            probe_dir = PKG_PARENT / '_probe'
            probe_dir.mkdir(parents=True, exist_ok=True)
            shot = probe_dir / 'probe.png'
            say('验证 exe 可用性（离屏渲染 + 写入演示数据）...')
            try:
                subprocess.run([str(exe_path), '--offscreen',
                                '--data-dir', str(probe_dir / 'data'),
                                '--demo', '--date', '2026-10-28',
                                '--screenshot', str(shot)],
                               timeout=180, capture_output=True)
                ok = shot.exists() and shot.stat().st_size > 10000
                say(f'exe 自检：{"通过" if ok else "失败"}'
                    f'（截图 {shot.stat().st_size if shot.exists() else 0} 字节）')
            except Exception as exc:      # noqa: BLE001
                say(f'exe 自检异常：{exc}')
    else:
        say('已跳过 exe 打包（--skip-exe）')

    # 4) 安全自查：确认包里没有密钥与缓存
    leaked = [str(p.relative_to(PKG)) for p in PKG.rglob('*')
              if p.is_file() and (p.name in EXCLUDE_FILES
                                 or p.suffix in EXCLUDE_SUFFIX)]
    say(f'安全自查：密钥/缓存残留 {len(leaked)} 个'
        + (f' -> {leaked[:5]}' if leaked else '（干净）'))

    # 5) 压缩
    if ZIP_PATH.exists():
        ZIP_PATH.unlink()
    say('压缩中...')
    with zipfile.ZipFile(ZIP_PATH, 'w', zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
        for f in PKG.rglob('*'):
            if f.is_file():
                zf.write(f, Path('NanjingTrip_交付包') / f.relative_to(PKG))
    size_mb = ZIP_PATH.stat().st_size / 1024 / 1024
    say(f'\n交付包：{ZIP_PATH}\n大小：{size_mb:.1f} MB')

    (PKG_PARENT / 'build_log.txt').write_text('\n'.join(log), encoding='utf-8')


if __name__ == '__main__':
    main()
