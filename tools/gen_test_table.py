# -*- coding: utf-8 -*-
"""从 junit xml 生成报告第 4 章的表 4.10（逐文件用例数/通过/待修/失败）。

为什么用脚本生成：报告里的用例数只能来自真实的执行结果。手抄一次就对不上，
而且每轮改完测试都有人忘（本项目已经因此落后两次口径）。

用法:
    cd code
    python -m pytest tests -q --junitxml=..\\_junit_formal.xml
    python -m pytest tests_extra -q --junitxml=..\\_junit_extra.xml
    cd ..
    python tools/gen_test_table.py _junit_formal.xml
"""
from __future__ import annotations

import ast
import re
import sys
import xml.etree.ElementTree as ET
from collections import OrderedDict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CODE = ROOT / 'code'


def module_summary(path: Path) -> str:
    """取模块 docstring 的首句当"覆盖内容"；没有就退到文件名"""
    try:
        tree = ast.parse(path.read_text(encoding='utf-8'))
    except (OSError, SyntaxError):
        return ''
    doc = ast.get_docstring(tree) or ''
    first = doc.strip().splitlines()[0].strip() if doc.strip() else ''
    first = re.sub(r'[（(][^）)]*\bD-\d+[^）)]*[）)]\s*$', '', first)   # 去掉"（D-58 回归）"这类尾巴
    first = re.sub(r'[（(][^）)]*回归[^）)]*[）)]\s*$', '', first)
    return first.strip().rstrip('。')


def collect(xml_path: Path) -> 'OrderedDict[str, dict]':
    tree = ET.parse(xml_path)
    rows: 'OrderedDict[str, dict]' = OrderedDict()
    for case in tree.iter('testcase'):
        rel = (case.get('file') or case.get('classname', '').replace('.', '/')).replace('\\', '/')
        name = rel.split('/')[-1]
        if not name.endswith('.py'):
            name += '.py'
        row = rows.setdefault(name, {'用例数': 0, '通过': 0, '待修': 0, '失败': 0})
        row['用例数'] += 1
        skipped = case.find('skipped')
        failed = case.find('failure') is not None or case.find('error') is not None
        if failed:
            row['失败'] += 1
        elif skipped is not None and skipped.get('type') == 'pytest.xfail':
            row['待修'] += 1
        elif skipped is not None:
            row['通过'] += 1          # 主动 skip 不算失败，也不算待修
        else:
            row['通过'] += 1
    return rows


def main(argv: list[str]) -> int:
    xml_path = Path(argv[1]) if len(argv) > 1 else ROOT / '_junit_formal.xml'
    if not xml_path.is_absolute():
        xml_path = ROOT / xml_path
    rows = collect(xml_path)
    total = {'用例数': 0, '通过': 0, '待修': 0, '失败': 0}
    for row in rows.values():
        for key in total:
            total[key] += row[key]
    print(f'<!-- 生成自 {xml_path.name}；合计 {total["用例数"]} 条 -->')
    print('| 测试文件 | 覆盖内容 | 用例数 | 通过 | 待修 | 失败 |')
    print('|---|---|---|---|---|---|')
    for name, row in rows.items():
        desc = module_summary(CODE / 'tests' / name) or module_summary(CODE / 'tests_extra' / name) or name
        print(f'| `{name}` | {desc} | {row["用例数"]} | {row["通过"]} | '
              f'{row["待修"]} | {row["失败"]} |')
    print(f'| **合计** | {len(rows)} 个文件 | **{total["用例数"]}** | **{total["通过"]}** | '
          f'**{total["待修"]}** | **{total["失败"]}** |')
    return 0 if total['失败'] == 0 else 1


if __name__ == '__main__':
    sys.exit(main(sys.argv))
