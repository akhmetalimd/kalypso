"""SVG-графики на сервере (без JavaScript; CSP default-src 'self').

Цвета — только через CSS-классы (.s1 … .s6, .muted-fill) и переменные --series-N из
style.css: светлая и тёмная тема выбраны отдельно и проверены валидатором палитры
(навык dataviz). Подписи — текстовыми токенами, не цветом серии. Подсказка при
наведении — нативная <title> у каждой метки. Цифры каждого графика дублируются
таблицей в шаблоне (контраст части оттенков на светлом фоне ниже 3:1).
"""
from __future__ import annotations

from html import escape

W = 720            # ширина viewBox; на странице svg тянется на 100 %
FONT = 12
MAX_SERIES = 6     # больше — сворачивать в «прочее» или разбивать на панели


def _svg(height: int, body: str, title: str, desc: str = "") -> str:
    return (f'<svg class="viz" viewBox="0 0 {W} {height}" role="img" preserveAspectRatio="xMinYMin meet" '
            f'xmlns="http://www.w3.org/2000/svg"><title>{escape(title)}</title>'
            f'<desc>{escape(desc)}</desc>{body}</svg>')


def _text(x, y, s, cls="viz-text", anchor="start", size=FONT) -> str:
    return (f'<text x="{x:.1f}" y="{y:.1f}" class="{cls}" text-anchor="{anchor}" '
            f'font-size="{size}">{escape(str(s))}</text>')


def _bar_path(x0, y, length, thick, horizontal=True, r=4) -> str:
    """Полоса со скруглённым (4px) концом данных и прямым основанием."""
    r = min(r, length / 2, thick / 2)
    if length <= 0:
        return ""
    if horizontal:
        x1 = x0 + length
        return (f"M{x0:.1f},{y:.1f}H{x1 - r:.1f}Q{x1:.1f},{y:.1f} {x1:.1f},{y + r:.1f}"
                f"V{y + thick - r:.1f}Q{x1:.1f},{y + thick:.1f} {x1 - r:.1f},{y + thick:.1f}H{x0:.1f}Z")
    # вертикальная колонка: y — основание, растёт вверх
    top = y - length
    return (f"M{x0:.1f},{y:.1f}V{top + r:.1f}Q{x0:.1f},{top:.1f} {x0 + r:.1f},{top:.1f}"
            f"H{x0 + thick - r:.1f}Q{x0 + thick:.1f},{top:.1f} {x0 + thick:.1f},{top + r:.1f}V{y:.1f}Z")


def _pct(n, total) -> str:
    return f"{100 * n / total:.0f} %" if total else "—"


# ---------------------------------------------------------------------------
# Горизонтальные полосы: категория -> число пациентов (n и %)
# ---------------------------------------------------------------------------
def bar_chart(items: list[tuple[str, int, bool]], title: str, total: int | None = None) -> str:
    """items: (подпись, n, служебная категория?) — служебные (коды пропуска, «не определено»)
    рисуются нейтральным серым, чтобы не выглядеть как результат."""
    total = total if total is not None else sum(n for _, n, _ in items)
    label_w, row, thick = 210, 30, 16
    track = W - label_w - 90
    peak = max([n for _, n, _ in items] + [1])
    body = []
    for i, (label, n, muted) in enumerate(items):
        y = 8 + i * row
        length = track * n / peak
        cls = "bar muted-fill" if muted else "bar s1"
        body.append(f'<g><title>{escape(label)}: {n} из {total} ({_pct(n, total)})</title>'
                    f'{_text(label_w - 10, y + thick - 3, label, "viz-text", "end")}'
                    f'<path class="{cls}" d="{_bar_path(label_w, y, max(length, 0.5), thick)}"/>'
                    f'{_text(label_w + length + 6, y + thick - 3, f"{n} · {_pct(n, total)}", "viz-muted")}</g>')
    body.insert(0, f'<line class="axis" x1="{label_w}" x2="{label_w}" y1="4" y2="{8 + len(items) * row - 10}"/>')
    return _svg(16 + len(items) * row, "".join(body), title, f"{len(items)} категорий, всего {total}")


# ---------------------------------------------------------------------------
# Колонки по годам
# ---------------------------------------------------------------------------
def column_chart(items: list[tuple[str, int]], title: str) -> str:
    h, top, base = 220, 24, 190
    n = max(len(items), 1)
    band = (W - 40) / n
    thick = min(24, band * 0.6)
    peak = max([v for _, v in items] + [1])
    body = [f'<line class="axis" x1="20" x2="{W - 20}" y1="{base}" y2="{base}"/>']
    for i, (label, v) in enumerate(items):
        x = 20 + i * band + (band - thick) / 2
        length = (base - top) * v / peak
        body.append(f'<g><title>{escape(label)}: {v}</title>'
                    f'<path class="bar s1 col" d="{_bar_path(x, base, max(length, 0.5), thick, horizontal=False)}"/>'
                    f'{_text(x + thick / 2, base - length - 5, v, "viz-muted", "middle")}'
                    f'{_text(x + thick / 2, base + 16, label, "viz-muted", "middle", 11)}</g>')
    return _svg(h, "".join(body), title, f"{len(items)} значений")


# ---------------------------------------------------------------------------
# Сложенные 100 % полосы: строка -> доли категорий (2px зазор между сегментами)
# ---------------------------------------------------------------------------
def stacked_bars(rows: list[tuple[str, dict[str, int]]], categories: list[tuple[str, str]], title: str) -> str:
    cats = categories[:MAX_SERIES]
    label_w, row, thick, gap = 130, 30, 16, 2
    track = W - label_w - 60
    legend_h = 26
    body = [_legend(cats, 0)]
    for i, (label, counts) in enumerate(rows):
        y = legend_h + 8 + i * row
        total = sum(counts.get(k, 0) for k, _ in cats)
        body.append(_text(label_w - 10, y + thick - 3, label, "viz-text", "end"))
        x = label_w
        for j, (key, name) in enumerate(cats):
            n = counts.get(key, 0)
            if not n:
                continue
            w = track * n / total - gap
            body.append(f'<rect class="seg s{j + 1}" x="{x:.1f}" y="{y}" width="{max(w, 1):.1f}" height="{thick}">'
                        f'<title>{escape(label)} · {escape(name)}: {n} из {total} ({_pct(n, total)})</title></rect>')
            x += w + gap
        body.append(_text(W - 50, y + thick - 3, f"n={total}", "viz-muted"))
    return _svg(legend_h + 16 + len(rows) * row, "".join(body), title, f"{len(rows)} строк, {len(cats)} категорий")


def _legend(series: list[tuple[str, str]], y: int, line: bool = False) -> str:
    out, x = [], 0
    for j, (_, name) in enumerate(series):
        swatch = (f'<line class="line s{j + 1}" x1="{x}" x2="{x + 18}" y1="{y + 10}" y2="{y + 10}"/>' if line else
                  f'<rect class="seg s{j + 1}" x="{x}" y="{y + 4}" width="12" height="12" rx="2"/>')
        out.append(swatch + _text(x + (24 if line else 18), y + 14, name, "viz-text"))
        x += 34 + len(name) * 7
    return "".join(out)


# ---------------------------------------------------------------------------
# Каплан–Майер: ступенчатые кривые, засечки цензурирования, полоса ДИ (для 1–2 групп)
# ---------------------------------------------------------------------------
DAYS_PER_MONTH = 30.4375


def km_chart(groups: list[dict], title: str, x_max_months: float | None = None) -> str:
    """groups: [{label, km: результат registry_core.km_estimate}] — не больше MAX_SERIES."""
    groups = groups[:MAX_SERIES]
    left, right, top, legend_h = 46, 16, 34, 26
    plot_h, h = 260, 340
    bottom = top + plot_h
    all_t = [s["t"] for g in groups for s in g["km"]["steps"]] + [c["t"] for g in groups for c in g["km"]["censored"]]
    x_max = x_max_months or max([t / DAYS_PER_MONTH for t in all_t] + [12])
    step = 12 if x_max <= 72 else 24
    x_max = step * -(-x_max // step)
    sx = lambda days: left + (W - left - right) * (days / DAYS_PER_MONTH) / x_max  # noqa: E731
    sy = lambda s: top + plot_h * (1 - s)  # noqa: E731

    body = [_legend([(g["label"], f'{g["label"]} (n={g["km"]["n"]})') for g in groups], 0, line=True)]
    for v in (0, 0.25, 0.5, 0.75, 1):
        y = sy(v)
        body.append(f'<line class="grid" x1="{left}" x2="{W - right}" y1="{y:.1f}" y2="{y:.1f}"/>'
                    + _text(left - 6, y + 4, f"{int(v * 100)} %", "viz-muted", "end", 11))
    m = 0
    while m <= x_max:
        x = sx(m * DAYS_PER_MONTH)
        body.append(_text(x, bottom + 16, m, "viz-muted", "middle", 11))
        m += step
    body.append(_text((left + W - right) / 2, bottom + 32, "месяцы", "viz-muted", "middle", 11))
    body.append(f'<line class="axis" x1="{left}" x2="{W - right}" y1="{bottom}" y2="{bottom}"/>')

    show_ci = len(groups) <= 2
    for j, g in enumerate(groups):
        cls = f"s{j + 1}"
        steps = g["km"]["steps"]
        last_t = max([s["t"] for s in steps] + [c["t"] for c in g["km"]["censored"]] + [0])
        if show_ci and steps:
            upper, lower, prev_hi, prev_lo, x0 = [], [], 1.0, 1.0, sx(0)
            for s in steps:
                x = sx(s["t"])
                upper += [(x, prev_hi), (x, s["ci_high"])]
                lower += [(x, prev_lo), (x, s["ci_low"])]
                prev_hi, prev_lo = s["ci_high"], s["ci_low"]
            xe = sx(last_t)
            pts = [(x0, 1.0)] + upper + [(xe, prev_hi), (xe, prev_lo)] + list(reversed(lower)) + [(x0, 1.0)]
            body.append(f'<path class="band {cls}" d="M' + "L".join(f"{x:.1f},{sy(v):.1f}" for x, v in pts) + 'Z"/>')
        d, s_prev = [f"M{sx(0):.1f},{sy(1):.1f}"], 1.0
        for s in steps:
            d.append(f"H{sx(s['t']):.1f}V{sy(s['survival']):.1f}")
            s_prev = s["survival"]
        d.append(f"H{sx(last_t):.1f}")
        body.append(f'<path class="line {cls}" pathLength="1" d="{"".join(d)}"><title>{escape(g["label"])}: n={g["km"]["n"]}, '
                    f'событий {g["km"]["events"]}</title></path>')
        for c in g["km"]["censored"]:
            x, y = sx(c["t"]), sy(c["survival"])
            body.append(f'<line class="tick {cls}" x1="{x:.1f}" x2="{x:.1f}" y1="{y - 4:.1f}" y2="{y + 4:.1f}">'
                        f'<title>цензура: {c["t"] / DAYS_PER_MONTH:.1f} мес.</title></line>')
        for s in steps:   # невидимые цели наведения у ступенек
            body.append(f'<circle class="hit" cx="{sx(s["t"]):.1f}" cy="{sy(s["survival"]):.1f}" r="6">'
                        f'<title>{escape(g["label"])} · {s["t"] / DAYS_PER_MONTH:.1f} мес.: {s["survival"] * 100:.1f} % '
                        f'(95 % ДИ {s["ci_low"] * 100:.0f}–{s["ci_high"] * 100:.0f} %), под риском {s["at_risk"]}</title></circle>')
    return _svg(h, "".join(body), title, f"{len(groups)} групп; ось X — месяцы, ось Y — доля без события")


def km_ticks(groups: list[dict], x_max_months: float | None = None) -> list[int]:
    """Моменты (месяцы) для таблицы «число под риском» — те же, что деления оси X."""
    all_t = [s["t"] for g in groups for s in g["km"]["steps"]] + [c["t"] for g in groups for c in g["km"]["censored"]]
    x_max = x_max_months or max([t / DAYS_PER_MONTH for t in all_t] + [12])
    step = 12 if x_max <= 72 else 24
    return list(range(0, int(step * -(-x_max // step)) + 1, step))


# ---------------------------------------------------------------------------
# Таймлайн пациента
# ---------------------------------------------------------------------------
_EVENT_SHAPES = {"progression": "▲", "relapse": "△", "death": "✕", "new_line_start": "◆"}


def timeline(start, end, lines: list[dict], events: list[dict], responses: list[dict], title: str) -> str:
    """start/end — date; lines: [{label, start, end|None}]; events: [{date, kind, label}];
    responses: [{date, label}]. Форма маркера несёт смысл вместе с подписью, не только цвет."""
    left, right, h = 96, 20, 150
    span = max((end - start).days, 30)
    sx = lambda d: left + (W - left - right) * ((d - start).days / span)  # noqa: E731
    rows = {"Терапия": 40, "Ответ": 80, "События": 118}
    body = [_text(left - 10, y + 4, name, "viz-muted", "end") for name, y in rows.items()]
    body += [f'<line class="grid" x1="{left}" x2="{W - right}" y1="{y}" y2="{y}"/>' for y in rows.values()]
    body.append(_text(left, 16, f"диагноз {start:%m.%Y}", "viz-muted", "start", 11))
    body.append(_text(W - right, 16, f"последний контакт {end:%m.%Y}", "viz-muted", "end", 11))
    for i, ln in enumerate(lines):
        x0, x1 = sx(ln["start"]), sx(ln["end"] or end)
        body.append(f'<rect class="seg s{(i % MAX_SERIES) + 1}" x="{x0:.1f}" y="32" width="{max(x1 - x0, 3):.1f}" '
                    f'height="16" rx="3"><title>{escape(ln["label"])}</title></rect>'
                    + _text(x0 + 3, 28, ln["short"], "viz-text", "start", 10))
    for r in responses:
        x = sx(r["date"])
        body.append(f'<circle class="dot" cx="{x:.1f}" cy="80" r="5"><title>{escape(r["label"])}</title></circle>'
                    + _text(x, 98, r["short"], "viz-muted", "middle", 10))
    for e in events:
        x = sx(e["date"])
        body.append(f'<g><title>{escape(e["label"])}</title>'
                    + _text(x, 123, _EVENT_SHAPES.get(e["kind"], "●"), "viz-text", "middle", 13)
                    + _text(x, 140, e["short"], "viz-muted", "middle", 10) + "</g>")
    return _svg(h, "".join(body), title, "Линии терапии, ответы и события на оси времени")
