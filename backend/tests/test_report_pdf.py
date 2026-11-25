"""A report builder run as a PDF: numbered pages, charts with gaps rather than
zeros, unmeasured figures said so, and text the standard fonts can print."""
from __future__ import annotations

import re

from api.core.report_pdf import pdf_filename, render_pdf


def _payload(**overrides) -> dict:
    points = [{"t": f"2025-03-0{d}T00:00:00Z", "v": v, "days": 1} for d, v in
              ((1, 4), (2, None), (3, 7), (4, 2), (5, 9), (6, 1), (7, 3))]
    payload = {
        "version": 1, "title": "Weekly → growth review",
        "period": {"from": "2025-03-01T00:00:00Z", "to": "2025-03-08T00:00:00Z", "label": "1–7 Mar 2025",
                   "days": 7.0, "timezone": "UTC", "in_progress": False},
        "comparison": {"kind": "previous_period", "from": "2025-02-22T00:00:00Z", "to": "2025-03-01T00:00:00Z",
                       "label": "the period before (22–28 Feb 2025)"},
        "generated_at": "2025-03-20T09:00:00Z", "data_as_of": "2025-03-08T00:00:00Z",
        "executive_summary": ["Weekly growth review covers 1–7 Mar 2025 (UTC).", "Biggest moves: New users 30 (−40%)."],
        "kpis": [
            {"key": "new_users", "label": "New users", "category": "growth", "unit": "count", "good": "up",
             "kind": "period", "value": 30, "previous": 50, "delta": -20, "change_pct": -40.0, "available": True,
             "unavailable_reason": None, "definition": "Accounts created in the period.", "source": "users.created_at",
             "filtered": False},
            {"key": "income_minor", "label": "Income", "category": "revenue", "unit": "kes_minor", "good": "up",
             "kind": "period", "value": 1_250_000, "previous": 1_000_000, "delta": 250_000, "change_pct": 25.0,
             "available": True, "unavailable_reason": None, "definition": "Money users paid BROKA.",
             "source": "revenue_report", "filtered": False},
            {"key": "active_users", "label": "Active users", "category": "engagement", "unit": "count", "good": "up",
             "kind": "period", "value": None, "previous": None, "delta": None, "change_pct": None, "available": False,
             "unavailable_reason": "Activity tracking had not started.", "definition": "Distinct active accounts.",
             "source": "user_activity_days", "filtered": False},
        ],
        "sections": [
            {"key": "growth", "title": "Platform growth", "kpis": ["new_users"], "series": ["new_users"]},
            {"key": "engagement", "title": "User engagement", "kpis": ["active_users"], "series": []},
            {"key": "revenue", "title": "Revenue and payments", "kpis": ["income_minor"], "series": ["income_minor"]},
        ],
        "series": [
            {"key": "new_users", "label": "New users", "unit": "count", "bucket": "day", "visualization": "line",
             "points": points, "note": "Null means not measured, never zero."},
            {"key": "income_minor", "label": "Income", "unit": "kes_minor", "bucket": "day", "visualization": "bar",
             "points": [{**p, "v": (p["v"] or 0) * 10_000} for p in points]},
        ],
        "tables": [
            {"key": "income_by_stream", "title": "Income by stream", "visualization": "bar",
             "chart_column": "amount_minor", "note": None,
             "columns": [{"key": "label", "label": "Stream", "unit": "text"},
                         {"key": "amount_minor", "label": "Income", "unit": "kes_minor"},
                         {"key": "count", "label": "Payments", "unit": "count"}],
             "rows": [{"label": "Listing fees", "amount_minor": 900_000, "count": 18},
                      {"label": "Boosts", "amount_minor": 350_000, "count": 7}]},
        ],
        "changes": [{"key": "new_users", "label": "New users", "unit": "count", "value": 30, "previous": 50,
                     "delta": -20, "change_pct": -40.0, "direction": "worse"}],
        "anomalies": {"evaluated_days": 7, "items": [
            {"metric": "registrations", "label": "New registrations", "date": "2025-03-05", "direction": "drop",
             "observed": 1, "baseline_median": 9, "z_score": -5.4, "pct": -88.9, "severity": "medium",
             "impact": "8 fewer", "source": "users.created_at", "suggested_action": "Check the sign-up flow."}],
            "note": "Each day checked against the same weekday."},
        "observations": [{"text": "New users fell 40% against the period before: 50 to 30.",
                          "action": "Check the sign-up and OTP flow.",
                          "evidence": [{"metric": "new_users", "label": "New users", "value": 30, "previous": 50,
                                        "unit": "count", "source": "users.created_at"}]}],
        "unavailable": [{"key": "active_users", "label": "Active users", "reason": "Activity tracking had not started."}],
        "filters": {"category": None, "county": None, "applies_to": [], "note": None},
        "definitions": {}, "sources": {}, "notes": ["Dates are whole UTC days (Nairobi is UTC+3); the end day is included."],
    }
    payload.update(overrides)
    return payload


def test_a_pdf_with_numbered_pages_and_every_section():
    pdf = render_pdf(_payload(), compress=False)
    assert pdf.startswith(b"%PDF-")
    pages = re.findall(rb"\(Page (\d+) of (\d+)\)", pdf)
    assert pages, "no page numbers"
    total = {int(t) for _, t in pages}
    assert len(total) == 1 and [int(n) for n, _ in pages] == list(range(1, total.pop() + 1))
    for text in (b"Executive summary", b"Key performance indicators", b"Important changes",
                 b"Significant anomalies", b"Actionable observations", b"Supporting tables",
                 b"Data sources and definitions", b"Not measured"):
        assert text in pdf, text
    # The arrow is printed as ASCII, not dropped.
    assert b"Weekly -> growth review" in pdf


def test_compressed_output_is_smaller_and_still_a_pdf():
    plain = render_pdf(_payload(), compress=False)
    packed = render_pdf(_payload())
    assert packed.startswith(b"%PDF-") and len(packed) < len(plain)


def test_a_series_with_nothing_measured_says_so_instead_of_plotting_zero():
    payload = _payload()
    payload["series"][0]["points"] = [{**p, "v": None} for p in payload["series"][0]["points"]]
    pdf = render_pdf(payload, compress=False)
    assert b"No measured value in this period." in pdf


def test_an_empty_report_still_renders():
    pdf = render_pdf(_payload(kpis=[], sections=[], series=[], tables=[], changes=[], observations=[],
                              unavailable=[], anomalies={"items": [], "note": None}, comparison=None),
                     compress=False)
    assert pdf.startswith(b"%PDF-")
    assert b"No comparison was chosen." in pdf


def test_file_names_are_safe():
    assert pdf_filename({"title": "Weekly / growth: review!", "period_start": "2025-03-01T00:00:00Z"}) == \
        "broka-weekly-growth-review-2025-03-01.pdf"
    assert pdf_filename({"title": "", "period_start": None}) == "broka-report-period.pdf"
