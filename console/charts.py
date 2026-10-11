"""
Server-rendered SVG charts. Pure functions from samples to markup; the page
inlines the result and re-requests it, so there is no chart JavaScript.
Hover detail comes from SVG <title> elements, which browsers show natively.
"""

from datetime import timedelta
from html import escape

MID = "#E6E3DD"
FAV = "#8DBBEA"
LONG = "#F0AE66"
BAND = "#2A3F57"
GRID = "#262B31"
INK = "#98A2AD"
FILL_MARK = "#F2A3A3"
PHASE_MARK = "#F2C14E"
# The validated dark categorical order (dataviz reference palette), checked
# against the panel surface #16191C. Markets past the eighth fold into Other.
SERIES = ("#3987e5", "#d95926", "#199e70", "#c98500", "#d55181", "#008300",
          "#9085e9", "#e66767")
OTHER = "#6B7480"

# The x axis starts at the first sample when the history is shorter than the
# window, so a farmer that started minutes ago still draws a readable line.
MIN_SPAN = timedelta(minutes=30)

FONT = "font-family=\"IBM Plex Mono, ui-monospace, monospace\" font-size=\"11\""


def _svg(width, height, body, label):
    return (f'<svg class="chart" viewBox="0 0 {width} {height}" role="img" '
            f'aria-label="{escape(label)}" '
            f'xmlns="http://www.w3.org/2000/svg">{body}</svg>')


def _empty(width, height, label, message):
    return _svg(width, height,
                f'<rect width="{width}" height="{height}" rx="8" fill="#1B1F23"/>'
                f'<text x="{width / 2}" y="{height / 2}" text-anchor="middle" '
                f'dominant-baseline="middle" fill="{INK}" font-size="13">'
                f'{escape(message)}</text>', label)


def _runs(points):
    """Split [(x, y or None)] into runs of consecutive known points."""
    runs, run = [], []
    for x, y in points:
        if y is None:
            if run:
                runs.append(run)
            run = []
        else:
            run.append((x, y))
    if run:
        runs.append(run)
    return runs


def _step_path(points, end_x):
    """A step line: each value holds until the next sample, the last until end_x."""
    parts = []
    for run in _runs(points):
        d = f"M{run[0][0]:.1f},{run[0][1]:.1f}"
        for (x0, y0), (x1, y1) in zip(run, run[1:]):
            d += f" H{x1:.1f} V{y1:.1f}"
        last_x = run[-1][0]
        next_xs = [x for x, y in points if x > last_x]
        d += f" H{(next_xs[0] if next_xs else end_x):.1f}"
        parts.append(d)
    return " ".join(parts)


def mid_chart(samples, v, markers, now, window=timedelta(hours=24), width=880, height=220):
    """The midpoint over the window with the reward band and our bids.

    samples: farm_db.MidSample oldest first. v: the reward max spread.
    markers: views.Marker (at, kind, text) for FILL and PHASE events; those
    outside the drawn span are left out. The longshot bid shows at its
    mirror, 1 - price. The span is the window, or from the first sample
    (at least MIN_SPAN back) when the history is shorter.
    """
    label = "Midpoint, reward band and our bids over the last 24 hours"
    shown = [s for s in samples if now - window <= s.at <= now
             and (s.mid if s.mid is not None else s.book_mid) is not None]
    if not shown:
        return _empty(width, height, label,
                      "No midpoint history yet. The farmer records one sample a minute.")
    start = max(now - window, min(shown[0].at, now - MIN_SPAN))
    span = now - start
    v = v or 0.0
    left, right, top, bottom = 52, 12, 12, 26
    plot_w, plot_h = width - left - right, height - top - bottom

    def mid_of(s):
        return s.mid if s.mid is not None else s.book_mid

    values = []
    for s in shown:
        m = mid_of(s)
        values += [max(0.0, m - v), min(1.0, m + v)]
        values += [p for p in (s.fav_bid, s.long_bid and 1 - s.long_bid) if p is not None]
    lo, hi = min(values), max(values)
    if hi - lo < 0.002:
        lo, hi = lo - 0.001, hi + 0.001

    def x(at):
        return left + (at - start) / span * plot_w

    def y(price):
        return top + (hi - price) / (hi - lo) * plot_h

    body = []
    for frac in (0, 0.5, 1):
        price = hi - frac * (hi - lo)
        yy = y(price)
        body.append(f'<line x1="{left}" x2="{width - right}" y1="{yy:.1f}" y2="{yy:.1f}" '
                    f'stroke="{GRID}" stroke-width="1"/>'
                    f'<text x="{left - 6}" y="{yy:.1f}" text-anchor="end" '
                    f'dominant-baseline="middle" fill="{INK}" {FONT}>{price * 100:.1f}c</text>')
    for frac in (0, 0.5, 1):
        anchor = ("start", "middle", "end")[int(frac * 2)]
        text = f"{(start + frac * span).astimezone():%H:%M}"
        body.append(f'<text x="{left + frac * plot_w:.1f}" y="{height - 8}" '
                    f'text-anchor="{anchor}" fill="{INK}" {FONT}>{text}</text>')

    for run in _runs([(x(s.at), mid_of(s)) for s in shown]):
        upper = " ".join(f"{px:.1f},{y(min(1.0, m + v)):.1f}" for px, m in run)
        lower = " ".join(f"{px:.1f},{y(max(0.0, m - v)):.1f}" for px, m in reversed(run))
        if len(run) == 1:
            px = run[0][0]
            upper = f"{px - 1:.1f},{y(min(1.0, run[0][1] + v)):.1f} " \
                    f"{px + 1:.1f},{y(min(1.0, run[0][1] + v)):.1f}"
            lower = f"{px + 1:.1f},{y(max(0.0, run[0][1] - v)):.1f} " \
                    f"{px - 1:.1f},{y(max(0.0, run[0][1] - v)):.1f}"
        body.append(f'<polygon class="band" points="{upper} {lower}" fill="{BAND}" '
                    f'fill-opacity="0.55"/>')
        points = " ".join(f"{px:.1f},{y(m):.1f}" for px, m in run)
        body.append(f'<polyline class="mid" points="{points}" fill="none" stroke="{MID}" '
                    f'stroke-width="2"/>')

    end_x = x(now)
    fav = _step_path([(x(s.at), s.fav_bid and y(s.fav_bid)) for s in shown], end_x)
    if fav:
        body.append(f'<path class="fav-bid" d="{fav}" fill="none" stroke="{FAV}" '
                    f'stroke-width="2"/>')
    long = _step_path([(x(s.at), s.long_bid and y(1 - s.long_bid)) for s in shown], end_x)
    if long:
        body.append(f'<path class="long-bid" d="{long}" fill="none" stroke="{LONG}" '
                    f'stroke-width="2"/>')

    for m in markers:
        if not start <= m.at <= now:
            continue
        color = FILL_MARK if m.kind == "FILL" else PHASE_MARK
        px = x(m.at)
        body.append(f'<g class="marker marker-{m.kind.lower()}"><title>{escape(m.text)}</title>'
                    f'<line x1="{px:.1f}" x2="{px:.1f}" y1="{top}" y2="{top + plot_h}" '
                    f'stroke="{color}" stroke-width="1" stroke-dasharray="3 3"/>'
                    f'<circle cx="{px:.1f}" cy="{top + 5}" r="4" fill="{color}" '
                    f'stroke="#16191C" stroke-width="2"/></g>')
    return _svg(width, height, "".join(body), label)


def rewards_bars(rows, names, today, days=30, width=880, height=220):
    """Stacked daily reward bars per market over the last `days` days.

    rows: (date YYYY-MM-DD, condition_id, usd). names: {condition_id: name}.
    A market keeps its color however many others show: colors go by the
    sorted condition ids, and markets past the eighth fold into Other.
    """
    label = f"Daily rewards by market, last {days} days"
    dates = [(today - timedelta(days=days - 1 - i)).strftime("%Y-%m-%d") for i in range(days)]
    rows = [r for r in rows if r[0] in dates and r[2] > 0]
    if not rows:
        return _empty(width, height, label,
                      "No rewards recorded yet. A dry run earns nothing; a live farmer "
                      "fetches earnings every 10 minutes.")
    ids = sorted({cid for _, cid, _ in rows})
    color = {cid: SERIES[i] if i < len(SERIES) else OTHER for i, cid in enumerate(ids)}
    left, right, top, bottom = 52, 12, 12, 26
    plot_w, plot_h = width - left - right, height - top - bottom
    per_day = {}
    for date, cid, amount in rows:
        per_day.setdefault(date, []).append((cid, amount))
    peak = max(sum(a for _, a in day) for day in per_day.values())
    slot_w = plot_w / days
    bar_w = max(2.0, slot_w - 4)
    body = [f'<line x1="{left}" x2="{width - right}" y1="{top + plot_h}" '
            f'y2="{top + plot_h}" stroke="{GRID}"/>',
            f'<text x="{left - 6}" y="{top}" text-anchor="end" dominant-baseline="middle" '
            f'fill="{INK}" {FONT}>${peak:,.2f}</text>']
    for i, date in enumerate(dates):
        base = top + plot_h
        bx = left + i * slot_w + (slot_w - bar_w) / 2
        for cid, amount in sorted(per_day.get(date, []), key=lambda p: ids.index(p[0])):
            h = amount / peak * plot_h
            body.append(f'<rect class="reward-bar" x="{bx:.1f}" y="{base - h:.1f}" width="{bar_w:.1f}" '
                        f'height="{max(0.0, h - 2):.1f}" rx="2" fill="{color[cid]}">'
                        f'<title>{escape(date)} {escape(names.get(cid, cid))}: '
                        f'${amount:,.2f}</title></rect>')
            base -= h
    for frac, text in ((0, dates[0][5:]), (1, dates[-1][5:])):
        anchor = "start" if frac == 0 else "end"
        body.append(f'<text x="{left + frac * plot_w:.1f}" y="{height - 8}" '
                    f'text-anchor="{anchor}" fill="{INK}" {FONT}>{text}</text>')
    return _svg(width, height, "".join(body), label)


def legend(names, ids):
    """[(color, name)] for the rewards chart, matching rewards_bars' colors."""
    ordered = sorted(ids)
    shown = [(SERIES[i], names.get(cid, cid)) for i, cid in enumerate(ordered[:len(SERIES)])]
    if len(ordered) > len(SERIES):
        shown.append((OTHER, f"Other ({len(ordered) - len(SERIES)})"))
    return shown
