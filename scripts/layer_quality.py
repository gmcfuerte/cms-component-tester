#!/usr/bin/env python3
"""Layer 5 - ecosystem quality tools.

Runs optional, local static analyzers when they are available: WordPress Plugin
Check, Composer scripts, PHPCS, PHPStan and Psalm. Missing tools SKIP instead of
failing so the layer can be enabled by default in disposable/local workflows.
"""

import argparse
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import cms_common as cc          # noqa: E402
import detect_target as dt       # noqa: E402

LAYER = "quality"


def _composer_json_path(root):
    path = os.path.join(root, "composer.json")
    return path if os.path.isfile(path) else None


def _vendor_bin(root, name):
    suffixes = [name, name + ".bat"]
    for suffix in suffixes:
        path = os.path.join(root, "vendor", "bin", suffix)
        if os.path.isfile(path):
            return path
    return cc.which(name)


def _short_output(res, limit=1200):
    text = (res.get("stdout") or "") + "\n" + (res.get("stderr") or "")
    return cc.redact(text.strip()[-limit:])


def _run_tool(name, cmd, cwd, timeout, pass_codes=(0,)):
    res = cc.run_cmd(cmd, cwd=cwd, timeout=timeout)
    status = cc.PASS if res["returncode"] in pass_codes else cc.FAIL
    return cc.check(name, status, "{} exited {}.".format(" ".join(cmd[:2]), res["returncode"]),
                    evidence=_short_output(res))


def _composer_scripts(root):
    path = _composer_json_path(root)
    if not path:
        return []
    try:
        data = cc.load_data_file(path) or {}
    except Exception:
        return []
    scripts = data.get("scripts") or {}
    preferred = []
    for name in ("lint", "cs", "phpcs", "analyse", "analyze", "phpstan", "psalm", "test:static"):
        if name in scripts:
            preferred.append(name)
    return preferred


def _wp_plugin_slug(desc):
    manifest = desc.get("manifest") or {}
    path = manifest.get("path", "")
    if "/" in path:
        return path.split("/")[0]
    if path.lower().endswith(".php"):
        return os.path.splitext(os.path.basename(path))[0]
    return manifest.get("text_domain", "")


def run(ctx):
    started = cc.now_iso()
    desc = ctx["target"]
    if desc["kind"] == "url-live":
        return cc.layer_result(LAYER, [cc.check("applicability", cc.SKIP,
                               "Quality tools need a source tree, not a live URL.")],
                               summary="Skipped (live URL).", started_at=started)
    root = ctx["target_path"]
    if not os.path.isdir(root):
        return cc.layer_result(LAYER, [cc.check("source", cc.SKIP,
                               "Quality tools need an unpacked source tree.")],
                               summary="Skipped (no source tree).", started_at=started)

    checks = []
    timeout = ctx.get("timeout", 300)
    run_quality = ctx.get("run_quality", False)

    composer = cc.which("composer")
    scripts = _composer_scripts(root)
    if composer and scripts and run_quality:
        for script in scripts[:4]:
            checks.append(_run_tool("composer." + script, [composer, "run", script], root, timeout))
    elif scripts:
        checks.append(cc.check("composer.scripts", cc.SKIP,
                               "Detected Composer static script(s) {}; pass --run-quality to execute."
                               .format(", ".join(scripts[:4]))))
    elif _composer_json_path(root):
        checks.append(cc.check("composer.scripts", cc.SKIP,
                               "composer.json found, but composer is unavailable or no known static scripts exist."))
    else:
        checks.append(cc.check("composer.scripts", cc.SKIP, "No composer.json found."))

    phpcs = _vendor_bin(root, "phpcs")
    if phpcs and run_quality:
        checks.append(_run_tool("phpcs", [phpcs, "--standard=WordPress" if desc["platform"] == dt.WORDPRESS else "--standard=PSR12", "."],
                                root, timeout))
    elif phpcs:
        checks.append(cc.check("phpcs", cc.SKIP, "PHPCS found; pass --run-quality to execute."))
    else:
        checks.append(cc.check("phpcs", cc.SKIP, "PHPCS not found (vendor/bin/phpcs or PATH)."))

    phpstan = _vendor_bin(root, "phpstan")
    if phpstan and run_quality:
        cmd = [phpstan, "analyse"]
        if not os.path.isfile(os.path.join(root, "phpstan.neon")) and not os.path.isfile(os.path.join(root, "phpstan.neon.dist")):
            cmd += ["--level=1", "."]
        checks.append(_run_tool("phpstan", cmd, root, timeout))
    elif phpstan:
        checks.append(cc.check("phpstan", cc.SKIP, "PHPStan found; pass --run-quality to execute."))
    else:
        checks.append(cc.check("phpstan", cc.SKIP, "PHPStan not found (vendor/bin/phpstan or PATH)."))

    psalm = _vendor_bin(root, "psalm")
    if psalm and run_quality:
        checks.append(_run_tool("psalm", [psalm, "--no-progress"], root, timeout))
    elif psalm:
        checks.append(cc.check("psalm", cc.SKIP, "Psalm found; pass --run-quality to execute."))
    else:
        checks.append(cc.check("psalm", cc.SKIP, "Psalm not found (vendor/bin/psalm or PATH)."))

    if desc["platform"] == dt.WORDPRESS:
        wp = cc.which("wp")
        slug = _wp_plugin_slug(desc)
        if wp and slug and run_quality:
            checks.append(_run_tool("wp.plugin-check", [wp, "plugin", "check", slug], root, timeout))
        elif wp and slug:
            checks.append(cc.check("wp.plugin-check", cc.SKIP, "WP-CLI found; pass --run-quality to execute Plugin Check."))
        else:
            checks.append(cc.check("wp.plugin-check", cc.SKIP,
                                   "WP-CLI or plugin slug unavailable; install plugin-check on a disposable WP site."))

    status = cc.rollup_status(checks)
    return cc.layer_result(LAYER, checks, summary="quality: {}".format(status),
                           meta={"tools": len(checks)}, started_at=started)


def _ctx_from_args(args):
    desc = dt.detect(args.target)
    return {"target": desc, "target_path": os.path.abspath(args.target), "timeout": args.timeout, "run_quality": args.run_quality}


def main(argv=None):
    parser = argparse.ArgumentParser(description="Optional ecosystem quality checks.")
    parser.add_argument("target", help="source tree")
    parser.add_argument("--timeout", type=int, default=300)
    parser.add_argument("--run-quality", action="store_true", help="execute discovered quality tools")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    result = run(_ctx_from_args(args))
    cc.emit(result, args.json)
    return cc.status_to_exit(result["status"])


if __name__ == "__main__":
    sys.exit(main())
