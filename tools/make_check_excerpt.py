# -*- coding: utf-8 -*-
"""从完整报告切出「今晚检查用」节选：封面+评分表 + 第一/二/三部分。

课程通知要求检查「设计文档的一、二部分，第三部分需画出与用例对应的顺序图」。
这三部分在完整报告中是从第 1 页到「四 测试」之前的一段连续页，其余（测试、
总结、参考文献）本次不需要。

**页码全部动态定位**：按章节标题在 PDF 文本中查找，因此后续增删内容导致页码
变化时不需要改本脚本（早期版本写死了 1~47，第 1 章加长后就切错了）。
"""
from pathlib import Path

import fitz

ROOT = Path(__file__).resolve().parents[1]      # 软件设计2-课程设计/
SRC = ROOT / '05_设计报告' / '软件设计II设计报告.pdf'
DST = Path.home() / 'Desktop' / '今晚检查_设计文档（一二三部分）.pdf'

# 节选终点：第一章之前那一页（封面+评分表）+ 一/二/三，到「四 测试」前一页结束
STOP_AT = '四 测试'

# 书签：(层级, 显示标题, 用于定位的正文字符串, 起始页下限用的锚点标题或 None)
# 起始页下限用于避免"数据库""顺序图"这类词在前面章节正文里提前命中
ANCHORS = [
    (1, '封面 / 评分表', '评分表', None),
    (1, '一 系统需求分析', '一 系统需求分析', None),
    (2, '1.1 需求调研（第一轮发现 + 第二轮验证）', '需求调研', '一 系统需求分析'),
    (2, '1.5 功能性分析与用例模型', '1.5 功能性分析', '一 系统需求分析'),
    (2, '1.6 用例规约', '1.6 用例规约', '一 系统需求分析'),
    (2, '1.12 命题范围外自选扩展（新增城市 / 新增核心功能）',
     '1.12 命题范围之外的自选扩展', '一 系统需求分析'),
    (1, '二 系统设计', '二 系统设计', None),
    (2, '2.1 设计系统体系结构', '设计系统体系结构', '二 系统设计'),
    (2, '2.2 用户界面设计', '2.2 用户界面设计', '二 系统设计'),
    (2, '2.3 概要设计（领域模型 / 服务与适配器 / E-R）', '2.3 概要设计', '二 系统设计'),
    (2, '2.4 数据库设计', '2.4 数据库设计', '二 系统设计'),
    (1, '三 系统详细设计和实现', '三 系统详细设计和实现', None),
    (2, '3.2~3.3 顺序图（UC-01 解析 / UC-01 排程入库 / UC-08 调整）',
     '3.2 录入并生成行程的顺序图', '三 系统详细设计和实现'),
    (2, '3.7 其余用例的顺序图（图 3.7~3.15，含用例对照表）',
     '3.7 其余用例的顺序图', '三 系统详细设计和实现'),
]


def _find_page(doc, text: str, start: int = 0) -> int:
    """返回首个包含 text 的页码（1-based）；找不到返回 -1"""
    for i in range(start, doc.page_count):
        if text in doc[i].get_text():
            return i + 1
    return -1


def main() -> int:
    if not SRC.exists():
        raise SystemExit(f'找不到完整报告：{SRC}')

    src = fitz.open(SRC)
    stop = _find_page(src, STOP_AT)
    start = _find_page(src, '一 系统需求分析')
    if stop < 0 or start < 0:
        raise SystemExit('未能在 PDF 中定位章节标题，请检查报告是否正常生成')

    # 先按锚点标题求各章节起点，作为子书签的搜索下限
    limits: dict[str, int] = {}
    for _, title, _, _ in ANCHORS:
        if title not in limits:
            page = _find_page(src, title)
            if page > 0:
                limits[title] = page - 1      # 转成 0-based 起点

    out = fitz.open()
    out.insert_pdf(src, from_page=0, to_page=stop - 2)   # 含封面与一/二/三

    toc = []
    for level, title, needle, since in ANCHORS:
        begin = limits.get(since, 0) if since else 0
        page = _find_page(out, needle, begin)
        if page > 0:
            toc.append([level, title, page])
    if toc:
        out.set_toc(toc)

    out.set_metadata({
        'title': '《软件设计II》设计文档 · 一二三部分（检查用节选）',
        'subject': '课程设计检查：第一、二部分 + 第三部分与用例对应的顺序图',
        'author': '某某同学',
    })
    total = out.page_count
    out.save(DST, garbage=1, deflate=True)
    out.close()

    print(f'已生成：{DST}')
    print(f'节选范围：原报告 p1 ~ p{stop - 1}（「{STOP_AT}」起于 p{stop}）')
    print(f'页数：{total}')
    src.close()
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
