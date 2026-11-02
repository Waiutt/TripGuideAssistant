# -*- coding: utf-8 -*-
"""代码体检脚本：改完代码跑一遍，先把"能自动发现的问题"挡在前面。

用法：cd code && python ../tools/audit_code.py
退出码 0 = 全过；1 = 有硬问题（重复定义/死代码/游离槽位/导入失败/冒烟失败）

检查项：
  1. 全量编译（语法）
  2. 导入装配（AppContext 能否建起来）
  3. 冒烟：顾问出草案 → 写入 → 读回当天（抓"装饰器丢了/接线断了"这类运行期事故）
  4. AST：重复定义、未使用导入、游离槽位、零引用公有函数
"""
from __future__ import annotations

import ast
import collections
import pathlib
import py_compile
import re
import sys
import tempfile
from datetime import date

ROOT = pathlib.Path(__file__).resolve().parents[1]
CODE = ROOT / 'code'
PKG = CODE / 'nanjingtrip'
problems: list[str] = []


def check_compile() -> None:
    bad = []
    for path in sorted(PKG.rglob('*.py')):
        try:
            py_compile.compile(str(path), doraise=True, cfile=str(path) + 'c')
        except py_compile.PyCompileError as exc:
            bad.append(f'{path.name}: {exc.msg.splitlines()[0]}')
    for junk in PKG.rglob('*.pyc'):
        junk.unlink(missing_ok=True)
    problems.extend(f'编译失败 {b}' for b in bad)


def check_smoke() -> None:
    sys.path.insert(0, str(CODE))
    try:
        from nanjingtrip.app import AppContext
        from nanjingtrip.consult import Consultant, ConsultStore
    except Exception as exc:                       # noqa: BLE001
        problems.append(f'导入失败：{exc}')
        return
    ctx = AppContext(tempfile.mkdtemp())
    try:
        ctx.settings.offline_mode = True
        ctx.apply_settings(ctx.settings)
        # 装饰器/接线类事故在这里就会炸
        degrade = ctx.degrade
        if degrade.map() is None or degrade.llm() is None:
            problems.append('降级控制器拿不到适配器（map/llm 装饰器或赋值被改坏）')
        con = Consultant(ctx.trip_svc, ConsultStore(ctx.db))
        plan = con.chat('audit', '11月中旬去南京玩3天，很懒，10点起床', date(2026, 11, 1)).get('plan')
        if not plan or not plan.get('days'):
            problems.append('冒烟失败：顾问没出草案')
            return
        ok, _msg = ctx.trip_svc.apply_proposal(plan)
        if not ok:
            problems.append('冒烟失败：草案写入未成功')
            return
        day = date.fromisoformat(plan['days'][0]['date'])
        if not ctx.trip_svc.day_view(day).tasks:
            problems.append('冒烟失败：写入后当天读不到事项')
    finally:
        ctx.close()


def check_ast() -> None:
    sources = {p: p.read_text(encoding='utf-8') for p in sorted(PKG.rglob('*.py'))}
    # 引用统计要算上测试、入口与工具脚本：只被测试调用的函数不算死代码
    extra = [q for d in (CODE / 'tests', CODE / 'tests_extra', CODE, ROOT / 'tools')
             for q in sorted(pathlib.Path(d).glob('*.py')) if q.is_file()]
    corpus = '\n'.join(sources.values()) + '\n' + '\n'.join(
        q.read_text(encoding='utf-8', errors='ignore') for q in extra)
    for path, src in sources.items():
        tree = ast.parse(src)
        # 重复定义
        names = collections.Counter()
        for node in tree.body:
            if isinstance(node, (ast.FunctionDef, ast.ClassDef)):
                names[node.name] += 1
            if isinstance(node, ast.ClassDef):
                methods = collections.Counter(
                    s.name for s in node.body if isinstance(s, ast.FunctionDef))
                problems.extend(f'{path.name}: {node.name}.{n} 定义了 {c} 次'
                                for n, c in methods.items() if c > 1)
        problems.extend(f'{path.name}: {n} 定义了 {c} 次'
                        for n, c in names.items() if c > 1)
        # 未使用导入（跳过 __future__ 与类型注解里出现的名字）
        imported = {}
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for a in node.names:
                    imported[(a.asname or a.name).split('.')[0]] = node.lineno
            elif isinstance(node, ast.ImportFrom) and node.module != '__future__':
                for a in node.names:
                    if a.name != '*':
                        imported[a.asname or a.name] = node.lineno
        for name, lineno in imported.items():
            body = re.sub(rf'^\s*(from\s+\S+\s+)?import\s+.*\b{re.escape(name)}\b.*$',
                          '', src, count=1, flags=re.M)
            if not re.search(rf'\b{re.escape(name)}\b', body):
                problems.append(f'{path.name}:{lineno} 导入了 {name} 但没用到')
        # 零引用公有函数（只看模块级：类方法常被基类/属性/框架调用，误报率高）
        for node in tree.body:
            if isinstance(node, ast.FunctionDef) and not node.name.startswith('_'):
                uses = len(re.findall(rf'\b{re.escape(node.name)}\b', corpus))
                defs = len(re.findall(rf'def {re.escape(node.name)}\b', corpus))
                if uses - defs <= 0 and '__maintenance__' not in src:
                    problems.append(f'{path.name}: {node.name}() 全仓零引用')
    # 槽位：写了但不在 FIELDS 里（会存不下来）
    consult = sources.get(PKG / 'consult.py')
    if consult:
        fields: set[str] = set()
        tree = ast.parse(consult)
        for node in ast.walk(tree):
            if isinstance(node, ast.ClassDef) and node.name == 'ConsultSession':
                for sub in node.body:
                    if (isinstance(sub, ast.Assign)
                            and any(isinstance(t, ast.Name) and t.id == 'FIELDS'
                                    for t in sub.targets)):
                        fields = {e.value for e in sub.value.elts}
        written = {t.attr for node in ast.walk(tree)
                   if isinstance(node, ast.Assign)
                   for t in node.targets
                   if isinstance(t, ast.Attribute) and isinstance(t.value, ast.Name)
                   and t.value.id == 'st'}
        for name in sorted(written - fields):
            problems.append(f'consult.py: 槽位 {name} 被写入但不在 FIELDS 里（存不下来）')


def main() -> int:
    check_compile()
    check_smoke()
    check_ast()
    if problems:
        print(f'发现 {len(problems)} 个问题：')
        for p in problems:
            print('  ✗', p)
        return 1
    print('代码体检通过：编译 / 冒烟 / AST 全部正常')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
