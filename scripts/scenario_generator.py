#!/usr/bin/env python3
"""Generate starter API specs and human scenarios from target detection."""

import argparse
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import cms_common as cc          # noqa: E402
import detect_target as dt       # noqa: E402


def _api_request_from_hint(hint):
    path = hint.get("example", "/")
    expect = {"status": 200, "max_latency_ms": 3000}
    if "admin-ajax.php" in path or "option=com_ajax" in path:
        expect["success_flag"] = True
        expect["json_has"] = ["data"]
    return {
        "name": (hint.get("kind") or "endpoint") + "-smoke",
        "method": "GET",
        "path": path,
        "auth": False,
        "expect": expect,
    }


def generate_api_spec(desc):
    requests = []
    for hint in (desc.get("entrypoints") or {}).get("endpoints_hint") or []:
        if hint.get("example"):
            requests.append(_api_request_from_hint(hint))
    yootheme = ((desc.get("entrypoints") or {}).get("yootheme") or {})
    if yootheme.get("detected"):
        requests.append({
            "name": "yootheme-template-ajax",
            "method": "GET",
            "path": "/index.php?option=com_ajax&template=${YOOTHEME_TEMPLATE}&format=json",
            "auth": False,
            "expect": {"status": 200, "success_flag": True, "json_has": ["data"]},
        })
    if not requests:
        requests.append({
            "name": "homepage-smoke",
            "method": "GET",
            "path": "/",
            "auth": False,
            "expect": {"status": 200, "max_latency_ms": 3000},
        })
    env = ["YOOTHEME_TEMPLATE"] if yootheme.get("detected") else []
    return {
        "platform": desc.get("platform", "unknown"),
        "env": env,
        "default_headers": {"Accept": "application/json"},
        "requests": requests,
    }


def generate_human_scenario(desc, base_url="${BASE_URL}"):
    platform = desc.get("platform", "unknown")
    if platform == dt.WORDPRESS:
        steps = [
            {"action": "goto", "url": base_url + "/wp-login.php", "name": "01-login"},
            {"action": "fill", "selector": "#user_login", "value": "${ADMIN_USER}"},
            {"action": "fill", "selector": "#user_pass", "value": "${ADMIN_PASS}", "secret": True},
            {"action": "click", "selector": "#wp-submit", "name": "02-submit"},
            {"action": "expect_selector", "selector": "#wpadminbar", "name": "03-dashboard"},
            {"action": "goto", "url": base_url + "/?cmsct_nocache=1", "name": "04-frontend"},
            {"action": "expect_selector", "selector": "body", "name": "05-body"},
        ]
    else:
        steps = [
            {"action": "goto", "url": base_url + "/administrator/index.php", "name": "01-login"},
            {"action": "fill", "selector": "#mod-login-username", "value": "${ADMIN_USER}"},
            {"action": "fill", "selector": "#mod-login-password", "value": "${ADMIN_PASS}", "secret": True},
            {"action": "click", "selector": "#btn-login-submit", "name": "02-submit"},
            {"action": "expect_selector", "selector": ".header-title, #sidebarmenu, .page-title", "name": "03-dashboard"},
            {"action": "goto", "url": base_url + "/?cmsct_nocache=1", "name": "04-frontend"},
            {"action": "expect_selector", "selector": "body", "name": "05-body"},
        ]
    yootheme = ((desc.get("entrypoints") or {}).get("yootheme") or {})
    env = []
    if yootheme.get("detected"):
        env.extend(["YOOTHEME_FRONTEND_URL"])
        steps.append({"action": "goto", "url": "${YOOTHEME_FRONTEND_URL}", "optional": True, "name": "06-yootheme-frontend"})
        steps.append({"action": "expect_selector", "selector": "body", "optional": True, "name": "07-yootheme-render"})
    return {
        "name": platform + "-generated-smoke",
        "platform": platform,
        "description": "Generated starter scenario from cms-component-tester detection.",
        "requires_auth": True,
        "env": env,
        "steps": steps,
    }


def generate(desc, out_dir):
    cc.ensure_dir(out_dir)
    api_path = os.path.join(out_dir, "generated-api.json")
    human_path = os.path.join(out_dir, "generated-human.json")
    cc.write_json(api_path, generate_api_spec(desc))
    cc.write_json(human_path, generate_human_scenario(desc))
    return {"api_spec": api_path, "human_scenario": human_path}


def main(argv=None):
    p = argparse.ArgumentParser(description="Generate starter API/human scenario files from a target.")
    p.add_argument("target")
    p.add_argument("--out-dir", default="cms-test-report/generated")
    p.add_argument("--json", action="store_true")
    args = p.parse_args(argv)
    desc = dt.detect(args.target)
    result = generate(desc, args.out_dir)
    if args.json:
        json.dump(result, sys.stdout, indent=2, ensure_ascii=False)
        sys.stdout.write("\n")
    else:
        sys.stdout.write("API spec: {}\nHuman scenario: {}\n".format(result["api_spec"], result["human_scenario"]))
    return cc.EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
