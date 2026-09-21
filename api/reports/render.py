"""Rendering the weekly email.

Email HTML is not web HTML. Tables for layout, styles inline, one column,
nothing that depends on a stylesheet surviving a mail client's sanitiser —
Outlook, Gmail's clipper and Apple Mail all disagree about everything else.

Both a plain-text and an HTML body are produced. The text part is not a
courtesy: a mail client that shows it is also the one most likely to strip the
HTML, and a report whose figures only exist in a `<td>` is a report some
customers never read.

Numbers are formatted in exactly one place, `_clicks`/`_ctr`/`_position`, so
the email and the screen round identically. A CTR that reads 0.8% in one and
0.77% in the other is a support ticket.
"""

from __future__ import annotations

from datetime import date
from html import escape
from typing import Any

from api.reports.weekly import ReportFigures

MAX_EXAMPLES_SHOWN = 3

_MOVEMENT_LABEL = {
    "fixed": "Fixed",
    "returned": "Came back",
    "verified": "Confirmed fixed",
}


# ---------------------------------------------------------------------------
# Formatting — one definition each
# ---------------------------------------------------------------------------
def _int(value: Any) -> str:
    return f"{int(value or 0):,}"


def _ctr(value: float | None) -> str:
    return "—" if value is None else f"{value * 100:.2f}%"


def _position(value: float | None) -> str:
    return "—" if value is None else f"{value:.1f}"


def _change(value: float | None) -> str:
    """Nullable on purpose. A first measurement has nothing to compare with,
    and "0.0%" would be a claim we cannot support."""
    if value is None:
        return ""
    if value == 0:
        return "no change"
    return f"{'+' if value > 0 else ''}{value:g}% vs previous 28 days"


def _position_change(value: float | None) -> str:
    """Position is the one metric where the sign lies.

    It is already inverted upstream so that "up" means improving — but a
    reader seeing "+5.6%" beside "12.4" has every reason to think the number
    12.4 went up, which is the opposite of what happened. So position says it
    in words and never in a sign.
    """
    if value is None:
        return ""
    if value == 0:
        return "no change"
    direction = "better" if value > 0 else "worse"
    return f"{abs(value):g}% {direction} than the previous 28 days"


def _period(start: date, end: date) -> str:
    if start.year == end.year:
        return f"{start:%-d %b} – {end:%-d %b %Y}"
    return f"{start:%-d %b %Y} – {end:%-d %b %Y}"


def _plural(count: int, one: str, many: str) -> str:
    return one if count == 1 else many.format(n=count)


def subject(figures: ReportFigures) -> str:
    domain = figures.website.get("domain") or "your website"
    count = len(figures.priorities)
    if figures.search:
        clicks = _int(figures.search["clicks"])
        if count:
            things = _plural(count, "1 thing", "{n} things")
            return f"{domain}: {clicks} clicks, {things} to fix"
        return f"{domain}: {clicks} clicks, nothing outstanding"
    if count:
        things = _plural(count, "1 thing", "{n} things")
        return f"{domain}: {things} to fix this week"
    return f"{domain}: your weekly summary"


# ---------------------------------------------------------------------------
# Plain text
# ---------------------------------------------------------------------------
def text(figures: ReportFigures, *, dashboard_url: str | None = None) -> str:
    period = _period(figures.period_start, figures.period_end)
    lines: list[str] = [
        figures.website.get("domain") or "Your website",
        f"Week of {figures.week_start:%-d %B %Y} · {period}",
        "",
        figures.summary,
        "",
    ]

    if figures.score:
        change = _change(figures.score["change_pct"]) or (
            "first measurement" if figures.score["change_pct"] is None else ""
        )
        lines += [
            f"Visibility score: {figures.score['total']:.0f}/100"
            + (f" ({change})" if change else ""),
            "",
        ]

    if figures.search:
        search = figures.search
        change = search.get("change") or {}
        lines += ["Search Console, last 28 days"]
        for label, value, moved in (
            ("Clicks", _int(search["clicks"]), change.get("clicks")),
            ("Impressions", _int(search["impressions"]), change.get("impressions")),
            ("Click-through rate", _ctr(search["ctr"]), None),
        ):
            moved_text = _change(moved)
            suffix = f"  ({moved_text})" if moved_text else ""
            lines.append(f"  {label}: {value}{suffix}")
        position_moved = _position_change(change.get("position"))
        lines.append(
            f"  Average position: {_position(search['position'])}"
            + (f"  ({position_moved})" if position_moved else "")
        )
        lines.append("")
    else:
        lines += [
            "No Search Console data for this period yet.",
            "",
        ]

    if figures.priorities:
        lines.append("This week's priorities")
        for priority in figures.priorities:
            lines.append("")
            lines.append(f"{priority.rank}. {priority.title}")
            if priority.why:
                lines.append(f"   {priority.why}")
            for index, step in enumerate(priority.how, 1):
                lines.append(f"   {index}. {step}")
            shown = priority.examples[:MAX_EXAMPLES_SHOWN]
            for example in shown:
                lines.append(f"   - {example}")
            # Only once something has been shown. "and 1 more" under an empty
            # list tells the reader nothing and looks like a bug, because it is.
            extra = priority.count - len(shown)
            if shown and extra > 0:
                lines.append(f"   - and {extra} more")
        lines.append("")
    else:
        lines += ["Nothing needs your attention this week.", ""]

    if figures.movements:
        lines.append("What changed")
        for movement in figures.movements:
            label = _MOVEMENT_LABEL.get(movement.kind, movement.kind)
            lines.append(f"  {label}: {movement.title}")
        lines.append("")

    if dashboard_url:
        lines.append(f"See everything: {dashboard_url}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# HTML
# ---------------------------------------------------------------------------
_WRAP = """\
<!doctype html>
<html lang="en-GB"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title}</title></head>
<body style="margin:0;padding:0;background:#f4f5f7;">
<div style="display:none;max-height:0;overflow:hidden;opacity:0;">{preheader}</div>
<table role="presentation" width="100%" cellpadding="0" cellspacing="0"
       style="background:#f4f5f7;padding:24px 12px;">
<tr><td align="center">
<table role="presentation" width="600" cellpadding="0" cellspacing="0"
       style="max-width:600px;width:100%;background:#ffffff;border-radius:8px;
              font-family:-apple-system,Segoe UI,Helvetica,Arial,sans-serif;
              color:#1b1f23;">
{body}
</table>
</td></tr></table></body></html>
"""


def _row(content: str, *, padding: str = "0 28px") -> str:
    return f'<tr><td style="padding:{padding};">{content}</td></tr>'


def _metric_cell(label: str, value: str, change: str) -> str:
    moved = (
        f'<div style="font-size:12px;color:#57606a;padding-top:2px;">'
        f"{escape(change)}</div>"
        if change
        else ""
    )
    return (
        '<td style="padding:10px 0;" width="50%">'
        f'<div style="font-size:12px;color:#57606a;text-transform:uppercase;'
        f'letter-spacing:.04em;">{escape(label)}</div>'
        f'<div style="font-size:24px;font-weight:600;padding-top:4px;">'
        f"{escape(value)}</div>{moved}</td>"
    )


def _priority_block(priority: Any) -> str:
    steps = "".join(
        f'<li style="padding-bottom:4px;">{escape(step)}</li>' for step in priority.how
    )
    shown = priority.examples[:MAX_EXAMPLES_SHOWN]
    pages = "".join(
        f'<div style="font-family:ui-monospace,SFMono-Regular,Menlo,monospace;'
        f'font-size:12px;color:#57606a;word-break:break-all;padding-top:2px;">'
        f"{escape(example)}</div>"
        for example in shown
    )
    remaining = priority.count - len(shown)
    if shown and remaining > 0:
        pages += (
            f'<div style="font-size:12px;color:#57606a;padding-top:2px;">'
            f"and {remaining} more</div>"
        )
    clicks = (
        f'<span style="color:#1a7f37;">about {_int(priority.estimated_clicks_delta)} '
        f"more clicks a month</span>"
        if priority.estimated_clicks_delta > 0
        else ""
    )
    effort = f'<span style="color:#57606a;">{escape(priority.effort)} effort</span>'
    meta = " · ".join(part for part in (clicks, effort) if part)

    return (
        '<table role="presentation" width="100%" cellpadding="0" cellspacing="0"'
        ' style="border:1px solid #e1e4e8;border-radius:6px;margin-bottom:12px;">'
        '<tr><td style="padding:16px 18px;">'
        f'<div style="font-size:12px;color:#57606a;">Priority {priority.rank}</div>'
        f'<div style="font-size:17px;font-weight:600;padding-top:2px;">'
        f"{escape(priority.title)}</div>"
        + (
            f'<p style="margin:8px 0 0;font-size:14px;line-height:1.5;">'
            f"{escape(priority.why)}</p>"
            if priority.why
            else ""
        )
        + (
            f'<ol style="margin:10px 0 0;padding-left:20px;font-size:14px;'
            f'line-height:1.5;">{steps}</ol>'
            if steps
            else ""
        )
        + (f'<div style="padding-top:10px;">{pages}</div>' if pages else "")
        + (
            f'<div style="padding-top:10px;font-size:13px;">{meta}</div>'
            if meta
            else ""
        )
        + "</td></tr></table>"
    )


def html(figures: ReportFigures, *, dashboard_url: str | None = None) -> str:
    domain = figures.website.get("domain") or "Your website"
    period = _period(figures.period_start, figures.period_end)
    blocks: list[str] = []

    blocks.append(
        _row(
            f'<div style="font-size:13px;color:#57606a;">Week of '
            f"{figures.week_start:%-d %B %Y}</div>"
            f'<h1 style="margin:4px 0 0;font-size:22px;">{escape(domain)}</h1>'
            f'<div style="font-size:13px;color:#57606a;padding-top:4px;">'
            f"Search Console data for {escape(period)}</div>",
            padding="28px 28px 8px",
        )
    )

    blocks.append(
        _row(
            f'<p style="margin:12px 0 0;font-size:15px;line-height:1.6;">'
            f"{escape(figures.summary)}</p>"
        )
    )

    if figures.score:
        # A bare "52/100" invites the reader to judge it against a benchmark
        # that does not exist. Saying it is the first reading is the same
        # thing the dashboard says, for the same reason.
        change = _change(figures.score["change_pct"]) or (
            "first measurement" if figures.score["change_pct"] is None else ""
        )
        moved = (
            f'<div style="font-size:12px;color:#57606a;padding-top:2px;">'
            f"{escape(change)}</div>"
            if change
            else ""
        )
        blocks.append(
            _row(
                '<table role="presentation" width="100%" cellpadding="0"'
                ' cellspacing="0" style="margin-top:18px;background:#f6f8fa;'
                'border-radius:6px;"><tr><td style="padding:14px 18px;">'
                '<div style="font-size:12px;color:#57606a;text-transform:'
                'uppercase;letter-spacing:.04em;">Visibility score</div>'
                f'<div style="font-size:24px;font-weight:600;padding-top:4px;">'
                f"{figures.score['total']:.0f}/100</div>{moved}"
                "</td></tr></table>"
            )
        )

    if figures.search:
        search = figures.search
        change = search.get("change") or {}
        cells = (
            _metric_cell("Clicks", _int(search["clicks"]), _change(change.get("clicks")))
            + _metric_cell(
                "Impressions",
                _int(search["impressions"]),
                _change(change.get("impressions")),
            )
            + "</tr><tr>"
            + _metric_cell("Click-through rate", _ctr(search["ctr"]), "")
            + _metric_cell(
                "Average position",
                _position(search["position"]),
                _position_change(change.get("position")),
            )
        )
        blocks.append(
            _row(
                '<table role="presentation" width="100%" cellpadding="0"'
                f' cellspacing="0" style="margin-top:18px;"><tr>{cells}</tr></table>'
            )
        )
    else:
        blocks.append(
            _row(
                '<p style="margin:18px 0 0;font-size:14px;color:#57606a;">'
                "We don't have Search Console data for this period yet.</p>"
            )
        )

    if figures.priorities:
        blocks.append(
            _row(
                '<h2 style="margin:26px 0 12px;font-size:16px;">'
                "This week's priorities</h2>"
            )
        )
        blocks.append(
            _row("".join(_priority_block(p) for p in figures.priorities))
        )
    else:
        blocks.append(
            _row(
                '<p style="margin:26px 0 0;font-size:15px;">Nothing needs your '
                "attention this week.</p>"
            )
        )

    if figures.movements:
        items = "".join(
            f'<li style="padding-bottom:6px;font-size:14px;">'
            f'<strong>{escape(_MOVEMENT_LABEL.get(m.kind, m.kind))}:</strong> '
            f"{escape(m.title)}"
            + (
                f'<div style="font-family:ui-monospace,Menlo,monospace;'
                f'font-size:12px;color:#57606a;word-break:break-all;">'
                f"{escape(m.url)}</div>"
                if m.url
                else ""
            )
            + "</li>"
            for m in figures.movements
        )
        blocks.append(
            _row(
                '<h2 style="margin:26px 0 8px;font-size:16px;">What changed</h2>'
                f'<ul style="margin:0;padding-left:20px;">{items}</ul>'
            )
        )

    if dashboard_url:
        blocks.append(
            _row(
                f'<div style="padding:24px 0 4px;"><a href="{escape(dashboard_url)}" '
                'style="background:#1b1f23;color:#ffffff;text-decoration:none;'
                'padding:11px 18px;border-radius:6px;font-size:14px;'
                'display:inline-block;">See everything</a></div>'
            )
        )

    blocks.append(
        _row(
            '<p style="margin:24px 0 0;font-size:12px;color:#8b949e;'
            'line-height:1.5;">Figures come from your own Google Search '
            "Console and from our scan of your website. Google withholds "
            "low-volume searches, so the query list does not add up to the "
            "totals above.</p>",
            padding="0 28px 28px",
        )
    )

    return _WRAP.format(
        title=escape(subject(figures)),
        preheader=escape(figures.summary[:140]),
        body="".join(blocks),
    )
