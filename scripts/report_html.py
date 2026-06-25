"""Static HTML report renderer for cms-component-tester."""

import html
import os
from urllib.parse import quote


STATUS_CLASS = {
    "pass": "pass",
    "fail": "fail",
    "error": "error",
    "skip": "skip",
    "warn": "warn",
}


def _esc(value):
    return html.escape("" if value is None else str(value), quote=True)


def _safe_artifact_href(path, out_dir):
    if not path:
        return None
    raw = str(path)
    if raw.lower().startswith(("javascript:", "data:")):
        return None
    try:
        abs_path = os.path.abspath(raw)
        abs_out = os.path.abspath(out_dir)
        if not (abs_path == abs_out or abs_path.startswith(abs_out + os.sep)):
            return None
        rel = os.path.relpath(abs_path, abs_out).replace(os.sep, "/")
        if rel.startswith("../") or rel == "..":
            return None
        return quote(rel, safe="/._-")
    except (OSError, TypeError, ValueError):
        return None


def _evidence_html(evidence):
    if evidence in (None, "", []):
        return ""
    if isinstance(evidence, list):
        body = "\n".join(_esc(x) for x in evidence)
    else:
        body = _esc(evidence)
    return "<details><summary>Evidence</summary><pre>{}</pre></details>".format(body)


def _yootheme_summary(descriptor):
    yootheme = ((descriptor.get("entrypoints") or {}).get("yootheme") or {})
    if not yootheme.get("detected"):
        return ""
    elements = yootheme.get("elements") or []
    modules = yootheme.get("modules") or []
    styles = yootheme.get("styles") or []
    overrides = yootheme.get("overrides") or []
    names = [e.get("name") or os.path.basename(e.get("dir", "")) for e in elements[:5]]
    suffix = " ({})".format(", ".join(names)) if names else ""
    return "{} custom element(s){}, {} module bootstrap(s), {} style file(s), {} override(s)".format(
        len(elements), suffix, len(modules), len(styles), len(overrides))


def render_html(meta, descriptor, results, out_dir):
    overall = meta.get("overall_status", "skip")
    rows = []
    detail_sections = []
    artifacts_html = []
    for result in results:
        layer = result.get("layer", "?")
        status = result.get("status", "?")
        cls = STATUS_CLASS.get(status, "skip")
        rows.append(
            "<tr><td>{}</td><td><span class='status {}'>{}</span></td><td>{}</td><td>{:.1f}s</td></tr>"
            .format(_esc(layer), cls, _esc(str(status).upper()), _esc(result.get("summary", "")),
                    float(result.get("duration_s", 0.0) or 0.0))
        )
        check_rows = []
        for check in result.get("checks", []):
            c_status = check.get("status", "?")
            check_rows.append(
                "<tr><td><code>{}</code></td><td><span class='status {}'>{}</span></td>"
                "<td>{}{}</td></tr>".format(
                    _esc(check.get("name", "")),
                    STATUS_CLASS.get(c_status, "skip"),
                    _esc(str(c_status).upper()),
                    _esc(check.get("detail", "")),
                    _evidence_html(check.get("evidence")),
                )
            )
        if not check_rows:
            check_rows.append("<tr><td colspan='3'>No checks.</td></tr>")
        detail_sections.append(
            "<section><h2>{}</h2><table><thead><tr><th>Check</th><th>Status</th><th>Detail</th></tr></thead>"
            "<tbody>{}</tbody></table></section>".format(_esc(layer), "\n".join(check_rows))
        )
        for artifact in result.get("artifacts", []):
            href = _safe_artifact_href(artifact.get("path"), out_dir)
            label = _esc(artifact.get("label", "") or artifact.get("path", "artifact"))
            kind = _esc(artifact.get("type", "artifact"))
            if href and artifact.get("type") == "screenshot":
                artifacts_html.append("<figure><img src='{}' alt='{}'><figcaption>{} ({})</figcaption></figure>"
                                      .format(href, label, label, kind))
            elif href:
                artifacts_html.append("<li><a href='{}'>{}</a> ({})</li>".format(href, label, kind))
            else:
                artifacts_html.append("<li>{} ({}) - external or unsafe path not linked</li>"
                                      .format(_esc(artifact.get("path", "")), kind))

    manifest = descriptor.get("manifest") or {}
    yootheme_summary = _yootheme_summary(descriptor)
    yootheme_row = ""
    if yootheme_summary:
        yootheme_row = "<tr><th>YOOtheme Pro</th><td>{}</td></tr>".format(_esc(yootheme_summary))
    artifact_block = ""
    if artifacts_html:
        figures = [x for x in artifacts_html if x.startswith("<figure")]
        list_items = [x for x in artifacts_html if not x.startswith("<figure")]
        artifact_block = "<section><h2>Artifacts</h2>{}<ul>{}</ul></section>".format(
            "\n".join(figures), "\n".join(list_items)
        )

    return """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta http-equiv="Content-Security-Policy" content="default-src 'none'; img-src 'self' data:; style-src 'unsafe-inline'; base-uri 'none'; form-action 'none'">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>CMS component test report</title>
<style>
body {{ font-family: system-ui, -apple-system, Segoe UI, sans-serif; margin: 0; color: #1f2933; background: #f7f9fb; }}
header {{ background: #12343b; color: white; padding: 24px clamp(16px, 4vw, 48px); }}
main {{ padding: 24px clamp(16px, 4vw, 48px); }}
table {{ width: 100%; border-collapse: collapse; background: white; margin: 12px 0 28px; }}
th, td {{ border: 1px solid #d9e2ec; padding: 8px 10px; text-align: left; vertical-align: top; }}
th {{ background: #eef3f7; }}
section {{ margin: 0 0 28px; }}
.status {{ border-radius: 4px; padding: 2px 6px; font-weight: 700; font-size: 0.84rem; }}
.pass {{ background: #d9f7df; color: #155724; }}
.fail {{ background: #ffe3e3; color: #8a1c1c; }}
.error {{ background: #ffd6a5; color: #7a3d00; }}
.skip {{ background: #e6edf5; color: #334e68; }}
.warn {{ background: #fff3bf; color: #7c5a00; }}
pre {{ white-space: pre-wrap; max-height: 260px; overflow: auto; background: #f1f5f9; padding: 8px; }}
img {{ max-width: min(100%, 960px); border: 1px solid #d9e2ec; }}
code {{ font-family: ui-monospace, SFMono-Regular, Consolas, monospace; }}
</style>
</head>
<body>
<header>
  <h1>CMS component test report</h1>
  <p>Overall: <span class="status {overall_cls}">{overall}</span></p>
</header>
<main>
<section>
<h2>Target</h2>
<table><tbody>
<tr><th>Input</th><td><code>{target}</code></td></tr>
<tr><th>Platform</th><td>{platform} ({confidence} confidence)</td></tr>
<tr><th>Kind</th><td>{kind}</td></tr>
<tr><th>Manifest</th><td><code>{manifest}</code></td></tr>
{yootheme_row}
<tr><th>Generated</th><td>{generated}</td></tr>
</tbody></table>
</section>
<section>
<h2>Summary</h2>
<table><thead><tr><th>Layer</th><th>Status</th><th>Summary</th><th>Duration</th></tr></thead><tbody>
{rows}
</tbody></table>
</section>
{details}
{artifacts}
</main>
</body>
</html>
""".format(
        overall_cls=STATUS_CLASS.get(overall, "skip"),
        overall=_esc(str(overall).upper()),
        target=_esc(descriptor.get("input")),
        platform=_esc(descriptor.get("platform")),
        confidence=_esc(descriptor.get("confidence")),
        kind=_esc(descriptor.get("kind")),
        manifest=_esc(manifest.get("path", "")),
        yootheme_row=yootheme_row,
        generated=_esc(meta.get("generated")),
        rows="\n".join(rows),
        details="\n".join(detail_sections),
        artifacts=artifact_block,
    )
