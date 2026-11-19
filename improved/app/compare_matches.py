#!/usr/bin/env python3
"""Compare requirement CSVs by identifier. Python standard library only."""
from __future__ import annotations

import argparse
import csv
import difflib
import html
import io
import itertools
import json
import re
import sys
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

from task_control import check_cancel


STATUS = {
    "only_a": "仅 A 提取命中", "only_b": "仅 B 提取命中",
    "same": "一致", "title_changed": "标题不同",
    "description_changed": "描述不同", "both_changed": "标题和描述均不同",
    "duplicate": "编号重复，需核对",
}
HEADER_ALIASES = (
    {"编号", "id", "item_id", "requirement id", "requirement_id"},
    {"标题", "title", "name"},
    {"具体描述", "描述", "description", "details"},
)
OUTPUT_COLUMNS = ["编号", "结果", "A存在", "B存在", "A标题", "B标题",
                  "标题是否不同", "A具体描述", "B具体描述", "描述是否不同",
                  "标题差异说明", "描述差异说明", "A行号", "B行号", "备注"]


@dataclass(frozen=True)
class Record:
    item_id: str
    title: str
    description: str
    row: int


@dataclass
class CsvData:
    path: Path
    encoding: str
    records: dict[str, list[Record]]


@dataclass
class Comparison:
    item_id: str
    a: list[Record]
    b: list[Record]
    status: str
    title_changed: bool | None = None
    description_changed: bool | None = None
    formatting_only: bool = False
    review_notes: list[str] = field(default_factory=list)


def read_csv(path: Path, *, encoding: str | None = None, no_header: bool = False) -> CsvData:
    raw = path.read_bytes()
    if encoding:
        text = raw.decode(encoding)
        chosen = encoding
    else:
        candidates = ["utf-16"] if raw.startswith((b"\xff\xfe", b"\xfe\xff")) else ["utf-8-sig", "gb18030"]
        for chosen in candidates:
            try:
                text = raw.decode(chosen)
                break
            except UnicodeError:
                continue
        else:
            raise ValueError(f"无法识别文件编码：{path}；可通过 --encoding 指定。")
    if not text.strip():
        raise ValueError(f"CSV 文件为空：{path}")
    try:
        dialect = csv.Sniffer().sniff(text[:65536], delimiters=",;\t")
    except csv.Error:
        dialect = csv.excel
    csv.field_size_limit(20_000_000)
    reader = csv.reader(io.StringIO(text, newline=""), dialect=dialect, strict=True)
    indexes = (0, 1, 2)
    start = 1
    if not no_header:
        header = next(reader, [])
        if len(header) < 3:
            raise ValueError(f"CSV 表头至少需要三列：{path}")
        cleaned_header = [s.strip().lstrip("\ufeff").casefold() for s in header]
        found = [next((i for i, name in enumerate(cleaned_header) if name in aliases), None)
                 for aliases in HEADER_ALIASES]
        if all(index is not None for index in found):
            indexes = tuple(found)
        elif re.match(r"^AT\s*-", header[0], re.I):
            raise ValueError(f"文件似乎没有表头：{path}；请添加 --no-header。")
        start = 2
    records: dict[str, list[Record]] = {}
    for row_no, cells in enumerate(reader, start):
        if not cells or all(not value.strip() for value in cells):
            continue
        if len(cells) <= max(indexes):
            raise ValueError(f"{path} 第 {row_no} 条 CSV 记录不足三列。")
        item_id = cells[indexes[0]].strip()
        if not item_id:
            raise ValueError(f"{path} 第 {row_no} 条 CSV 记录的编号为空。")
        rec = Record(item_id, cells[indexes[1]], cells[indexes[2]], row_no)
        records.setdefault(item_id, []).append(rec)
    return CsvData(path.resolve(), chosen, records)


def normalized(text: str, ignore_whitespace: bool) -> str:
    # Windows/Linux line-ending encodings are equivalent; line positions matter.
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    return re.sub(r"\s+", " ", text).strip() if ignore_whitespace else text


def compare(a: CsvData, b: CsvData, *, ignore_whitespace: bool = False,
            cancel_check=None) -> list[Comparison]:
    check_cancel(cancel_check)
    results = []
    # Preserve A's record order, then append identifiers found only in B.
    for item_id in dict.fromkeys([*a.records, *b.records]):
        check_cancel(cancel_check)
        left, right = a.records.get(item_id, []), b.records.get(item_id, [])
        item = Comparison(item_id, left, right, "same")
        if len(left) > 1 or len(right) > 1:
            item.status = "duplicate"
        elif not right:
            item.status = "only_a"
        elif not left:
            item.status = "only_b"
        else:
            x, y = left[0], right[0]
            item.title_changed = normalized(x.title, ignore_whitespace) != normalized(y.title, ignore_whitespace)
            item.description_changed = normalized(x.description, ignore_whitespace) != normalized(y.description, ignore_whitespace)
            if item.title_changed and item.description_changed:
                item.status = "both_changed"
            elif item.title_changed:
                item.status = "title_changed"
            elif item.description_changed:
                item.status = "description_changed"
            item.formatting_only = item.status == "same" and (x.title != y.title or x.description != y.description)
        results.append(item)
    check_cancel(cancel_check)
    return results


def tokens(text: str) -> list[str]:
    # The fallback must retain every Unicode character, including full-width
    # digits, scientific symbols and emoji.
    return re.findall(r"\s+|[A-Za-z0-9_]+|.", text, re.DOTALL)


def _merge_edits(opcodes: list[tuple], *, cancel_check=None) -> list[tuple]:
    """Combine consecutive insert/delete operations into readable replacements."""
    result = []
    pending = None
    for tag, i, j, k, l in opcodes:
        check_cancel(cancel_check)
        if tag != "equal":
            pending = (pending[0], j, pending[2], l) if pending else (i, j, k, l)
            continue
        if pending:
            p, q, r, s = pending
            result.append(("replace" if p < q and r < s else "delete" if p < q else "insert", p, q, r, s))
            pending = None
        if i < j:
            if result and result[-1][0] == "equal":
                old = result.pop()
                result.append(("equal", old[1], j, old[3], l))
            else:
                result.append((tag, i, j, k, l))
    if pending:
        p, q, r, s = pending
        result.append(("replace" if p < q and r < s else "delete" if p < q else "insert", p, q, r, s))
    return result


def _myers_opcodes(a: list[str], b: list[str], *, work_limit: int = 1_000_000,
                   distance_limit: int = 256, cancel_check=None) -> list[tuple] | None:
    """Find a minimal token edit path, with explicit CPU and trace-memory limits.

    Repeated Chinese characters are useful matches, so they must not be discarded
    as ``SequenceMatcher(autojunk=True)`` does. Myers is especially efficient for
    long descriptions with a small number of edits. Return None when the budget
    is exhausted; callers can then mark a broader interval without hiding edits.
    """
    n, m = len(a), len(b)
    check_cancel(cancel_check)
    frontier = {1: 0}
    trace = []
    work = 0
    for distance in range(min(n + m, distance_limit) + 1):
        check_cancel(cancel_check)
        trace.append(frontier.copy())
        for diagonal in range(-distance, distance + 1, 2):
            check_cancel(cancel_check)
            work += 1
            if work > work_limit:
                return None
            if diagonal == -distance or (diagonal != distance and
                    frontier.get(diagonal - 1, -1) < frontier.get(diagonal + 1, -1)):
                x = frontier.get(diagonal + 1, 0)
            else:
                x = frontier.get(diagonal - 1, 0) + 1
            y = x - diagonal
            while x < n and y < m and a[x] == b[y]:
                x += 1
                y += 1
                work += 1
                if work % 1024 == 0:
                    check_cancel(cancel_check)
                if work > work_limit:
                    return None
            frontier[diagonal] = x
            if x < n or y < m:
                continue
            # Walk the saved wavefronts backwards. Each wavefront adds one
            # insertion/deletion, followed by a (possibly empty) equal run.
            reversed_ops = []
            for depth in range(distance, 0, -1):
                check_cancel(cancel_check)
                previous = trace[depth]
                diagonal = x - y
                insertion = diagonal == -depth or (diagonal != depth and
                    previous.get(diagonal - 1, -1) < previous.get(diagonal + 1, -1))
                previous_diagonal = diagonal + 1 if insertion else diagonal - 1
                px = previous[previous_diagonal]
                py = px - previous_diagonal
                sx, sy = (px, py + 1) if insertion else (px + 1, py)
                if x > sx:
                    reversed_ops.append(("equal", sx, x, sy, y))
                reversed_ops.append(("insert" if insertion else "delete", px, sx, py, sy))
                x, y = px, py
            if x:
                reversed_ops.append(("equal", 0, x, 0, y))
            return _merge_edits(list(reversed(reversed_ops)), cancel_check=cancel_check)
    return None


def _diff_opcodes(a: list[str], b: list[str], *, cancel_check=None) -> tuple[list[tuple], bool]:
    """Preserve equal ends, then compare the changed middle within a fixed budget."""
    prefix = 0
    check_cancel(cancel_check)
    limit = min(len(a), len(b))
    while prefix < limit and a[prefix] == b[prefix]:
        prefix += 1
        if prefix % 1024 == 0:
            check_cancel(cancel_check)
    end_a, end_b = len(a), len(b)
    while end_a > prefix and end_b > prefix and a[end_a - 1] == b[end_b - 1]:
        end_a -= 1
        end_b -= 1
        if end_a % 1024 == 0:
            check_cancel(cancel_check)
    result = [("equal", 0, prefix, 0, prefix)] if prefix else []
    coarse = False
    if end_a == prefix or end_b == prefix:
        if end_a > prefix or end_b > prefix:
            result.append(("delete" if end_a > prefix else "insert", prefix, end_a, prefix, end_b))
    else:
        middle_a, middle_b = a[prefix:end_a], b[prefix:end_b]
        middle = _myers_opcodes(middle_a, middle_b, cancel_check=cancel_check)
        if middle is None:
            # Only use the quadratic matcher on a bounded small rectangle.
            # Large, heavily changed blocks remain visible as coarse changes;
            # never silently discard their frequent characters as junk.
            if len(middle_a) * len(middle_b) <= 250_000:
                check_cancel(cancel_check)
                middle = difflib.SequenceMatcher(None, middle_a, middle_b, autojunk=False).get_opcodes()
                check_cancel(cancel_check)
            else:
                middle = [("replace", 0, len(middle_a), 0, len(middle_b))]
                coarse = True
        result.extend((tag, i + prefix, j + prefix, k + prefix, l + prefix)
                      for tag, i, j, k, l in middle)
    if end_a < len(a):
        result.append(("equal", end_a, len(a), end_b, len(b)))
    check_cancel(cancel_check)
    return _merge_edits(result, cancel_check=cancel_check), coarse


def text_diff_segments(a: str, b: str, *, cancel_check=None) -> tuple[list[tuple[str, str]], list[tuple[str, str]], str]:
    """Return exact raw text with equal/delete/insert tags and a readable note.

    Native views and HTML use the same bounded comparison. Raw segments never
    contain HTML or visible whitespace substitutions, so copying native text
    retains the source contents, including Unicode and line endings.
    """
    check_cancel(cancel_check)
    x, y = tokens(a), tokens(b)
    check_cancel(cancel_check)
    left, right, notes = [], [], []
    opcodes, coarse = _diff_opcodes(x, y, cancel_check=cancel_check)
    if coarse:
        notes.append("较长且改动较多的区间已整体标记；请结合两侧原文核对区间内的具体变化。")
    for tag, i, j, k, l in opcodes:
        check_cancel(cancel_check)
        old, new = "".join(x[i:j]), "".join(y[k:l])
        if old:
            left.append((old, "equal" if tag == "equal" else "delete"))
        if new:
            right.append((new, "equal" if tag == "equal" else "insert"))
        if tag == "equal":
            continue
        quote = lambda value: json.dumps(value, ensure_ascii=False)
        if tag == "replace":
            notes.append(f"替换 {quote(old)} → {quote(new)}")
        elif tag == "delete":
            notes.append(f"A有、B无 {quote(old)}")
        else:
            notes.append(f"A无、B有 {quote(new)}")
    return left, right, "\n".join(notes)


def text_diff(a: str, b: str, *, cancel_check=None) -> tuple[str, str, str]:
    """Return safe HTML for both sides, plus a readable change list."""
    left, right, notes = text_diff_segments(a, b, cancel_check=cancel_check)
    def render(segments):
        result = []
        for value, tag in segments:
            check_cancel(cancel_check)
            if tag == "equal":
                result.append(html.escape(value))
                continue
            if value.isspace():
                value = value.replace("\r", "␍").replace("\n", "↵\n").replace("\t", "⇥").replace(" ", "·")
            element = "del" if tag == "delete" else "ins"
            result.append(f"<{element}>" + html.escape(value) + f"</{element}>")
        return "".join(result)
    return render(left), render(right), notes


def record_text(records: list[Record], name: str) -> str:
    if len(records) == 1:
        return getattr(records[0], name)
    return "\n\n".join(f"【CSV第{r.row}行】\n{getattr(r, name)}" for r in records)


def as_csv_row(item: Comparison, *, cancel_check=None) -> dict[str, str]:
    check_cancel(cancel_check)
    title_note = description_note = ""
    if item.a and item.b and item.status != "duplicate":
        if item.title_changed:
            title_note = text_diff(item.a[0].title, item.b[0].title, cancel_check=cancel_check)[2]
        if item.description_changed:
            description_note = text_diff(item.a[0].description, item.b[0].description, cancel_check=cancel_check)[2]
    note = "按比较规则一致，原文存在空白排版差异。" if item.formatting_only else ""
    if item.status == "duplicate":
        note = "存在重复编号，未自动选择其中一条。原始记录见 issues.csv。"
    note = "\n".join([value for value in [note, *item.review_notes] if value])
    flag = lambda v: "" if v is None else ("是" if v else "否")
    return dict(zip(OUTPUT_COLUMNS, [
        item.item_id, STATUS[item.status], "是" if item.a else "否", "是" if item.b else "否",
        record_text(item.a, "title"), record_text(item.b, "title"), flag(item.title_changed),
        record_text(item.a, "description"), record_text(item.b, "description"), flag(item.description_changed),
        title_note, description_note, "; ".join(str(r.row) for r in item.a),
        "; ".join(str(r.row) for r in item.b), note,
    ]))


def write_csv(path: Path, headers: list[str], rows: list[dict[str, str]], *, cancel_check=None) -> None:
    check_cancel(cancel_check)
    def excel_safe(value: str) -> str:
        return "'" + value if value.lstrip().startswith(("=", "+", "-", "@")) else value
    with path.open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=headers)
        writer.writeheader()
        for row in rows:
            check_cancel(cancel_check)
            writer.writerow({k: excel_safe(str(v)) for k, v in row.items()})
    check_cancel(cancel_check)


def summary(a: CsvData, b: CsvData, results: list[Comparison], ignore_whitespace: bool) -> dict:
    counts = Counter(item.status for item in results)
    return {
        "a_file": str(a.path), "b_file": str(b.path), "a_encoding": a.encoding, "b_encoding": b.encoding,
        "ignore_whitespace": ignore_whitespace,
        "a_record_count": sum(map(len, a.records.values())), "b_record_count": sum(map(len, b.records.values())),
        "a_id_count": len(a.records), "b_id_count": len(b.records),
        "only_a": len(a.records.keys() - b.records.keys()),
        "only_b": len(b.records.keys() - a.records.keys()),
        "common": len(a.records.keys() & b.records.keys()),
        "same": counts["same"],
        "title_changed": sum(item.title_changed is True for item in results),
        "description_changed": sum(item.description_changed is True for item in results),
        "duplicate_ids": counts["duplicate"],
        "formatting_only": sum(item.formatting_only for item in results),
        "status_counts": {key: counts[key] for key in STATUS},
        "only_a_ids": [key for key in a.records if key not in b.records],
        "only_b_ids": [key for key in b.records if key not in a.records],
    }


CSS = """
:root{color-scheme:light;font-family:system-ui,-apple-system,'Segoe UI','Microsoft YaHei',sans-serif;color:#18343b;background:#f4f7f7}
*{box-sizing:border-box}body{margin:0}main{max-width:1440px;margin:auto;padding:36px 28px 70px}h1{font-size:28px;margin:5px 0 12px}h2{font-size:18px}p{line-height:1.65}.sub{color:#566d73;font-size:14px}.sources{background:white;padding:15px 20px;border:1px solid #dbe5e5;border-radius:10px;overflow-wrap:anywhere}
.cards{display:grid;grid-template-columns:repeat(6,minmax(0,1fr));gap:12px;margin:22px 0}.card{background:white;border:1px solid #dbe5e5;border-radius:10px;padding:15px}.number{font-size:28px;font-weight:650;margin-top:4px}.controls{display:flex;gap:10px;flex-wrap:wrap;margin:20px 0}input,select,button{font:inherit;padding:10px 12px;border:1px solid #b5cbcf;border-radius:7px;background:white;color:#18343b}input{flex:1;min-width:200px}button{cursor:pointer}details.item{border:1px solid #d9e3e5;border-radius:9px;background:white;margin-bottom:12px;overflow:hidden}summary{padding:15px 18px;cursor:pointer;line-height:1.65}.id{font-weight:700;margin-right:14px}.status{font-size:13px;color:#46636a}.state-title_changed,.state-description_changed,.state-both_changed{border-left:4px solid #c88d32!important}.state-only_a{border-left:4px solid #b05e65!important}.state-only_b{border-left:4px solid #288572!important}.state-duplicate{border-left:4px solid #9a64a2!important}.pair{display:grid;grid-template-columns:1fr 1fr;border-top:1px solid #e1e9ea}.side{min-width:0;padding:18px 20px}.side+.side{border-left:1px solid #e1e9ea}.side h3{font-size:14px;margin:0 0 16px;color:#58727a}.label{font-size:12px;color:#667c81;margin:15px 0 7px}.text{white-space:pre-wrap;overflow-wrap:anywhere;line-height:1.7;font-size:14px}.note{padding:10px 18px;font-size:13px;background:#fff9e9;color:#7b5a18}.muted{color:#73888d}del{background:#ffe1e3;color:#8f2835;text-decoration:none}ins{background:#d8f2e5;color:#0f6447;text-decoration:none}.legend{font-size:13px;color:#5b7076}.legend del,.legend ins{padding:2px 5px}.record+.record{border-top:1px dashed #d1dfe1;margin-top:20px;padding-top:12px}a{color:#176d83}table{border-collapse:collapse;width:100%;background:white}th,td{text-align:left;padding:12px;border-bottom:1px solid #dce6e7;vertical-align:top}th{background:#e9f1f2}[hidden]{display:none!important}
@media(max-width:800px){main{padding:20px 12px}.cards{grid-template-columns:repeat(3,minmax(0,1fr))}.pair{grid-template-columns:1fr}.side+.side{border-left:0;border-top:1px solid #e1e9ea}}@media print{.controls{display:none}.cards{grid-template-columns:repeat(6,1fr)}details{break-inside:avoid}}
"""


def html_document(title: str, body: str, script: str = "") -> str:
    return '<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>' + html.escape(title) + '</title><style>' + CSS + '</style></head><body><main>' + body + '</main><script>' + script + '</script></body></html>'


def render_report(a: CsvData, b: CsvData, results: list[Comparison], totals: dict, *, cancel_check=None) -> str:
    check_cancel(cancel_check)
    esc = html.escape
    cards = [("仅 A 提取命中", "only_a"), ("仅 B 提取命中", "only_b"), ("同编号标题不同", "title_changed"),
             ("同编号描述不同", "description_changed"), ("一致", "same"), ("重复编号", "duplicate_ids")]
    body = '<p class="sub">REQUIREMENT COMPARISON</p><h1>CSV 需求对比</h1>'
    body += '<div class="sources"><div><strong>A</strong> ' + esc(str(a.path)) + '</div><div><strong>B</strong> ' + esc(str(b.path)) + '</div></div>'
    body += '<div class="cards">' + ''.join(f'<div class="card"><div class="sub">{label}</div><div class="number">{totals[key]}</div></div>' for label, key in cards) + '</div>'
    mode = "换行和连续空白折叠成一个空格，并去掉首尾空白" if totals["ignore_whitespace"] else "逐字比较（统一 Windows / Linux 换行编码）"
    body += '<p class="sub">按编号匹配，忽略行顺序。当前规则：' + mode + '。标题与描述同时变化时，两项计数均包含该编号。</p>'
    body += '<p class="legend"><del>A 中删除或修改的文字</del>　<ins>B 中新增或修改的文字</ins>　差异中的纯空白用 ·、↵、⇥ 显示。原文保存在 CSV 中。</p>'
    body += '<div class="controls"><input id="search" type="search" placeholder="搜索编号、标题或描述" aria-label="搜索"><select id="filter" aria-label="筛选结果"><option value="differences">只看差异</option><option value="all">全部</option>'
    body += ''.join(f'<option value="{key}">{value}</option>' for key, value in STATUS.items()) + '</select><button id="expand">展开当前结果</button><button id="collapse">收起</button></div><p id="visible" class="sub"></p>'
    for item in results:
        check_cancel(cancel_check)
        body += f'<details class="item state-{item.status}" data-status="{item.status}"><summary><span class="id">{esc(item.item_id)}</span><span class="status">{STATUS[item.status]}</span></summary>'
        if item.formatting_only:
            body += '<div class="note">按当前规则一致；原文存在空白排版差异。</div>'
        if item.status == "duplicate":
            body += '<div class="note">同一文件中编号重复。以下保留全部记录，未自动选择其中一条比较。</div>'
        for note in item.review_notes:
            body += '<div class="note">' + esc(note) + '</div>'
        title_pair = description_pair = None
        if item.a and item.b and item.status != "duplicate":
            if item.title_changed:
                title_pair = text_diff(item.a[0].title, item.b[0].title, cancel_check=cancel_check)
            if item.description_changed:
                description_pair = text_diff(item.a[0].description, item.b[0].description, cancel_check=cancel_check)
        body += '<div class="pair">'
        for side_index, (label, records) in enumerate((("A", item.a), ("B", item.b))):
            body += '<section class="side"><h3>' + label + '</h3>'
            if not records:
                body += '<p class="muted">本次提取命中结果中没有该编号；请结合提取记录核对原文。</p>'
            for rec in records:
                check_cancel(cancel_check)
                title = title_pair[side_index] if title_pair else esc(rec.title)
                description = description_pair[side_index] if description_pair else esc(rec.description)
                if item.status in ("only_a", "only_b"):
                    element = "del" if item.status == "only_a" else "ins"
                    title = f"<{element}>{title}</{element}>" if title else ""
                    description = f"<{element}>{description}</{element}>" if description else ""
                body += f'<div class="record"><div class="sub">CSV 第 {rec.row} 行</div><div class="label">标题</div><div class="text">{title or "（空）"}</div><div class="label">具体描述</div><div class="text">{description or "（空）"}</div></div>'
            body += '</section>'
        body += '</div></details>'
    script = """const items=[...document.querySelectorAll('details.item')];const search=document.getElementById('search'),filter=document.getElementById('filter');function update(){let n=0;const q=search.value.toLocaleLowerCase();for(const item of items){const s=item.dataset.status;const ok=(filter.value==='all'||(filter.value==='differences'?s!=='same':s===filter.value))&&item.textContent.toLocaleLowerCase().includes(q);item.hidden=!ok;if(ok)n++;}document.getElementById('visible').textContent='显示 '+n+' / '+items.length+' 个编号';}search.addEventListener('input',update);filter.addEventListener('change',update);document.getElementById('expand').onclick=()=>items.forEach(x=>{if(!x.hidden)x.open=true;});document.getElementById('collapse').onclick=()=>items.forEach(x=>x.open=false);update();"""
    check_cancel(cancel_check)
    return html_document("CSV 需求对比", body, script)


def write_pair(output: Path, a: CsvData, b: CsvData, *, ignore_whitespace: bool,
               notes_by_id: dict[str, list[str]] | None = None, cancel_check=None) -> dict:
    check_cancel(cancel_check)
    results = compare(a, b, ignore_whitespace=ignore_whitespace, cancel_check=cancel_check)
    for item in results:
        check_cancel(cancel_check)
        item.review_notes = (notes_by_id or {}).get(item.item_id, [])
    totals = summary(a, b, results, ignore_whitespace)
    output.mkdir(parents=True, exist_ok=True)
    rows = [as_csv_row(item, cancel_check=cancel_check) for item in results]
    write_csv(output / "comparison.csv", OUTPUT_COLUMNS, rows, cancel_check=cancel_check)
    write_csv(output / "differences.csv", OUTPUT_COLUMNS,
              [row for item, row in zip(results, rows) if item.status != "same"], cancel_check=cancel_check)
    issues = []
    for side, data in (("A", a), ("B", b)):
        for item_id, records in data.records.items():
            check_cancel(cancel_check)
            if len(records) > 1:
                for rec in records:
                    check_cancel(cancel_check)
                    issues.append({"文件侧": side, "来源文件": str(data.path), "编号": item_id,
                                   "行号": str(rec.row), "标题": rec.title, "具体描述": rec.description,
                                   "问题": "同一文件中编号重复"})
    write_csv(output / "issues.csv", ["文件侧", "来源文件", "编号", "行号", "标题", "具体描述", "问题"], issues, cancel_check=cancel_check)
    check_cancel(cancel_check)
    (output / "summary.json").write_text(json.dumps(totals, ensure_ascii=False, indent=2), encoding="utf-8")
    (output / "report.html").write_text(render_report(a, b, results, totals, cancel_check=cancel_check), encoding="utf-8")
    check_cancel(cancel_check)
    return totals


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="按编号比较 matches.csv 的增减、标题和具体描述；无需第三方库。")
    parser.add_argument("files", nargs="+", help="至少两份 CSV；多份时默认以第一份为基准逐一比较")
    parser.add_argument("--out", default="compare_results", help="输出目录")
    parser.add_argument("--ignore-whitespace", action="store_true", help="将换行和连续空白统一为一个空格，并去掉首尾空白")
    parser.add_argument("--all-pairs", action="store_true", help="多份 CSV 两两比较")
    parser.add_argument("--encoding", help="强制使用此编码；默认逐文件检测 UTF-8 / UTF-16 / GB18030")
    parser.add_argument("--no-header", action="store_true", help="输入没有表头，直接将前三列作为编号、标题、描述")
    args = parser.parse_args(argv)
    if len(args.files) < 2:
        parser.error("至少需要两份 CSV 文件。")
    try:
        data = [read_csv(Path(p).expanduser(), encoding=args.encoding, no_header=args.no_header) for p in args.files]
        pairs = list(itertools.combinations(range(len(data)), 2)) if args.all_pairs else [(0, i) for i in range(1, len(data))]
        root = Path(args.out).expanduser().resolve()
        targets = [(root if len(pairs) == 1 else root / f"pair_{i+1:02d}_{j+1:02d}", i, j) for i, j in pairs]
        output_names = {"comparison.csv", "differences.csv", "issues.csv", "summary.json", "report.html"}
        outputs = {(directory / name).resolve() for directory, _, _ in targets for name in output_names}
        outputs.update({root / "index.html", root / "summary.json"})
        if outputs & {item.path for item in data}:
            raise ValueError("输出路径与输入文件重合，请使用另一个 --out 目录。")
        batch, duplicate_count = [], 0
        for directory, i, j in targets:
            totals = write_pair(directory, data[i], data[j], ignore_whitespace=args.ignore_whitespace)
            totals["report"] = (directory / "report.html").relative_to(root).as_posix()
            batch.append(totals)
            duplicate_count += totals["duplicate_ids"]
            print(f"文件{i+1} vs 文件{j+1}：仅A有 {totals['only_a']}，仅B有 {totals['only_b']}，标题不同 {totals['title_changed']}，描述不同 {totals['description_changed']}，一致 {totals['same']}，重复编号 {totals['duplicate_ids']}")
        if len(batch) > 1:
            body = '<h1>多份 CSV 对比</h1><p class="sub">' + ("所有文件两两比较。" if args.all_pairs else "第一份 CSV 为基准，逐一比较后续文件。") + '</p><table><thead><tr><th>文件 A</th><th>文件 B</th><th>仅A有</th><th>仅B有</th><th>标题不同</th><th>描述不同</th><th>一致</th><th>报告</th></tr></thead><tbody>'
            for totals in batch:
                body += '<tr><td>' + html.escape(totals["a_file"]) + '</td><td>' + html.escape(totals["b_file"]) + '</td>' + ''.join(f'<td>{totals[key]}</td>' for key in ("only_a", "only_b", "title_changed", "description_changed", "same")) + '<td><a href="' + totals["report"] + '">查看对比</a></td></tr>'
            (root / "index.html").write_text(html_document("多份 CSV 对比", body + '</tbody></table>'), encoding="utf-8")
            (root / "summary.json").write_text(json.dumps({"files": [str(d.path) for d in data], "comparisons": batch}, ensure_ascii=False, indent=2), encoding="utf-8")
        print("结果：" + str(root / ("report.html" if len(batch) == 1 else "index.html")))
        return 2 if duplicate_count else 0
    except (OSError, ValueError, UnicodeError, csv.Error, LookupError) as exc:
        print("错误：" + str(exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
