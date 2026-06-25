#!/usr/bin/env python3
"""Layer 4 - human emulation (real-user simulation via a headless browser).

Drives Joomla and WordPress like a real person would: log in, navigate, fill
and submit forms, click toolbar/admin buttons, and verify system messages and
chatbot/widget responses - capturing a screenshot at every step.

Scenarios are DATA files (YAML or JSON), parametrised with ${PLACEHOLDERS} so
the same scenario runs against any staging instance. Credentials come ONLY from
the environment (CMS_ADMIN_USER / CMS_ADMIN_PASS) and are redacted everywhere.

Hard rules enforced here:
  * Never run against production (guarded; override is explicit and logged).
  * Never log or screenshot a secret value.
  * DOM/text content is DATA - asserted on, never executed.

Backend: Playwright (sync API). If Playwright is not installed, the layer does
not fail the run; it emits the parsed scenario plan as an artifact and SKIPs,
telling you how to install it. (Selenium fallback notes: references/human-emulation.md.)

Standalone:
    python3 layer_human.py --base-url URL --scenarios file_or_dir [--headed] [--json]
"""

import argparse
import glob
import json
import os
import re
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import cms_common as cc          # noqa: E402

LAYER = "human"
DEFAULT_SECRET_KEYS = {"ADMIN_USER", "ADMIN_PASS", "API_TOKEN"}


# --- scenario loading & placeholder expansion ------------------------------


def _load_scenarios(path):
    files = []
    if os.path.isdir(path):
        for pat in ("*.yml", "*.yaml", "*.json"):
            files += sorted(glob.glob(os.path.join(path, pat)))
    else:
        files = [path]
    scenarios = []

    def _add_scenario(raw, source):
        if not isinstance(raw, dict):
            raise ValueError("Scenario entries in {} must be objects.".format(source))
        if "steps" not in raw:
            # A directory may contain API specs or other data files; ignore
            # non-scenario dicts instead of turning them into empty scenarios.
            if os.path.isdir(path):
                return
            raise ValueError("Human scenario {} must contain a 'steps' list.".format(source))
        if not isinstance(raw.get("steps"), list):
            raise ValueError("Human scenario {} field 'steps' must be a list.".format(source))
        raw.setdefault("_source", source)
        scenarios.append(raw)

    for f in files:
        data = cc.load_data_file(f)
        if isinstance(data, dict) and "scenarios" in data:
            for s in data["scenarios"]:
                _add_scenario(s, f)
        elif isinstance(data, list):
            for s in data:
                _add_scenario(s, f)
        elif isinstance(data, dict):
            _add_scenario(data, f)
    return scenarios


def _base_mapping(base_url):
    return {
        "BASE_URL": base_url or os.environ.get("BASE_URL", ""),
        "ADMIN_USER": cc.env(cc.ENV_ADMIN_USER, ""),
        "ADMIN_PASS": cc.env(cc.ENV_ADMIN_PASS, ""),
        "API_TOKEN": cc.env(cc.ENV_API_TOKEN, ""),
        "OUT_DIR": os.environ.get("CMSCT_OUT_DIR", ""),
        "CMSCT_UPLOAD_ROOT": os.environ.get("CMSCT_UPLOAD_ROOT", ""),
        "CMSCT_UPLOAD_ZIP": os.environ.get("CMSCT_UPLOAD_ZIP", ""),
    }


def _scenario_mapping(base_url, scenario):
    """Allowlisted placeholder mapping for one scenario.

    Only the four canonical vars plus any the scenario explicitly opts into via
    `env:` (non-secret) / `secret_env:` (secret) are exposed — NEVER the whole
    process environment, so unrelated secrets (DB_PASSWORD, AWS_*, …) can't be
    interpolated into URLs, form values, check details or screenshots.

    Returns (mapping, secret_keys, secret_values).
    """
    mapping = _base_mapping(base_url)
    secret_keys = set(DEFAULT_SECRET_KEYS)
    for name in (scenario.get("env") or []):
        mapping[name] = os.environ.get(name, "")
    for name in (scenario.get("secret_env") or []):
        mapping[name] = os.environ.get(name, "")
        secret_keys.add(name)
    secret_values = {mapping[k] for k in secret_keys if mapping.get(k)}
    return mapping, secret_keys, secret_values


def _expand(value, mapping, secret_keys):
    return cc.substitute(value, mapping, secret_keys)


def _allowed_upload_roots(mapping):
    roots = []
    for key in ("CMSCT_UPLOAD_ROOT", "OUT_DIR"):
        if mapping.get(key):
            roots.append(os.path.abspath(mapping[key]))
    if mapping.get("CMSCT_UPLOAD_ZIP"):
        roots.append(os.path.dirname(os.path.abspath(mapping["CMSCT_UPLOAD_ZIP"])))
    return [r for r in roots if r]


def _safe_upload_path(raw_path, mapping):
    path, _ = _expand(raw_path, mapping, set())
    abs_path = os.path.abspath(path)
    roots = _allowed_upload_roots(mapping)
    if not roots:
        raise cc.GuardError("No upload root configured; set CMSCT_UPLOAD_ROOT or CMSCT_UPLOAD_ZIP.")
    if not any(abs_path == root or abs_path.startswith(root + os.sep) for root in roots):
        raise cc.GuardError("Upload path is outside allowed lab upload roots.")
    if not os.path.isfile(abs_path):
        raise cc.GuardError("Upload file does not exist: " + abs_path)
    return abs_path


# --- scenario plan (used for dry output and when Playwright is missing) ----


def _plan(scenarios):
    plan = []
    for s in scenarios:
        plan.append({
            "name": s.get("name", "scenario"),
            "platform": s.get("platform", "?"),
            "description": s.get("description", ""),
            "steps": [st.get("action", "?") for st in s.get("steps", [])],
        })
    return plan


# --- step execution (Playwright) -------------------------------------------


def _scope(page, step):
    frame_selector = step.get("frame_selector")
    if frame_selector:
        return page.frame_locator(frame_selector)
    return page


def _locator(page, step, selector=None):
    return _scope(page, step).locator(selector or step.get("selector"))


def _settle(page):
    # 'load' is safe; 'networkidle' is discouraged (admin dashboards long-poll).
    try:
        page.wait_for_load_state("load", timeout=15000)
    except Exception:
        pass


def _shot(page, scenario_dir, label):
    cc.ensure_dir(scenario_dir)
    safe = "".join(c if c.isalnum() else "-" for c in label)[:70]
    path = os.path.join(scenario_dir, safe + ".png")
    try:
        _settle(page)
        page.screenshot(path=path, full_page=True)
        return path
    except Exception:
        return None


def _exec_step(page, step, mapping, secret_keys, default_ms):
    """Run one step. Returns (status, detail). Raises only on interpreter bugs.

    Detail strings may embed expanded values; the caller redacts them before
    they reach a report, and secret-flagged inputs are masked here too.
    """
    action = step.get("action", "")
    timeout = int(step.get("timeout_ms", default_ms))
    sel = step.get("selector")

    if action == "goto":
        url, _ = _expand(step.get("url", ""), mapping, secret_keys)
        page.goto(url, timeout=timeout, wait_until="load")
        return cc.PASS, "goto " + cc.redact(url)

    if action in ("fill", "type"):
        value, is_secret = _expand(step.get("value", step.get("text", "")), mapping, secret_keys)
        loc = _locator(page, step, sel).first
        loc.wait_for(state="visible", timeout=timeout)
        if action == "fill":
            loc.fill(value)
        else:
            loc.type(value, delay=step.get("delay", 0))
        shown = "***" if (is_secret or step.get("secret")) else cc.redact(value)
        return cc.PASS, "{} {} = {}".format(action, sel, shown)

    if action == "click":
        loc = _locator(page, step, sel).first
        loc.wait_for(state="visible", timeout=timeout)
        loc.click(timeout=timeout)
        return cc.PASS, "click " + str(sel)

    if action == "upload":
        upload_path = _safe_upload_path(step.get("path", ""), mapping)
        loc = _locator(page, step, sel).first
        loc.wait_for(state="attached", timeout=timeout)
        loc.set_input_files(upload_path, timeout=timeout)
        return cc.PASS, "upload {} into {}".format(os.path.basename(upload_path), sel)

    if action == "press":
        _locator(page, step, sel).first.press(step.get("key", "Enter"), timeout=timeout)
        return cc.PASS, "press {} on {}".format(step.get("key", "Enter"), sel)

    if action in ("select", "select_option"):
        value, _ = _expand(step.get("value", ""), mapping, secret_keys)
        _locator(page, step, sel).first.select_option(value, timeout=timeout)
        return cc.PASS, "select {} = {}".format(sel, value)

    if action in ("check", "uncheck"):
        getattr(_locator(page, step, sel).first, action)(timeout=timeout)
        return cc.PASS, "{} {}".format(action, sel)

    if action == "wait_for":
        if sel:
            _locator(page, step, sel).first.wait_for(state=step.get("state", "visible"), timeout=timeout)
            return cc.PASS, "wait_for selector " + sel
        if step.get("state"):
            page.wait_for_load_state(step["state"], timeout=timeout)
            return cc.PASS, "wait_for state " + step["state"]
        page.wait_for_timeout(int(step.get("ms", 500)))
        return cc.PASS, "wait_for {} ms".format(step.get("ms", 500))

    if action in ("expect_visible", "expect_selector"):
        _locator(page, step, sel).first.wait_for(state="visible", timeout=timeout)
        return cc.PASS, "visible: " + str(sel)

    if action == "expect_text":
        needle, _ = _expand(step.get("text", ""), mapping, secret_keys)
        if needle == "":
            return cc.ERROR, "expect_text requires a non-empty text value; use expect_nonempty_text for generic replies"
        loc = _locator(page, step, sel or "body")
        loc.first.wait_for(state="visible", timeout=timeout)
        # Poll inner_text until the needle appears or the timeout elapses, so an
        # async/streamed reply (e.g. a chatbot) isn't a spurious FAIL.
        deadline = time.time() + timeout / 1000.0
        while True:
            try:
                content = loc.first.inner_text()
            except Exception:
                content = ""
            if needle in content:
                return cc.PASS, "found text {!r} in {}".format(needle, sel or "body")
            if time.time() >= deadline:
                return cc.FAIL, "text {!r} not found in {}".format(needle, sel or "body")
            page.wait_for_timeout(200)

    if action in ("expect_text_regex", "expect_regex"):
        pattern, _ = _expand(step.get("pattern", step.get("text", "")), mapping, secret_keys)
        if not pattern:
            return cc.ERROR, "expect_text_regex requires a non-empty pattern"
        try:
            regex = re.compile(pattern, re.S)
        except re.error as exc:
            return cc.ERROR, "invalid regex: " + str(exc)
        loc = _locator(page, step, sel or "body")
        loc.first.wait_for(state="visible", timeout=timeout)
        deadline = time.time() + timeout / 1000.0
        while True:
            try:
                content = loc.first.inner_text()
            except Exception:
                content = ""
            if regex.search(content):
                return cc.PASS, "text in {} matches {!r}".format(sel or "body", pattern)
            if time.time() >= deadline:
                return cc.FAIL, "text in {} did not match {!r}".format(sel or "body", pattern)
            page.wait_for_timeout(200)

    if action == "expect_nonempty_text":
        min_len = int(step.get("min_length", 1))
        loc = _locator(page, step, sel or "body")
        loc.first.wait_for(state="visible", timeout=timeout)
        deadline = time.time() + timeout / 1000.0
        while True:
            try:
                content = loc.first.inner_text()
            except Exception:
                content = ""
            measured = content.strip() if step.get("trim", True) else content
            if len(measured) >= min_len:
                return cc.PASS, "{} has at least {} visible character(s)".format(sel or "body", min_len)
            if time.time() >= deadline:
                return cc.FAIL, "{} stayed shorter than {} visible character(s)".format(sel or "body", min_len)
            page.wait_for_timeout(200)

    if action == "expect_not_text":
        needle, _ = _expand(step.get("text", ""), mapping, secret_keys)
        body = _locator(page, step, sel or "body").first.inner_text()
        if needle in body:
            return cc.FAIL, "unexpected text {!r} present".format(needle)
        return cc.PASS, "text {!r} correctly absent".format(needle)

    if action == "expect_url":
        pattern, _ = _expand(step.get("pattern", step.get("text", "")), mapping, secret_keys)
        try:
            page.wait_for_url(re.compile(pattern), timeout=timeout)
            return cc.PASS, "url matches " + pattern
        except Exception:
            return cc.FAIL, "url {} did not match {}".format(cc.redact(page.url), pattern)

    if action == "screenshot":
        return cc.PASS, "screenshot " + step.get("name", "shot")

    return cc.ERROR, "unknown action: " + str(action)


def _step_uses_secret(step, secret_keys):
    if step.get("secret"):
        return True
    for field in ("value", "text"):
        raw = step.get(field)
        if isinstance(raw, str):
            for key in secret_keys:
                if "${" + key + "}" in raw:
                    return True
    return False


def _mask_secret_element(page, selector):
    if not selector:
        return False
    try:
        page.locator(selector).first.evaluate(
            """el => {
                el.dataset.cmsTesterMaskColor = el.style.color || "";
                el.dataset.cmsTesterMaskShadow = el.style.textShadow || "";
                el.dataset.cmsTesterMaskSecurity = el.style.webkitTextSecurity || "";
                el.style.color = "transparent";
                el.style.textShadow = "0 0 0 #111";
                el.style.webkitTextSecurity = "disc";
            }"""
        )
        return True
    except Exception:
        return False


def _restore_secret_element(page, selector):
    if not selector:
        return
    try:
        page.locator(selector).first.evaluate(
            """el => {
                el.style.color = el.dataset.cmsTesterMaskColor || "";
                el.style.textShadow = el.dataset.cmsTesterMaskShadow || "";
                el.style.webkitTextSecurity = el.dataset.cmsTesterMaskSecurity || "";
                delete el.dataset.cmsTesterMaskColor;
                delete el.dataset.cmsTesterMaskShadow;
                delete el.dataset.cmsTesterMaskSecurity;
            }"""
        )
    except Exception:
        pass


def _attach_browser_event_log(page, scenario_dir, secret_values):
    path = os.path.join(scenario_dir, "browser-events.jsonl")
    cc.ensure_dir(os.path.dirname(path))
    with open(path, "w", encoding="utf-8"):
        pass

    def emit(event):
        event = cc.redact_tree(event, secret_values)
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(event, ensure_ascii=False) + "\n")

    try:
        page.on("console", lambda msg: emit({
            "type": "console",
            "level": getattr(msg, "type", ""),
            "text": getattr(msg, "text", ""),
        }))
        page.on("requestfailed", lambda req: emit({
            "type": "requestfailed",
            "url": getattr(req, "url", ""),
            "failure": str(req.failure) if getattr(req, "failure", None) else "",
        }))
        page.on("pageerror", lambda exc: emit({
            "type": "pageerror",
            "text": str(exc),
        }))

        def _response(resp):
            status = getattr(resp, "status", 0)
            if status >= 400:
                emit({"type": "response", "status": status, "url": getattr(resp, "url", "")})

        page.on("response", _response)
    except Exception:
        pass
    return path


def _state_path(ctx, raw):
    """Confine session-state files (cookies/tokens) to <out-dir>/human/state/.

    Scenario-supplied paths are treated as untrusted DATA; only their basename
    is honoured so a scenario can't write auth material to an arbitrary location.
    """
    base = cc.ensure_dir(os.path.join(ctx["out_dir"], "human", "state"))
    return os.path.join(base, os.path.basename(str(raw)) or "state.json")


def _run_scenario(browser, scenario, base_url, ctx, checks, artifacts):
    name = scenario.get("name", "scenario")
    scenario_dir = os.path.join(ctx["out_dir"], "human", "".join(c if c.isalnum() else "-" for c in name)[:60])
    cc.ensure_dir(scenario_dir)

    mapping, secret_keys, secret_values = _scenario_mapping(base_url, scenario)

    new_ctx_args = {}
    if scenario.get("viewport"):
        new_ctx_args["viewport"] = scenario["viewport"]
    if scenario.get("storage_state"):
        sp = _state_path(ctx, scenario["storage_state"])
        if os.path.isfile(sp):
            new_ctx_args["storage_state"] = sp
    context = browser.new_context(**new_ctx_args)
    page = context.new_page()
    event_log = _attach_browser_event_log(page, scenario_dir, secret_values)
    artifacts.append(cc.artifact("browser-log", event_log, "{}/browser events".format(name)))

    continue_on_failure = scenario.get("continue_on_failure", False)
    try:
        for i, step in enumerate(scenario.get("steps", [])):
            label = "{:02d}-{}".format(i, step.get("action", "step"))
            try:
                status, detail = _exec_step(page, step, mapping, secret_keys, int(ctx.get("timeout", 30)) * 1000)
            except Exception as exc:  # selector/timeout/navigation error
                status, detail = cc.ERROR, (str(exc).splitlines()[0] if str(exc) else "step error")
            # Screenshot at EVERY step (the point of human emulation).
            shot_label = step.get("name") or label
            secret_entry = step.get("action") in ("fill", "type") and _step_uses_secret(step, secret_keys)
            masked = _mask_secret_element(page, step.get("selector")) if secret_entry else False
            if secret_entry and not masked:
                checks.append(cc.check("human[{}].{}.screenshot".format(name, label), cc.WARN,
                                       "Skipped screenshot for a secret entry step because the field could not be masked."))
            else:
                try:
                    shot = _shot(page, scenario_dir, shot_label)
                    if shot and os.path.isfile(shot):
                        artifacts.append(cc.artifact("screenshot", shot, "{}/{}".format(name, shot_label)))
                    else:
                        checks.append(cc.check("human[{}].{}.screenshot".format(name, label), cc.ERROR,
                                               "Screenshot capture failed or produced no file."))
                finally:
                    if masked:
                        _restore_secret_element(page, step.get("selector"))
            optional = step.get("optional", False)
            eff_status = cc.WARN if (status in (cc.FAIL, cc.ERROR) and optional) else status
            # Redact canonical + scenario-declared secret values from the detail.
            checks.append(cc.check("human[{}].{}".format(name, label), eff_status,
                                   cc.redact(detail, secret_values)))
            if status in (cc.FAIL, cc.ERROR) and not optional and not continue_on_failure:
                checks.append(cc.check("human[{}].halt".format(name), cc.WARN,
                                       "Stopped scenario after a failing step (set continue_on_failure to override)."))
                break
        try:
            dom_text = page.locator("body").first.inner_text(timeout=1000)
        except Exception:
            dom_text = ""
        suspicious = cc.suspicious_instruction_findings(dom_text, "DOM")
        if suspicious:
            checks.append(cc.check("human[{}].untrusted_content".format(name), cc.WARN,
                                   "DOM contains instruction-like text; reported as data only.",
                                   evidence=suspicious))
        if scenario.get("save_storage_state"):
            try:
                context.storage_state(path=_state_path(ctx, scenario["save_storage_state"]))
            except Exception:
                pass
    finally:
        context.close()


# --- entry points -----------------------------------------------------------


def run(ctx):
    started = cc.now_iso()
    base_url = ctx.get("base_url")
    scen_path = ctx.get("scenarios")
    if not scen_path:
        return cc.layer_result(LAYER, [cc.check("scenarios", cc.SKIP,
                               "No --scenarios file/dir provided.")],
                               summary="Skipped (no scenarios).", started_at=started)
    if not base_url:
        return cc.layer_result(LAYER, [cc.check("base_url", cc.SKIP,
                               "No --base-url; human emulation needs a running staging site.")],
                               summary="Skipped (no base URL).", started_at=started)
    try:
        cc.guard_target(base_url, ctx.get("allow_production", False), "human emulation")
    except cc.GuardError as exc:
        return cc.error_result(LAYER, str(exc), started_at=started)

    try:
        scenarios = _load_scenarios(scen_path)
    except cc.GuardError as exc:  # optional dep missing (e.g. PyYAML) -> graceful skip
        return cc.layer_result(LAYER, [cc.check("scenarios", cc.SKIP, str(exc))],
                               summary="Skipped (scenario loader).", started_at=started)
    except (OSError, ValueError) as exc:  # malformed file -> real error
        return cc.error_result(LAYER, "Could not load scenarios: " + str(exc), started_at=started)
    if not scenarios:
        return cc.layer_result(LAYER, [cc.check("scenarios", cc.SKIP, "No scenarios found.")],
                               summary="Skipped (empty scenarios).", started_at=started)

    # Emit the plan up-front as an artifact (and the only output if Playwright missing).
    plan_path = os.path.join(ctx["out_dir"], "human", "scenario-plan.json")
    cc.write_json(plan_path, _plan(scenarios))

    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        return cc.layer_result(LAYER, [cc.check("playwright", cc.SKIP,
                               "Playwright not installed. Run: pip install playwright && "
                               "playwright install chromium. The scenario plan was written so you "
                               "can review the steps meanwhile.")],
                               summary="Skipped (Playwright missing); plan emitted.",
                               artifacts=[cc.artifact("plan", plan_path, "scenario plan")],
                               started_at=started)

    base_map = _base_mapping(base_url)
    # Pre-flight: warn (do not fail) if creds are missing for auth scenarios.
    checks, artifacts = [], [cc.artifact("plan", plan_path, "scenario plan")]
    needs_auth = any(s.get("requires_auth") for s in scenarios)
    if needs_auth and not (base_map["ADMIN_USER"] and base_map["ADMIN_PASS"]):
        checks.append(cc.check("credentials", cc.WARN,
                               "A scenario needs auth but CMS_ADMIN_USER / CMS_ADMIN_PASS are not both set."))

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=not ctx.get("headed", False))
        try:
            for scenario in scenarios:
                _run_scenario(browser, scenario, base_url, ctx, checks, artifacts)
        finally:
            browser.close()

    status = cc.rollup_status(checks)
    return cc.layer_result(LAYER, checks,
                           summary="human: {} scenario(s), {}".format(len(scenarios), status),
                           artifacts=artifacts, meta={"scenarios": len(scenarios)}, started_at=started)


def _ctx_from_args(args):
    return {
        "base_url": args.base_url,
        "scenarios": args.scenarios,
        "out_dir": args.out_dir,
        "headed": args.headed,
        "allow_production": args.allow_production,
        "timeout": args.timeout,
        "target": {"platform": "unknown"},
    }


def main(argv=None):
    p = argparse.ArgumentParser(description="Human-emulation layer (Playwright).")
    p.add_argument("--base-url", required=True)
    p.add_argument("--scenarios", required=True, help="scenario file or directory (YAML/JSON)")
    p.add_argument("--out-dir", default="cms-test-report")
    p.add_argument("--headed", action="store_true")
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
