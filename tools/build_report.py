# -*- coding: utf-8 -*-
"""把 05_设计报告/章节/*.md 组装成符合学院规范的 Word 设计报告。

版式规范（来自《软件设计II》内容及报告要求）：
  1) 正文 1.25 倍行距；中文小四宋体，英文小四 Times New Roman；
  2) 页脚页码：阿拉伯数字、底部居中、小五 Times New Roman；
  3) 表题置于表上方居中，图题置于图下方居中，编号连续（表 2.5 / 图 1.3）；
  4) 数据库表结构以表格呈现，不使用截图。

用法: python tools/build_report.py
"""
from __future__ import annotations

import re
from pathlib import Path

from docx import Document
from docx.enum.section import WD_SECTION
from docx.enum.table import WD_TABLE_ALIGNMENT
from docx.enum.text import WD_ALIGN_PARAGRAPH, WD_BREAK
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.shared import Cm, Pt, RGBColor
from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
CH_DIR = ROOT / '05_设计报告' / '章节'
OUT = ROOT / '05_设计报告' / '软件设计II设计报告.docx'

CN_BODY = '宋体'
CN_HEAD = '黑体'
EN_FONT = 'Times New Roman'
MONO = 'Consolas'
BODY_SIZE = Pt(12)          # 小四
CAPTION_SIZE = Pt(10.5)     # 五号
TABLE_SIZE = Pt(10.5)       # 五号
CODE_SIZE = Pt(9)           # 小五
PAGE_NO_SIZE = Pt(9)        # 小五

CONTENT_W_CM = 21.0 - 3.17 * 2      # A4 宽减左右页边距

CHAPTER_FILES = [
    '01_系统需求分析.md', '02_系统设计.md', '03_系统详细设计和实现.md',
    '04_测试.md', '05_总结.md', '06_参考文献.md',
]

# ------------------------------------------------------------------ 基础工具
def set_rfonts(el, ascii_font=EN_FONT, east=CN_BODY):
    """给 run/style 设置中西文字体（python-docx 只设 ascii，需手动补 eastAsia）"""
    rPr = el.get_or_add_rPr()
    rFonts = rPr.find(qn('w:rFonts'))
    if rFonts is None:
        rFonts = OxmlElement('w:rFonts')
        rPr.append(rFonts)
    rFonts.set(qn('w:ascii'), ascii_font)
    rFonts.set(qn('w:hAnsi'), ascii_font)
    rFonts.set(qn('w:eastAsia'), east)


def style_font(style, ascii_font=EN_FONT, east=CN_BODY, size=None, bold=None,
               color=RGBColor(0, 0, 0)):
    style.font.name = ascii_font
    if size:
        style.font.size = size
    if bold is not None:
        style.font.bold = bold
    if color is not None:
        style.font.color.rgb = color
    set_rfonts(style.element, ascii_font, east)


def run_font(run, ascii_font=EN_FONT, east=CN_BODY, size=None, bold=None,
             color=None):
    run.font.name = ascii_font
    if size:
        run.font.size = size
    if bold is not None:
        run.font.bold = bold
    if color is not None:
        run.font.color.rgb = color
    set_rfonts(run._r, ascii_font, east)


def shade(cell_or_par, hex_fill):
    el = cell_or_par._tc if hasattr(cell_or_par, '_tc') else cell_or_par._p
    pr = el.get_or_add_tcPr() if hasattr(el, 'get_or_add_tcPr') else el.get_or_add_pPr()
    shd = OxmlElement('w:shd')
    shd.set(qn('w:val'), 'clear')
    shd.set(qn('w:color'), 'auto')
    shd.set(qn('w:fill'), hex_fill)
    pr.append(shd)


def add_page_number(paragraph):
    """页脚页码域（PAGE）"""
    paragraph.alignment = WD_ALIGN_PARAGRAPH.CENTER
    run = paragraph.add_run()
    fld_begin = OxmlElement('w:fldChar')
    fld_begin.set(qn('w:fldCharType'), 'begin')
    instr = OxmlElement('w:instrText')
    instr.set(qn('xml:space'), 'preserve')
    instr.text = 'PAGE'
    fld_end = OxmlElement('w:fldChar')
    fld_end.set(qn('w:fldCharType'), 'end')
    run._r.append(fld_begin)
    run._r.append(instr)
    run._r.append(fld_end)
    run_font(run, EN_FONT, CN_BODY, PAGE_NO_SIZE)


INLINE_RE = re.compile(r'(\*\*.+?\*\*|`[^`]+`)')


def add_inline(par, text, size=BODY_SIZE, base_bold=False):
    """处理 **加粗** 与 `代码` 两种行内标记"""
    for piece in INLINE_RE.split(text):
        if not piece:
            continue
        if piece.startswith('**') and piece.endswith('**'):
            r = par.add_run(piece[2:-2])
            run_font(r, EN_FONT, CN_BODY, size, True)
        elif piece.startswith('`') and piece.endswith('`'):
            r = par.add_run(piece[1:-1])
            run_font(r, MONO, MONO, Pt(size.pt - 1) if hasattr(size, 'pt') else size)
        else:
            r = par.add_run(piece)
            run_font(r, EN_FONT, CN_BODY, size, base_bold or None)



# ------------------------------------------------------------------ 官方封面与评分表
#: 直接搬官方模板（附件2）的封面与评分表，保证与学校表格"原封不动"一致。
#: 只搬文字与表格（模板里没有图片），正文里的插图仍由 python-docx 原生插入。
OFFICIAL_FRONT = Path(__file__).resolve().parent / '封面评分表_官方.docx'


def build_official_front(doc, title='行程规划 AI Agent 桌面应用',
                         who='＿＿＿＿＿＿', sid='＿＿＿＿＿＿'):
    """把官方封面 + 评分表整体搬到报告最前面，并填好题目/姓名/学号"""
    import copy
    from docx import Document as _Doc

    src = _Doc(str(OFFICIAL_FRONT))
    for child in list(src.element.body):
        if child.tag.endswith('sectPr'):          # 页面设置留在各自文档
            continue
        doc.element.body.append(copy.deepcopy(child))

    # 填空：题目（正文段落里的"题   目："）、学生姓名、班级学号（表格里的空格）
    for par in doc.paragraphs:
        if par.text.strip().startswith('题') and '目' in par.text and '：' in par.text:
            run = par.add_run(title)
            run_font(run, EN_FONT, CN_BODY, Pt(14))

    for tbl in doc.tables:
        for row in tbl.rows:
            cells = row.cells
            texts = [c.text.strip() for c in cells]
            for label, value in (('学生姓名', who), ('班级学号', sid)):
                if any(t == label for t in texts):
                    idx = texts.index(label)
                    for cell in cells[idx + 1:]:
                        if not cell.text.strip():
                            cell.paragraphs[0].add_run(value)
                            break
                    break

    brk = doc.add_paragraph()
    brk.add_run().add_break(WD_BREAK.PAGE)


# ------------------------------------------------------------------ 封面

    def line(text, size, bold=False, east=CN_HEAD, space_before=0, space_after=0):
        p = doc.add_paragraph()
        p.alignment = WD_ALIGN_PARAGRAPH.CENTER
        p.paragraph_format.space_before = Pt(space_before)
        p.paragraph_format.space_after = Pt(space_after)
        p.paragraph_format.line_spacing = 1.25
        r = p.add_run(text)
        run_font(r, EN_FONT, east, size, bold)
        return p

    for _ in range(3):
        doc.add_paragraph()
    line('软件设计Ⅱ报告', Pt(36), True, CN_HEAD, space_after=18)
    line('（2026/2027学年  第一学期）', Pt(16), False, CN_HEAD, space_after=48)

    fields = [
        ('题        目', '行程规划 AI Agent 桌面应用'),
        ('专        业', '数字媒体技术'),
        ('学 生 姓 名', '＿＿＿＿＿＿'),
        ('班 级 学 号', '＿＿＿＿＿＿'),
        ('指 导 教 师', '＿＿＿    ＿＿＿'),
        ('指 导 单 位', '＿＿＿＿＿＿＿＿'),
        ('日        期', '2026 年 10 月 27 日 — 11 月 3 日'),
    ]
    for label, value in fields:
        p = doc.add_paragraph()
        p.alignment = WD_ALIGN_PARAGRAPH.CENTER
        p.paragraph_format.line_spacing = 1.75
        p.paragraph_format.space_after = Pt(2)
        r1 = p.add_run(f'{label}    ')
        run_font(r1, EN_FONT, CN_HEAD, Pt(14), True)
        r2 = p.add_run(value)
        run_font(r2, EN_FONT, CN_BODY, Pt(14))

    for _ in range(4):
        doc.add_paragraph()


# ------------------------------------------------------------------ 成绩评定表

    p = doc.add_paragraph()
    p.alignment = WD_ALIGN_PARAGRAPH.CENTER
    r = p.add_run('《软件设计II》评分表')
    run_font(r, EN_FONT, CN_HEAD, Pt(18), True)
    p.paragraph_format.space_after = Pt(10)
    p.paragraph_format.line_spacing = 1.25

    rows = [
        ('题目', '行程规划 AI Agent 桌面应用', None, None),
        ('学生姓名', '＿＿＿＿＿＿', '班级学号', '＿＿＿＿＿＿'),
        ('专        业', '数字媒体技术', '任课教师', '＿＿＿＿＿＿'),
        ('指导单位', '＿＿＿＿＿＿＿＿', '', ''),
    ]
    tbl_head = doc.add_table(rows=len(rows), cols=4)
    tbl_head.style = 'Table Grid'
    tbl_head.alignment = WD_TABLE_ALIGNMENT.CENTER
    tbl_head.autofit = False
    for i, (c1, c2, c3, c4) in enumerate(rows):
        cells = tbl_head.rows[i].cells
        for j, val in enumerate((c1, c2, c3, c4)):
            if val is None:
                continue
            par = cells[j].paragraphs[0]
            par.paragraph_format.line_spacing = 1.25
            run = par.add_run(val)
            run_font(run, EN_FONT, CN_BODY, TABLE_SIZE, bold=(j % 2 == 0))
    for r_ in tbl_head.rows:
        r_.cells[1].merge(r_.cells[3]) if r_.cells[3].text == '' else None
    set_col_widths(tbl_head, [2.7, 4.5, 2.7, 4.6])

    doc.add_paragraph()

    score_rows = [
        ('评分内容', '具 体 要 求', '总分', '评分'),
        ('平时表现', '努力学习，遵守纪律；有一定的动手能力和计算机应用能力。', '20', ''),
        ('设计创新', '设计方案良好；功能建模正确；界面友好。', '20', ''),
        ('验收答辩', '运行结果合理；能准确有条理地回答各种问题，实物操作演示顺利。', '30', ''),
        ('设计报告', '报告书撰写规范。', '30', ''),
        ('总评分', '', '100', ''),
        ('评    语', '综合考虑，给予该课程总评成绩为：', '', ''),

    ]
    tbl = doc.add_table(rows=len(score_rows), cols=4)
    tbl.style = 'Table Grid'
    tbl.alignment = WD_TABLE_ALIGNMENT.CENTER
    tbl.autofit = False
    set_col_widths(tbl, [2.5, 7.5, 1.8, 2.8])

    # 官方表末尾还有"评分日期"与"注"，以及两位评分教师的职称/单位行
    doc.add_paragraph()
    tail = [
        ('评分教师', '姓    名', '职    称', '所 在 单 位', '电 子 签 名'),
        ('', '＿＿＿', '＿＿＿', '＿＿＿＿＿＿＿＿', ''),
        ('', '＿＿＿', '讲  师', '＿＿＿＿＿＿＿＿', ''),
    ]
    tt = doc.add_table(rows=len(tail), cols=5)
    tt.style = 'Table Grid'
    tt.alignment = WD_TABLE_ALIGNMENT.CENTER
    tt.autofit = False
    set_col_widths(tt, [2.2, 2.6, 2.4, 5.4, 3.0])
    for i, row in enumerate(tail):
        for j, val in enumerate(row):
            par = tt.rows[i].cells[j].paragraphs[0]
            par.paragraph_format.line_spacing = 1.25
            run = par.add_run(val)
            run_font(run, EN_FONT, CN_BODY, TABLE_SIZE, bold=(i == 0))
    pn = doc.add_paragraph()
    pn.paragraph_format.line_spacing = 1.25
    pn.add_run('注：小组中标明小组成员及其分工。').font.size = Pt(9)
    pd_ = doc.add_paragraph()
    pd_.alignment = WD_ALIGN_PARAGRAPH.RIGHT
    pd_.paragraph_format.line_spacing = 1.25
    pd_.add_run('2026 年 11 月 7 日')

    for i, row in enumerate(score_rows):
        for j, val in enumerate(row):
            cell = tbl.rows[i].cells[j]
            par = cell.paragraphs[0]
            par.paragraph_format.line_spacing = 1.25
            par.alignment = (WD_ALIGN_PARAGRAPH.CENTER if j != 1 or i == 0
                             else WD_ALIGN_PARAGRAPH.LEFT)
            run = par.add_run(val)
            run_font(run, EN_FONT, CN_BODY, TABLE_SIZE, bold=(i == 0 or j == 0))
    # 总评分与评语行的合并
    tbl.rows[5].cells[1].merge(tbl.rows[5].cells[2])
    tbl.rows[6].cells[1].merge(tbl.rows[6].cells[3])

    note = doc.add_paragraph()
    note.paragraph_format.line_spacing = 1.25
    note.paragraph_format.space_before = Pt(6)
    rn = note.add_run('注：本设计由本人单人独立完成，无其他组员，故评分表共 1 份。')
    run_font(rn, EN_FONT, CN_BODY, TABLE_SIZE)


# ------------------------------------------------------------------ 正文渲染
def add_image(doc, path: Path):
    if not path.exists():
        p = doc.add_paragraph()
        r = p.add_run(f'[图片缺失：{path.name}]')
        run_font(r, EN_FONT, CN_BODY, CAPTION_SIZE, color=RGBColor(0xC0, 0, 0))
        return
    with Image.open(path) as im:
        w, h = im.size
    aspect = h / w
    width_cm = CONTENT_W_CM
    if aspect > 1.05:                       # 偏高的图：按页高限制，占满整页高度以保证可读
        width_cm = min(width_cm, 22.0 / aspect)
    p = doc.add_paragraph()
    p.alignment = WD_ALIGN_PARAGRAPH.CENTER
    p.paragraph_format.space_before = Pt(6)
    p.paragraph_format.space_after = Pt(2)
    p.paragraph_format.line_spacing = 1.0
    p.add_run().add_picture(str(path), width=Cm(width_cm))


def add_caption(doc, text, below=False):
    p = doc.add_paragraph()
    p.alignment = WD_ALIGN_PARAGRAPH.CENTER
    p.paragraph_format.line_spacing = 1.0
    p.paragraph_format.space_before = Pt(2 if below else 6)
    p.paragraph_format.space_after = Pt(8 if below else 3)
    r = p.add_run(text)
    run_font(r, EN_FONT, CN_BODY, CAPTION_SIZE, True)


def add_code_block(doc, code: str):
    tbl = doc.add_table(rows=1, cols=1)
    tbl.style = 'Table Grid'
    cell = tbl.rows[0].cells[0]
    shade(cell, 'F7F7F7')
    lines = code.rstrip('\n').split('\n')
    for idx, ln in enumerate(lines):
        par = cell.paragraphs[0] if idx == 0 else cell.add_paragraph()
        par.paragraph_format.line_spacing = 1.0
        par.paragraph_format.space_after = Pt(0)
        r = par.add_run(ln if ln else ' ')
        run_font(r, MONO, MONO, CODE_SIZE)
    doc.add_paragraph().paragraph_format.space_after = Pt(2)


def _visual_len(s: str) -> float:
    """粗略视觉长度：中文按 1、西文按 0.55 计"""
    return sum(1.0 if ord(ch) > 127 else 0.55 for ch in (s or ''))


def set_col_widths(table, widths_cm):
    """固定列宽（Word/LibreOffice 都需要 tblLayout=fixed 才听话）"""
    tbl = table._tbl
    tblPr = tbl.tblPr
    layout = OxmlElement('w:tblLayout')
    layout.set(qn('w:type'), 'fixed')
    tblPr.append(layout)
    for j, w in enumerate(widths_cm):
        if j < len(table.columns):
            table.columns[j].width = Cm(w)
    for row in table.rows:
        for j, w in enumerate(widths_cm):
            if j < len(row.cells):
                row.cells[j].width = Cm(w)


def _token_cm(s: str) -> float:
    """列内最长不可断片段的宽度（cm），用于保证"NFR-01"这类标识符不被折断"""
    tokens = re.split(r'[\s、，,；;（）()/]+', s or '')
    best = 0.0
    for tk in tokens:
        if not tk:
            continue
        width = sum(0.38 if ord(ch) > 127 else 0.24 for ch in tk)
        best = max(best, width)
    return best + 0.45      # 单元格内边距


def compute_widths(rows, ncols, total_cm=CONTENT_W_CM, min_cm=1.6):
    """按各列内容长度分配列宽，同时保证标识符不被折断"""
    if ncols == 1:
        return [total_cm]
    raw, floors = [], []
    for j in range(ncols):
        lens = sorted(_visual_len(r[j]) for r in rows if j < len(r))
        p80 = lens[max(0, int(len(lens) * 0.8) - 1)] if lens else 4.0
        header = _visual_len(rows[0][j]) if j < len(rows[0]) else 0.0
        raw.append(max(p80, header, 4.0))
        floors.append(max(min_cm, min(4.0, max(_token_cm(r[j]) for r in rows
                                               if j < len(r)))))
    total_raw = sum(raw)
    widths = [r / total_raw * total_cm for r in raw]
    widths = [max(w, f) for w, f in zip(widths, floors)]
    # 超额则按比例压缩可压缩列，不足则等比放大
    total = sum(widths)
    if total > total_cm:
        flexible = [i for i, (w, f) in enumerate(zip(widths, floors)) if w > f]
        excess = total - total_cm
        pool = sum(widths[i] - floors[i] for i in flexible)
        if pool > 0 and pool >= excess:
            for i in flexible:
                widths[i] -= excess * (widths[i] - floors[i]) / pool
        else:
            # 列多或标识符长到最小宽度之和已超过页宽：保底不可行，放弃保底整体等比压缩
            # （早期实现此处会算出负列宽并使 docx 写入失败）
            widths = [w * total_cm / total for w in widths]
    # 最终兜底：不留非正值，并把总宽精确归一到页宽
    widths = [max(0.9, w) for w in widths]
    final = sum(widths)
    if final > 0:
        widths = [w * total_cm / final for w in widths]
    return widths


def add_table(doc, rows: list[list[str]]):
    if not rows:
        return
    ncols = max(len(r) for r in rows)
    tbl = doc.add_table(rows=len(rows), cols=ncols)
    tbl.style = 'Table Grid'
    tbl.alignment = WD_TABLE_ALIGNMENT.CENTER
    tbl.autofit = False
    set_col_widths(tbl, compute_widths(rows, ncols))
    for i, row in enumerate(rows):
        for j in range(ncols):
            cell = tbl.rows[i].cells[j]
            text = row[j] if j < len(row) else ''
            par = cell.paragraphs[0]
            par.paragraph_format.line_spacing = 1.0
            par.paragraph_format.space_after = Pt(0)
            par.alignment = WD_ALIGN_PARAGRAPH.CENTER if (i == 0 or len(text) <= 6) \
                else WD_ALIGN_PARAGRAPH.LEFT
            add_inline(par, text, TABLE_SIZE, base_bold=(i == 0))
            if i == 0:
                shade(cell, 'EAF1FB')
    # 表头跨页重复
    tr_pr = tbl.rows[0]._tr.get_or_add_trPr()
    tbl_header = OxmlElement('w:tblHeader')
    tbl_header.set(qn('w:val'), 'true')
    tr_pr.append(tbl_header)
    doc.add_paragraph().paragraph_format.space_after = Pt(2)


TABLE_CAP_RE = re.compile(r'^\*\*(表\s?\d+\.\d+.*)\*\*$')
FIG_CAP_RE = re.compile(r'^\*\*(图\s?\d+\.\d+.*)\*\*$')
IMG_RE = re.compile(r'^!\[[^\]]*\]\(([^)]+)\)\s*$')


def render_chapter(doc, md_path: Path, page_break_before=True):
    if page_break_before:
        doc.add_paragraph().add_run().add_break(WD_BREAK.PAGE)
    lines = md_path.read_text(encoding='utf-8').split('\n')
    i = 0
    while i < len(lines):
        raw = lines[i]
        line = raw.rstrip()

        if not line.strip() or line.strip() == '---':
            i += 1
            continue

        # 代码块
        if line.startswith('```'):
            i += 1
            buf = []
            while i < len(lines) and not lines[i].startswith('```'):
                buf.append(lines[i])
                i += 1
            i += 1
            add_code_block(doc, '\n'.join(buf))
            continue

        # 图片
        m = IMG_RE.match(line.strip())
        if m:
            target = (md_path.parent / m.group(1)).resolve()
            add_image(doc, target)
            i += 1
            continue

        # 图题 / 表题
        s = line.strip()
        if FIG_CAP_RE.match(s):
            add_caption(doc, FIG_CAP_RE.match(s).group(1), below=True)
            i += 1
            continue
        if TABLE_CAP_RE.match(s):
            add_caption(doc, TABLE_CAP_RE.match(s).group(1), below=False)
            i += 1
            continue

        # 表格
        if s.startswith('|'):
            block = []
            while i < len(lines) and lines[i].strip().startswith('|'):
                block.append(lines[i].strip())
                i += 1
            rows = []
            for k, row in enumerate(block):
                if re.match(r'^\|[\s:\-\|]+\|$', row):
                    continue                        # 分隔行
                cells = [c.strip() for c in row.strip().strip('|').split('|')]
                rows.append(cells)
            add_table(doc, rows)
            continue

        # 标题
        if s.startswith('#'):
            level = len(s) - len(s.lstrip('#'))
            text = s.lstrip('#').strip()
            p = doc.add_heading(level=min(level, 3))
            p.paragraph_format.line_spacing = 1.25
            if level == 1:
                p.alignment = WD_ALIGN_PARAGRAPH.LEFT
            run = p.add_run(text)
            size = {1: Pt(16), 2: Pt(14), 3: Pt(12)}[min(level, 3)]
            run_font(run, EN_FONT, CN_HEAD if level <= 2 else CN_BODY, size, True)
            i += 1
            continue

        # 列表
        if re.match(r'^(\s*)([-*]|\d+[.)])\s+', line):
            text = re.sub(r'^(\s*)([-*]|\d+[.)])\s+', '', line)
            p = doc.add_paragraph()
            p.paragraph_format.line_spacing = 1.25
            p.paragraph_format.left_indent = Cm(0.74)
            p.paragraph_format.first_line_indent = Cm(-0.37)
            prefix = '· ' if re.match(r'^\s*[-*]', line) else \
                re.match(r'^\s*(\d+[.)])', line).group(1) + ' '
            r = p.add_run(prefix)
            run_font(r, EN_FONT, CN_BODY, BODY_SIZE)
            add_inline(p, text, BODY_SIZE)
            i += 1
            continue

        # 引用块（图源说明等）
        if s.startswith('>'):
            text = s.lstrip('> ').strip()
            p = doc.add_paragraph()
            p.paragraph_format.line_spacing = 1.25
            p.paragraph_format.left_indent = Cm(0.74)
            add_inline(p, text, CAPTION_SIZE)
            i += 1
            continue

        # 参考文献条目：左对齐 + 悬挂缩进（长 URL 会被两端对齐拉散，故不走两端对齐）
        if re.match(r'^\[\d+\]', s):
            p = doc.add_paragraph()
            p.paragraph_format.line_spacing = 1.25
            p.paragraph_format.left_indent = Cm(0.9)
            p.paragraph_format.first_line_indent = Cm(-0.9)
            p.paragraph_format.space_after = Pt(0)
            p.alignment = WD_ALIGN_PARAGRAPH.LEFT
            add_inline(p, s, BODY_SIZE)
            i += 1
            continue

        # 普通段落
        p = doc.add_paragraph()
        p.paragraph_format.line_spacing = 1.25
        p.paragraph_format.first_line_indent = Pt(24)     # 首行缩进 2 字符
        p.paragraph_format.space_after = Pt(0)
        p.alignment = WD_ALIGN_PARAGRAPH.JUSTIFY
        add_inline(p, s, BODY_SIZE)
        i += 1


# ------------------------------------------------------------------ 主流程
def main() -> None:
    doc = Document()

    # 页面设置：A4，上下 2.54cm、左右 3.17cm
    sec = doc.sections[0]
    sec.page_width = Cm(21.0)
    sec.page_height = Cm(29.7)
    sec.top_margin = Cm(2.54)
    sec.bottom_margin = Cm(2.54)
    sec.left_margin = Cm(3.17)
    sec.right_margin = Cm(3.17)

    # Normal 样式：小四宋体 + Times New Roman，1.25 倍行距
    normal = doc.styles['Normal']
    style_font(normal, EN_FONT, CN_BODY, BODY_SIZE)
    normal.paragraph_format.line_spacing = 1.25

    # 标题样式：黑色，不用 Word 默认蓝色主题色
    for lvl, size, east in ((1, Pt(16), CN_HEAD), (2, Pt(14), CN_HEAD),
                            (3, Pt(12), CN_BODY)):
        st = doc.styles[f'Heading {lvl}']
        style_font(st, EN_FONT, east, size, True)
        st.paragraph_format.line_spacing = 1.25
        st.paragraph_format.space_before = Pt(12 if lvl > 1 else 6)
        st.paragraph_format.space_after = Pt(6)

    build_official_front(doc)
    # 封面后换页，接成绩评定表（正文接在封面与评定表之后）
    doc.add_paragraph().add_run().add_break(WD_BREAK.PAGE)
    pass  # 评分表已随官方封面一起搬入

    for name in CHAPTER_FILES:
        path = CH_DIR / name
        if not path.exists():
            raise SystemExit(f'缺少章节文件：{path}')
        render_chapter(doc, path)

    # 页脚页码：底部居中，小五 Times New Roman
    add_page_number(sec.footer.paragraphs[0])

    OUT.parent.mkdir(parents=True, exist_ok=True)
    doc.save(str(OUT))
    print(f'已生成：{OUT}')


if __name__ == '__main__':
    main()
