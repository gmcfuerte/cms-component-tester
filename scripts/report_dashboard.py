"""Small static QA dashboard for cms-component-tester reports."""

import html
import os
from urllib.parse import quote

import cms_common as cc
import report_html

STATUS_CLASS = report_html.STATUS_CLASS


def _esc(value):
    return html.escape("" if value is None else str(value), quote=True)


def _safe_href(path, out_dir):
    href = report_html._safe_artifact_href(path, out_dir)
    return href


def _checks(results):
    for result in results:
        for check in result.get("checks", []) or []:
            item = dict(check)
            item["layer"] = result.get("layer", "")
            yield item


def _status_counts(results):
    counts = {cc.PASS: 0, cc.FAIL: 0, cc.ERROR: 0, cc.WARN: 0, cc.SKIP: 0}
    for check in _checks(results):
        status = check.get("status")
        counts[status] = counts.get(status, 0) + 1
    return counts


def _summary_cards(counts):
    order = [cc.FAIL, cc.ERROR, cc.WARN, cc.PASS, cc.SKIP]
    return "\n".join(
        "<div class='card {cls}'><span>{label}</span><strong>{count}</strong></div>".format(
            cls=STATUS_CLASS.get(status, "skip"),
            label=_esc(status.upper()),
            count=counts.get(status, 0),
        )
        for status in order
    )


def _finding_rows(results):
    rows = []
    for check in _checks(results):
        status = check.get("status")
        if status not in (cc.FAIL, cc.ERROR, cc.WARN):
            continue
        rows.append(
            "<tr><td>{}</td><td><span class='status {}'>{}</span></td><td><code>{}</code></td><td>{}</td></tr>"
            .format(
                _esc(check.get("layer")),
                STATUS_CLASS.get(status, "skip"),
                _esc(status.upper()),
                _esc(check.get("name", "")),
                _esc(check.get("detail", "")),
            )
        )
    if not rows:
        rows.append("<tr><td colspan='4'>No fail/error/warn findings.</td></tr>")
    return "\n".join(rows)


def _layer_nav(results):
    links = []
    for result in results:
        layer = result.get("layer", "")
        status = result.get("status", cc.SKIP)
        links.append("<a href='#layer-{}'><span class='status {}'>{}</span> {}</a>".format(
            quote(layer, safe=""),
            STATUS_CLASS.get(status, "skip"),
            _esc(str(status).upper()),
            _esc(layer),
        ))
    return "\n".join(links)


def _artifact_gallery(results, out_dir):
    figures = []
    links = []
    for result in results:
        for artifact in result.get("artifacts", []) or []:
            href = _safe_href(artifact.get("path"), out_dir)
            label = artifact.get("label") or artifact.get("path") or "artifact"
            kind = artifact.get("type", "artifact")
            if not href:
                continue
            if kind == "screenshot":
                figures.append("<figure><a href='{href}'><img src='{href}' alt='{label}'></a>"
                               "<figcaption>{label}</figcaption></figure>".format(
                                   href=href, label=_esc(label)))
            else:
                links.append("<li><a href='{}'>{}</a> ({})</li>".format(href, _esc(label), _esc(kind)))
    if not figures and not links:
        return "<p>No local artifacts linked.</p>"
    return "<div class='gallery'>{}</div><ul>{}</ul>".format("\n".join(figures), "\n".join(links))


def _layer_sections(results):
    sections = []
    for result in results:
        layer = result.get("layer", "")
        status = result.get("status", cc.SKIP)
        checks = []
        for check in result.get("checks", []) or []:
            c_status = check.get("status", cc.SKIP)
            checks.append("<tr><td><code>{}</code></td><td><span class='status {}'>{}</span></td><td>{}</td></tr>"
                          .format(
                              _esc(check.get("name", "")),
                              STATUS_CLASS.get(c_status, "skip"),
                              _esc(str(c_status).upper()),
                              _esc(check.get("detail", "")),
                          ))
        sections.append("<section id='layer-{id}'><h2>{layer} <span class='status {cls}'>{status}</span></h2>"
                        "<p>{summary}</p><table><tbody>{rows}</tbody></table></section>".format(
                            id=quote(layer, safe=""),
                            layer=_esc(layer),
                            cls=STATUS_CLASS.get(status, "skip"),
                            status=_esc(str(status).upper()),
                            summary=_esc(result.get("summary", "")),
                            rows="\n".join(checks) or "<tr><td>No checks.</td></tr>",
                        ))
    return "\n".join(sections)


def render_dashboard(report_json, out_dir):
    meta = report_json.get("meta", {})
    target = report_json.get("target", {})
    results = report_json.get("results", []) or []
    counts = _status_counts(results)
    overall = meta.get("overall_status", cc.SKIP)
    return """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta http-equiv="Content-Security-Policy" content="default-src 'none'; img-src 'self' data:; style-src 'unsafe-inline'; base-uri 'none'; form-action 'none'">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>CMS QA dashboard</title>
<style>
body {{ margin: 0; font-family: system-ui, -apple-system, Segoe UI, sans-serif; color: #18202a; background: #f5f7fa; }}
header {{ background: #0f2f2e; color: white; padding: 22px clamp(16px, 4vw, 44px); }}
main {{ padding: 22px clamp(16px, 4vw, 44px); }}
.cards {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(120px, 1fr)); gap: 10px; margin: 18px 0; }}
.card {{ background: white; border: 1px solid #d9e2ec; border-left: 5px solid #8aa0b4; padding: 12px; border-radius: 6px; }}
.card strong {{ display: block; font-size: 1.8rem; }}
.card.fail {{ border-left-color: #b42318; }} .card.error {{ border-left-color: #b65c00; }}
.card.warn {{ border-left-color: #b08800; }} .card.pass {{ border-left-color: #1f7a3a; }}
.nav {{ display: flex; flex-wrap: wrap; gap: 8px; margin: 14px 0 22px; }}
.nav a {{ color: #12343b; background: white; border: 1px solid #d9e2ec; padding: 6px 8px; text-decoration: none; border-radius: 4px; }}
table {{ width: 100%; border-collapse: collapse; background: white; margin: 10px 0 26px; }}
th, td {{ border: 1px solid #d9e2ec; padding: 8px 10px; text-align: left; vertical-align: top; }}
th {{ background: #eef3f7; }}
.status {{ border-radius: 4px; padding: 2px 6px; font-weight: 700; font-size: .82rem; }}
.pass {{ background: #d9f7df; color: #155724; }} .fail {{ background: #ffe3e3; color: #8a1c1c; }}
.error {{ background: #ffd6a5; color: #7a3d00; }} .skip {{ background: #e6edf5; color: #334e68; }}
.warn {{ background: #fff3bf; color: #7c5a00; }}
.gallery {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(220px, 1fr)); gap: 12px; }}
figure {{ margin: 0; background: white; border: 1px solid #d9e2ec; padding: 8px; }}
img {{ max-width: 100%; display: block; }}
code {{ font-family: ui-monospace, SFMono-Regular, Consolas, monospace; }}
</style>
</head>
<body>
<header>
<h1>CMS QA dashboard</h1>
<p><span class="status {overall_cls}">{overall}</span> <code>{target}</code></p>
</header>
<main>
<div class="cards">{cards}</div>
<nav class="nav">{nav}</nav>
<section>
<h2>Priority Findings</h2>
<table><thead><tr><th>Layer</th><th>Status</th><th>Check</th><th>Detail</th></tr></thead><tbody>{findings}</tbody></table>
</section>
<section>
<h2>Artifacts</h2>
{artifacts}
</section>
{layers}
</main>
</body>
</html>
""".format(
        overall_cls=STATUS_CLASS.get(overall, "skip"),
        overall=_esc(str(overall).upper()),
        target=_esc(target.get("input", "")),
        cards=_summary_cards(counts),
        nav=_layer_nav(results),
        findings=_finding_rows(results),
        artifacts=_artifact_gallery(results, out_dir),
        layers=_layer_sections(results),
    )


def write_dashboard(report_json, out_dir, path=None):
    path = path or os.path.join(out_dir, "dashboard.html")
    cc.ensure_dir(os.path.dirname(path) or ".")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(render_dashboard(report_json, out_dir))
    return path
