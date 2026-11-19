"""Conservative, position-aware removal of repeated PDF marginal text."""
from __future__ import annotations
import math
import re
import unicodedata
from collections import defaultdict
from task_control import check_cancel


def normalized_heading(text):
    return unicodedata.normalize('NFKC', text).translate(str.maketrans({c: '-' for c in '‐‑‒–—―−－﹣'}))


def signature(text: str) -> str:
    # Numbers in a requirement are data, not page-number placeholders.
    return re.sub(r'\s+', ' ', text).strip().casefold()


def edge_side(bbox, height: float, zone: float) -> str:
    if bbox[3] <= height * zone:
        return 'top'
    if bbox[1] >= height * (1 - zone):
        return 'bottom'
    return ''


def _visible_bbox(page):
    box = page.bbox
    crop = getattr(page, 'cropbox', None) or box
    return (max(box[0], crop[0]), max(box[1], crop[1]),
            min(box[2], crop[2]), min(box[3], crop[3]))


def _margin_rules(page, bounds, *, mode="conservative", zone=20, min_width=55, cancel_check=None):
    """Use exactly the accepted geometric boundary for repeated-text cleanup.

    A deeper body rule must not make text preserved by automatic cropping
    eligible for removal by the separate repetition pass.
    """
    return [candidate['y'] for candidate in separator_analysis(page, bounds=bounds, mode=mode, zone=zone, min_width=min_width, cancel_check=cancel_check)['candidates']
            if candidate['accepted']]


def repeated_keys(pdf, make_lines, *, zone: float = .10, ratio: float = .60,
                  heading_pattern=None, page_bbox=None, separator_mode="conservative",
                  separator_zone=20, separator_min_width=55, cancel_check=None):
    """Repeat alone is insufficient: require a separate header/footer region.

    Plain repeated prose close to an edge is retained. Users can use the
    preview's explicit crop settings for documents without separating rules.
    """
    seen = defaultdict(set)
    for number, page in enumerate(pdf.pages):
        check_cancel(cancel_check)
        bounds = page_bbox(page) if page_bbox else _visible_bbox(page)
        height = bounds[3] - bounds[1]
        rules = _margin_rules(page, bounds, mode=separator_mode,
                              zone=separator_zone, min_width=separator_min_width, cancel_check=cancel_check)
        cropped = page.crop(bounds)
        for line in make_lines(cropped.extract_words(x_tolerance=2, y_tolerance=3)):
            check_cancel(cancel_check)
            relative = (line.bbox[0], line.bbox[1] - bounds[1],
                        line.bbox[2], line.bbox[3] - bounds[1])
            side = edge_side(relative, height, zone)
            if not side or (heading_pattern and heading_pattern.match(normalized_heading(line.text))):
                continue
            separated = (side == 'top' and any(line.bbox[3] < y for y in rules
                         if y <= bounds[1] + height * separator_zone / 100)) or (
                         side == 'bottom' and any(line.bbox[1] > y for y in rules
                         if y >= bounds[3] - height * separator_zone / 100))
            if not separated:
                continue
            seen[(side, signature(line.text))].add(number)
        page.close()
    required = max(2, math.ceil(len(pdf.pages) * ratio))
    return {key for key, pages in seen.items() if len(pages) >= required}


def marginal_line(line, height: float, keys, zone: float, heading_pattern=None, *, bounds=None):
    bbox = line.bbox
    if bounds is not None:
        height = bounds[3] - bounds[1]
        bbox = (bbox[0], bbox[1] - bounds[1], bbox[2], bbox[3] - bounds[1])
    side = edge_side(bbox, height, zone)
    if not side or (heading_pattern and heading_pattern.match(normalized_heading(line.text))):
        return False
    value = line.text.strip()
    # Separators and explicit page labels are safe to remove at the page edge.
    separator = bool(re.fullmatch(r'[-_=~.·•—–\s]{4,}', value))
    page_number = bool(re.fullmatch(r'(?:page\s*\d+\s*(?:of|/)\s*\d+|第\s*\d+\s*页(?:\s*[/共]\s*\d+\s*页?)?)', value, re.I))
    return separator or page_number or (side, signature(value)) in keys


def _text_lines(words):
    """Small geometry-only line grouping, independent of the requirement parser."""
    groups = []
    for word in sorted(words, key=lambda w: (w['top'], w['x0'])):
        if groups and abs(word['top'] - groups[-1][0]['top']) <= 3:
            groups[-1].append(word)
        else:
            groups.append([word])
    return [' '.join(w['text'] for w in sorted(group, key=lambda w: w['x0']))
            for group in groups]


def _join_rule_fragments(geometry, page_width, cancel_check=None):
    """Join physical dash segments before applying the line-width threshold.

    PDF exporters sometimes draw each dash as a separate solid edge. Require
    close collinearity, small gaps, and at least three fragments; two fragments
    may join only when effectively continuous. This does not infer raster lines.
    """
    groups = []
    for rule in sorted(geometry, key=lambda r: (r['y'], r['bbox'][0])):
        check_cancel(cancel_check)
        if rule['kind'] == 'filled_rectangle':
            groups.append([rule])
            continue
        if groups and groups[-1][0]['kind'] != 'filled_rectangle':
            first, previous = groups[-1][0], groups[-1][-1]
            gap = rule['bbox'][0] - previous['bbox'][2]
            if (abs(rule['y'] - first['y']) <= 0.6 and -0.5 <= gap <= min(12.0, page_width * .02)
                    and abs(rule['linewidth'] - first['linewidth']) <= .5):
                groups[-1].append(rule)
                continue
        groups.append([rule])
    result = []
    for group in groups:
        check_cancel(cancel_check)
        gaps = [b['bbox'][0] - a['bbox'][2] for a, b in zip(group, group[1:])]
        if len(group) >= 3 or (len(group) == 2 and max(gaps) <= 2):
            first, last = group[0], group[-1]
            span = last['bbox'][2] - first['bbox'][0]
            coverage = sum(r['bbox'][2] - r['bbox'][0] for r in group)
            if span > 0 and coverage / span >= .4:
                result.append({'bbox': (first['bbox'][0], min(r['bbox'][1] for r in group),
                                           last['bbox'][2], max(r['bbox'][3] for r in group)),
                               'y': sum(r['y'] for r in group) / len(group),
                               'kind': 'segmented_line',
                               'linewidth': max(r['linewidth'] for r in group),
                               'fragment_count': len(group)})
                continue
        result.extend(group)
    return result


def separator_analysis(page, *, bounds=None, zone=20, min_width=55, mode="conservative", cancel_check=None):
    """Recognize isolated geometric separators, never using text wording/repetition.

    Candidate ``bbox`` and ``y`` use pdfplumber's absolute rotated top-origin
    coordinates. ``top`` / ``bottom`` are points relative to ``visible_bbox``.
    Conservative mode requires a trustworthy isolated rule. Outermost mode
    uses the first top-zone rule and last bottom-zone rule by geometry alone;
    it intentionally bypasses text, table, and image-content evidence checks.
    Both modes keep the configured edge zones and minimum horizontal width.
    """
    check_cancel(cancel_check)
    if mode not in ("conservative", "outermost"):
        raise ValueError('横线识别模式须为 conservative 或 outermost。')
    zone, min_width = float(zone), float(min_width)
    if not math.isfinite(zone) or not 1 <= zone <= 40:
        raise ValueError('分隔线识别区域须为 1–40%。')
    if not math.isfinite(min_width) or not 1 <= min_width <= 100:
        raise ValueError('分隔线最短宽度须为 1–100%。')
    bounds = tuple(bounds or _visible_bbox(page))
    x0, y0, x1, y1 = bounds
    width, height = x1 - x0, y1 - y0
    if width <= 0 or height <= 0:
        raise ValueError('页面可见范围无效。')
    result = {'top': 0.0, 'bottom': 0.0, 'visible_bbox': list(bounds),
              'zone': zone, 'min_width': min_width, 'mode': mode, 'candidates': [],
              'removed_text': {'top': [], 'bottom': []}, 'warnings': []}
    edges = page.edges
    check_cancel(cancel_check)
    # Intersections anywhere along a rule count, including internal table dividers.
    vertical = [e for e in edges if abs(e['x1'] - e['x0']) <= 1.5
                and e['bottom'] - e['top'] > 5]
    raw = []
    # A thin filled rectangle is commonly how Word/PDF exports a horizontal rule.
    for rectangle in page.rects:
        check_cancel(cancel_check)
        thickness = rectangle['bottom'] - rectangle['top']
        if rectangle.get('fill') and 0 < thickness <= 3.0:
            raw.append({'bbox': (rectangle['x0'], rectangle['top'], rectangle['x1'], rectangle['bottom']),
                        'kind': 'filled_rectangle', 'linewidth': thickness})
    filled = [r['bbox'] for r in raw]
    for edge in edges:
        check_cancel(cancel_check)
        if abs(edge['bottom'] - edge['top']) > 1.5:
            continue
        box = (edge['x0'], edge['top'], edge['x1'], edge['bottom'])
        if any(abs(box[0] - r[0]) <= 1 and abs(box[2] - r[2]) <= 1
               and r[1] - 1 <= (box[1] + box[3]) / 2 <= r[3] + 1 for r in filled):
            continue
        dash = edge.get('dash')
        raw.append({'bbox': box, 'kind': 'dashed_line' if dash and dash[0] else 'solid_line',
                    'linewidth': float(edge.get('linewidth') or 0.5)})
    # Identical stroke/rectangle representations must not create extra candidates.
    geometry = []
    for rule in sorted(raw, key=lambda r: (r['bbox'][1], r['bbox'][0])):
        check_cancel(cancel_check)
        a, top, b, bottom = rule['bbox']
        a, b = max(a, x0), min(b, x1)
        y = (top + bottom) / 2
        if b <= a or not y0 <= y <= y1:
            continue
        if any(abs(y - r['y']) <= 0.6 and abs(a-r['bbox'][0]) <= 1
               and abs(b-r['bbox'][2]) <= 1 for r in geometry):
            continue
        geometry.append({**rule, 'bbox': (a, top, b, bottom), 'y': y})
    geometry = _join_rule_fragments(geometry, width, cancel_check)
    visible_page = page.crop(bounds)
    words = visible_page.extract_words(x_tolerance=2, y_tolerance=3)
    chars = visible_page.chars
    images = visible_page.images
    check_cancel(cancel_check)
    eligible = []
    for rule in geometry:
        check_cancel(cancel_check)
        a, top, b, bottom = rule['bbox']
        span, y = b - a, rule['y']
        # Retain sufficiently long objects in diagnostics, including rejected
        # central/table lines. Very short ornaments are not separator candidates.
        if span < width * min_width / 100:
            continue
        side = 'top' if y <= y0 + height * zone / 100 else (
               'bottom' if y >= y1 - height * zone / 100 else '')
        candidate = {'bbox': [round(v, 3) for v in rule['bbox']], 'y': round(y, 3),
                     'side': side, 'kind': rule['kind'], 'accepted': False, 'reason': ''}
        if 'fragment_count' in rule:
            candidate['fragment_count'] = rule['fragment_count']
        result['candidates'].append(candidate)
        reason = ''
        if not side:
            reason = '位于正文区域，超出页边识别范围。'
        elif rule['linewidth'] > 4:
            reason = '横线较粗，可能是正文图形或色块，保留。'
        elif mode == 'outermost':
            # The user explicitly chooses geometric first/last boundaries.
            # Header boxes often connect to vertical lines, have no text layer,
            # or sit close to prose; these must remain eligible in this mode.
            pass
        elif any(v['top'] - 2 <= y <= v['bottom'] + 2 and a - 2 <= v['x0'] <= b + 2
                 for v in vertical):
            reason = '与竖线相接或相交，可能是表格/边框，保留。'
        elif any(ch['top'] < bottom + 1 and ch['bottom'] > top - 1 for ch in chars):
            reason = '分隔线穿过文字或与文字过近，保留。'
        else:
            # Three similarly aligned, regularly spaced rules indicate a grid
            # even when its vertical borders are absent from the PDF.
            aligned = sorted(r['y'] for r in geometry if abs(r['bbox'][0]-a) <= 8
                             and abs(r['bbox'][2]-b) <= 8 and abs(r['y']-y) <= 110)
            grid = False
            for i in range(len(aligned)-2):
                triplet = aligned[i:i+3]
                d1, d2 = triplet[1]-triplet[0], triplet[2]-triplet[1]
                if min(d1, d2) >= 8 and max(d1, d2) <= 70 and max(d1,d2) <= min(d1,d2)*1.45:
                    if triplet[0]-1 <= y <= triplet[-1]+1:
                        grid = True
                        break
            if grid:
                reason = '多条等距平行线构成疑似表格，保留。'
        cut = (bottom + max(1.0, rule['linewidth']/2 + 0.5) if side == 'top'
               else top - max(1.0, rule['linewidth']/2 + 0.5))
        outside = ([w for w in words if w['bottom'] < top - 1] if side == 'top'
                   else [w for w in words if w['top'] > bottom + 1])
        if not reason and not y0 < cut < y1:
            reason = '裁切位置超出页面可见范围，保留。'
        if mode == 'conservative' and not reason and not outside:
            reason = '线外未找到独立页眉/页脚文字，证据不足，保留。'
        if mode == 'conservative' and not reason and any(ch['top'] < cut < ch['bottom'] for ch in chars):
            reason = '裁切位置会穿过文字，保留。'
        if mode == 'conservative' and not reason and any(im['top'] < cut < im['bottom'] for im in images):
            reason = '裁切位置会穿过图像，无法确定页边边界，保留。'
        if reason:
            candidate['reason'] = reason
        else:
            candidate['reason'] = '几何位置符合候选条件，等待选择。'
            eligible.append((candidate, cut, outside))
    for side in ('top', 'bottom'):
        choices = sorted((item for item in eligible if item[0]['side'] == side),
                         key=lambda item: item[0]['y'], reverse=side == 'bottom')
        if not choices:
            continue
        selected, cut, outside = choices[0]
        selected['accepted'] = True
        selected['reason'] = ('首尾横线模式：采用页顶第一条横线作为页眉边界。' if side == 'top'
                              else '首尾横线模式：采用页底最后一条横线作为页脚边界。') if mode == 'outermost' else (
                              '靠近页边的首条独立分隔线，线外有文字且不与正文/表格相交。')
        if mode == 'outermost' and (any(ch['top'] < cut < ch['bottom'] for ch in chars)
                or any(im['top'] < cut < im['bottom'] for im in images)):
            result['warnings'].append(('页顶' if side == 'top' else '页底') +
                '首尾横线的裁切位置穿过文字或图像；已按所选模式采用，请核对预览。')
        selected['cut'] = round(cut, 3)
        result[side] = round(cut - y0 if side == 'top' else y1 - cut, 3)
        # Evidence can require a gap from the rule, but the audit must reflect
        # the actual padded cut, including near-rule words removed by that cut.
        removed_words = ([w for w in words if w['bottom'] <= cut] if side == 'top'
                         else [w for w in words if w['top'] >= cut])
        result['removed_text'][side] = _text_lines(removed_words)
        for candidate, _, _ in choices[1:]:
            candidate['reason'] = ('已有更靠近页边的横线；首尾横线模式只采用最外侧一条。' if mode == 'outermost'
                                   else '已有更靠近页边的可信分隔线；保留较深位置，避免裁掉正文。')
        if len(choices) > 1:
            result['warnings'].append(('页顶' if side == 'top' else '页底') +
                '存在多条候选分隔线，仅采用最靠近页边的一条，请通过预览核对。')
    for candidate in result['candidates']:
        if candidate['side'] and not candidate['accepted'] and '已有更靠近' not in candidate['reason']:
            result['warnings'].append(('页顶' if candidate['side'] == 'top' else '页底') +
                f"候选线 y={candidate['y']:.1f} pt：" + candidate['reason'])
    return result


def separator_bounds(page, *, bounds=None, zone=20, min_width=55, mode="conservative", cancel_check=None):
    """Backward-compatible crop tuple; conservative unless explicitly overridden."""
    analysis = separator_analysis(page, bounds=bounds, zone=zone, min_width=min_width, mode=mode, cancel_check=cancel_check)
    return analysis['top'], analysis['bottom']
