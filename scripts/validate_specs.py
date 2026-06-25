#!/usr/bin/env python3
"""Validate cms-component-tester scenario and API spec files.

This is intentionally dependency-light: JSON works with the standard library;
YAML uses the same optional PyYAML loader as the runtime layers. The validator
checks the shape of data files before a browser or HTTP request ever runs.
"""

import argparse
import os
import re
import sys
from urllib.parse import urlsplit

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import cms_common as cc          # noqa: E402
import layer_api                # noqa: E402
import layer_human              # noqa: E402

API_ASSERT_TYPES = set(layer_api._TYPE_MAP.keys())
AUTH_HEADER_NAMES = set(layer_api._AUTH_HEADERS)
HUMAN_ACTIONS = {
    "goto", "fill", "type", "click", "press", "select", "select_option",
    "check", "uncheck", "wait_for", "expect_visible", "expect_selector",
    "expect_text", "expect_text_regex", "expect_regex", "expect_nonempty_text",
    "expect_not_text", "expect_url", "screenshot", "upload",
}
KNOWN_METHODS = {"GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"}
DESTRUCTIVE_METHODS = {"DELETE"}
DESTRUCTIVE_PATH_RE = re.compile(r"\b(delete|drop|truncate|destroy|wipe|purge)\b", re.I)
ENV_NAME_RE = re.compile(r"^[A-Z_][A-Z0-9_]*$")
PLACEHOLDER_RE = re.compile(r"\$\{([A-Z0-9_]+)\}")


def _issue(status, name, detail, source):
    return cc.check(name, status, detail, evidence=source)


def _is_auth_header(name):
    return str(name).lower() in AUTH_HEADER_NAMES


def _artifact_safe(name):
    return "".join(c if c.isalnum() else "-" for c in str(name))[:60]


def _all_strings(value):
    if isinstance(value, str):
        yield value
    elif isinstance(value, list):
        for item in value:
            yield from _all_strings(item)
    elif isinstance(value, dict):
        for key, item in value.items():
            yield from _all_strings(key)
            yield from _all_strings(item)


def _check_placeholders(value, allowed, source, prefix, issues):
    for text in _all_strings(value):
        for name in PLACEHOLDER_RE.findall(text):
            if name not in allowed:
                issues.append(_issue(cc.FAIL, prefix + ".placeholder",
                                     "Placeholder ${%s} is not declared in defaults/env/secret_env." % name,
                                     source))


def _validate_headers(headers, source, prefix, issues):
    if headers is None:
        return
    if not isinstance(headers, dict):
        issues.append(_issue(cc.FAIL, prefix + ".headers", "headers must be an object/dict.", source))
        return
    bad = sorted(k for k in headers if _is_auth_header(k))
    if bad:
        issues.append(_issue(
            cc.FAIL,
            prefix + ".headers.auth",
            "Do not hardcode auth header(s): {}. Use CMS_API_TOKEN and auth.header/auth.scheme."
            .format(", ".join(bad)),
            source,
        ))


def validate_api_spec_obj(spec, source="<memory>"):
    issues = []
    if not isinstance(spec, dict):
        return [_issue(cc.FAIL, "api.root", "API spec must be an object/dict.", source)]

    allowed_placeholders = {"BASE_URL"}
    env_names = spec.get("env", [])
    secret_names = spec.get("secret_env", [])
    for key, value in (("env", env_names), ("secret_env", secret_names)):
        if value is None:
            value = []
        if not isinstance(value, list) or any(not isinstance(x, str) or not ENV_NAME_RE.match(x) for x in value):
            issues.append(_issue(cc.FAIL, "api." + key, key + " must be a list of ENV_NAME strings.", source))
        elif key == "env":
            allowed_placeholders.update(value)
        else:
            allowed_placeholders.update(value)
    defaults = spec.get("defaults", {})
    if defaults is not None:
        if not isinstance(defaults, dict):
            issues.append(_issue(cc.FAIL, "api.defaults", "defaults must be an object/dict.", source))
        else:
            for key in defaults:
                if isinstance(key, str) and ENV_NAME_RE.match(key):
                    allowed_placeholders.add(key)

    _validate_headers(spec.get("default_headers"), source, "api.default_headers", issues)
    auth = spec.get("auth")
    if auth is not None and not isinstance(auth, dict):
        issues.append(_issue(cc.FAIL, "api.auth", "auth must be an object/dict.", source))
    if isinstance(auth, dict):
        if "header" in auth and not isinstance(auth["header"], str):
            issues.append(_issue(cc.FAIL, "api.auth.header", "auth.header must be a string.", source))
        if "scheme" in auth and not isinstance(auth["scheme"], str):
            issues.append(_issue(cc.FAIL, "api.auth.scheme", "auth.scheme must be a string.", source))

    requests = spec.get("requests", spec.get("endpoints"))
    if requests is None:
        issues.append(_issue(cc.FAIL, "api.requests", "API spec must contain requests or endpoints.", source))
        return issues
    if not isinstance(requests, list):
        issues.append(_issue(cc.FAIL, "api.requests", "requests/endpoints must be a list.", source))
        return issues

    seen = set()
    safe_seen = {}
    for idx, req in enumerate(requests):
        prefix = "api.requests[{}]".format(idx)
        if not isinstance(req, dict):
            issues.append(_issue(cc.FAIL, prefix, "request must be an object/dict.", source))
            continue
        name = req.get("name", "req{}".format(idx))
        if not isinstance(name, str) or not name.strip():
            issues.append(_issue(cc.FAIL, prefix + ".name", "request name must be a non-empty string.", source))
        elif name in seen:
            issues.append(_issue(cc.WARN, prefix + ".name", "duplicate request name '{}'.".format(name), source))
        seen.add(name)
        safe = _artifact_safe(name)
        if safe in safe_seen:
            issues.append(_issue(cc.WARN, prefix + ".name",
                                 "request name collides with '{}' for response artifact '{}.txt'."
                                 .format(safe_seen[safe], safe), source))
        safe_seen[safe] = name

        method = req.get("method", "GET")
        method_upper = method.upper() if isinstance(method, str) else ""
        if not isinstance(method, str) or method_upper not in KNOWN_METHODS:
            issues.append(_issue(cc.FAIL, prefix + ".method", "method must be a known HTTP verb string.", source))
        if method_upper in DESTRUCTIVE_METHODS:
            issues.append(_issue(cc.WARN, prefix + ".method", "DELETE requests are destructive; keep them out of smoke specs.", source))
        path = req.get("path", req.get("url"))
        if not isinstance(path, str) or not path:
            issues.append(_issue(cc.FAIL, prefix + ".path", "path or url must be a non-empty string.", source))
        elif path.startswith("http") and cc.looks_like_production(path):
            issues.append(_issue(cc.FAIL, prefix + ".path", "absolute URL looks like production: " + path, source))
        elif DESTRUCTIVE_PATH_RE.search(path):
            issues.append(_issue(cc.WARN, prefix + ".path", "path contains destructive-looking words.", source))
        _validate_headers(req.get("headers"), source, prefix, issues)

        if "auth" in req and not isinstance(req["auth"], bool):
            issues.append(_issue(cc.FAIL, prefix + ".auth", "auth must be true or false.", source))
        if "timeout" in req and not isinstance(req["timeout"], (int, float)):
            issues.append(_issue(cc.FAIL, prefix + ".timeout", "timeout must be numeric seconds.", source))
        body_modes = [k for k in ("json", "form", "data") if k in req and req[k] is not None]
        if len(body_modes) > 1:
            issues.append(_issue(cc.FAIL, prefix + ".body", "Use only one of json, form, or data.", source))

        expect = req.get("expect", {})
        if expect is None:
            expect = {}
        if not isinstance(expect, dict):
            issues.append(_issue(cc.FAIL, prefix + ".expect", "expect must be an object/dict.", source))
            continue
        if "status" in expect:
            if not isinstance(expect["status"], int):
                issues.append(_issue(cc.FAIL, prefix + ".expect.status", "status must be an integer.", source))
            elif not (100 <= expect["status"] <= 599):
                issues.append(_issue(cc.FAIL, prefix + ".expect.status", "status must be in 100..599.", source))
        if "max_latency_ms" in expect:
            if not isinstance(expect["max_latency_ms"], (int, float)):
                issues.append(_issue(cc.FAIL, prefix + ".expect.max_latency_ms", "max_latency_ms must be numeric.", source))
            elif expect["max_latency_ms"] <= 0:
                issues.append(_issue(cc.FAIL, prefix + ".expect.max_latency_ms", "max_latency_ms must be positive.", source))
        if "body_contains" in expect and not isinstance(expect["body_contains"], str):
            issues.append(_issue(cc.FAIL, prefix + ".expect.body_contains", "body_contains must be a string.", source))
        if "body_matches" in expect:
            if not isinstance(expect["body_matches"], str):
                issues.append(_issue(cc.FAIL, prefix + ".expect.body_matches", "body_matches must be a string regex.", source))
            else:
                try:
                    re.compile(expect["body_matches"])
                except re.error as exc:
                    issues.append(_issue(cc.FAIL, prefix + ".expect.body_matches", "invalid regex: " + str(exc), source))
        if "success_flag" in expect and not isinstance(expect["success_flag"], bool):
            issues.append(_issue(cc.FAIL, prefix + ".expect.success_flag", "success_flag must be boolean.", source))
        if isinstance(path, str) and ("admin-ajax.php" in path or "option=com_ajax" in path) and "success_flag" not in expect:
            detail = "AJAX endpoint should assert success_flag to avoid HTTP-200 false positives."
            if "option=com_ajax" in path and ("template=" in path or "module=" in path):
                detail = "YOOtheme/Joomla com_ajax template/module endpoints should assert success_flag."
            issues.append(_issue(cc.WARN, prefix + ".expect.success_flag", detail, source))

        json_has = expect.get("json_has", [])
        if isinstance(json_has, str):
            json_has = [json_has]
        if not isinstance(json_has, list) or any(not isinstance(x, str) for x in json_has):
            issues.append(_issue(cc.FAIL, prefix + ".expect.json_has", "json_has must be a list of strings or one string.", source))
        json_types = expect.get("json_types", {})
        if not isinstance(json_types, dict):
            issues.append(_issue(cc.FAIL, prefix + ".expect.json_types", "json_types must be an object/dict.", source))
        else:
            for key, typename in json_types.items():
                if not isinstance(key, str) or typename not in API_ASSERT_TYPES:
                    issues.append(_issue(cc.FAIL, prefix + ".expect.json_types",
                                         "json_types entries must be path -> one of {}."
                                         .format(", ".join(sorted(API_ASSERT_TYPES))), source))
                    break
        _check_placeholders(req, allowed_placeholders, source, prefix, issues)
    return issues


def _human_files(path):
    if os.path.isdir(path):
        files = []
        for name in sorted(os.listdir(path)):
            if name.lower().endswith((".json", ".yml", ".yaml")):
                files.append(os.path.join(path, name))
        return files
    return [path]


def _human_scenarios_from_data(data, source):
    if isinstance(data, dict) and "scenarios" in data:
        return data["scenarios"]
    if isinstance(data, list):
        return data
    if isinstance(data, dict) and "steps" in data:
        return [data]
    return []


def validate_human_scenarios_obj(data, source="<memory>"):
    issues = []
    scenarios = _human_scenarios_from_data(data, source)
    if not scenarios:
        if isinstance(data, dict) and any(k in data for k in ("name", "platform", "description", "stepz", "requires_auth")):
            issues.append(_issue(cc.FAIL, "human.scenarios", "File looks like a scenario but has no 'steps' list.", source))
            return issues
        issues.append(_issue(cc.WARN, "human.scenarios", "No human scenarios found in file.", source))
        return issues
    if not isinstance(scenarios, list):
        return [_issue(cc.FAIL, "human.scenarios", "scenarios must be a list.", source)]

    names = set()
    for s_idx, scenario in enumerate(scenarios):
        prefix = "human.scenarios[{}]".format(s_idx)
        if not isinstance(scenario, dict):
            issues.append(_issue(cc.FAIL, prefix, "scenario must be an object/dict.", source))
            continue
        name = scenario.get("name", "scenario{}".format(s_idx))
        if not isinstance(name, str) or not name.strip():
            issues.append(_issue(cc.FAIL, prefix + ".name", "scenario name must be non-empty.", source))
        elif name in names:
            issues.append(_issue(cc.WARN, prefix + ".name", "duplicate scenario name '{}'.".format(name), source))
        names.add(name)
        steps = scenario.get("steps")
        if not isinstance(steps, list) or not steps:
            issues.append(_issue(cc.FAIL, prefix + ".steps", "steps must be a non-empty list.", source))
            continue
        env_names = scenario.get("env", [])
        secret_names = scenario.get("secret_env", [])
        for key, value in (("env", env_names), ("secret_env", secret_names)):
            if value is None:
                value = []
            if not isinstance(value, list) or any(not isinstance(x, str) or not ENV_NAME_RE.match(x) for x in value):
                issues.append(_issue(cc.FAIL, prefix + "." + key, key + " must be a list of ENV_NAME strings.", source))
        allowed_placeholders = {"BASE_URL", "ADMIN_USER", "ADMIN_PASS", "API_TOKEN",
                                "OUT_DIR", "CMSCT_UPLOAD_ROOT", "CMSCT_UPLOAD_ZIP"}
        if isinstance(env_names, list):
            allowed_placeholders.update(x for x in env_names if isinstance(x, str))
        if isinstance(secret_names, list):
            allowed_placeholders.update(x for x in secret_names if isinstance(x, str))
        viewport = scenario.get("viewport")
        if viewport is not None:
            if not isinstance(viewport, dict) or not all(isinstance(viewport.get(k), int) and viewport.get(k) > 0 for k in ("width", "height")):
                issues.append(_issue(cc.FAIL, prefix + ".viewport", "viewport must contain positive integer width and height.", source))
        used_shots = set()
        for st_idx, step in enumerate(steps):
            sp = "{}.steps[{}]".format(prefix, st_idx)
            if not isinstance(step, dict):
                issues.append(_issue(cc.FAIL, sp, "step must be an object/dict.", source))
                continue
            action = step.get("action")
            if action not in HUMAN_ACTIONS:
                issues.append(_issue(cc.FAIL, sp + ".action", "unknown or missing action '{}'.".format(action), source))
                continue
            _check_placeholders(step, allowed_placeholders, source, sp, issues)
            if "timeout_ms" in step and (not isinstance(step["timeout_ms"], int) or step["timeout_ms"] <= 0):
                issues.append(_issue(cc.FAIL, sp + ".timeout_ms", "timeout_ms must be a positive integer.", source))
            if "frame_selector" in step and not isinstance(step["frame_selector"], str):
                issues.append(_issue(cc.FAIL, sp + ".frame_selector", "frame_selector must be a string selector.", source))
            shot_name = step.get("name") or "{:02d}-{}".format(st_idx, action)
            safe_shot = _artifact_safe(shot_name)
            if safe_shot in used_shots:
                issues.append(_issue(cc.WARN, sp + ".name", "duplicate screenshot name '{}'.".format(shot_name), source))
            used_shots.add(safe_shot)
            if action == "goto" and not isinstance(step.get("url"), str):
                issues.append(_issue(cc.FAIL, sp + ".url", "goto requires url string.", source))
            if action in ("fill", "type", "click", "press", "select", "select_option",
                          "check", "uncheck", "expect_visible", "expect_selector", "upload") and not isinstance(step.get("selector"), str):
                issues.append(_issue(cc.FAIL, sp + ".selector", "{} requires selector string.".format(action), source))
            if action == "upload" and not isinstance(step.get("path"), str):
                issues.append(_issue(cc.FAIL, sp + ".path", "upload requires path string.", source))
            if action in ("fill", "type", "select", "select_option") and not isinstance(step.get("value", step.get("text")), str):
                issues.append(_issue(cc.FAIL, sp + ".value", "{} requires value/text string.".format(action), source))
            if action == "expect_text" and not step.get("text"):
                issues.append(_issue(cc.FAIL, sp + ".text", "expect_text requires non-empty text.", source))
            if action in ("expect_text_regex", "expect_regex"):
                pattern = step.get("pattern", step.get("text"))
                if not isinstance(pattern, str) or not pattern:
                    issues.append(_issue(cc.FAIL, sp + ".pattern", "{} requires non-empty pattern/text.".format(action), source))
                else:
                    try:
                        re.compile(pattern)
                    except re.error as exc:
                        issues.append(_issue(cc.FAIL, sp + ".pattern", "invalid regex: " + str(exc), source))
            if action == "expect_nonempty_text" and "min_length" in step:
                if not isinstance(step["min_length"], int) or step["min_length"] <= 0:
                    issues.append(_issue(cc.FAIL, sp + ".min_length", "min_length must be a positive integer.", source))
            if action == "expect_url" and not isinstance(step.get("pattern"), str):
                issues.append(_issue(cc.FAIL, sp + ".pattern", "expect_url requires pattern string.", source))
            elif action == "expect_url":
                try:
                    re.compile(step["pattern"])
                except re.error as exc:
                    issues.append(_issue(cc.FAIL, sp + ".pattern", "invalid regex: " + str(exc), source))
    return issues


def validate_api_spec_file(path):
    try:
        data = cc.load_data_file(path)
    except Exception as exc:
        return [_issue(cc.ERROR, "api.load", cc.redact(str(exc)), path)]
    return validate_api_spec_obj(data, path)


def validate_human_scenarios_path(path):
    issues = []
    for file_path in _human_files(path):
        try:
            data = cc.load_data_file(file_path)
        except Exception as exc:
            issues.append(_issue(cc.ERROR, "human.load", cc.redact(str(exc)), file_path))
            continue
        # Ignore API specs in mixed directories; validate them through --api-spec.
        if isinstance(data, dict) and "requests" in data and "steps" not in data and "scenarios" not in data:
            continue
        issues.extend(validate_human_scenarios_obj(data, file_path))
    return issues


def _print_text(issues):
    if not issues:
        sys.stdout.write("No issues found.\n")
        return
    for issue in issues:
        sys.stdout.write("[{status}] {name}: {detail} ({evidence})\n".format(
            status=issue["status"].upper(),
            name=issue["name"],
            detail=issue["detail"],
            evidence=issue.get("evidence", ""),
        ))


def main(argv=None):
    parser = argparse.ArgumentParser(description="Validate CMS tester scenario/API spec files.")
    parser.add_argument("--api-spec", action="append", default=[], help="API spec file to validate (repeatable)")
    parser.add_argument("--scenarios", action="append", default=[], help="Human scenario file or directory (repeatable)")
    parser.add_argument("--json", action="store_true", help="emit machine-readable result")
    args = parser.parse_args(argv)

    issues = []
    for path in args.api_spec:
        issues.extend(validate_api_spec_file(path))
    for path in args.scenarios:
        issues.extend(validate_human_scenarios_path(path))
    if not args.api_spec and not args.scenarios:
        parser.error("provide --api-spec and/or --scenarios")

    checks = issues or [cc.check("validate", cc.PASS, "No issues found.")]
    result = cc.layer_result("validate", checks, summary="{} issue(s)".format(len(issues)))
    if args.json:
        cc.emit(result, True)
    else:
        _print_text(issues)
    return cc.status_to_exit(result["status"])


if __name__ == "__main__":
    sys.exit(main())
