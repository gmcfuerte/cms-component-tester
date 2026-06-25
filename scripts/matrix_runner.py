#!/usr/bin/env python3
"""Generate or run a CMS QA matrix for CI/staging gates."""

import argparse
import itertools
import json
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import cms_common as cc          # noqa: E402
import detect_target as dt       # noqa: E402

DEFAULT_PHP = ["8.1", "8.2", "8.3", "8.4"]
DEFAULT_WORDPRESS = ["latest", "nightly"]
DEFAULT_JOOMLA = ["4.4", "5.1", "5.2"]
DEFAULT_BROWSERS = ["chromium"]
DEFAULT_VIEWPORTS = ["desktop", "mobile"]


def _split_csv(value, default):
    if value is None or value == "":
        return list(default)
    return [item.strip() for item in str(value).split(",") if item.strip()]


def _cms_versions(platform, versions):
    if versions:
        return versions
    if platform == dt.WORDPRESS:
        return list(DEFAULT_WORDPRESS)
    if platform == dt.JOOMLA:
        return list(DEFAULT_JOOMLA)
    return ["detected"]


def _case_id(platform, profile, php, cms, browser, viewport):
    raw = "{}-{}-php{}-cms{}-{}-{}".format(platform, profile, php, cms, browser, viewport)
    return "".join(c if c.isalnum() or c in "-._" else "-" for c in raw).strip("-")


def build_matrix_plan(target, out_dir, profile="static", base_url=None, scenarios=None,
                      api_spec=None, platform=None, php_versions=None, cms_versions=None,
                      browsers=None, viewports=None, max_cases=64):
    descriptor = dt.detect(target)
    if platform:
        descriptor = dict(descriptor)
        descriptor["platform"] = platform
    platform = descriptor.get("platform") or dt.UNKNOWN
    php_versions = _split_csv(php_versions, DEFAULT_PHP)
    cms_versions = _cms_versions(platform, _split_csv(cms_versions, []))
    browsers = _split_csv(browsers, DEFAULT_BROWSERS)
    viewports = _split_csv(viewports, DEFAULT_VIEWPORTS)

    cases = []
    for php, cms, browser, viewport in itertools.product(php_versions, cms_versions, browsers, viewports):
        case = {
            "id": _case_id(platform, profile, php, cms, browser, viewport),
            "platform": platform,
            "profile": profile,
            "php": php,
            "cms": cms,
            "browser": browser,
            "viewport": viewport,
            "env": {
                "CMSCT_MATRIX_PHP": php,
                "CMSCT_MATRIX_CMS": cms,
                "CMSCT_MATRIX_BROWSER": browser,
                "CMSCT_MATRIX_VIEWPORT": viewport,
            },
        }
        command = [
            sys.executable,
            os.path.join(HERE, "cmsct.py"),
            "run",
            target,
            "--profile",
            profile,
            "--out-dir",
            os.path.join(out_dir, case["id"]),
        ]
        if base_url:
            command.extend(["--base-url", base_url])
        if scenarios:
            command.extend(["--scenarios", scenarios])
        if api_spec:
            command.extend(["--api-spec", api_spec])
        if platform in (dt.JOOMLA, dt.WORDPRESS):
            command.extend(["--platform", platform])
        case["command"] = command
        cases.append(case)
        if len(cases) >= max_cases:
            break

    return {
        "tool": "cms-component-tester",
        "target": cc.redact_tree(descriptor),
        "profile": profile,
        "case_count": len(cases),
        "truncated": len(cases) >= max_cases,
        "cases": cases,
        "github_actions_matrix": {"include": [
            {k: case[k] for k in ("id", "platform", "profile", "php", "cms", "browser", "viewport")}
            for case in cases
        ]},
    }


def write_matrix_outputs(plan, out_dir):
    cc.ensure_dir(out_dir)
    json_path = os.path.join(out_dir, "matrix-plan.json")
    cc.write_json(json_path, plan)
    md_path = os.path.join(out_dir, "matrix-summary.md")
    lines = [
        "# CMS QA matrix",
        "",
        "Target: `{}`".format(plan.get("target", {}).get("input", "")),
        "Profile: `{}`".format(plan.get("profile")),
        "Cases: {}".format(plan.get("case_count")),
        "",
        "| Case | Platform | PHP | CMS | Browser | Viewport |",
        "|---|---|---|---|---|---|",
    ]
    for case in plan.get("cases", []):
        lines.append("| `{id}` | {platform} | {php} | {cms} | {browser} | {viewport} |".format(**case))
    with open(md_path, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n")
    return {"json": json_path, "summary": md_path}


def execute_matrix(plan, timeout=1800):
    results = []
    for case in plan.get("cases", []):
        env = dict(os.environ)
        env.update(case.get("env") or {})
        try:
            proc = subprocess.run(
                case["command"],
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=timeout,
            )
            results.append({
                "id": case["id"],
                "returncode": proc.returncode,
                "stdout": cc.redact(proc.stdout.decode("utf-8", "replace")),
                "stderr": cc.redact(proc.stderr.decode("utf-8", "replace")),
            })
        except Exception as exc:
            results.append({"id": case["id"], "returncode": -1, "stdout": "", "stderr": cc.redact(str(exc))})
    plan["executions"] = results
    return results


def main(argv=None):
    p = argparse.ArgumentParser(description="Generate or execute a CMS QA CI matrix.")
    p.add_argument("target")
    p.add_argument("--out-dir", default="cms-test-report/matrix")
    p.add_argument("--profile", default="static")
    p.add_argument("--base-url", default=None)
    p.add_argument("--scenarios", default=None)
    p.add_argument("--api-spec", default=None)
    p.add_argument("--platform", choices=[dt.JOOMLA, dt.WORDPRESS], default=None)
    p.add_argument("--php", default=None, help="comma-separated PHP versions")
    p.add_argument("--cms", default=None, help="comma-separated CMS versions")
    p.add_argument("--browsers", default=None, help="comma-separated browser names")
    p.add_argument("--viewports", default=None, help="comma-separated viewport names")
    p.add_argument("--max-cases", type=int, default=64)
    p.add_argument("--execute", action="store_true")
    p.add_argument("--timeout", type=int, default=1800)
    p.add_argument("--json", action="store_true")
    args = p.parse_args(argv)
    try:
        plan = build_matrix_plan(
            args.target,
            args.out_dir,
            profile=args.profile,
            base_url=args.base_url,
            scenarios=args.scenarios,
            api_spec=args.api_spec,
            platform=args.platform,
            php_versions=args.php,
            cms_versions=args.cms,
            browsers=args.browsers,
            viewports=args.viewports,
            max_cases=args.max_cases,
        )
        if args.execute:
            execute_matrix(plan, args.timeout)
        paths = write_matrix_outputs(plan, args.out_dir)
    except cc.GuardError as exc:
        sys.stderr.write(str(exc) + "\n")
        return cc.EXIT_USAGE
    if args.json:
        payload = dict(plan)
        payload["paths"] = paths
        json.dump(payload, sys.stdout, indent=2, ensure_ascii=False)
        sys.stdout.write("\n")
    else:
        sys.stdout.write("Matrix: {} case(s)\n".format(plan["case_count"]))
        sys.stdout.write("JSON  : {}\n".format(paths["json"]))
        sys.stdout.write("Report: {}\n".format(paths["summary"]))
    return cc.EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
