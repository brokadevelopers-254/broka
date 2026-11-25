"""A stored report builder run as a PDF: A4, numbered pages, charts and tables.

Rendered from the run's stored payload and nothing else, so the PDF says what
the run said when it was generated, and matches its CSV export and the
dashboard. ReportLab's standard fonts carry Latin-1 only; text is reduced to
that (arrows become "->"), never dropped silently.

A figure that was not measured prints "Not measured" with its reason, and a
day with no measurement is a gap in a line chart, never a zero.
"""
from __future__ import annotations

import functools
from io import BytesIO
from typing import Any, Optional
from xml.sax.saxutils import escape

from reportlab.graphics.charts.barcharts import VerticalBarChart
from reportlab.graphics.charts.lineplots import LinePlot
from reportlab.graphics.shapes import Drawing
from reportlab.graphics.widgets.markers import makeMarker
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import mm
from reportlab.pdfgen import canvas as rl_canvas
from reportlab.platypus import (
    KeepTogether, ListFlowable, ListItem, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle,
)

from api.core.report_builder import fmt

INK = colors.HexColor("#141a2e")
MUTED = colors.HexColor("#5b6478")
ACCENT = colors.HexColor("#6c55e6")
RULE = colors.HexColor("#d9dce6")
BAND = colors.HexColor("#f3f1ff")
GOOD = colors.HexColor("#1f8a5b")
BAD = colors.HexColor("#c2364b")

PAGE_W, PAGE_H = A4
MARGIN = 18 * mm
CONTENT_W = PAGE_W - 2 * MARGIN

_REPLACE = {"→": "->", "←": "<-", "≥": ">=", "≤": "<=", "−": "-", "×": "x", "✓": "yes", "≈": "~",
            "₍": "(", "₎": ")", "‑": "-", "\u00a0": " "}


def _latin(text: Any) -> str:
    out = str(text if text is not None else "")
    for a, b in _REPLACE.items():
        out = out.replace(a, b)
    # cp1252 is what the standard fonts encode; anything else shows as "?".
    return out.encode("cp1252", "replace").decode("cp1252")


def _p(text: Any) -> str:
    return escape(_latin(text))


_styles = {
    "brand": ParagraphStyle("brand", fontName="Helvetica-Bold", fontSize=8, textColor=ACCENT, leading=10,
                            spaceAfter=2),
    "title": ParagraphStyle("title", fontName="Helvetica-Bold", fontSize=20, textColor=INK, leading=24,
                            spaceAfter=4),
    "meta": ParagraphStyle("meta", fontName="Helvetica", fontSize=9, textColor=MUTED, leading=12),
    "h2": ParagraphStyle("h2", fontName="Helvetica-Bold", fontSize=13, textColor=INK, leading=16,
                         spaceBefore=12, spaceAfter=6),
    "h3": ParagraphStyle("h3", fontName="Helvetica-Bold", fontSize=10, textColor=INK, leading=13,
                         spaceBefore=8, spaceAfter=4),
    "body": ParagraphStyle("body", fontName="Helvetica", fontSize=9, textColor=INK, leading=12.5),
    "small": ParagraphStyle("small", fontName="Helvetica", fontSize=7.5, textColor=MUTED, leading=10),
    "cell": ParagraphStyle("cell", fontName="Helvetica", fontSize=8, textColor=INK, leading=10),
    "cell_muted": ParagraphStyle("cell_muted", fontName="Helvetica", fontSize=7.5, textColor=MUTED, leading=9.5),
}


class _NumberedCanvas(rl_canvas.Canvas):
    """Draws "Page n of N" once the page count is known."""

    def __init__(self, *args, footer: str = "", **kwargs):
        super().__init__(*args, **kwargs)
        self._pages: list[dict] = []
        self._footer_text = footer

    def showPage(self):  # noqa: N802 - ReportLab's name
        self._pages.append(dict(self.__dict__))
        self._startPage()

    def save(self):
        total = len(self._pages)
        for state in self._pages:
            self.__dict__.update(state)
            self.setFont("Helvetica", 7.5)
            self.setFillColor(MUTED)
            self.setStrokeColor(RULE)
            self.line(MARGIN, 14 * mm, PAGE_W - MARGIN, 14 * mm)
            self.drawString(MARGIN, 10 * mm, _latin(self._footer_text)[:110])
            self.drawRightString(PAGE_W - MARGIN, 10 * mm, f"Page {self._pageNumber} of {total}")
            super().showPage()
        super().save()


def _header(title: str):
    def draw(canvas, doc):
        if doc.page == 1:
            return
        canvas.saveState()
        canvas.setFont("Helvetica-Bold", 7.5)
        canvas.setFillColor(ACCENT)
        canvas.drawString(MARGIN, PAGE_H - 11 * mm, "BROKA")
        canvas.setFont("Helvetica", 7.5)
        canvas.setFillColor(MUTED)
        canvas.drawString(MARGIN + 12 * mm, PAGE_H - 11 * mm, _latin(title)[:100])
        canvas.restoreState()
    return draw


def _table(rows: list[list[Any]], widths: list[float], *, header: bool = True, zebra: bool = True) -> Table:
    table = Table(rows, colWidths=widths, repeatRows=1 if header else 0)
    style = [
        ("FONTNAME", (0, 0), (-1, -1), "Helvetica"),
        ("FONTSIZE", (0, 0), (-1, -1), 8),
        ("TEXTCOLOR", (0, 0), (-1, -1), INK),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("TOPPADDING", (0, 0), (-1, -1), 3),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
        ("LINEBELOW", (0, 0), (-1, -1), 0.25, RULE),
    ]
    if header:
        style += [("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"), ("TEXTCOLOR", (0, 0), (-1, 0), MUTED),
                  ("FONTSIZE", (0, 0), (-1, 0), 7.5), ("LINEBELOW", (0, 0), (-1, 0), 0.75, INK)]
    if zebra:
        for i in range(1 if header else 0, len(rows)):
            if i % 2 == 0:
                style.append(("BACKGROUND", (0, i), (-1, i), colors.HexColor("#fafafc")))
    table.setStyle(TableStyle(style))
    return table


def _change(k: dict) -> tuple[str, Optional[colors.Color]]:
    if not k.get("available") or k.get("previous") is None or k.get("delta") is None:
        return "", None
    if k["unit"] == "pct":
        text = f"{k['delta']:+.1f} pts"
    elif k.get("change_pct") is not None:
        text = f"{k['change_pct']:+.0f}%"
    else:
        text = "new" if k["delta"] else "0"
    if k.get("good") == "neutral" or not k["delta"]:
        return text, MUTED
    better = (k["delta"] > 0) == (k.get("good") == "up")
    return text, GOOD if better else BAD


def _kpi_table(kpis: list[dict]) -> Table:
    rows = [["Figure", "This period", "Comparison", "Change"]]
    tones = []
    for i, k in enumerate(kpis, start=1):
        label = Paragraph(_p(k["label"]) + (" <font color='#5b6478' size='7'>(filtered)</font>" if k.get("filtered") else ""),
                          _styles["cell"])
        if not k.get("available"):
            rows.append([label, Paragraph("Not measured", _styles["cell_muted"]), "", ""])
            continue
        change, tone = _change(k)
        previous = "point in time" if k.get("kind") == "point" else fmt(k.get("previous"), k["unit"])
        rows.append([label, _latin(fmt(k.get("value"), k["unit"])), _latin(previous), change])
        if tone is not None:
            tones.append(("TEXTCOLOR", (3, i), (3, i), tone))
    table = _table(rows, [CONTENT_W * 0.46, CONTENT_W * 0.2, CONTENT_W * 0.2, CONTENT_W * 0.14])
    table.setStyle(TableStyle([("ALIGN", (1, 0), (-1, -1), "RIGHT"), *tones]))
    return table


def _short(value: float) -> str:
    value = float(value)
    if abs(value) >= 1_000_000:
        return f"{value / 1_000_000:.1f}M"
    if abs(value) >= 1_000:
        return f"{value / 1_000:.1f}k"
    return f"{value:g}"


def _scale(unit: str):
    # Money is stored in cents; charts read in shillings.
    return (lambda v: v / 100) if unit == "kes_minor" else (lambda v: v)


def _line_chart(points: list[dict], unit: str, labels: list[str]) -> Drawing:
    scale = _scale(unit)
    width, height = CONTENT_W, 62 * mm
    drawing = Drawing(width, height)
    values = [None if p.get("v") is None else scale(p["v"]) for p in points]
    segments: list[list[tuple[float, float]]] = []
    current: list[tuple[float, float]] = []
    for i, v in enumerate(values):
        if v is None:
            if current:
                segments.append(current)
            current = []
        else:
            current.append((i, v))
    if current:
        segments.append(current)
    plot = LinePlot()
    plot.x, plot.y, plot.width, plot.height = 36, 22, width - 50, height - 34
    plot.data = segments
    for i in range(len(segments)):
        plot.lines[i].strokeColor = ACCENT
        plot.lines[i].strokeWidth = 1.4
        plot.lines[i].symbol = makeMarker("Circle", size=2.2, fillColor=ACCENT, strokeColor=ACCENT)
    n = max(1, len(values))
    plot.xValueAxis.valueMin = 0
    plot.xValueAxis.valueMax = max(1, n - 1)
    step = max(1, n // 8)
    plot.xValueAxis.valueSteps = list(range(0, n, step))
    plot.xValueAxis.labelTextFormat = lambda v: labels[int(v)] if 0 <= int(v) < len(labels) else ""
    plot.xValueAxis.labels.fontName = "Helvetica"
    plot.xValueAxis.labels.fontSize = 6.5
    measured = [v for v in values if v is not None]
    plot.yValueAxis.valueMin = min(0, min(measured)) if measured else 0
    plot.yValueAxis.valueMax = (max(measured) * 1.1 or 1) if measured else 1
    plot.yValueAxis.labelTextFormat = _short
    plot.yValueAxis.labels.fontName = "Helvetica"
    plot.yValueAxis.labels.fontSize = 6.5
    plot.yValueAxis.strokeColor = RULE
    plot.xValueAxis.strokeColor = RULE
    drawing.add(plot)
    return drawing


def _bar_chart(values: list[float], names: list[str], unit: str) -> Drawing:
    scale = _scale(unit)
    width, height = CONTENT_W, 64 * mm
    drawing = Drawing(width, height)
    chart = VerticalBarChart()
    chart.x, chart.y, chart.width, chart.height = 36, 34, width - 50, height - 44
    chart.data = [[scale(v) for v in values]]
    chart.categoryAxis.categoryNames = [_latin(n)[:16] for n in names]
    chart.categoryAxis.labels.angle = 30
    chart.categoryAxis.labels.boxAnchor = "ne"
    chart.categoryAxis.labels.fontName = "Helvetica"
    chart.categoryAxis.labels.fontSize = 6.5
    chart.valueAxis.valueMin = min(0, min(chart.data[0])) if values else 0
    chart.valueAxis.labelTextFormat = _short
    chart.valueAxis.labels.fontName = "Helvetica"
    chart.valueAxis.labels.fontSize = 6.5
    chart.bars[0].fillColor = ACCENT
    chart.bars[0].strokeColor = None
    drawing.add(chart)
    return drawing


def _series_flowables(series: dict) -> list:
    points = series.get("points") or []
    labels = [str(p.get("t") or "")[5:10] for p in points]
    title = Paragraph(f"{_p(series['label'])} <font color='#5b6478' size='8'>by {_p(series['bucket'])}"
                      f"{' (KES)' if series['unit'] == 'kes_minor' else ''}</font>", _styles["h3"])
    if not any(p.get("v") is not None for p in points):
        return [title, Paragraph("No measured value in this period.", _styles["small"])]
    out: list = [title]
    has_gap = any(p.get("v") is None for p in points)
    if series.get("visualization") == "table":
        rows = [["Starting", series["label"]]] + [
            [str(p.get("t") or "")[:10], _latin(fmt(p.get("v"), series["unit"]))] for p in points]
        out.append(_table(rows, [CONTENT_W * 0.4, CONTENT_W * 0.3]))
    elif series.get("visualization") == "bar" and not has_gap:
        out.append(_bar_chart([p["v"] for p in points], labels, series["unit"]))
    else:
        out.append(_line_chart(points, series["unit"], labels))
    if has_gap:
        out.append(Paragraph("Gaps are days with no measurement, not zero.", _styles["small"]))
    return [KeepTogether(out)]


def _cell(value: Any, unit: str) -> str:
    if unit == "text":
        return _latin(value if value not in (None, "") else "–")
    return _latin(fmt(value, unit))


def _data_table(t: dict) -> list:
    columns = t.get("columns") or []
    rows = t.get("rows") or []
    out: list = [Paragraph(_p(t["title"]), _styles["h3"])]
    if not rows:
        out.append(Paragraph("Nothing to show for this period.", _styles["small"]))
        return out
    chart_column = t.get("chart_column")
    if t.get("visualization") == "bar" and chart_column:
        unit = next((c["unit"] for c in columns if c["key"] == chart_column), "count")
        top = [r for r in rows if r.get(chart_column) is not None][:12]
        if top:
            out.append(_bar_chart([r[chart_column] for r in top], [str(r.get("label")) for r in top], unit))
    first = CONTENT_W * (0.34 if len(columns) > 4 else 0.45)
    rest = (CONTENT_W - first) / max(1, len(columns) - 1)
    data = [[Paragraph(_p(c["label"]), _styles["cell_muted"]) for c in columns]]
    for r in rows[:40]:
        data.append([Paragraph(_p(_cell(r.get(c["key"]), c["unit"])), _styles["cell"]) for c in columns])
    out.append(_table(data, [first] + [rest] * (len(columns) - 1)))
    if len(rows) > 40:
        out.append(Paragraph(f"First 40 of {len(rows)} rows.", _styles["small"]))
    if t.get("note"):
        out.append(Paragraph(_p(t["note"]), _styles["small"]))
    return out


def _bullets(items: list[str]) -> ListFlowable:
    return ListFlowable([ListItem(Paragraph(_p(i), _styles["body"]), leftIndent=10) for i in items],
                        bulletType="bullet", start="•", leftIndent=10, bulletFontSize=8)


def render_pdf(payload: dict, *, compress: bool = True) -> bytes:
    """The PDF of one stored run's payload."""
    buffer = BytesIO()
    title = payload.get("title") or "BROKA report"
    period = payload.get("period") or {}
    comparison = payload.get("comparison")
    generated = str(payload.get("generated_at") or "").replace("T", " ").rstrip("Z")[:16]
    footer = f"BROKA admin report · {title} · generated {generated} UTC"
    doc = SimpleDocTemplate(
        buffer, pagesize=A4, leftMargin=MARGIN, rightMargin=MARGIN, topMargin=16 * mm, bottomMargin=20 * mm,
        title=_latin(title), author="BROKA", subject="BROKA admin report",
        pageCompression=1 if compress else 0, invariant=0 if compress else 1,
    )
    story: list = [
        Paragraph("BROKA · ADMIN REPORT", _styles["brand"]),
        Paragraph(_p(title), _styles["title"]),
        Paragraph(f"{_p(period.get('label', ''))} (UTC){' · in progress' if period.get('in_progress') else ''}",
                  _styles["meta"]),
    ]
    if comparison:
        story.append(Paragraph(f"Compared with {_p(comparison.get('label'))}", _styles["meta"]))
    story.append(Paragraph(f"Generated {_p(generated)} UTC · data as of "
                           f"{_p(str(payload.get('data_as_of') or '').replace('T', ' ').rstrip('Z')[:16])} UTC",
                           _styles["meta"]))
    story.append(Spacer(1, 8))

    story.append(Paragraph("Executive summary", _styles["h2"]))
    story.append(_bullets(payload.get("executive_summary") or ["No summary."]))

    kpis = {k["key"]: k for k in payload.get("kpis") or []}
    series = {s["key"]: s for s in payload.get("series") or []}
    story.append(Paragraph("Key performance indicators", _styles["h2"]))
    for section in payload.get("sections") or []:
        rows = [kpis[k] for k in section.get("kpis") or [] if k in kpis]
        if not rows:
            continue
        story.append(KeepTogether([Paragraph(_p(section["title"]), _styles["h3"]), _kpi_table(rows)]))
        for key in section.get("series") or []:
            if key in series:
                story.extend(_series_flowables(series[key]))

    changes = payload.get("changes") or []
    story.append(Paragraph("Important changes", _styles["h2"]))
    if changes:
        rows = [["Figure", "Before", "Now", "Change", "Reading"]]
        for c in changes:
            change = f"{c['delta']:+.1f} pts" if c["unit"] == "pct" else f"{c['change_pct']:+.0f}%"
            rows.append([Paragraph(_p(c["label"]), _styles["cell"]), _latin(fmt(c["previous"], c["unit"])),
                         _latin(fmt(c["value"], c["unit"])), change, c["direction"]])
        story.append(_table(rows, [CONTENT_W * 0.38, CONTENT_W * 0.17, CONTENT_W * 0.17, CONTENT_W * 0.13,
                                   CONTENT_W * 0.15]))
    else:
        story.append(Paragraph("No measured figure moved enough to call out." if comparison else
                               "No comparison was chosen.", _styles["body"]))

    anomalies = payload.get("anomalies") or {}
    story.append(Paragraph("Significant anomalies", _styles["h2"]))
    items = anomalies.get("items") or []
    if items:
        rows = [["Day", "Figure", "Seen", "Usual", "z", "Severity"]]
        for a in items:
            rows.append([a["date"], Paragraph(_p(a["label"]), _styles["cell"]), f"{a['observed']:g}",
                         f"{a['baseline_median']:g}", f"{a['z_score']:g}", a["severity"]])
        story.append(_table(rows, [CONTENT_W * 0.15, CONTENT_W * 0.37, CONTENT_W * 0.12, CONTENT_W * 0.12,
                                   CONTENT_W * 0.1, CONTENT_W * 0.14]))
    else:
        story.append(Paragraph("No day stood out against its usual weekday.", _styles["body"]))
    if anomalies.get("note"):
        story.append(Paragraph(_p(anomalies["note"]), _styles["small"]))

    story.append(Paragraph("Actionable observations", _styles["h2"]))
    observations = payload.get("observations") or []
    if observations:
        for o in observations:
            parts = [Paragraph(_p(o["text"]), _styles["body"])]
            if o.get("action"):
                parts.append(Paragraph(f"<b>Check:</b> {_p(o['action'])}", _styles["small"]))
            evidence = "; ".join(f"{e['label']} from {e['source']}" for e in o.get("evidence") or [])
            if evidence:
                parts.append(Paragraph(f"Based on {_p(evidence)}.", _styles["small"]))
            parts.append(Spacer(1, 4))
            story.append(KeepTogether(parts))
    else:
        story.append(Paragraph("Nothing in this period's figures calls for action.", _styles["body"]))

    tables = payload.get("tables") or []
    if tables:
        story.append(Paragraph("Supporting tables", _styles["h2"]))
        for t in tables:
            story.extend(_data_table(t))

    unavailable = payload.get("unavailable") or []
    if unavailable:
        story.append(Paragraph("Not measured", _styles["h2"]))
        rows = [["Figure", "Why"]] + [[Paragraph(_p(u["label"]), _styles["cell"]),
                                       Paragraph(_p(u.get("reason") or ""), _styles["cell"])] for u in unavailable]
        story.append(_table(rows, [CONTENT_W * 0.35, CONTENT_W * 0.65]))

    story.append(Paragraph("Data sources and definitions", _styles["h2"]))
    rows = [["Figure", "Definition", "Source"]]
    for k in payload.get("kpis") or []:
        rows.append([Paragraph(_p(k["label"]), _styles["cell"]), Paragraph(_p(k.get("definition")), _styles["cell"]),
                     Paragraph(_p(k.get("source")), _styles["cell_muted"])])
    story.append(_table(rows, [CONTENT_W * 0.25, CONTENT_W * 0.5, CONTENT_W * 0.25]))
    notes = payload.get("notes") or []
    filters = payload.get("filters") or {}
    if filters.get("note"):
        notes = [f"Filters: category {filters.get('category') or 'all'}, county {filters.get('county') or 'all'}. "
                 + filters["note"], *notes]
    if notes:
        story.append(Spacer(1, 6))
        story.append(_bullets(notes))

    doc.build(story, onFirstPage=_header(title), onLaterPages=_header(title),
              canvasmaker=functools.partial(_NumberedCanvas, footer=footer))
    return buffer.getvalue()


def pdf_filename(meta: dict) -> str:
    start = str(meta.get("period_start") or "")[:10] or "period"
    stem = "".join(ch if ch.isalnum() else "-" for ch in (meta.get("title") or "report").lower()).strip("-")
    stem = "-".join(part for part in stem.split("-") if part)[:60] or "report"
    return f"broka-{stem}-{start}.pdf"
