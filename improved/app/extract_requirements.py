#!/usr/bin/env python3
"""Filter requirement blocks in text PDFs, without LLMs or network access."""
from __future__ import annotations

import argparse
import csv
import json
import re
import sys
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pdfplumber
from margins import repeated_keys, marginal_line, separator_bounds, separator_analysis
from task_control import TaskCancelled, check_cancel


DEFAULT_ID_PATTERN = r"AT\s*-\s*[A-Za-z][A-Za-z0-9]*\s*-\s*\d+"
FIELD_NAMES = (
    "Req Classification", "Requirement Classification", "Risk Related",
    "Stakeholder SHS", "Require Key", "Requirement Key", "Rearrange",
    "Rearrange Reason", "Test Level", "Acceptance Criteria",
    "Submission Systems", "Product/Project/Release", "Product Structure",
    "Linked Work Items", "Standards",
)
DASHES = str.maketrans({c: "-" for c in "\u2010\u2011\u2012\u2013\u2014\u2015\u2212\uff0d\ufe63"})


def clean(text: str | None) -> str:
    """Normalize lookup keys only; never use this for exported requirement text."""
    return unicodedata.normalize("NFKC", text or "").translate(DASHES).replace("\u00ad", "").replace("\u200b", "").strip()


def flat(text: str | None) -> str:
    return re.sub(r"\s+", " ", clean(text))


def display_clean(text: str | None) -> str:
    """Keep meaningful Unicode, including superscripts, in the exported text."""
    return unicodedata.normalize("NFC", text or "").replace("\u00ad", "").replace("\u200b", "").strip()


def display_flat(text: str | None) -> str:
    return re.sub(r"\s+", " ", display_clean(text))


def effective_page_bbox(page: Any) -> tuple[float, float, float, float]:
    """Visible CropBox intersection in pdfplumber's rotated, top-origin coordinates."""
    media = page.bbox
    crop = getattr(page, "cropbox", media)
    bounds = (max(media[0], crop[0]), max(media[1], crop[1]),
              min(media[2], crop[2]), min(media[3], crop[3]))
    if bounds[2] <= bounds[0] or bounds[3] <= bounds[1]:
        raise ValueError("PDF 的 CropBox 与页面范围没有有效交集。")
    return bounds


def field_key(text: str | None) -> str:
    return re.sub(r"[\s:]+", "", clean(text)).casefold()


def compile_query(query: str) -> re.Pattern[str]:
    """Ignore all query/value whitespace while preserving option boundaries.

    PDFs can split even one word into several fragments (``XG\nen`` or
    ``X Gen``). Match each literal character with optional Unicode whitespace
    between characters. Searching the original field value retains separators
    between distinct options; compacting the whole field would merge them and
    make a valid option in a list fail the endpoint guards.
    """
    compact = re.sub(r"\s+", "", clean(query))
    if not compact:
        raise ValueError("查询内容不能为空。")
    return re.compile(r"(?<!\w)" + r"\s*".join(map(re.escape, compact)) + r"(?!\w)", re.I)


def lookup_with_offsets(text: str) -> tuple[str, list[int]]:
    """Normalize for matching, while retaining offsets into the original value."""
    result, ends = [], []
    for i, char in enumerate(text):
        part = unicodedata.normalize("NFKC", char).translate(DASHES)
        if char in "\u00ad\u200b\ufeff":
            continue
        result.append(part)
        ends.extend([i + 1] * len(part))
    return "".join(result), ends


def make_field_patterns(names: list[str]) -> list[tuple[str, re.Pattern[str]]]:
    # PDF text streams sometimes insert spaces between *letters*, not words.
    return [(name, re.compile(r"^" + r"\s*".join(map(re.escape,
              re.sub(r"\s+", "", clean(name)))) + r"(?=\s|:|$)\s*:?\s*(.*)$", re.I))
            for name in sorted(set(names), key=len, reverse=True)]


def parse_field_line(text: str, patterns: list[tuple[str, re.Pattern[str]]]) -> tuple[str, str] | None:
    original = display_clean(text)
    normalized, ends = lookup_with_offsets(original)
    for name, pattern in patterns:
        match = pattern.match(normalized)
        if match:
            start = match.start(1)
            offset = ends[start - 1] if start else 0
            return name, original[offset:].strip()
    return None


@dataclass
class Line:
    text: str
    bbox: tuple[float, float, float, float]
    words: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class Item:
    source_file: str
    item_id: str
    title: str
    heading: str
    description_lines: list[str] = field(default_factory=list)
    field_parts: dict[str, list[str]] = field(default_factory=dict)
    field_labels: dict[str, str] = field(default_factory=dict)
    pages: set[int] = field(default_factory=set)
    metadata_pages: set[int] = field(default_factory=set)
    evidence: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)
    in_metadata: bool = False
    last_field: str = ""
    field_source: str = ""
    field_value_x: float | None = None
    field_bbox: tuple[float, float, float, float] | None = None
    field_page: int = 0
    initial_description_in_heading: bool = False
    start_position: tuple[int, float] = (0, 0.0)
    end_position: tuple[int, float] | None = None

    def as_dict(self, query: re.Pattern[str], field_name: str) -> dict[str, Any]:
        description = "\n".join(self.description_lines).strip()
        remaining_description = self.description_lines[1:] if self.initial_description_in_heading else self.description_lines
        key = field_key(field_name)
        value = "\n".join(self.field_parts.get(key, [])).strip()
        notes = list(dict.fromkeys(self.notes))
        if key not in self.field_parts:
            notes.append(f"未提取到字段 {field_name}，请核对原 PDF。")
        elif not value:
            notes.append(f"字段 {field_name} 的内容为空，请核对原 PDF。")
        return {
            "source_file": self.source_file,
            "item_id": self.item_id,
            "title": self.title,
            "description": description,
            "text_before_metadata": self.heading + ("\n" + "\n".join(remaining_description) if remaining_description else ""),
            "query_field": field_name,
            "field_value": value,
            "field_status": "missing" if key not in self.field_parts else ("resolved" if value else "empty"),
            "attributes": {self.field_labels[k]: "\n".join(parts).strip()
                           for k, parts in self.field_parts.items()},
            "matched": bool(query.search(clean(value))),
            "pages": sorted(self.pages),
            "metadata_pages": sorted(self.metadata_pages),
            "field_evidence": self.evidence.get(key, []),
            "notes": notes,
        }


def make_lines(words: list[dict[str, Any]], tolerance: float = 3.0) -> list[Line]:
    groups: list[list[dict[str, Any]]] = []
    for word in sorted(words, key=lambda w: (w["top"], w["x0"])):
        anchor = groups[-1][0] if groups else None
        # Checkbox/icon fonts often have a taller ascent while sharing the text
        # baseline. Comparing only their top puts the icon in the chapter title.
        same_baseline = bool(anchor and abs(word["bottom"] - anchor["bottom"]) <= tolerance
            and min(word["bottom"], anchor["bottom"]) > max(word["top"], anchor["top"]))
        if anchor and (abs(word["top"] - anchor["top"]) <= tolerance or same_baseline):
            groups[-1].append(word)
        else:
            groups.append([word])
    lines = []
    for group in groups:
        group.sort(key=lambda w: w["x0"])
        lines.append(Line(
            text=display_flat(" ".join(w["text"] for w in group)),
            bbox=(min(w["x0"] for w in group), min(w["top"] for w in group),
                  max(w["x1"] for w in group), max(w["bottom"] for w in group)),
            words=group,
        ))
    return sorted(lines, key=lambda line: (line.bbox[1], line.bbox[0]))


def in_box(line: Line, box: tuple[float, float, float, float]) -> bool:
    # Use the text centre to tolerate glyphs slightly crossing cell boundaries.
    cx = (line.bbox[0] + line.bbox[2]) / 2
    cy = (line.bbox[1] + line.bbox[3]) / 2
    return box[0] - 1 <= cx <= box[2] + 1 and box[1] - 1 <= cy <= box[3] + 1


STATUS_NAMES = ("Approved", "Accepted", "Draft", "Reviewed", "Released", "Proposed",
                "Rejected", "Obsolete", "In Review", "In Progress", "Implemented",
                "Validated", "Verified", "New")
STATUS_RE = re.compile(
    r"^[\s,，;:]*(?:(?:[^\w\s]|\(cid:\d+\))\s*)*"
    r"(?P<status>" + "|".join(re.escape(s) for s in STATUS_NAMES) + r")\s*[-:]\s*(?P<body>.*)$", re.I)
EXTRACTED_ICON_STATUS_RE = re.compile(
    r"^\s*[,，;]\s*\S{1,2}\s+(?P<status>" + "|".join(re.escape(s) for s in STATUS_NAMES)
    + r")\s*[-:]\s*(?P<body>.*)$", re.I)
GENERIC_STATUS_RE = re.compile(
    r"^\s*[,，;]\s*(?:(?:[^\w\s]|\(cid:\d+\))\s*)*"
    r"(?P<status>[A-Za-z][A-Za-z ]{0,35})\s*[-:]\s*(?P<body>.*)$")
SECTION_RE = re.compile(r"^\s*(?P<number>\d+(?:\.\d+){1,8})[.)]?\s+(?P<title>\S.*)$")


def requirement_heading(text: str, id_start: re.Pattern[str]) -> tuple[str, str, str] | None:
    """Return (ID, payload, status), preserving the literal scientific body."""
    original = display_clean(text)
    translated, ends = lookup_with_offsets(original)
    match = id_start.match(translated)
    if not match:
        return None
    tail = translated[match.end():]
    status = STATUS_RE.match(tail) or EXTRACTED_ICON_STATUS_RE.match(tail) or GENERIC_STATUS_RE.match(tail)
    item_id = re.sub(r"\s+", "", match.group("id")).upper()
    if status:
        start = match.end() + status.start("body")
        return item_id, original[ends[start - 1] if start else 0:].strip(), status.group("status")
    legacy = re.match(r"^\s*[-:]\s*(?P<title>.*)$", tail)
    if legacy:
        start = match.end() + legacy.start("title")
        return item_id, original[ends[start - 1] if start else 0:].strip(), ""
    # An isolated ID is common when a heading wraps onto the next physical line.
    if not tail.strip():
        return item_id, "", ""
    # Some exports separate ID and title using a tab/space rather than ':'.
    # A punctuation-delimited unknown status is still rejected for review.
    if tail[:1].isspace() and not re.match(r"^\s*[,;]", tail):
        offset = ends[match.end() - 1]
        return item_id, original[offset:].strip(), ""
    return None


def words_line(words: list[dict], text: str | None = None) -> Line:
    return Line(text if text is not None else display_flat(" ".join(w['text'] for w in words)),
                (min(w['x0'] for w in words), min(w['top'] for w in words),
                 max(w['x1'] for w in words), max(w['bottom'] for w in words)), words)


def repair_field_layout(lines: list[Line], field_patterns, id_start, cancel_check=None) -> list[Line]:
    """Recover unruled attribute columns using explicit horizontal separation.

    We never search for a field in arbitrary prose. A row must start with an
    exact label (or its wrapped prefix), optionally after a detached index.
    Additional pairs require a real column gap and a value for both pairs.
    """
    keys = {field_key(name) for name, _ in field_patterns}
    def prefix(text):
        key = field_key(text)
        return bool(key and any(k.startswith(key) and k != key for k in keys))
    def other_labeled_content(text):
        return (parse_field_line(text, field_patterns) or requirement_heading(text, id_start)
                or re.match(r'^[A-Za-z][A-Za-z /_()\-]{1,60}[:：]\s*\S', text))
    def clusters(line):
        parts = []
        for word in line.words:
            if not parts or word['x0'] - parts[-1][-1]['x1'] >= 24:
                parts.append([word])
            else:
                parts[-1].append(word)
        return parts
    prepared = []
    for line in lines:
        check_cancel(cancel_check)
        words = line.words
        # Numbered rows are supported only when the index is visibly detached.
        if (len(words) >= 2 and re.fullmatch(r'\d+[.)]?', words[0]['text'])
                and words[1]['x0'] - words[0]['x1'] >= 18):
            remainder = words_line(words[1:])
            pieces = clusters(remainder)
            first_piece = words_line(pieces[0]).text if pieces else ''
            explicit = parse_field_line(remainder.text, field_patterns) and ':' in clean(first_piece)
            separate_value = len(pieces) > 1 and field_key(first_piece) in keys
            if explicit or separate_value or (len(pieces) > 1 and prefix(first_piece)):
                line = remainder
        prepared.append(line)
    result = []
    consumed = set()
    for index, line in enumerate(prepared):
        check_cancel(cancel_check)
        if index in consumed:
            continue
        parts = clusters(line)
        # A wrapped label can have its value to the right of its FIRST line.
        # Keep label fragments in reading order, then the distinct value column.
        if len(parts) >= 2 and prefix(words_line(parts[0]).text):
            label_words = list(parts[0])
            value_words = [w for part in parts[1:] for w in part]
            if other_labeled_content(words_line(value_words).text):
                # This is another labeled pair, not the wrapped label's value.
                # Leave the ambiguous row unresolved instead of creating a hit.
                result.append(line)
                continue
            used = []
            previous = line
            for following_index in range(index + 1, min(index + 4, len(prepared))):
                following = prepared[following_index]
                more = clusters(following)
                if (not more or abs(more[0][0]['x0'] - label_words[0]['x0']) > 6
                        or not -1 <= following.bbox[1] - previous.bbox[3] <= 22
                        or requirement_heading(following.text, id_start)):
                    break
                candidate_words = label_words + more[0]
                candidate_text = words_line(candidate_words).text
                parsed = parse_field_line(candidate_text, field_patterns)
                if not parsed and not prefix(candidate_text):
                    break
                # A second field in a right column is not this field's value.
                right = [w for part in more[1:] for w in part]
                if right and other_labeled_content(words_line(right).text):
                    break
                label_words = candidate_words
                value_words += right
                used.append(following_index)
                previous = following
                if parsed:
                    value = " ".join(v for v in (parsed[1], display_flat(' '.join(w['text'] for w in value_words))) if v)
                    line = words_line(label_words + value_words, parsed[0] + ': ' + value)
                    consumed.update(used)
                    break
        # Separate several pairs only when the first pair was already explicit.
        if not parse_field_line(line.text, field_patterns):
            result.append(line)
            continue
        words = line.words
        start = 0
        splits = []
        for position in range(1, len(words)):
            if words[position]['x0'] - words[position - 1]['x1'] < 24:
                continue
            left = words_line(words[start:position])
            right = words_line(words[position:])
            left_field = parse_field_line(left.text, field_patterns)
            right_field = parse_field_line(right.text, field_patterns)
            explicit_empty = bool(right_field and not right_field[1] and clean(right.text).endswith(':'))
            if left_field and left_field[1] and right_field and (right_field[1] or explicit_empty):
                splits.append(left)
                start = position
        if splits:
            result.extend(splits + [words_line(words[start:])])
        else:
            result.append(line)
    return sorted(result, key=lambda line: (line.bbox[1], line.bbox[0]))


def join_wrapped_lines(lines: list[Line], field_patterns, id_start, cancel_check=None) -> list[Line]:
    """Repair adjacent wrapped labels and headings, only at the same left edge."""
    result = []
    index = 0
    field_names = [name for name, _ in field_patterns]
    while index < len(lines):
        check_cancel(cancel_check)
        line = lines[index]
        # Join up to four adjacent label fragments. The former two-line limit
        # lost labels such as Requirement / Classifi / cation.
        initial = field_key(line.text)
        if initial and any(field_key(name).startswith(initial) and field_key(name) != initial for name in field_names):
            joined_words = list(line.words)
            text = line.text
            previous = line
            for next_index in range(index + 1, min(index + 4, len(lines))):
                nxt = lines[next_index]
                if (abs(line.bbox[0] - nxt.bbox[0]) > 6
                        or not -1 <= nxt.bbox[1] - previous.bbox[3] <= 22
                        or requirement_heading(nxt.text, id_start)):
                    break
                text += ' ' + nxt.text
                joined_words += nxt.words
                parsed = parse_field_line(text, field_patterns)
                if parsed:
                    line = Line(text, (min(line.bbox[0], nxt.bbox[0]), line.bbox[1],
                                       max(line.bbox[2], nxt.bbox[2]), nxt.bbox[3]), joined_words)
                    index = next_index
                    break
                key = field_key(text)
                if not any(field_key(name).startswith(key) for name in field_names):
                    break
                previous = nxt
        if index + 1 < len(lines):
            nxt = lines[index + 1]
            close = (abs(line.bbox[0] - nxt.bbox[0]) <= 6
                     and -1 <= nxt.bbox[1] - line.bbox[3] <= 22)
            compact = field_key(line.text)
            label_prefix = any(field_key(name).startswith(compact) and field_key(name) != compact
                               for name in field_names) if compact else False
            heading = requirement_heading(line.text, id_start)
            bare_heading = heading is not None and not heading[1]
            id_prefix = bool(re.match(r"^AT\s*[-–—－]", clean(line.text), re.I)
                             and not id_start.match(clean(line.text)) and len(line.text) < 70)
            joined = line.text + " " + nxt.text
            can_join_label = label_prefix and parse_field_line(joined, field_patterns)
            can_join_heading = ((bare_heading or id_prefix)
                and not parse_field_line(nxt.text, field_patterns)
                and not id_start.match(clean(nxt.text))
                and requirement_heading(joined, id_start))
            if close and (can_join_label or can_join_heading):
                line = Line(joined, (min(line.bbox[0], nxt.bbox[0]), line.bbox[1],
                                    max(line.bbox[2], nxt.bbox[2]), nxt.bbox[3]), line.words + nxt.words)
                index += 1
        result.append(line)
        index += 1
    return result


def metadata_events(page: Any, field_keys: dict[str, str], *,
                    id_start: re.Pattern[str] | None = None,
                    continuation_box: tuple | None = None,
                    cancel_check=None) -> tuple[list[tuple], list[tuple], list[tuple]]:
    """Read attribute pairs by their actual columns, preserving content rows.

    Some exports add an index column or put two label/value pairs on one row.
    A value must stop at the next label, otherwise e.g. Test Level is silently
    included in Product Structure. Continuation is accepted only for an empty
    label cell with geometry matching the preceding attribute row.
    """
    events, metadata_boxes, all_table_boxes = [], [], []
    check_cancel(cancel_check)
    for table in page.find_tables():
        check_cancel(cancel_check)
        all_table_boxes.append(table.bbox)
        rows = table.extract(x_tolerance=2, y_tolerance=3)
        check_cancel(cancel_check)
        def known_columns(row):
            result = []
            for index, cell in enumerate(row):
                if (field_key(cell) in field_keys and
                        (index < len(row) - 1 or index == 0) and
                        (not result or index >= result[-1] + 2)):
                    result.append(index)
            return result

        label_columns = {index for row in rows if row for index in known_columns(row)}
        known = bool(label_columns)
        first_row = rows[0] if rows else []
        continuation = bool(continuation_box and first_row and not display_clean(first_row[0])
            and abs(table.bbox[0] - continuation_box[0]) <= 12
            and abs(table.bbox[2] - continuation_box[2]) <= 12
            and table.bbox[1] <= page.bbox[1] + page.height * .30)
        if not known and not continuation:
            continue
        active = continuation
        prior_labels = {}
        for row, geometric_row in zip(rows, table.rows):
            check_cancel(cancel_check)
            if not row:
                continue
            # A new ID row (including a bare ID with its title in another cell)
            # belongs to the main reader, never the previous item's metadata.
            nonempty = [(i, display_flat(cell)) for i, cell in enumerate(row) if display_clean(cell)]
            content_start = nonempty[:2] if nonempty and re.fullmatch(r"\d+[.)]?", nonempty[0][1]) else nonempty[:1]
            if id_start and any(id_start.match(clean(cell)) for _, cell in content_start):
                active = False
                prior_labels.clear()
                continue
            labels = known_columns(row)
            if labels:
                active = True
                # Reuse the table's established paired-column layout so an
                # unknown field on the same row still bounds a known value.
                labels = sorted(set(labels) | {i for i in label_columns
                    if i >= labels[0] and i < len(row) - 1})
            else:
                # For ordinary two-column tables, retain unknown labels too:
                # they are boundaries and must stop a selected field's value.
                start = min(label_columns) if label_columns else 0
                if start >= len(row):
                    continue
                first = display_flat(row[start])
                value = "\n".join(display_clean(cell) for cell in row[start + 1:] if display_clean(cell))
                if not active or (first and not value):
                    continue
                labels = sorted(i for i in label_columns if i < len(row) - 1) or [start]
            for offset, start in enumerate(labels):
                check_cancel(cancel_check)
                stop = labels[offset + 1] if offset + 1 < len(labels) else len(row)
                first = display_flat(row[start])
                key = field_key(first)
                value = "\n".join(display_clean(cell) for cell in row[start + 1:stop] if display_clean(cell))
                cells = [cell for cell in geometric_row.cells[start:stop] if cell]
                if not cells:
                    continue
                box = (min(cell[0] for cell in cells), min(cell[1] for cell in cells),
                       max(cell[2] for cell in cells), max(cell[3] for cell in cells))
                right_cells = [cell for cell in geometric_row.cells[start + 1:stop] if cell]
                value_x = min((cell[0] for cell in right_cells), default=None)
                label = field_keys.get(key, first)
                if label:
                    prior_labels[start] = label
                elif len(labels) > 1:
                    # Each blank pair continues its own column, not whichever
                    # field happened to be consumed last on the preceding row.
                    label = prior_labels.get(start, "")
                    if not label:
                        continue
                if not label and not value:
                    continue
                metadata_boxes.append(box)
                events.append((box[1], box[0], "field", (label, value, box, value_x)))
    return events, metadata_boxes, all_table_boxes


def text_value_x(line: Line, label: str) -> float | None:
    """Locate the value column for conservative unruled-table continuation."""
    for index in range(1, len(line.words) + 1):
        if field_key(" ".join(word["text"] for word in line.words[:index])) == field_key(label):
            return line.words[index]["x0"] if index < len(line.words) else None
    return None


def field_mentions(lines, field_name, page_number, visible, crop, repeated_lines=(), cancel_check=None):
    """Locate literal label text for diagnostics only, never infer a value."""
    pattern = compile_query(field_name)
    evidence = []
    seen = set()
    for index, line in enumerate(lines):
        check_cancel(cancel_check)
        group = [line]
        text = line.text
        if not pattern.search(clean(text)):
            compact = field_key(text)
            if not compact or not field_key(field_name).startswith(compact):
                continue
            # Diagnostic matching includes wrapped labels, but stays within one
            # aligned column and a bounded physical neighborhood.
            for nxt in lines[index + 1:index + 4]:
                if abs(nxt.bbox[0] - line.bbox[0]) > 6 or not -1 <= nxt.bbox[1] - group[-1].bbox[3] <= 22:
                    break
                group.append(nxt)
                text += ' ' + nxt.text
                if pattern.search(clean(text)):
                    break
                if not field_key(field_name).startswith(field_key(text)):
                    break
        if not pattern.search(clean(text)):
            continue
        box = (min(x.bbox[0] for x in group), min(x.bbox[1] for x in group),
               max(x.bbox[2] for x in group), max(x.bbox[3] for x in group))
        marker = tuple(round(v, 2) for v in box)
        if marker in seen:
            continue
        seen.add(marker)
        disposition = ('cropped' if box[3] <= crop[1] or box[1] >= crop[3] else
                       'partially_cropped' if box[1] < crop[1] or box[3] > crop[3] else
                       'repeated_removed' if any(x.bbox in repeated_lines for x in group) else 'retained')
        evidence.append({'page': page_number, 'bbox': [round(v - visible[i % 2], 2) for i, v in enumerate(box)],
                         'coordinate_space': 'visible_page_top_left_pt', 'text': text[:1000],
                         'disposition': disposition, '_position': (page_number, box[1])})
    return evidence


def explain_field_resolution(item, result, sightings, diagnostics, field_name, page_count):
    """Explain unresolved parsing without asserting absence in the source PDF."""
    status = result['field_status']
    if status == 'resolved':
        result['field_diagnostic'] = {'status': status, 'reason_codes': [], 'summary': '', 'evidence': []}
        return
    end = item.end_position or (page_count + 1, 0)
    within = [s for s in sightings if item.start_position <= s['_position'] < end]
    # Cropped page headers may precede the heading. They are explicitly page
    # evidence, not proof of ownership by this requirement.
    excluded = [s for s in sightings if s['page'] in item.pages and s['disposition'] != 'retained']
    selected = list(within)
    selected.extend(s for s in excluded if s not in selected)
    codes, messages = [], []
    if any(s['disposition'] != 'retained' for s in selected):
        codes.append('label_text_excluded')
        pages = sorted({s['page'] for s in selected if s['disposition'] != 'retained'})
        messages.append('第 ' + '、'.join(map(str, pages)) + ' 页被裁切或清理区域出现查询字段字样；这不证明属于本条目，请关闭页边裁切/清理后重新提取核对。')
    if any(s['disposition'] == 'retained' for s in within):
        codes.append('label_text_unparsed' if status == 'missing' else 'value_not_resolved')
        messages.append('本条目范围内找到查询字段字样，但未能可靠取得字段值；可能是未支持的排版或正文引用，未自动挪用其他条目的内容。')
    if status == 'empty' and 'value_not_resolved' not in codes:
        codes.append('value_not_resolved')
        messages.append('字段名已识别，但字段值尚未提取到；请核对原页是否为空、值是否换列或跨页。')
    damaged = [d['page'] for d in diagnostics if d['page'] in item.pages and d['status'] == 'damaged_text']
    if damaged:
        codes.append('damaged_text')
        messages.append('条目所在页含无法解码字符，可能影响字段识别；请核对文字层。')
    aliases = {'requirekey': 'requirementkey', 'requirementkey': 'requirekey',
               'reqclassification': 'requirementclassification', 'requirementclassification': 'reqclassification'}
    alternative = aliases.get(field_key(field_name))
    if alternative in item.field_labels:
        codes.append('different_label_present')
        messages.append(f"本条目识别到相近字段 {item.field_labels[alternative]}，与当前查询字段 {field_name} 名称不同；请确认查询字段，程序未自动把两者等同。")
    if not codes:
        codes.append('label_not_located')
        messages.append('在本条目的已读取文字范围内未定位到查询字段名；不等于原 PDF 没有该字段，请核对字段名称、文字层及条目边界。')
    summary = '\n'.join(messages)
    public_evidence = [{k: v for k, v in s.items() if not k.startswith('_')} for s in selected[:12]]
    result['field_diagnostic'] = {'status': status, 'reason_codes': codes, 'summary': summary,
                                  'evidence': public_evidence, 'evidence_count': len(selected)}
    result['notes'].extend(messages)


def extract_pdf(
    path: Path, query: re.Pattern[str], *, field_name: str,
    id_pattern: str = DEFAULT_ID_PATTERN,
    top_margin: float = 0, bottom_margin: float = 0,
    heading_indent: float = 36, no_tables: bool = False,
    remove_repeated: bool = False, auto_separators: bool = False,
    margin_zone: float = 10, repeat_ratio: float = 60, progress=None,
    separator_zone: float = 20, separator_min_width: float = 55,
    separator_mode: str = "conservative",
    cancel_check=None,
) -> dict[str, Any]:
    check_cancel(cancel_check)
    if separator_mode not in ("conservative", "outermost"):
        raise ValueError('横线识别模式须为 conservative 或 outermost。')
    id_start = re.compile(r"^\s*(?P<id>" + id_pattern + r")(?!\w)", re.I)
    # Marginal cleaning must protect both legacy and status-template headings.
    heading_re = id_start
    field_name = flat(field_name)
    selected_key = field_key(field_name)
    if not selected_key:
        raise ValueError("查询字段名不能为空。")
    field_keys = {field_key(name): name for name in FIELD_NAMES}
    field_keys[selected_key] = field_name
    field_patterns = make_field_patterns(list(field_keys.values()))
    items: list[Item] = []
    current: Item | None = None
    warnings: list[str] = []
    page_count = 0
    orphan_field_rows = 0
    cleaning = []
    diagnostics = []
    sightings = []
    active_section: dict[str, Any] | None = None
    pending_section = False
    deferred_section: dict[str, Any] | None = None

    with pdfplumber.open(path) as pdf:
        check_cancel(cancel_check)
        page_count = len(pdf.pages)
        repeated = repeated_keys(pdf, make_lines, zone=margin_zone / 100, ratio=repeat_ratio / 100,
            heading_pattern=heading_re, page_bbox=effective_page_bbox,
            separator_mode=separator_mode if auto_separators else "conservative",
            separator_zone=separator_zone, separator_min_width=separator_min_width,
            cancel_check=cancel_check) if remove_repeated else set()
        for page_number, original_page in enumerate(pdf.pages, 1):
            check_cancel(cancel_check)
            if progress:
                progress(page_number, page_count)
            visible = effective_page_bbox(original_page)
            visible_height = visible[3] - visible[1]
            auto_detection = (separator_analysis(original_page, bounds=visible,
                zone=separator_zone, min_width=separator_min_width, mode=separator_mode,
                cancel_check=cancel_check) if auto_separators else
                {'top': 0.0, 'bottom': 0.0, 'visible_bbox': list(visible), 'candidates': [],
                 'removed_text': {'top': [], 'bottom': []}, 'warnings': [], 'enabled': False,
                 'mode': separator_mode})
            auto_detection['enabled'] = bool(auto_separators)
            auto_top, auto_bottom = auto_detection['top'], auto_detection['bottom']
            warnings.extend(f"第 {page_number} 页：{message}" for message in auto_detection['warnings'])
            page_top, page_bottom = max(top_margin, auto_top), max(bottom_margin, auto_bottom)
            if page_top + page_bottom >= visible_height:
                raise ValueError("上下裁切范围不能覆盖整页。")
            crop = (visible[0], visible[1] + page_top, visible[2], visible[3] - page_bottom)
            cropped_lines = 0
            full_lines = None
            if page_top or page_bottom:
                full_words = original_page.crop(visible).dedupe_chars(tolerance=0.7).extract_words(
                    x_tolerance=2, y_tolerance=3)
                full_lines = make_lines(full_words)
                check_cancel(cancel_check)
                cropped_lines = sum(line.bbox[3] <= crop[1] or line.bbox[1] >= crop[3]
                                    for line in full_lines)
            page_cleaning = {'page': page_number, 'top_margin': page_top, 'bottom_margin': page_bottom,
                'removed_lines': cropped_lines, 'cropped_lines': cropped_lines,
                'repeated_removed_lines': 0,
                'auto_removed_lines': sum(len(auto_detection['removed_text'][side]) for side in ('top', 'bottom')),
                'visible_bbox': list(visible), 'crop_bbox': list(crop),
                'auto_detection': auto_detection}
            cleaning.append(page_cleaning)
            page = original_page.crop(crop)
            # Bold effects often draw the same glyph twice; deduplicate before grouping.
            page = page.dedupe_chars(tolerance=0.7)
            words = page.extract_words(x_tolerance=2, y_tolerance=3)
            check_cancel(cancel_check)
            diagnostic = {"page": page_number, "word_count": len(words), "candidate_ids": [],
                          "unparsed_headings": [], "warnings": [], "text_preview": "",
                          "engine": "pdfplumber", "image_count": len(original_page.images)}
            diagnostics.append(diagnostic)
            if not words:
                if full_lines:
                    diagnostic["status"] = "cropped_empty"
                    message = f"第 {page_number} 页原页有文字，但当前裁切范围内没有可读文字；请检查页眉页脚及手动裁切。"
                    sightings.extend(field_mentions(full_lines, field_name, page_number, visible, crop, cancel_check=cancel_check))
                else:
                    diagnostic["status"] = "no_text"
                    message = f"第 {page_number} 页无可提取文字；可能是扫描页，本工具未执行 OCR。"
                diagnostic["warnings"].append(message)
                warnings.append(message)
                if current is not None:
                    current.notes.append(message + "已停止跨页续接，避免属性错归。")
                    current.end_position = (page_number, visible[1])
                    items.append(current)
                    current = None
                active_section = None
                pending_section = False
                deferred_section = None
                original_page.close()
                continue
            lines = make_lines(words)
            check_cancel(cancel_check)
            diagnostic["status"] = "text"
            page_text = "\n".join(line.text for line in lines)
            diagnostic["text_preview"] = page_text[:12000]
            diagnostic["candidate_ids"] = list(dict.fromkeys(re.sub(r"\s+", "", m.group(0)).upper()
                for m in re.finditer(id_pattern, clean(page_text), re.I)))
            bad_chars = page_text.count("\x00") + page_text.count("\ufffd") + len(re.findall(r"\(cid:\d+\)", page_text))
            if bad_chars:
                diagnostic["status"] = "damaged_text"
                message = f"第 {page_number} 页文字层有 {bad_chars} 处无法解码字符，可能影响编号或字段识别；请核对原页或重新 OCR。"
                diagnostic["warnings"].append(message)
                warnings.append(message)
            before = len(lines)
            original_lines = list(lines)
            if remove_repeated:
                lines = [line for line in lines if not marginal_line(line, original_page.height,
                    repeated, margin_zone / 100, heading_re, bounds=visible)]
            retained_boxes = {line.bbox for line in lines}
            removed_boxes = {line.bbox for line in original_lines if line.bbox not in retained_boxes}
            sightings.extend(field_mentions(full_lines if full_lines is not None else original_lines,
                field_name, page_number, visible, crop, removed_boxes, cancel_check))
            check_cancel(cancel_check)
            page_cleaning['repeated_removed_lines'] = before - len(lines)
            page_cleaning['removed_lines'] = cropped_lines + before - len(lines)
            if not lines:
                warnings.append(f"第 {page_number} 页清理后无正文，请检查裁切和页边设置。")
                diagnostic["status"] = "cleaned_empty"
                if current is not None:
                    current.notes.append(f"第 {page_number} 页清理后无正文，已停止跨页续接。")
                    current.end_position = (page_number, visible[1])
                    items.append(current)
                    current = None
                active_section = None
                pending_section = False
                deferred_section = None
                original_page.close()
                continue
            lines = repair_field_layout(lines, field_patterns, id_start, cancel_check)
            lines = join_wrapped_lines(lines, field_patterns, id_start, cancel_check)
            events, metadata_boxes, all_boxes = [], [], []
            if not no_tables:
                try:
                    previous_box = (current.field_bbox if current and current.in_metadata
                        and current.field_source == "table" and current.field_page < page_number else None)
                    events, metadata_boxes, all_boxes = metadata_events(page, field_keys,
                        id_start=id_start, continuation_box=previous_box, cancel_check=cancel_check)
                except TaskCancelled:
                    raise
                except Exception as exc:
                    warnings.append(f"第 {page_number} 页表格识别失败，改用文字行规则：{type(exc).__name__}: {exc}")
            # Determine the left edge from tables, or a low percentile of text.
            # Linked Work Items references are normally in the right-hand cell.
            left_edges = sorted(line.bbox[0] for line in lines)
            body_left = min((box[0] for box in metadata_boxes), default=left_edges[max(0, len(left_edges) // 10)])
            for line in lines:
                check_cancel(cancel_check)
                if not any(in_box(line, box) for box in metadata_boxes):
                    events.append((line.bbox[1], line.bbox[0], "line", line))

            def consume_field(label: str, value: str, bbox: tuple, value_x: float | None = None,
                              *, source: str) -> None:
                nonlocal orphan_field_rows
                check_cancel(cancel_check)
                if current is None:
                    if field_key(label) == selected_key:
                        orphan_field_rows += 1
                    return
                if not label and (not current.in_metadata or not current.last_field):
                    warnings.append(f"第 {page_number} 页有无字段名表格续行，缺少可靠归属，未自动合并。")
                    return
                if not label and current.field_page < page_number:
                    current.notes.append(f"第 {page_number} 页无字段名续行按上一页 {current.field_labels.get(current.last_field, current.last_field)} 字段及表格位置续接，请核对。")
                current.in_metadata = True
                current.pages.add(page_number)
                current.metadata_pages.add(page_number)
                if label:
                    current.last_field = field_key(label)
                    current.field_labels.setdefault(current.last_field, flat(label))
                    current.field_parts.setdefault(current.last_field, [])
                    current.field_source = source
                    current.field_value_x = value_x
                    if source == "text" and not value and value_x is None:
                        # Label-only lines: permit a close, indented following value,
                        # bounded by the next known field or requirement heading.
                        current.field_value_x = bbox[0]
                current.field_bbox = bbox
                current.field_page = page_number
                if source == "text" and "使用文字行规则识别属性，请核对字段边界。" not in current.notes:
                    current.notes.append("使用文字行规则识别属性，请核对字段边界。")
                if current.last_field:
                    if value:
                        current.field_parts[current.last_field].append(value)
                    current.evidence.setdefault(current.last_field, []).append({
                        "page": page_number,
                        "bbox": [round(v - visible[index % 2], 2) for index, v in enumerate(bbox)],
                        "coordinate_space": "visible_page_top_left_pt", "text": value})

            ordered_events = sorted(events, key=lambda event: (event[0], event[1]))
            if deferred_section is not None:
                leading = ordered_events[:3]
                if any(event[2] == "line" and requirement_heading(event[3].text, id_start)
                       and all(previous[2] == "line" for previous in leading[:index])
                       for index, event in enumerate(leading)):
                    active_section = deferred_section
                    pending_section = True
                elif current is not None and not current.in_metadata:
                    current.description_lines.extend(deferred_section["raw"].splitlines())
                    current.pages.add(deferred_section["page"])
                deferred_section = None
            page_continuation = False
            defer_tail = False
            for event_index, (_, _, kind, data) in enumerate(ordered_events):
                check_cancel(cancel_check)
                if defer_tail:
                    continue
                if kind == "field":
                    consume_field(*data, source="table")
                    pending_section = False
                    continue
                line = data
                heading = requirement_heading(line.text, id_start)
                # Section headings are accepted only immediately before a real
                # requirement, so e.g. "1.3 GHz ..." in a paragraph is not lost.
                section = SECTION_RE.match(line.text)
                unit_text = section and re.match(r"^(?:GHz|MHz|kHz|Hz|V|W|mm|cm|kg|mg|MPa|kPa)\b", section.group("title"), re.I)
                if section and not unit_text and line.bbox[0] <= body_left + heading_indent:
                    nearby = ordered_events[event_index + 1:event_index + 4]
                    if any(e[2] == "line" and requirement_heading(e[3].text, id_start)
                           and all(prior[2] == "line" for prior in nearby[:index])
                           for index, e in enumerate(nearby)):
                        active_section = {"title": section.group("title"), "raw": line.text, "page": page_number}
                        pending_section = True
                        continue
                    tail = ordered_events[event_index + 1:]
                    # Defer a short chapter at a page boundary. Commit it only
                    # if the next page immediately starts a requirement; else
                    # restore the literal lines to the previous description.
                    if (len(tail) <= 2 and page_number < page_count
                            and all(e[2] == "line" and not parse_field_line(e[3].text, field_patterns) for e in tail)):
                        trailing = [e[3].text for e in tail]
                        deferred_section = {"title": " ".join([section.group("title"), *trailing]),
                            "raw": "\n".join([line.text, *trailing]), "page": page_number}
                        defer_tail = True
                        continue
                if heading and line.bbox[0] > body_left + heading_indent:
                    diagnostic["unparsed_headings"].append(line.text)
                    warning = f"第 {page_number} 页编号 {heading[0]} 位于正文缩进范围外，可能是关联引用或缩进标题；未作为新条目，请核对标题缩进设置。"
                    diagnostic["warnings"].append(warning)
                    warnings.append(warning)
                if heading and line.bbox[0] <= body_left + heading_indent:
                    # Two independently formatted headings on one baseline are
                    # an unsupported layout, never silently a single title.
                    candidates = list(re.finditer(id_pattern, line.text.translate(DASHES), re.I))
                    if len(candidates) > 1:
                        warnings.append(f"第 {page_number} 页同一行包含多个需求编号，可能为双栏或并排内容；请按列拆分 PDF 并核对本页，当前版本按单列读取。")
                    if current is not None:
                        current.end_position = (page_number, line.bbox[1])
                        items.append(current)
                    item_id, payload, status = heading
                    title = active_section["title"] if status and active_section else ("" if status else payload)
                    heading_text = (active_section["raw"] + "\n" if status and active_section else "") + line.text
                    current = Item(str(path.resolve()), item_id, title, heading_text)
                    current.start_position = (page_number, line.bbox[1])
                    if not status and not payload:
                        current.notes.append("编号后未识别到标题，已保留条目和字段，请核对标题。")
                    if status:
                        if payload:
                            current.description_lines.append(payload)
                            current.initial_description_in_heading = True
                        if not active_section:
                            current.notes.append("识别到编号和状态，但没有找到关联章节标题；标题留空，请核对。")
                        elif active_section["page"] < page_number:
                            current.notes.append("章节标题位于上一页，已按紧邻的需求编号关联，请核对。")
                        if status.casefold() not in {name.casefold() for name in STATUS_NAMES}:
                            current.notes.append(f"识别到自定义状态 {status}，请核对编号后的描述边界。")
                    current.pages.add(page_number)
                    pending_section = False
                    page_continuation = False
                    continue
                if pending_section and active_section:
                    active_section["title"] += " " + line.text
                    active_section["raw"] += "\n" + line.text
                    continue
                if id_start.match(clean(line.text)) and line.bbox[0] <= body_left + heading_indent:
                    # Refuse to attach an unparsed new block's attributes to the
                    # previous item, which was the most dangerous failure mode.
                    diagnostic["unparsed_headings"].append(line.text)
                    warnings.append(f"第 {page_number} 页发现未解析的编号开头文字：{line.text[:100]}。请核对编号/状态格式；后续属性不归入上一条。")
                    if current is not None:
                        current.end_position = (page_number, line.bbox[1])
                        items.append(current)
                    current = None
                    continue
                parsed = parse_field_line(line.text, field_patterns)
                if parsed:
                    consume_field(*parsed, line.bbox, text_value_x(line, parsed[0]), source="text")
                elif current is not None and not current.in_metadata:
                    current.description_lines.append(line.text)
                    current.pages.add(page_number)
                elif current is not None and current.last_field == selected_key:
                    # In a metadata block, a new 'Label: value' is a boundary
                    # even if it is not in our vocabulary. Do not turn another
                    # field's value into a match for the selected field.
                    unknown_label = re.match(r"^([A-Za-z][A-Za-z /_()\-]{1,60})[:：]\s+\S", line.text)
                    if current.field_source == "text" and unknown_label:
                        current.notes.append(f"第 {page_number} 页遇到未配置字段 {unknown_label.group(1)}，已停止上一字段续接，请核对。")
                        current.last_field = ""
                        continue
                    aligned = current.field_value_x is not None and line.bbox[0] >= current.field_value_x - 3
                    close_value = current.field_page == page_number and current.field_bbox is not None and line.bbox[1] - current.field_bbox[3] <= 25
                    if current.field_source == "text" and aligned and (current.field_parts.get(selected_key) or close_value):
                        consume_field("", line.text, line.bbox, current.field_value_x, source="text")
                    elif (current.field_source == "table" and aligned
                          and (current.field_page < page_number or page_continuation)):
                        current.notes.append(f"第 {page_number} 页无标签文字按字段值列位置续接，请核对跨页字段边界。")
                        consume_field("", line.text, line.bbox, current.field_value_x, source="table")
                        page_continuation = True
                    elif current.field_source == "text":
                        current.notes.append(f"第 {page_number} 页文字未与 {field_name} 的值列对齐，已停止续接，避免把后续字段误判为命中；请核对。")
                        current.last_field = ""
            original_page.close()
            check_cancel(cancel_check)
    if current is not None:
        items.append(current)
    if orphan_field_rows:
        warnings.append(f"发现 {orphan_field_rows} 个 {field_name} 行没有对应标题，未分配给其他条目。")
    if not items:
        warnings.append("未识别到需求标题。请核对 PDF 是否有文字层，以及编号和标题格式。")
    results = []
    sightings_by_page = {}
    for sighting in sightings:
        sightings_by_page.setdefault(sighting['page'], []).append(sighting)
    for item in items:
        check_cancel(cancel_check)
        result = item.as_dict(query, field_name)
        end_page = item.end_position[0] if item.end_position else page_count
        relevant_pages = set(range(item.start_position[0], min(end_page, page_count) + 1)) | item.pages
        local_sightings = [s for number in sorted(relevant_pages) for s in sightings_by_page.get(number, [])]
        explain_field_resolution(item, result, local_sightings, diagnostics, field_name, page_count)
        results.append(result)
    return {"source_file": str(path.resolve()), "page_count": page_count,
            "warnings": warnings, "page_cleaning": cleaning, "diagnostics": diagnostics,
            "field_label_sightings": [{k: v for k, v in s.items() if not k.startswith('_')} for s in sightings],
            "items": results}


MATCH_FIELDS = {"item_id": "编号", "title": "标题", "description": "具体描述"}

CSV_FIELDS = {
    "matched": "命中", "source_file": "来源文件", "item_id": "编号",
    "title": "标题", "description": "描述", "text_before_metadata": "表格前完整文字",
    "query_field": "查询字段", "field_value": "字段原文", "pages": "条目PDF页码",
    "metadata_pages": "属性表PDF页码", "notes": "核对提示",
}


def write_csv(path: Path, rows: list[dict[str, Any]],
              fields: dict[str, str] | None = None, *, cancel_check=None) -> None:
    check_cancel(cancel_check)
    fields = CSV_FIELDS if fields is None else fields
    with path.open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=list(fields.values()))
        writer.writeheader()
        for item in rows:
            check_cancel(cancel_check)
            row = {}
            for key, label in fields.items():
                value = item[key]
                if isinstance(value, list):
                    value = "; ".join(map(str, value))
                elif isinstance(value, bool):
                    value = "是" if value else "否"
                # Keep spreadsheet applications from interpreting source text
                # as formulas. The JSON report preserves the original text.
                if isinstance(value, str) and value.lstrip().startswith(("=", "+", "-", "@")):
                    value = "'" + value
                row[label] = value
            writer.writerow(row)


def collect_pdfs(inputs: list[str]) -> list[Path]:
    result: set[Path] = set()
    for name in inputs:
        path = Path(name).expanduser()
        if path.is_dir():
            result.update(p.resolve() for p in path.rglob("*") if p.is_file() and p.suffix.lower() == ".pdf")
        elif path.is_file() and path.suffix.lower() == ".pdf":
            result.add(path.resolve())
        else:
            raise ValueError(f"不是有效的 PDF 文件或文件夹：{name}")
    if not result:
        raise ValueError("未找到 PDF 文件。")
    return sorted(result, key=str)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="按自选字段和内容筛选 PDF 需求条目，不使用大模型。")
    parser.add_argument("inputs", nargs="+", help="PDF 文件或包含 PDF 的文件夹；可传多个")
    parser.add_argument("--field", metavar="字段名", help="在哪个字段中查，例如 Product Structure、Test Level")
    parser.add_argument("--query", metavar="查询内容", help="在指定字段中要包含的内容")
    # Retain the previous command as a shortcut; the main interface is generic.
    parser.add_argument("--product-structure", help=argparse.SUPPRESS)
    parser.add_argument("--out", default="results", help="输出文件夹，默认 results")
    parser.add_argument("--id-pattern", default=DEFAULT_ID_PATTERN, help="条目编号的正则表达式")
    parser.add_argument("--top-margin", type=float, default=0, help="忽略页顶多少 pt，默认 0")
    parser.add_argument("--bottom-margin", type=float, default=0, help="忽略页底多少 pt，默认 0")
    parser.add_argument("--heading-indent", type=float, default=36, help="标题相对正文左边缘允许的缩进 pt")
    parser.add_argument("--no-tables", action="store_true", help="仅使用文字行规则；用于表格线无法识别时")
    parser.add_argument("--auto-separators", action="store_true", help="按横线识别并排除页眉页脚")
    parser.add_argument("--separator-mode", choices=("outermost", "conservative"), default="outermost",
                        help="outermost：顶部第一条/底部最后一条；conservative：保守识别")
    parser.add_argument("--separator-zone", type=float, default=20, help="横线搜索范围：页顶/页底各占页面高度的百分比")
    parser.add_argument("--separator-min-width", type=float, default=55, help="候选横线至少占可见页宽的百分比")
    args = parser.parse_args(argv)
    if args.product_structure is not None:
        if args.field is not None or args.query is not None:
            parser.error("请使用 --field 和 --query；不要同时使用旧版 --product-structure。")
        field_name, query_text = "Product Structure", args.product_structure
    else:
        if args.field is None or args.query is None:
            parser.error("请同时指定 --field \"字段名\" 和 --query \"查询内容\"。")
        field_name, query_text = flat(args.field), args.query
    if not field_key(field_name):
        parser.error("查询字段名不能为空。")
    if min(args.top_margin, args.bottom_margin, args.heading_indent) < 0:
        parser.error("裁切范围和缩进不能为负数。")
    if not 1 <= args.separator_zone <= 40 or not 1 <= args.separator_min_width <= 100:
        parser.error("横线搜索范围须为 1–40%，最小宽度须为 1–100%。")
    try:
        query = compile_query(query_text)
        re.compile(args.id_pattern)
        files = collect_pdfs(args.inputs)
    except (ValueError, re.error) as exc:
        parser.error(str(exc))
    reports, failures = [], []
    for index, path in enumerate(files, 1):
        print(f"[{index}/{len(files)}] {path.name}", file=sys.stderr)
        try:
            report = extract_pdf(path, query, field_name=field_name, id_pattern=args.id_pattern,
                top_margin=args.top_margin, bottom_margin=args.bottom_margin,
                heading_indent=args.heading_indent, no_tables=args.no_tables,
                auto_separators=args.auto_separators, separator_mode=args.separator_mode,
                separator_zone=args.separator_zone, separator_min_width=args.separator_min_width)
            reports.append(report)
            for warning in report["warnings"]:
                print(f"  提示：{warning}", file=sys.stderr)
        except Exception as exc:
            failures.append({"source_file": str(path), "error": f"{type(exc).__name__}: {exc}"})
            print(f"  失败：{exc}", file=sys.stderr)
    all_items = [item for report in reports for item in report["items"]]
    matches = [item for item in all_items if item["matched"]]
    output = Path(args.out)
    output.mkdir(parents=True, exist_ok=True)
    write_csv(output / "matches.csv", matches, MATCH_FIELDS)
    write_csv(output / "all_items.csv", all_items)
    full_report = {"query_field": field_name, "query": query_text, "matched_count": len(matches),
                   "item_count": len(all_items), "files": reports, "failures": failures}
    (output / "extraction_report.json").write_text(json.dumps(full_report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"识别 {len(all_items)} 条需求，命中 {len(matches)} 条。结果：{output.resolve()}")
    return 2 if failures or not all_items or any(r["warnings"] for r in reports) else 0


if __name__ == "__main__":
    raise SystemExit(main())
