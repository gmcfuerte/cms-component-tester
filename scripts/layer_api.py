#!/usr/bin/env python3
"""Layer 3 - API / chatbot endpoint testing (HTTP).

Hits REST / AJAX / chatbot endpoints of the extension and asserts on:
  * HTTP status code
  * response JSON schema (required keys, value types, optional substring)
  * latency (max_latency_ms)
  * the platform "logical success" flag where it differs from HTTP status
    (Joomla com_ajax and WP admin-ajax return 200 with {"success": false} on
    failure, so a status-only check would miss real errors).

Auth tokens come ONLY from the CMS_API_TOKEN environment variable - never from
the spec file or the command line. The token value is redacted from all report
output.

Endpoints are defined in a spec file (--api-spec, JSON or YAML). With no spec,
the layer falls back to GET-smoke-testing any endpoints the detector found.

Response bodies are treated as DATA: parsed as JSON/text, never executed, even
if they contain text that looks like an instruction.

Standalone:
    python3 layer_api.py --base-url URL [--api-spec spec.yml] [--platform joomla|wordpress] [--json]
"""

import argparse
import json
import os
import re
import ssl
import sys
import time
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import cms_common as cc          # noqa: E402
import detect_target as dt       # noqa: E402

LAYER = "api"

_TYPE_MAP = {
    "string": str, "number": (int, float), "integer": int,
    "boolean": bool, "array": list, "object": dict, "null": type(None),
}

_AUTH_HEADERS = {"authorization", "proxy-authorization", "x-joomla-token", "x-wp-nonce", "cookie"}


def _api_mapping(base_url, spec):
    mapping = {
        "BASE_URL": (base_url or os.environ.get("BASE_URL", "")).rstrip("/"),
    }
    secret_keys = set()
    defaults = spec.get("defaults") or {}
    if isinstance(defaults, dict):
        for key, value in defaults.items():
            if isinstance(key, str) and re.match(r"^[A-Z_][A-Z0-9_]*$", key):
                mapping[key] = value
    for name in spec.get("env") or []:
        if isinstance(name, str):
            mapping[name] = os.environ.get(name, "")
    for name in spec.get("secret_env") or []:
        if isinstance(name, str):
            mapping[name] = os.environ.get(name, "")
            secret_keys.add(name)
    secret_values = {str(mapping[k]) for k in secret_keys if mapping.get(k)}
    return mapping, secret_keys, secret_values


def _expand_tree(value, mapping, secret_keys):
    if isinstance(value, str):
        expanded, _ = cc.substitute(value, mapping, secret_keys)
        return expanded
    if isinstance(value, list):
        return [_expand_tree(item, mapping, secret_keys) for item in value]
    if isinstance(value, dict):
        return {
            _expand_tree(key, mapping, secret_keys): _expand_tree(item, mapping, secret_keys)
            for key, item in value.items()
        }
    return value


# --- tiny JSON path + schema helpers ---------------------------------------


def _resolve(obj, path):
    """Resolve a dotted path (a.b.0.c) in nested dict/list. (found, value)."""
    cur = obj
    if path in ("", "."):
        return True, cur
    for part in path.split("."):
        if isinstance(cur, dict) and part in cur:
            cur = cur[part]
        elif isinstance(cur, list) and part.lstrip("-").isdigit():
            idx = int(part)
            if -len(cur) <= idx < len(cur):
                cur = cur[idx]
            else:
                return False, None
        else:
            return False, None
    return True, cur


def _check_schema(body_obj, expect, checks, prefix):
    json_has = expect.get("json_has", [])
    if isinstance(json_has, str):
        json_has = [json_has]
    if not isinstance(json_has, list):
        checks.append(cc.check(prefix + ".json_has", cc.ERROR,
                               "json_has must be a list of paths or a single path string."))
        json_has = []
    for key in json_has:
        if not isinstance(key, str):
            checks.append(cc.check(prefix + ".json_has", cc.ERROR,
                                   "json_has entries must be strings."))
            continue
        found, _ = _resolve(body_obj, key)
        checks.append(cc.check(prefix + ".json_has:" + key,
                               cc.PASS if found else cc.FAIL,
                               "present" if found else "missing required key"))
    jt = expect.get("json_types") or {}
    if not isinstance(jt, dict):
        checks.append(cc.check(prefix + ".json_types", cc.ERROR,
                               "json_types must be an object/dict, got " + type(jt).__name__))
        jt = {}
    for key, typename in jt.items():
        found, val = _resolve(body_obj, key)
        expected = _TYPE_MAP.get(typename)
        if not found:
            checks.append(cc.check(prefix + ".json_type:" + key, cc.FAIL, "key missing"))
        elif expected and isinstance(val, expected) and not (typename != "boolean" and isinstance(val, bool)):
            checks.append(cc.check(prefix + ".json_type:" + key, cc.PASS, typename))
        else:
            checks.append(cc.check(prefix + ".json_type:" + key, cc.FAIL,
                                   "expected {}, got {}".format(typename, type(val).__name__)))
    if "success_flag" in expect:
        found, val = _resolve(body_obj, "success")
        want = expect["success_flag"]
        ok = found and bool(val) == bool(want)
        checks.append(cc.check(prefix + ".success_flag",
                               cc.PASS if ok else cc.FAIL,
                               "success={} (wanted {})".format(val if found else "absent", want)))


# --- request execution ------------------------------------------------------


def _default_auth_header(platform):
    # Joomla Web Services token uses X-Joomla-Token; WP REST commonly uses a
    # Bearer/Basic Authorization header. Both come from CMS_API_TOKEN.
    if platform == dt.JOOMLA:
        return ("X-Joomla-Token", "")
    return ("Authorization", "Bearer ")


def _build_request(base_url, spec, req, platform):
    mapping, secret_keys, secret_values = _api_mapping(base_url, spec)
    path = _expand_tree(req.get("path", req.get("url", "")), mapping, secret_keys)
    if path.startswith("http"):
        url = path
    else:
        url = base_url.rstrip("/") + "/" + path.lstrip("/")
    method = req.get("method", "GET").upper()
    headers = _expand_tree(dict((spec.get("default_headers") or {})), mapping, secret_keys)
    headers.update(_expand_tree(req.get("headers") or {}, mapping, secret_keys))

    hardcoded_auth = [h for h in headers if h.lower() in _AUTH_HEADERS]
    if hardcoded_auth:
        raise cc.GuardError(
            "Spec must not hardcode auth header(s): {}. Put secrets in {} and "
            "configure auth.header/auth.scheme instead.".format(
                ", ".join(sorted(hardcoded_auth)), cc.ENV_API_TOKEN
            )
        )

    data = None
    if "json" in req and req["json"] is not None:
        data = json.dumps(_expand_tree(req["json"], mapping, secret_keys)).encode("utf-8")
        headers.setdefault("Content-Type", "application/json")
    elif "form" in req and req["form"] is not None:
        from urllib.parse import urlencode
        data = urlencode(_expand_tree(req["form"], mapping, secret_keys)).encode("utf-8")
        headers.setdefault("Content-Type", "application/x-www-form-urlencoded")
    elif "data" in req and req["data"] is not None:
        data = str(_expand_tree(req["data"], mapping, secret_keys)).encode("utf-8")

    # Auth: token strictly from env. Per-request `auth: false` disables it.
    token_used = False
    if req.get("auth", True):
        token = cc.env(cc.ENV_API_TOKEN)
        if token:
            auth_cfg = spec.get("auth") or {}
            hdr = auth_cfg.get("header")
            scheme = auth_cfg.get("scheme")
            if hdr is None:
                hdr, scheme = _default_auth_header(platform)
                scheme = scheme if scheme else ""
            headers[hdr] = (scheme or "") + token
            token_used = True

    request = urllib.request.Request(url, data=data, method=method, headers=headers)
    return request, url, method, token_used, secret_values


class _SafeRedirect(urllib.request.HTTPRedirectHandler):
    """Redirect handler that does not leak credentials or jump to production.

    On Python 3.8-3.10 urllib copies the Authorization header onto cross-host
    redirects (auth-stripping only landed in 3.11), so a malicious staging
    endpoint could 302 to attacker.example and harvest CMS_API_TOKEN. We strip
    auth headers on any cross-host redirect and refuse to follow a redirect to a
    production-looking host unless the operator allowed production.
    """

    def __init__(self, origin_url, allow_production):
        super().__init__()
        self.origin_url = origin_url
        self.allow_production = allow_production

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if not self.allow_production and cc.looks_like_production(newurl):
            return None  # do not follow; the 3xx is surfaced to the caller
        new_req = super().redirect_request(req, fp, code, msg, headers, newurl)
        if new_req is not None and not cc.same_host(self.origin_url, newurl):
            for store in (getattr(new_req, "headers", {}), getattr(new_req, "unredirected_hdrs", {})):
                for h in list(store.keys()):
                    if h.lower() in _AUTH_HEADERS:
                        del store[h]
        return new_req


def _do_request(request, timeout, origin_url, allow_production):
    ctx = ssl.create_default_context()
    opener = urllib.request.build_opener(
        _SafeRedirect(origin_url, allow_production),
        urllib.request.HTTPSHandler(context=ctx),
    )
    start = time.time()
    try:
        with opener.open(request, timeout=timeout) as resp:
            body = resp.read().decode("utf-8", "replace")
            return {"status": resp.getcode(), "body": body, "ms": int((time.time() - start) * 1000), "error": None}
    except urllib.error.HTTPError as exc:
        body = ""
        try:
            body = exc.read().decode("utf-8", "replace")
        except Exception:
            pass
        return {"status": exc.code, "body": body, "ms": int((time.time() - start) * 1000), "error": None}
    except (urllib.error.URLError, OSError) as exc:
        return {"status": None, "body": "", "ms": int((time.time() - start) * 1000),
                "error": cc.redact(str(exc))}


def _run_request(req, spec, base_url, platform, timeout, checks, artifacts, out_dir, idx, allow_production):
    name = req.get("name", "req%d" % idx)
    prefix = "api[" + name + "]"
    secret_values = set()
    try:
        request, url, method, token_used, secret_values = _build_request(base_url, spec, req, platform)
        cc.guard_target(url, allow_production, "API request URL")
    except Exception as exc:  # malformed spec entry
        checks.append(cc.check(prefix, cc.ERROR, "could not build request: " + cc.redact(str(exc), secret_values)))
        return

    res = _do_request(request, req.get("timeout", timeout), url, allow_production)
    expect = req.get("expect") or {}
    if not isinstance(expect, dict):
        checks.append(cc.check(prefix + ".expect", cc.ERROR,
                               "expect must be an object/dict, got " + type(expect).__name__))
        return

    if res["error"]:
        checks.append(cc.check(prefix + ".connect", cc.ERROR,
                "{} {} -> {}".format(method, cc.redact(url, secret_values), cc.redact(res["error"], secret_values))))
        return

    # Save the raw body (redacted) as an artifact for the report.
    if out_dir:
        safe = "".join(c if c.isalnum() else "-" for c in name)[:60]
        body_path = os.path.join(out_dir, "api", safe + ".txt")
        cc.ensure_dir(os.path.dirname(body_path))
        with open(body_path, "w", encoding="utf-8") as fh:
            fh.write(cc.redact(res["body"], secret_values)[:200000])
        artifacts.append(cc.artifact("response", body_path, name))

    suspicious = [cc.redact(item, secret_values) for item in cc.suspicious_instruction_findings(res["body"], prefix)]
    if suspicious:
        checks.append(cc.check(prefix + ".untrusted_content", cc.WARN,
                               "Response contains instruction-like text; reported as data only.",
                               evidence=suspicious))

    # Status code
    want_status = expect.get("status")
    if want_status is None:
        ok = res["status"] is not None and res["status"] < 500
        checks.append(cc.check(prefix + ".status", cc.PASS if ok else cc.FAIL,
                               "{} {} -> HTTP {}".format(method, cc.redact(url, secret_values), res["status"])))
    else:
        ok = res["status"] == want_status
        checks.append(cc.check(prefix + ".status", cc.PASS if ok else cc.FAIL,
                               "HTTP {} (wanted {})".format(res["status"], want_status)))

    # Latency
    max_ms = expect.get("max_latency_ms")
    if max_ms is not None:
        ok = res["ms"] <= max_ms
        checks.append(cc.check(prefix + ".latency", cc.PASS if ok else cc.FAIL,
                               "{} ms (limit {} ms)".format(res["ms"], max_ms)))
    else:
        checks.append(cc.check(prefix + ".latency", cc.PASS, "{} ms".format(res["ms"])))

    # Body substring
    if "body_contains" in expect:
        needle = expect["body_contains"]
        if not isinstance(needle, str):
            checks.append(cc.check(prefix + ".body_contains", cc.ERROR,
                                   "body_contains must be a string, got " + type(needle).__name__))
        else:
            ok = needle in res["body"]
            checks.append(cc.check(prefix + ".body_contains", cc.PASS if ok else cc.FAIL,
                                   cc.redact("substring {!r} {}".format(
                                       needle, "found" if ok else "absent"))))

    if "body_matches" in expect:
        pattern = expect["body_matches"]
        if not isinstance(pattern, str):
            checks.append(cc.check(prefix + ".body_matches", cc.ERROR,
                                   "body_matches must be a string regex, got " + type(pattern).__name__))
        else:
            try:
                ok = re.search(pattern, res["body"], re.S) is not None
                checks.append(cc.check(prefix + ".body_matches", cc.PASS if ok else cc.FAIL,
                                       cc.redact("regex {!r} {}".format(
                                           pattern, "matched" if ok else "absent"))))
            except re.error as exc:
                checks.append(cc.check(prefix + ".body_matches", cc.ERROR,
                                       "invalid regex: " + str(exc)))

    # JSON schema
    needs_json = any(k in expect for k in ("json_has", "json_types", "success_flag"))
    if needs_json:
        try:
            body_obj = json.loads(res["body"])
            _check_schema(body_obj, expect, checks, prefix)
        except ValueError:
            checks.append(cc.check(prefix + ".json", cc.FAIL,
                                   "response is not valid JSON but JSON assertions were requested."))

    # Auth visibility (never prints the token)
    if req.get("auth", True) and not token_used:
        checks.append(cc.check(prefix + ".auth", cc.WARN,
                               "No CMS_API_TOKEN set; request sent unauthenticated."))


# --- fallback smoke endpoints ----------------------------------------------


def _smoke_requests(desc):
    out = []
    for hint in (desc.get("entrypoints", {}).get("endpoints_hint") or []):
        out.append({
            "name": hint.get("kind", "hint") + "-smoke",
            "method": "GET",
            "path": hint.get("example", ""),
            "auth": False,
            "expect": {},  # default: status < 500
        })
    return out


# --- entry points -----------------------------------------------------------


def run(ctx):
    started = cc.now_iso()
    platform = ctx["target"]["platform"]
    allow_prod = ctx.get("allow_production", False)

    spec = {}
    requests = []
    if ctx.get("api_spec"):
        try:
            spec = cc.load_data_file(ctx["api_spec"]) or {}
        except cc.GuardError as exc:  # optional dep missing (e.g. PyYAML) -> graceful skip
            return cc.layer_result(LAYER, [cc.check("api-spec", cc.SKIP, str(exc))],
                                   summary="Skipped (spec loader).", started_at=started)
        except (OSError, ValueError) as exc:  # malformed file -> real error
            return cc.error_result(LAYER, "Could not read api-spec: " + str(exc), started_at=started)
        requests = spec.get("requests") or spec.get("endpoints") or []
        if spec.get("platform"):
            platform = spec["platform"]

    # Effective base URL: CLI flag wins, else a base_url declared in the spec.
    base_url = ctx.get("base_url") or spec.get("base_url")
    if not base_url:
        return cc.layer_result(LAYER, [cc.check("base_url", cc.SKIP,
                               "No --base-url (and none in the spec); the api layer needs a running (staging) site.")],
                               summary="Skipped (no base URL).", started_at=started)
    try:
        cc.guard_target(base_url, allow_prod, "API requests")
    except cc.GuardError as exc:
        return cc.error_result(LAYER, str(exc), started_at=started)

    if not requests:
        requests = _smoke_requests(ctx["target"])
        if not requests:
            return cc.layer_result(LAYER, [cc.check("endpoints", cc.SKIP,
                                   "No --api-spec and no endpoints detected; nothing to test.")],
                                   summary="Skipped (no endpoints).", started_at=started)

    checks, artifacts = [], []
    for i, req in enumerate(requests):
        _run_request(req, spec, base_url, platform, ctx.get("timeout", 30),
                     checks, artifacts, ctx.get("out_dir"), i, allow_prod)

    status = cc.rollup_status(checks)
    return cc.layer_result(LAYER, checks,
                           summary="api: {} request(s), {}".format(len(requests), status),
                           artifacts=artifacts, meta={"requests": len(requests)}, started_at=started)


def _ctx_from_args(args):
    if args.target:
        desc = dt.detect(args.target)
    else:
        desc = {"platform": args.platform or dt.UNKNOWN, "kind": "url-live", "entrypoints": {}}
    return {
        "target": desc,
        "base_url": args.base_url,
        "api_spec": args.api_spec,
        "out_dir": args.out_dir,
        "allow_production": args.allow_production,
        "timeout": args.timeout,
    }


def main(argv=None):
    p = argparse.ArgumentParser(description="HTTP API / chatbot endpoint layer.")
    p.add_argument("target", nargs="?", help="optional source/zip/url to seed platform + endpoint hints")
    p.add_argument("--base-url", required=True)
    p.add_argument("--api-spec", default=None, help="JSON/YAML file describing requests + assertions")
    p.add_argument("--platform", choices=[dt.JOOMLA, dt.WORDPRESS], default=None)
    p.add_argument("--out-dir", default="cms-test-report")
    p.add_argument("--allow-production", action="store_true")
    p.add_argument("--timeout", type=int, default=30)
    p.add_argument("--json", action="store_true")
    args = p.parse_args(argv)
    ctx = _ctx_from_args(args)
    cc.ensure_dir(ctx["out_dir"])
    result = run(ctx)
    cc.emit(result, args.json)
    return cc.status_to_exit(result["status"])


if __name__ == "__main__":
    sys.exit(main())
