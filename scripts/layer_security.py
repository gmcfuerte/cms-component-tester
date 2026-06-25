#!/usr/bin/env python3
"""Layer 7 - CMS security review.

Static, conservative checks for common Joomla/WordPress extension footguns:
missing nonce/token/capability guards, public AJAX endpoints, raw SQL fed from
request data, unsafe upload handling, and hardcoded-looking secrets. This layer
never executes target code.
"""

import argparse
import os
import re
import sys
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import cms_common as cc          # noqa: E402
import detect_target as dt       # noqa: E402

LAYER = "security"
CODE_EXT = (".php", ".js", ".xml", ".ini", ".sql", ".json", ".yml", ".yaml")


def _iter_files(ctx):
    target = ctx["target"]
    if target.get("kind") == "zip":
        try:
            with zipfile.ZipFile(ctx["target_path"]) as zf:
                for name in zf.namelist():
                    if name.endswith("/") or not name.lower().endswith(CODE_EXT):
                        continue
                    info = zf.getinfo(name)
                    if info.file_size > 1_000_000:
                        continue
                    try:
                        raw = zf.read(name).decode("utf-8", "replace")
                    except (OSError, zipfile.BadZipFile):
                        raw = ""
                    yield name, raw
        except (OSError, zipfile.BadZipFile):
            return
    root = ctx.get("target_path")
    if not root or not os.path.isdir(root):
        return
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in {".git", "node_modules", "vendor", "__pycache__"}]
        for filename in filenames:
            if not filename.lower().endswith(CODE_EXT):
                continue
            path = os.path.join(dirpath, filename)
            rel = os.path.relpath(path, root).replace(os.sep, "/")
            try:
                if os.path.getsize(path) > 1_000_000:
                    continue
                with open(path, "r", encoding="utf-8", errors="replace") as fh:
                    yield rel, fh.read(1_000_000)
            except OSError:
                continue


def _php_blob(ctx):
    parts = []
    for rel, text in _iter_files(ctx):
        if rel.lower().endswith(".php"):
            parts.append("\n/* FILE:{} */\n".format(rel) + text)
    return "\n".join(parts)


def _evidence(pattern, text, limit=8):
    out = []
    for m in re.finditer(pattern, text, re.I | re.S):
        start = max(0, m.start() - 120)
        end = min(len(text), m.end() + 120)
        out.append(text[start:end].replace("\r", " ").replace("\n", " ")[:500])
        if len(out) >= limit:
            break
    return out


def _has_any(text, needles):
    return any(n in text for n in needles)


def _wordpress_checks(blob):
    checks = []
    if not blob:
        return checks
    public_ajax = re.findall(r"add_action\(\s*['\"]wp_ajax_nopriv_([^'\"]+)['\"]", blob)
    private_ajax = re.findall(r"add_action\(\s*['\"]wp_ajax_([^'\"]+)['\"]", blob)
    nonce_ok = _has_any(blob, ("check_ajax_referer", "wp_verify_nonce", "check_admin_referer"))
    cap_ok = _has_any(blob, ("current_user_can", "is_user_logged_in", "permission_callback"))
    if public_ajax:
        checks.append(cc.check("security.wp.ajax.public", cc.WARN,
                               "{} public wp_ajax_nopriv handler(s) detected; verify rate limits, nonce where appropriate, and no privileged data exposure."
                               .format(len(set(public_ajax))), evidence=sorted(set(public_ajax))[:20]))
    if (public_ajax or private_ajax) and not nonce_ok:
        checks.append(cc.check("security.wp.ajax.nonce", cc.WARN,
                               "AJAX handlers detected but no WordPress nonce verification helper was found."))
    elif public_ajax or private_ajax:
        checks.append(cc.check("security.wp.ajax.nonce", cc.PASS, "WordPress nonce helper found near AJAX surface."))
    if private_ajax and not cap_ok:
        checks.append(cc.check("security.wp.ajax.capability", cc.WARN,
                               "Authenticated AJAX handlers detected but no obvious capability/login guard was found."))

    for m in re.finditer(r"register_rest_route\((.*?)\)\s*;", blob, re.S):
        snippet = m.group(1)
        if "permission_callback" not in snippet:
            checks.append(cc.check("security.wp.rest.permission_callback", cc.FAIL,
                                   "register_rest_route() without permission_callback.", evidence=snippet[:500]))
        elif "__return_true" in snippet:
            checks.append(cc.check("security.wp.rest.public_callback", cc.WARN,
                                   "REST route uses __return_true; confirm this endpoint is intentionally public.",
                                   evidence=snippet[:500]))
    raw_sql = _evidence(r"\$wpdb\s*->\s*(query|get_results|get_var|get_row)\s*\([^)]*\$_(GET|POST|REQUEST|COOKIE)", blob)
    if raw_sql:
        checks.append(cc.check("security.wp.sql.request_input", cc.FAIL,
                               "wpdb query appears to consume request input directly; use prepare/sanitize.",
                               evidence=raw_sql))
    uploads = _evidence(r"\$_FILES", blob)
    if uploads and not _has_any(blob, ("wp_handle_upload", "wp_check_filetype", "media_handle_upload")):
        checks.append(cc.check("security.wp.uploads", cc.WARN,
                               "$_FILES usage detected without obvious WordPress upload helper/filetype check.",
                               evidence=uploads))
    return checks


def _joomla_checks(blob):
    checks = []
    if not blob:
        return checks
    token_ok = _has_any(blob, ("Session::checkToken", "JSession::checkToken", "checkToken("))
    request_mutators = _evidence(r"(task=|->input->get|Factory::getApplication\(\)->input|getApplication\(\)->input)", blob)
    if request_mutators and not token_ok:
        checks.append(cc.check("security.joomla.csrf", cc.WARN,
                               "Joomla request/task handling detected but no obvious Session::checkToken guard was found.",
                               evidence=request_mutators[:5]))
    elif request_mutators:
        checks.append(cc.check("security.joomla.csrf", cc.PASS, "Joomla token guard found near request/task surface."))
    acl_ok = _has_any(blob, ("authorise(", "getAuthorised", "Access::check", "core."))
    admin_surface = _evidence(r"(administrator|Controller|Toolbar|task\.)", blob)
    if admin_surface and not acl_ok:
        checks.append(cc.check("security.joomla.acl", cc.WARN,
                               "Admin/controller surface detected but no obvious Joomla ACL authorise() check was found."))
    raw_sql = _evidence(r"(setQuery|query\()\s*\([^)]*(\$_(GET|POST|REQUEST)|->input->get)", blob)
    if raw_sql:
        checks.append(cc.check("security.joomla.sql.request_input", cc.FAIL,
                               "Joomla database query appears to consume request input directly; use query binding/sanitize.",
                               evidence=raw_sql))
    uploads = _evidence(r"(\$_FILES|File::upload|InputFilter::isSafeFile)", blob)
    if uploads and "isSafeFile" not in blob and "InputFilter" not in blob:
        checks.append(cc.check("security.joomla.uploads", cc.WARN,
                               "Upload handling detected without obvious safe-file validation.",
                               evidence=uploads[:5]))
    return checks


def _generic_checks(ctx):
    checks = []
    secret_hits = []
    patterns = [
        (r"sk-[A-Za-z0-9_-]{20,}", "OpenAI-style API key"),
        (r"AIza[0-9A-Za-z_-]{20,}", "Google API key"),
        (r"(?i)(api[_-]?key|secret|token)\s*[:=]\s*['\"][^'\"]{12,}['\"]", "hardcoded secret-like assignment"),
    ]
    for rel, text in _iter_files(ctx):
        for pattern, label in patterns:
            if re.search(pattern, text):
                secret_hits.append("{}: {}".format(rel, label))
                break
    if secret_hits:
        checks.append(cc.check("security.hardcoded_secrets", cc.FAIL,
                               "Secret-like literals detected in source files.", evidence=secret_hits[:20]))
    else:
        checks.append(cc.check("security.hardcoded_secrets", cc.PASS, "No obvious secret-like literals detected."))
    return checks


def run(ctx):
    started = cc.now_iso()
    if ctx["target"].get("kind") == "url-live":
        return cc.layer_result(LAYER, [cc.check("security.applicability", cc.SKIP,
                               "Security static review needs a source tree or .zip.")],
                               summary="Skipped (live URL).", started_at=started)
    blob = _php_blob(ctx)
    checks = _generic_checks(ctx)
    platform = ctx["target"].get("platform")
    if platform == dt.WORDPRESS:
        checks.extend(_wordpress_checks(blob))
    elif platform == dt.JOOMLA:
        checks.extend(_joomla_checks(blob))
    else:
        checks.append(cc.check("security.platform", cc.SKIP, "Platform unknown; only generic checks ran."))
    status = cc.rollup_status(checks)
    return cc.layer_result(LAYER, checks, summary="security: {}".format(status), started_at=started)


def _ctx_from_args(args):
    desc = dt.detect(args.target)
    return {
        "target": desc,
        "target_path": os.path.abspath(args.target) if not args.target.lower().startswith("http") else args.target,
    }


def main(argv=None):
    p = argparse.ArgumentParser(description="Static CMS security review layer.")
    p.add_argument("target")
    p.add_argument("--json", action="store_true")
    args = p.parse_args(argv)
    result = run(_ctx_from_args(args))
    cc.emit(result, args.json)
    return cc.status_to_exit(result["status"])


if __name__ == "__main__":
    sys.exit(main())
