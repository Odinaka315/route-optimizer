"""Render the optimized route as an interactive HTML map (Folium)."""

from __future__ import annotations

from html import escape
from pathlib import Path

import folium

from .geo import Stop
from .route import RouteGeometry


def _fmt_duration(seconds: float) -> str:
    minutes = int(round(seconds / 60.0))
    hours, mins = divmod(minutes, 60)
    return f"{hours} h {mins} min" if hours else f"{mins} min"


def _number_icon(label: str, color: str) -> folium.DivIcon:
    html = (
        f'<div style="background:{color};color:#fff;border:2px solid #fff;'
        "border-radius:50%;width:28px;height:28px;line-height:24px;"
        "text-align:center;font:bold 13px sans-serif;"
        f'box-shadow:0 1px 4px rgba(0,0,0,.5);">{label}</div>'
    )
    return folium.DivIcon(html=html, icon_size=(28, 28), icon_anchor=(14, 14))


def render_map(
    ordered_stops: list[Stop],
    geometry: RouteGeometry,
    solver: str,
    output_path: str | Path = "route_map.html",
    percent_saved: float | None = None,
    open_path: bool = True,
) -> Path:
    """Write an HTML map with numbered markers, the route line and a summary box."""
    if not ordered_stops:
        raise ValueError("Nothing to draw: no stops.")

    lat_c = sum(s.lat for s in ordered_stops) / len(ordered_stops)
    lng_c = sum(s.lng for s in ordered_stops) / len(ordered_stops)
    fmap = folium.Map(location=[lat_c, lng_c], zoom_start=12, tiles="OpenStreetMap")

    # For a closed loop the caller requests geometry that already includes the
    # return leg, so the line is drawn exactly as given.
    line_points = list(geometry.points)
    if len(line_points) > 1:
        folium.PolyLine(
            line_points,
            color="#d9480f" if geometry.is_fallback else "#1c7ed6",
            weight=5,
            opacity=0.85,
            dash_array="8 8" if geometry.is_fallback else None,
        ).add_to(fmap)

    for position, stop in enumerate(ordered_stops, start=1):
        color = "#2b8a3e" if position == 1 else "#364fc7"
        folium.Marker(
            [stop.lat, stop.lng],
            icon=_number_icon(str(position), color),
            tooltip=escape(f"{position}. {stop.name}"),
            popup=folium.Popup(f"<b>{position}. {escape(stop.name)}</b>", max_width=250),
        ).add_to(fmap)

    all_points = line_points + [(s.lat, s.lng) for s in ordered_stops]
    lats = [p[0] for p in all_points]
    lngs = [p[1] for p in all_points]
    fmap.fit_bounds([[min(lats), min(lngs)], [max(lats), max(lngs)]])

    rows = [
        f"<b>Stops:</b> {len(ordered_stops)}",
        f"<b>Distance:</b> {geometry.distance_m / 1000.0:.1f} km",
        f"<b>Est. time:</b> {_fmt_duration(geometry.duration_s)}",
        f"<b>Solver:</b> {escape(solver)}",
        f"<b>Route type:</b> {'open path' if open_path else 'closed loop'}",
    ]
    if percent_saved is not None:
        rows.append(f"<b>Saved vs. input order:</b> {percent_saved:.1f}%")
    if geometry.is_fallback:
        rows.append(
            '<span style="color:#d9480f"><b>Straight-line estimate</b> '
            "(road data unavailable)</span>"
        )
    box = (
        '<div style="position:fixed;bottom:24px;left:24px;z-index:9999;'
        "background:#fff;padding:10px 14px;border-radius:8px;"
        'box-shadow:0 2px 8px rgba(0,0,0,.35);font:13px/1.5 sans-serif;">'
        + "<br>".join(rows)
        + "</div>"
    )
    fmap.get_root().html.add_child(folium.Element(box))

    out = Path(output_path)
    fmap.save(str(out))
    return out
