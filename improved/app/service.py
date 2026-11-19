from __future__ import annotations
import csv
import html
import io
import json
import math
import re
import shutil
import threading
import tempfile
import uuid
import zipfile
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path
from typing import Callable

import pdfplumber
from extract_requirements import (DEFAULT_ID_PATTERN, FIELD_NAMES, MATCH_FIELDS, clean,
    compile_query, effective_page_bbox, extract_pdf, write_csv)
import compare_matches as comparator
from task_control import TaskCancelled, check_cancel

DEFAULTS = {
    'field': '', 'query': '', 'same_filter': True, 'field_b': '', 'query_b': '',
    'id_pattern': DEFAULT_ID_PATTERN, 'heading_indent': 36, 'no_tables': False,
    'top_margin_a': 0, 'bottom_margin_a': 0, 'top_margin_b': 0, 'bottom_margin_b': 0,
    'remove_repeated': False, 'auto_separators': False, 'margin_zone': 10, 'repeat_ratio': 60,
    'separator_zone': 20, 'separator_min_width': 55, 'separator_mode': 'outermost',
    'ignore_whitespace': False, 'output_root': 'outputs', 'task_name': 'pdf_compare',
    'open_results': False,
}
LEGACY_ID_PATTERN = r'AT\s*-\s*[A-Za-z]\d+\s*-\s*\d+'
PDFIUM_LOCK = threading.RLock()


class ComparisonRecoveryError(OSError):
    """A failed rollback retained its backup directory for manual recovery."""


def checked_config(raw: dict) -> dict:
    if not isinstance(raw, dict):
        raise ValueError('设置必须是一个 JSON 对象。')
    unknown = set(raw) - set(DEFAULTS)
    if unknown:
        raise ValueError('未知设置：' + ', '.join(sorted(unknown)))
    c = {**DEFAULTS, **raw}
    for key in ('same_filter','no_tables','remove_repeated','auto_separators','ignore_whitespace','open_results'):
        if not isinstance(c[key], bool):
            raise ValueError(key + ' 必须为开关值。')
    for key in ('heading_indent','top_margin_a','bottom_margin_a','top_margin_b','bottom_margin_b','margin_zone','repeat_ratio','separator_zone','separator_min_width'):
        if isinstance(c[key], bool):
            raise ValueError(key + ' 必须为数字。')
        try:
            c[key] = float(c[key])
        except (TypeError, ValueError):
            raise ValueError(key + ' 必须为数字。') from None
        if not math.isfinite(c[key]) or c[key] < 0:
            raise ValueError(key + ' 必须为非负有限数字。')
    if not 1 <= c['margin_zone'] <= 20 or not 1 <= c['repeat_ratio'] <= 100:
        raise ValueError('页边识别区域须为 1–20%，重复比例须为 1–100%。')
    if not 5 <= c['separator_zone'] <= 35:
        raise ValueError('横线搜索范围须为页顶和页底各 5–35%。')
    if not 30 <= c['separator_min_width'] <= 95:
        raise ValueError('分隔横线最小长度须为页宽的 30–95%。')
    if c['separator_mode'] not in ('conservative', 'outermost'):
        raise ValueError('横线识别模式须为 conservative 或 outermost。')
    for key in ('field','query','field_b','query_b','id_pattern','output_root','task_name'):
        if not isinstance(c[key], str) or '\x00' in c[key]:
            raise ValueError(key + ' 必须是文本。')
    # Upgrade only the previous built-in, including old exported settings.
    # A genuinely custom expression is kept byte-for-byte.
    if c['id_pattern'] == LEGACY_ID_PATTERN:
        c['id_pattern'] = DEFAULT_ID_PATTERN
    c['field'] = c['field'].strip()
    c['field_b'] = c['field_b'].strip()
    if not c['field'] or not c['query'].strip():
        raise ValueError('请填写查询字段和查询内容。')
    compile_query(c['query'])
    if c['same_filter']:
        c['field_b'], c['query_b'] = c['field'], c['query']
    elif not c['field_b'].strip() or not c['query_b'].strip():
        raise ValueError('请填写 PDF B 的查询字段和查询内容。')
    compile_query(c['query_b'])
    try:
        p = re.compile(c['id_pattern'])
        re.compile(r'^\s*(?P<id>' + c['id_pattern'] + r')\s*[-:]\s*(?P<title>\S.*)$', re.I)
    except re.error as exc:
        raise ValueError('编号正则表达式无效：' + str(exc)) from None
    if p.match(''):
        raise ValueError('编号正则表达式不能匹配空编号。')
    if not c['output_root'].strip():
        raise ValueError('请填写结果保存位置。')
    if not c['task_name'].strip() or len(c['task_name']) > 80 or re.search(r'[<>:"/\\|?*\x00-\x1f]',c['task_name']):
        raise ValueError('任务名称须为 1–80 个字符，且不能包含文件夹分隔符等文件名特殊字符。')
    if re.fullmatch(r'(?:CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\..*)?', c['task_name'].strip(), re.I):
        raise ValueError('任务名称是 Windows 保留名称，请换一个名称。')
    return c


STYLE = """
:root{font-family:'Segoe UI','Microsoft YaHei',system-ui,sans-serif;color:#172e35;background:#f5f7f8}*{box-sizing:border-box}body{margin:0}main{max-width:1450px;margin:auto;padding:28px 32px 64px}h1{font-size:26px;letter-spacing:-.5px;margin:8px 0 12px}p{line-height:1.6}a{color:#00796d;text-decoration:none}a:hover{text-decoration:underline}.eyebrow{font-size:11px;letter-spacing:2px;font-weight:700;color:#008274}.sub{font-size:13px;color:#64787d}.bar{display:flex;align-items:center;justify-content:space-between;gap:16px;flex-wrap:wrap}.button,button,input{font:inherit;border:1px solid #ccd9dc;border-radius:8px;background:#fff;color:inherit;padding:9px 13px}.button{display:inline-block;font-size:13px}button{cursor:pointer}input{width:min(440px,100%);outline-color:#009b88}table{width:100%;border-collapse:separate;border-spacing:0;background:white;border:1px solid #dce5e6;border-radius:12px;overflow:hidden;margin-top:20px}th{font-size:12px;color:#536d74;background:#edf3f3;position:sticky;top:0;z-index:1}th,td{padding:15px 17px;text-align:left;vertical-align:top;border-bottom:1px solid #e4ebed}th:first-child{width:155px}th:nth-child(2){width:30%}td{white-space:pre-wrap;overflow-wrap:anywhere;font-size:13px;line-height:1.75}td:first-child{font-weight:650;color:#087d70}tr:last-child td{border:0}.warn{background:#fff7df;border:1px solid #e9d490;padding:12px 16px;border-radius:9px;font-size:13px;line-height:1.6}.empty{padding:42px;text-align:center;color:#6c7e83}.count{font-size:13px;padding:6px 10px;border-radius:6px;background:#e1f4ee;color:#00765d}.tools{display:flex;gap:10px;align-items:center;flex-wrap:wrap;margin:20px 0}details{margin:14px 0}summary{cursor:pointer;color:#9b7018}.meta{font-size:12px;color:#6a7a7f}iframe{width:100%;height:74vh;border:1px solid #dce5e6;border-radius:12px;background:white}.tabs{display:flex;gap:10px;margin:22px 0 12px}.tabs button.active{background:#087d70;color:white;border-color:#087d70}[hidden]{display:none!important}@media(max-width:720px){main{padding:20px 12px}th,td{padding:12px 9px}h1{font-size:22px}}@media print{.tools,.button{display:none}main{padding:0}}
"""


def page(title: str, body: str, script: str = '') -> str:
    return '<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>' + html.escape(title) + '</title><style>' + STYLE + '</style></head><body><main>' + body + '</main><script>' + script + '</script></body></html>'


def field_resolution(item: dict) -> str:
    """A missing field is unknown, rather than a confirmed filter rejection."""
    status = item.get('field_status')
    if status in ('missing', 'empty'):
        return status
    return 'resolved' if str(item.get('field_value', '')).strip() else 'missing'


def annotate_extraction(report: dict) -> dict:
    """Carry item-level uncertainty into the same notices shown by the UI.

    CSV filtering still includes only positive matches. Coverage counts explain
    which other items were rejected and which could not be evaluated at all.
    """
    items = report['items']
    unresolved = [item for item in items if field_resolution(item) != 'resolved']
    coverage = {
        'matched_count': sum(bool(item['matched']) for item in items),
        'rejected_count': sum(not item['matched'] and field_resolution(item) == 'resolved' for item in items),
        'unresolved_count': len(unresolved),
        'noted_item_count': sum(bool(item.get('notes')) for item in items),
    }
    warnings = list(report.get('warnings', []))
    if unresolved:
        ids = ', '.join(item['item_id'] for item in unresolved[:5])
        if len(unresolved) > 5:
            ids += ' 等'
        warnings.append(f"{len(unresolved)} 条需求的查询字段未提取到或值为空，未能判断是否命中；不能视为明确未命中。编号：{ids}。")
    grouped_notes: dict[str, list[str]] = {}
    for item in items:
        for note in dict.fromkeys(item.get('notes', [])):
            grouped_notes.setdefault(note, []).append(item['item_id'])
    for note, ids in grouped_notes.items():
        examples = ', '.join(ids[:5]) + (' 等' if len(ids) > 5 else '')
        warnings.append(f'{len(ids)} 条需求需核对：{note}（编号：{examples}）')
    report['warnings'] = list(dict.fromkeys(warnings))
    report['coverage'] = coverage
    return coverage


def extracted_records(report_path: Path, source_name: str, *, cancel_check=None) -> comparator.CsvData:
    """Compare preserved extraction text, never spreadsheet-escaped CSV cells."""
    check_cancel(cancel_check)
    report = json.loads(report_path.read_text(encoding='utf-8'))
    check_cancel(cancel_check)
    records: dict[str, list[comparator.Record]] = {}
    # Logical row numbers continue to correspond to the downloadable matches CSV.
    for row, item in enumerate((item for item in report['items'] if item['matched']), 2):
        check_cancel(cancel_check)
        record = comparator.Record(item['item_id'], item['title'], item['description'], row)
        records.setdefault(record.item_id, []).append(record)
    return comparator.CsvData(Path(source_name), 'extraction-json', records)


def literal_pdf_pattern(value: str) -> re.Pattern:
    compact = re.sub(r'\s+', '', clean(value))
    if not compact:
        raise ValueError('请输入要在原文中查找的编号或文字。')
    if len(compact) > 500:
        raise ValueError('查找文字请控制在 500 个字符以内。')
    return re.compile(r'\s*'.join(map(re.escape, compact)), re.I)


def searchable_pages(path: Path, *, cancel_check=None):
    """Independent PDFium text decoder; never claim that image pages were OCRed."""
    import pypdfium2 as pdfium
    check_cancel(cancel_check)
    # Poll while waiting for preview/search use of the global PDFium lock.
    while not PDFIUM_LOCK.acquire(timeout=.1):
        check_cancel(cancel_check)
    try:
        check_cancel(cancel_check)
        with pdfium.PdfDocument(str(path)) as document:
            yield from _document_text_pages(document, cancel_check=cancel_check)
    finally:
        PDFIUM_LOCK.release()


def _document_text_pages(document, *, cancel_check=None):
    for index in range(len(document)):
        check_cancel(cancel_check)
        page = document[index]
        try:
            textpage = page.get_textpage()
            try:
                text = textpage.get_text_range()
            finally:
                textpage.close()
            check_cancel(cancel_check)
            yield index + 1, text
        finally:
            page.close()

def comparison_audit(reports: list[dict], a: comparator.CsvData, b: comparator.CsvData,
                     documents: list[dict], *, cancel_check=None) -> list[dict]:
    """Explain why a record is absent from the opposite filtered extraction."""
    check_cancel(cancel_check)
    audit = []
    for present, other, other_report, document, missing_side in (
            (a, b, reports[1], documents[1], 'B'),
            (b, a, reports[0], documents[0], 'A')):
        indexes = {}
        for item in other_report['items']:
            check_cancel(cancel_check)
            indexes.setdefault(item['item_id'], []).append(item)
        unresolved = []
        for identifier in present.records:
            check_cancel(cancel_check)
            if identifier in other.records:
                continue
            found = indexes.get(identifier, [])
            row = {'item_id': identifier, 'missing_side': missing_side,
                   'pages': sorted({p for item in found for p in item['pages']}),
                   'field_values': [item['field_value'] for item in found], 'reason': ''}
            if found:
                unknown = any(field_resolution(item) != 'resolved' for item in found)
                row['reason'] = (f'{missing_side} 已识别该编号，但查询字段未提取到或值为空，未能判断是否命中。'
                                 if unknown else f'{missing_side} 已识别该编号，但字段原文未满足本次查询条件。')
                if row['pages']:
                    row['reason'] += ' PDF 页码：' + ', '.join(map(str, row['pages'])) + '。'
            else:
                row['reason'] = f'{missing_side} 未识别到该需求条目；这不代表原文不存在。'
                unresolved.append(row)
            audit.append(row)
        if unresolved:
            patterns = [(row, re.compile(r'(?<!\w)' + literal_pdf_pattern(row['item_id']).pattern + r'(?!\w)', re.I))
                        for row in unresolved]
            try:
                for number, text in searchable_pages(document['path'], cancel_check=cancel_check):
                    normalized_text = clean(text)
                    for row, pattern in patterns:
                        check_cancel(cancel_check)
                        if pattern.search(normalized_text):
                            row['pages'].append(number)
                for row in unresolved:
                    if row['pages']:
                        row['reason'] += ' 独立文字检索在第 ' + ', '.join(map(str, row['pages'])) + ' 页发现该编号，可能为正文或引用，请打开原文核对。'
            except TaskCancelled:
                raise
            except Exception as exc:
                for row in unresolved:
                    row['reason'] += f' 独立文字检索未完成（{type(exc).__name__}），请人工核对。'
    return audit


def extraction_page(side: str, name: str, matches: list[dict], report: dict, config: dict, *, cancel_check=None) -> str:
    e = html.escape
    field, query = (config['field'], config['query']) if side == 'A' else (config['field_b'], config['query_b'])
    body = '<div class="bar"><div><div class="eyebrow">EXTRACTED REQUIREMENTS · ' + side + '</div><h1>' + side + ' · ' + e(name) + '</h1></div><a class="button" href="matches.csv" download>下载 matches.csv</a></div>'
    body += '<p class="sub">在 <strong>' + e(field) + '</strong> 中查询 <strong>' + e(query) + '</strong></p>'
    body += '<div class="tools"><input id="search" type="search" placeholder="搜索编号、标题或描述" aria-label="搜索提取结果"><span class="count">命中 ' + str(len(matches)) + ' 条 / 识别 ' + str(len(report['items'])) + ' 条</span><span id="visible" class="sub"></span></div>'
    coverage = report.get('coverage', {})
    body += '<p class="sub">明确未命中 ' + str(coverage.get('rejected_count', 0)) + ' 条；查询字段未提取到或值为空、未能判定 ' + str(coverage.get('unresolved_count', 0)) + ' 条。比较范围仅包含已提取且命中的条目。</p>'
    if not matches:
        body += '<p class="warn">未提取到匹配条目。请核对查询条件、编号规则、裁切范围和 PDF 文字层。此结果不能证明原文没有相应需求。</p>'
    if report['warnings']:
        body += '<details open><summary>提取提示（' + str(len(report['warnings'])) + '）</summary><ul>' + ''.join('<li class="sub">' + e(w) + '</li>' for w in report['warnings']) + '</ul></details>'
    body += '<table><thead><tr><th>编号</th><th>标题</th><th>具体描述</th></tr></thead><tbody>'
    for item in matches:
        check_cancel(cancel_check)
        body += '<tr class="record">' + ''.join('<td>' + e(item[k]) + '</td>' for k in ('item_id','title','description')) + '</tr>'
    if not matches:
        body += '<tr><td colspan="3" class="empty">没有匹配结果</td></tr>'
    body += '</tbody></table><p class="meta">CSV 固定为编号、标题、具体描述三列；多段文字保留在同一单元格。</p><p class="sub"><a href="all_items.csv" download>下载全部识别条目（含页码与字段原文）</a>　·　<a href="extraction_report.json" download>下载提取记录</a></p>'
    script = "const rows=[...document.querySelectorAll('tr.record')];document.getElementById('search').addEventListener('input',function(){let n=0;for(const row of rows){row.hidden=!row.textContent.toLocaleLowerCase().includes(this.value.toLocaleLowerCase());if(!row.hidden)n++;}document.getElementById('visible').textContent='显示 '+n+' 条';});"
    return page(side + ' 提取结果',body,script)


def tables_page(names: list[str]) -> str:
    e = html.escape
    body = '<div class="eyebrow">PDF COMPARISON · STEP 02</div><h1>两份 PDF 的提取结果</h1><p class="sub">分别查看三列表格，或在独立标签页打开。下载的 CSV 可用电子表格软件打开。</p>'
    body += '<div class="bar"><div class="tabs"><button class="active" data-side="A">A · ' + e(names[0]) + '</button><button data-side="B">B · ' + e(names[1]) + '</button></div><div><a class="button" href="A/matches.html" target="_blank" rel="noopener">独立打开 A</a> <a class="button" href="B/matches.html" target="_blank" rel="noopener">独立打开 B</a></div></div><iframe id="table" title="提取结果表格" src="A/matches.html"></iframe><p class="sub">回到工具主页，点击「生成并打开比较报告」，完成第三步。</p>'
    script = "for(const button of document.querySelectorAll('[data-side]'))button.onclick=()=>{for(const b of document.querySelectorAll('[data-side]'))b.classList.toggle('active',b===button);document.getElementById('table').src=button.dataset.side+'/matches.html';};"
    return page('PDF 提取结果',body,script)


class Workspace:
    def __init__(self, root: Path):
        self.root = root.resolve()
        self.temp = self.root / '.work'
        self.temp.mkdir(parents=True, exist_ok=True)
        self.documents: dict[str,dict] = {}
        self.runs: dict[str,dict] = {}
        self.jobs: dict[str,dict] = {}
        self.lock = threading.RLock()
        self.pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix='pdf-worker')

    def register_document(self, stream, length: int, name: str, *, cancel_check=None) -> dict:
        check_cancel(cancel_check)
        if not 1 <= length <= 200 * 1024 * 1024:
            raise ValueError('请选择不超过 200 MB 的 PDF 文件。')
        filename = name.replace('\\','/').split('/')[-1]
        if not filename.lower().endswith('.pdf'):
            raise ValueError('请选择 PDF 文件。')
        identifier = uuid.uuid4().hex
        path = self.temp / (identifier + '.pdf')
        try:
            with path.open('wb') as out:
                remaining = length
                while remaining:
                    check_cancel(cancel_check)
                    block = stream.read(min(1024*1024,remaining))
                    if not block:
                        raise ValueError('文件上传未完成，请重试。')
                    check_cancel(cancel_check)
                    out.write(block)
                    remaining -= len(block)
            check_cancel(cancel_check)
            with path.open('rb') as f:
                if b'%PDF-' not in f.read(1024):
                    raise ValueError('文件内容不是有效的 PDF。')
            with pdfplumber.open(path) as pdf:
                if not pdf.pages:
                    raise ValueError('PDF 没有页面。')
                first = pdf.pages[0]
                visible = effective_page_bbox(first)
                check_cancel(cancel_check)
                document = {'id':identifier,'name':filename,'path':path,'size':length,
                            'page_count':len(pdf.pages),'width':visible[2]-visible[0],'height':visible[3]-visible[1],
                            'has_text':bool(first.crop(visible).chars)}
            check_cancel(cancel_check)
        except Exception as exc:
            path.unlink(missing_ok=True)
            if isinstance(exc,(ValueError,TaskCancelled)):
                raise
            raise ValueError('无法打开 PDF；请检查文件是否损坏或受到密码保护。') from exc
        with self.lock:
            self.documents[identifier] = document
        return {k:v for k,v in document.items() if k != 'path'}

    def document(self, identifier: str) -> dict:
        with self.lock:
            if identifier not in self.documents:
                raise ValueError('文件已失效，请重新选择 PDF。')
            return self.documents[identifier]

    def search_document(self, identifier: str, query: str, progress=lambda *args: None, *, cancel_check=None) -> dict:
        check_cancel(cancel_check)
        document = self.document(identifier)
        pattern = literal_pdf_pattern(query)
        hits, scanned_pages = [], []
        for number, text in searchable_pages(document['path'], cancel_check=cancel_check):
            progress(f'查找原文：第 {number} / {document["page_count"]} 页',
                     100 * number / max(document['page_count'], 1))
            check_cancel(cancel_check)
            normalized_text = clean(text)
            if not normalized_text.strip():
                scanned_pages.append(number)
                continue
            matches = []
            for match in pattern.finditer(normalized_text):
                check_cancel(cancel_check)
                matches.append(match)
            if matches:
                excerpts = [normalized_text[max(0, match.start()-140):match.end()+260]
                            for match in matches[:10]]
                hits.append({'page': number, 'text': '\n\n…\n\n'.join(excerpts),
                             'occurrences': len(matches)})
        check_cancel(cancel_check)
        return {'query': query, 'hits': hits, 'scanned_pages': scanned_pages,
                'note': '检索范围为整份 PDF 的文字层（不受本次裁切或字段筛选影响）。编号可能出现在引用中；没有文字层的页面无法用此功能检索。'}

    def submit(self, action: Callable[[Callable],dict]) -> str:
        identifier = uuid.uuid4().hex
        with self.lock:
            self.jobs[identifier] = {'id':identifier,'state':'queued','message':'任务已排队','progress':0}
        def update(message: str, progress: float = 0):
            with self.lock:
                self.jobs[identifier].update(state='running',message=message,progress=round(progress))
        def work():
            try:
                update('开始处理',0)
                result = action(update)
                with self.lock:
                    self.jobs[identifier].update(state='done',message='处理完成',progress=100,result=result)
            except TaskCancelled:
                with self.lock:
                    self.jobs[identifier].update(state='cancelled',message='任务已中止')
            except Exception as exc:
                with self.lock:
                    self.jobs[identifier].update(state='error',message=str(exc),error_type=type(exc).__name__)
        self.pool.submit(work)
        return identifier

    def extract(self, a_id: str, b_id: str, raw: dict, update=lambda *args:None, *, cancel_check=None) -> dict:
        check_cancel(cancel_check)
        c = checked_config(raw)
        documents = [self.document(a_id), self.document(b_id)]
        run_id = uuid.uuid4().hex
        output = Path(c['output_root'].strip()).expanduser()
        if not output.is_absolute():
            output = self.root / output
        output.mkdir(parents=True,exist_ok=True)
        label = c['task_name'].strip().rstrip(' .') or 'pdf_compare'
        run_dir = output / (label + '_' + datetime.now().strftime('%Y%m%d_%H%M%S') + '_' + run_id[:6])
        run_dir.mkdir()
        try:
            details = []
            for i,(side,document) in enumerate(zip(('A','B'),documents)):
                check_cancel(cancel_check)
                update('分析 PDF ' + side + ' 的版面和页眉页脚',i*45)
                directory = run_dir / side
                directory.mkdir()
                field,query = (c['field'],c['query']) if side=='A' else (c['field_b'],c['query_b'])
                report = extract_pdf(document['path'],compile_query(query),field_name=field,
                    id_pattern=c['id_pattern'],heading_indent=c['heading_indent'],no_tables=c['no_tables'],
                    top_margin=c['top_margin_'+side.lower()],bottom_margin=c['bottom_margin_'+side.lower()],
                    remove_repeated=c['remove_repeated'],auto_separators=c['auto_separators'],
                    margin_zone=c['margin_zone'],repeat_ratio=c['repeat_ratio'],
                    separator_zone=c['separator_zone'],separator_min_width=c['separator_min_width'],
                    separator_mode=c['separator_mode'],cancel_check=cancel_check,
                    progress=lambda current,total,side=side,i=i:update(f'正在提取 PDF {side}：第 {current} / {total} 页',i*45+45*current/max(total,1)))
                check_cancel(cancel_check)
                # Display original filenames, never the temporary upload path.
                report['source_file'] = document['name']
                for item in report['items']:
                    check_cancel(cancel_check)
                    item['source_file'] = document['name']
                coverage = annotate_extraction(report)
                matches = [item for item in report['items'] if item['matched']]
                write_csv(directory/'matches.csv',matches,MATCH_FIELDS,cancel_check=cancel_check)
                write_csv(directory/'all_items.csv',report['items'],cancel_check=cancel_check)
                check_cancel(cancel_check)
                report.update(query_field=field,query=query,matched_count=len(matches),item_count=len(report['items']))
                (directory/'extraction_report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
                (directory/'matches.html').write_text(extraction_page(side,document['name'],matches,report,c,cancel_check=cancel_check),encoding='utf-8')
                details.append({'side':side,'name':document['name'],'matched_count':len(matches),
                    'item_count':len(report['items']),'page_count':report['page_count'],'warnings':report['warnings'],
                    **coverage,
                    'removed_lines':sum(x['removed_lines'] for x in report.get('page_cleaning',[]))})
            check_cancel(cancel_check)
            (run_dir/'tables.html').write_text(tables_page([d['name'] for d in documents]),encoding='utf-8')
            (run_dir/'settings.json').write_text(json.dumps(c,ensure_ascii=False,indent=2),encoding='utf-8')
            result = {'id':run_id,'folder':str(run_dir),'files':details,'config':c,'comparison_ready':False,
                      'document_ids':[a_id,b_id]}
            update('已生成两份 CSV 和网页表格',100)
            check_cancel(cancel_check)
            with self.lock:
                self.runs[run_id] = {**result,'path':run_dir}
            return result

        except Exception:
            shutil.rmtree(run_dir,ignore_errors=True)
            raise

    def run(self, identifier: str) -> dict:
        with self.lock:
            if identifier not in self.runs:
                raise ValueError('未找到提取任务；请先提取两份 PDF。')
            return self.runs[identifier]

    def compare(self, run_id: str, ignore_whitespace: bool, update=lambda *args:None,
                *, cancel_check=None) -> dict:
        check_cancel(cancel_check)
        if not isinstance(ignore_whitespace,bool):
            raise ValueError('忽略空白设置必须为开关值。')
        run = self.run(run_id)
        path = run['path']
        # Prepare everything beside the target so rename works across Windows
        # drives. Nothing replaces an earlier completed comparison before the
        # final cancellation checkpoint, including settings and the ZIP.
        stage = Path(tempfile.mkdtemp(prefix='.comparison-',dir=path))
        keep_stage = False
        try:
            totals = self._prepare_comparison(run,stage,ignore_whitespace,update,cancel_check)
            config = {**run['config'],'ignore_whitespace':ignore_whitespace}
            (stage/'settings.json').write_text(json.dumps(config,ensure_ascii=False,indent=2),encoding='utf-8')
            update('正在整理可下载结果',90)
            check_cancel(cancel_check)
            entries = []
            for source in sorted(path.rglob('*')):
                check_cancel(cancel_check)
                relative = source.relative_to(path)
                if (source.is_file() and relative.parts[0] not in
                        ('comparison','settings.json','all_results.zip',stage.name)
                        and not relative.parts[0].startswith('.comparison-')
                        and not relative.name.startswith('.all_results-')):
                    entries.append((source,relative.as_posix()))
            entries.extend((source,source.relative_to(stage).as_posix())
                           for source in sorted((stage/'comparison').rglob('*')) if source.is_file())
            entries.append((stage/'settings.json','settings.json'))
            self._write_archive(stage/'all_results.zip',entries,cancel_check=cancel_check)
            check_cancel(cancel_check)
            self._commit_comparison(path,stage)
        except ComparisonRecoveryError:
            keep_stage = True
            raise
        finally:
            if not keep_stage:
                shutil.rmtree(stage,ignore_errors=True)
        # Commit is a short, non-cancellable transaction. A cancellation that
        # arrives after its final checkpoint must not turn saved output partial.
        with self.lock:
            run['comparison_ready'] = True
            run['summary'] = totals
            run['config'] = config
        return {'id':run_id,'summary':totals,'folder':str(path)}

    def _prepare_comparison(self,run,stage,ignore_whitespace,update,cancel_check):
        path = run['path']
        update('按编号比较标题和描述',15)
        a,b = [extracted_records(path/side/'extraction_report.json', side+' · '+file['name'],cancel_check=cancel_check)
               for side,file in zip(('A','B'),run['files'])]
        check_cancel(cancel_check)
        reports = [json.loads((path/side/'extraction_report.json').read_text(encoding='utf-8'))
                   for side in ('A', 'B')]
        check_cancel(cancel_check)
        documents = [self.document(identifier) for identifier in run['document_ids']]
        update('核对未命中编号的字段和原文位置',30)
        audit = comparison_audit(reports, a, b, documents,cancel_check=cancel_check)
        notes_by_id = {row['item_id']: [row['reason']] for row in audit}
        totals = comparator.write_pair(stage/'comparison',a,b,ignore_whitespace=ignore_whitespace,
                                       notes_by_id=notes_by_id,cancel_check=cancel_check)
        (stage/'comparison'/'comparison_audit.json').write_text(
            json.dumps(audit,ensure_ascii=False,indent=2),encoding='utf-8')
        # The HTML report remains usable when downloaded or opened from disk.
        report_path = stage/'comparison'/'report.html'
        rendered = report_path.read_text(encoding='utf-8')
        notices = []
        for file in run['files']:
            check_cancel(cancel_check)
            if not file['matched_count']:
                notices.append(file['side']+' 未提取到匹配条目，请先核对其提取结果。')
            if file['warnings']:
                notices.append(file['side']+' 有 '+str(len(file['warnings']))+' 条提取提示，请核对提取记录。')
            if file.get('unresolved_count'):
                notices.append(file['side']+' 有 '+str(file['unresolved_count'])+' 条需求未能判定查询条件；以下结果不能证明完整原文一致。')
        if not run['config']['same_filter']:
            notices.append('两份 PDF 使用不同查询条件；编号增减可能来自筛选范围差异。')
        intro = '<div class="sub">PDF 需求对比 · 比较已提取且命中的需求原文</div>'
        if notices:
            intro += '<div class="note"><strong>提取结果需核对；一致仅表示当前比较范围内一致。</strong><br>'+ '<br>'.join(html.escape(s) for s in notices)+'</div>'
        intro += '<p class="sub"><a href="comparison.csv" download>下载完整比较 CSV</a>　·　<a href="differences.csv" download>下载差异 CSV</a>　·　<a href="issues.csv" download>下载重复编号记录</a></p>'
        rendered = rendered.replace('<main>','<main>'+intro,1)
        report_path.write_text(rendered,encoding='utf-8')
        totals['extraction_notices'] = notices
        totals['requires_review'] = bool(notices)
        totals['audit_count'] = len(audit)
        totals['extraction_coverage'] = {file['side']: {key: file.get(key, 0) for key in
            ('matched_count', 'rejected_count', 'unresolved_count', 'noted_item_count')}
            for file in run['files']}
        (stage/'comparison'/'summary.json').write_text(json.dumps(totals,ensure_ascii=False,indent=2),encoding='utf-8')
        check_cancel(cancel_check)
        return totals

    @staticmethod
    def _commit_comparison(path: Path, stage: Path):
        names = ('comparison','settings.json','all_results.zip')
        previous = stage/'previous'
        previous.mkdir()
        backed_up, installed = [], []
        try:
            for name in names:
                target = path/name
                if target.exists():
                    target.replace(previous/name)
                    backed_up.append(name)
                (stage/name).replace(target)
                installed.append(name)
        except Exception as original_error:
            # A Windows lock can also block rollback. Never delete the only
            # remaining previous reports when restoring one target fails.
            rollback_errors = []
            for name in reversed(installed):
                target = path/name
                try:
                    if target.is_dir():shutil.rmtree(target)
                    else:target.unlink(missing_ok=True)
                except Exception as exc:
                    rollback_errors.append((name,exc))
            for name in reversed(backed_up):
                try:(previous/name).replace(path/name)
                except Exception as exc:rollback_errors.append((name,exc))
            if rollback_errors:
                raise ComparisonRecoveryError(
                    '保存比较结果失败，部分文件被占用，无法自动恢复。原有结果备份已保留在：'
                    +str(previous)+'。请关闭占用文件的程序后核对恢复。') from original_error
            raise

    @staticmethod
    def _write_archive(destination: Path, entries, *, cancel_check=None):
        check_cancel(cancel_check)
        with zipfile.ZipFile(destination,'w',zipfile.ZIP_DEFLATED) as archive:
            for source,name in entries:
                check_cancel(cancel_check)
                # Chunking keeps large report/archive writes cancellable too.
                with source.open('rb') as incoming, archive.open(name,'w') as outgoing:
                    while True:
                        check_cancel(cancel_check)
                        block = incoming.read(1024*1024)
                        if not block:break
                        outgoing.write(block)
        check_cancel(cancel_check)

    def archive(self, run_id: str, *, cancel_check=None) -> Path:
        check_cancel(cancel_check)
        run = self.run(run_id)
        archive = run['path']/'all_results.zip'
        temp = run['path']/('.all_results-'+uuid.uuid4().hex+'.tmp')
        try:
            entries=[]
            for source in sorted(run['path'].rglob('*')):
                check_cancel(cancel_check)
                relative=source.relative_to(run['path'])
                if (source.is_file() and source not in (archive,temp)
                        and not relative.parts[0].startswith('.comparison-')
                        and not relative.name.startswith('.all_results-')):
                    entries.append((source,relative.as_posix()))
            self._write_archive(temp,entries,cancel_check=cancel_check)
            check_cancel(cancel_check)
            temp.replace(archive)
            return archive
        finally:
            temp.unlink(missing_ok=True)

    def close(self):
        self.pool.shutdown(wait=True)
        # Uploaded inputs are temporary; outputs are retained on disk.
        for document in self.documents.values():
            document['path'].unlink(missing_ok=True)
