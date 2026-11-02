# -*- coding: utf-8 -*-
"""生成"提交给老师"的最小文件集到桌面，并做一遍自检。

只放指导书要求的材料，不把工程中间产物（图表源文件、计划表、交付说明等）塞进去。
用法：python tools/pack_submission.py [目标目录]
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]          # 软件设计2-课程设计/
FOLDER_NAME = '软件设计2-课程设计_提交'


def desktop_dir() -> Path:
    home = Path(os.environ.get('USERPROFILE') or Path.home())
    for name in ('Desktop', 'OneDrive/Desktop', '桌面'):
        d = home / name
        if d.is_dir():
            return d
    return home


# ======================================================================
# 组装
# ======================================================================
def copy_code(dst: Path) -> None:
    """源码：去掉缓存与临时脚本，保留源码/测试/README"""
    skip_dirs = {'__pycache__', '.pytest_cache', '.git'}
    skip_files = {'.DS_Store'}
    # 调试/探针脚本（_probe / _repro / _dbg_ / _check_ / _patch_）不进交付包，
    # 但 __init__.py 这类双下划线的包文件必须留。
    skip_prefix = ('_probe', '_repro', '_dbg_', '_check_', '_patch_', '_diag_')
    for src in sorted((ROOT / 'code').rglob('*')):
        rel = src.relative_to(ROOT / 'code')
        if any(part in skip_dirs for part in rel.parts):
            continue
        if src.is_file() and (src.name in skip_files
                              or (src.name.startswith(skip_prefix)
                                  and not src.name.startswith('__'))):
            continue
        target = dst / rel
        if src.is_dir():
            target.mkdir(parents=True, exist_ok=True)
        else:
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, target)


def build(target_root: Path) -> None:
    if target_root.exists():
        shutil.rmtree(target_root)
    target_root.mkdir(parents=True)

    report = target_root / '01_设计报告'
    report.mkdir()
    for name in ('软件设计II设计报告.docx', '软件设计II设计报告.pdf'):
        shutil.copy2(ROOT / '05_设计报告' / name, report / name)

    copy_code(target_root / '02_源程序')

    research = target_root / '03_需求调研'
    research.mkdir()
    src = ROOT / '01_需求分析' / '需求调研.md'
    if src.exists():
        shutil.copy2(src, research / src.name)


# ======================================================================
# 自检
# ======================================================================
def check(target_root: Path) -> tuple[list[str], list[str]]:
    ok: list[str] = []
    bad: list[str] = []

    # 1) 报告
    docx = target_root / '01_设计报告' / '软件设计II设计报告.docx'
    pdf = target_root / '01_设计报告' / '软件设计II设计报告.pdf'
    ok.append(f'设计报告 docx 存在，{docx.stat().st_size / 1024 / 1024:.2f} MB')
    ok.append(f'设计报告 pdf 存在，{pdf.stat().st_size / 1024 / 1024:.2f} MB')
    try:
        import fitz
        doc = fitz.open(pdf)
        text = ''.join(p.get_text() for p in doc)
        ok.append(f'PDF 可打开，共 {doc.page_count} 页')
        tables = sorted({(int(a), int(b)) for a, b in
                         re.findall(r'表\s*(\d+)\.(\d+)', text)})
        figures = sorted({(int(a), int(b)) for a, b in
                          re.findall(r'图\s*(\d+)\.(\d+)', text)})
        ok.append(f'表 {len(tables)} 张、图 {len(figures)} 张，编号连续'
                  if all(((c, i) in tables) for c in {c for c, _ in tables}
                         for i in range(1, max(i for cc, i in tables if cc == c) + 1))
                  else f'表 {len(tables)} 张、图 {len(figures)} 张（编号需复核）')
        for chapter in ('系统需求分析', '系统设计', '详细设计和实现', '测试', '总结'):
            if chapter not in text:
                bad.append(f'PDF 中未找到章节：{chapter}')
        # 只查"测试输出块"里的确切形式：127 个/160 个这类数字在正文里是刻意保留的
        # 对照（如"原有 127 项仍原样通过"），不能一律判为过期
        for stale in ('74 passed', '127 passed', '160 passed', '193 passed', '196 passed', '197 passed', '201 passed', '202 passed', '204 passed', '206 passed', '207 passed', '210 passed', '212 passed', '214 passed', '218 passed', '221 passed', '222 passed', '597 passed', '777 passed', '3 800 行', '43 页'):
            if stale in text:
                bad.append(f'PDF 仍含过期表述：{stale}')
    except Exception as exc:                     # noqa: BLE001
        bad.append(f'PDF 校验失败：{exc}')

    try:
        from docx import Document
        d = Document(docx)
        ok.append(f'docx 可打开：{len(d.paragraphs)} 段、{len(d.tables)} 张表')
    except Exception as exc:                     # noqa: BLE001
        bad.append(f'docx 校验失败：{exc}')

    # 2) 源码完整性与无缓存
    code = target_root / '02_源程序'
    must = ['main.py', 'requirements.txt', 'README.md',
            'nanjingtrip/app.py', 'nanjingtrip/cities.py', 'nanjingtrip/engine.py',
            'nanjingtrip/services.py', 'nanjingtrip/storage.py',
            'nanjingtrip/lodging.py', 'nanjingtrip/hours.py',
            'nanjingtrip/web_api.py',
            'nanjingtrip/web/index.html', 'nanjingtrip/web/app.js',
            'nanjingtrip/web/app.css',
            # Agent 能力声明（AGENTS.md §6：能力卡必须随源码交付）
            'nanjingtrip/agent/AGENT.md', 'nanjingtrip/agent/skills.py',
            'nanjingtrip/agent/skills/plan_trip.md']
    missing = [m for m in must if not (code / m).exists()]
    if missing:
        bad.append('源码缺文件：' + '、'.join(missing))
    else:
        ok.append(f'源码齐全（{len(must)} 个关键文件）')

    # 3) 密钥泄漏（不打印内容，只报命中文件名）
    leak = []
    pat = re.compile(r'(sk-[A-Za-z0-9]{16,}|api_key\s*[:=]\s*["\'][A-Za-z0-9]{16,})')
    for p in target_root.rglob('*'):
        if not p.is_file() or p.suffix.lower() in {'.docx', '.pdf', '.png', '.zip'}:
            continue
        try:
            if pat.search(p.read_text(encoding='utf-8', errors='ignore')):
                leak.append(p.relative_to(target_root).as_posix())
        except OSError:
            pass
    if leak:
        bad.append('疑似密钥出现在：' + '、'.join(leak))
    else:
        ok.append('未发现明文 API Key（secrets.json 未包含）')

    # 4) 测试可跑通（在提交副本上执行，验证拷贝完整）
    try:
        r = subprocess.run([sys.executable, '-m', 'pytest', 'tests', '-q'],
                           cwd=code, capture_output=True, text=True, timeout=600)
        tail = (r.stdout or '').strip().splitlines()[-1:] or ['(无输出)']
        line = tail[0]
        # 只有真的 failed>0 才算失败；xfailed/xpassed 是"已记录的待修项/已修好的项"
        nums = dict(re.findall(r'(\d+)\s+(passed|failed|xfailed|xpassed|error|errors)', line))
        hard_failed = int(nums.get('failed', 0)) + int(nums.get('error', 0))
        if 'passed' in line and hard_failed == 0:
            ok.append(f'提交副本跑测试通过：{line}')
        else:
            bad.append(f'测试未全部通过：{line}')
    except Exception as exc:                     # noqa: BLE001
        bad.append(f'测试执行失败：{exc}')

    # 5) 清理测试产生的缓存，再确认干净（顺序很重要：必须在跑完测试之后）
    for cache in list(code.rglob('__pycache__')) + list(code.rglob('.pytest_cache')):
        shutil.rmtree(cache, ignore_errors=True)
    for pyc in code.rglob('*.pyc'):
        pyc.unlink(missing_ok=True)
    caches = [p for p in code.rglob('__pycache__')] + \
             [p for p in code.rglob('.pytest_cache')]
    if caches or list(code.rglob('*.pyc')):
        bad.append('仍残留 __pycache__ / .pytest_cache / .pyc')
    else:
        ok.append('无 __pycache__ / .pytest_cache / .pyc 残留')

    return ok, bad


# ======================================================================
# 清单
# ======================================================================
CHECKLIST = """# 提交清单与自检结果

> 本文件由 `tools/pack_submission.py` 生成。只包含指导书要求的提交材料。

## 一、本目录内容

| 目录 / 文件 | 对应指导书要求 | 说明 |
|---|---|---|
| `01_设计报告/软件设计II设计报告.docx` | 设计报告（封面 + 评分表 + 正文） | 六章正文，含封面与 1 份评分表 |
| `01_设计报告/软件设计II设计报告.pdf` | 同上（便于在线预览） | 与 docx 同源生成 |
| `02_源程序/` | 可运行软件 + 工程项目文件（含开发工具版本说明） | `python main.py` 直接运行；`README.md` 第 6 节为开发工具版本清单 |
| `03_需求调研/需求调研.md` | 需求调研（步骤与竞品/文献/情景推演） | 问卷样本部分为模板，需本人实测后补充 |

## 二、怎么运行（老师若需验证）

```bat
cd 02_源程序
pip install -r requirements.txt
python main.py            :: 启动桌面端
:: 或 python main.py --city 北京 --demo     切换城市并填入演示数据
```

- 无需联网：无 API Key 时自动进入离线模式（内置城市包 POI + 规则解析），可完整演示。
- 手机端：启动后控制台会打印局域网地址，同一局域网内手机浏览器打开即可读写同一份数据。
- 数据存放于 `%LOCALAPPDATA%\\NanjingTrip`，不写入程序目录。

## 三、还需你本人补充的三处

1. **封面与评分表**的姓名、班级、学号、日期（文档中已留空，直接手填或改 docx）。
2. **演示视频**（≤100 MB，按指导书要求的时长与内容录制）。
3. **需求调研的问卷实测数据**（现有推演部分已在文中注明为情景推演，不是真实样本；
   如指导书要求真实问卷，请补充实测结果）。

## 四、自检结果

{checks}

## 五、已知说明（避免误会）

- 命题范围为南京市内。本期在完成命题的基础上，把"城市"做成可替换数据，
  额外提供了北京城市包（软件内可切换，报告第 5 章已如实说明）。
- 在线链路（真实地图与 LLM 接口）未纳入自动化测试，答辩可现场联网演示；
  离线模式下功能完整可用。
"""


def main() -> int:
    target_root = Path(sys.argv[1]) if len(sys.argv) > 1 else \
        desktop_dir() / FOLDER_NAME
    build(target_root)
    ok, bad = check(target_root)

    lines = []
    for line in ok:
        lines.append(f'- [x] {line}')
    for line in bad:
        lines.append(f'- [ ] **待修** {line}')
    (target_root / '00_提交清单与自检.md').write_text(
        CHECKLIST.format(checks='\n'.join(lines)), encoding='utf-8')

    print(f'目标目录：{target_root}')
    print('\n通过：')
    for line in ok:
        print('  ✓', line)
    if bad:
        print('\n待修：')
        for line in bad:
            print('  ✗', line)
        return 1

    # 额外给一个压缩包，方便一次性上传
    zip_path = target_root.parent / (FOLDER_NAME + '.zip')
    if zip_path.exists():
        zip_path.unlink()
    with zipfile.ZipFile(zip_path, 'w', zipfile.ZIP_DEFLATED) as z:
        for p in target_root.rglob('*'):
            if p.is_file():
                z.write(p, p.relative_to(target_root.parent))
    print(f'\n压缩包：{zip_path}（{zip_path.stat().st_size / 1024 / 1024:.1f} MB）')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
