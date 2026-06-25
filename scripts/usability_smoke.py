#!/usr/bin/env python3
"""Smoke-test cms-component-tester logic and CLI usability."""

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import cms_common as cc  # noqa: E402


def _write(path, text):
    cc.ensure_dir(os.path.dirname(path))
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text)


def write_fixture(root):
    cc.ensure_dir(root)
    _write(os.path.join(root, "sample-plugin.php"), """<?php
/*
Plugin Name: Sample Plugin
Version: 1.2.3
Text Domain: sample-plugin
Requires PHP: 8.1
Requires at least: 6.4
*/
add_action('rest_api_init', function () {
    register_rest_route('sample/v1', '/ping', [
        'methods' => 'GET',
        'callback' => '__return_true',
        'permission_callback' => '__return_true',
    ]);
});
register_activation_hook(__FILE__, 'sample_activate');
function sample_activate() {}
""")
    _write(os.path.join(root, "readme.txt"), "=== Sample Plugin ===\nStable tag: 1.2.3\n")
    return root


def _run(args, timeout=90):
    proc = subprocess.run(
        args,
        cwd=ROOT,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=timeout,
    )
    return {
        "returncode": proc.returncode,
        "stdout": cc.redact(proc.stdout.decode("utf-8", "replace")),
        "stderr": cc.redact(proc.stderr.decode("utf-8", "replace")),
        "cmd": args,
    }


def _cmd(*parts):
    return [sys.executable, os.path.join(HERE, "cmsct.py")] + list(parts)


def _evidence(result):
    return {
        "cmd": " ".join(result["cmd"]),
        "returncode": result["returncode"],
        "stdout": result["stdout"][-1200:],
        "stderr": result["stderr"][-1200:],
    }


def _add_command_check(checks, name, result, ok_detail, fail_detail, accept=lambda r: r["returncode"] == 0):
    if accept(result):
        checks.append(cc.check(name, cc.PASS, ok_detail))
        return True
    checks.append(cc.check(name, cc.FAIL, fail_detail, evidence=_evidence(result)))
    return False


def _load_json_stdout(result):
    try:
        return json.loads(result["stdout"])
    except ValueError:
        return None


def run_usability_smoke(out_dir, target=None, keep_fixture=False, timeout=90):
    started = cc.now_iso()
    out_dir = os.path.abspath(out_dir)
    cc.ensure_dir(out_dir)
    fixture_tmp = None
    if target:
        fixture = os.path.abspath(target)
    else:
        fixture_tmp = tempfile.mkdtemp(prefix="cmsct-usability-fixture-", dir=out_dir)
        fixture = write_fixture(fixture_tmp)

    checks = []
    artifacts = []

    help_result = _run(_cmd("--help"), timeout)
    expected_words = ("doctor", "run", "swarm", "matrix", "validate", "package")
    help_ok = help_result["returncode"] == 0 and all(word in help_result["stdout"] for word in expected_words)
    _add_command_check(
        checks,
        "usability.help.discoverable",
        help_result,
        "Top-level help exposes the main operator commands.",
        "Top-level help is missing expected commands.",
        lambda _r: help_ok,
    )

    doctor_result = _run(_cmd("doctor", fixture, "--json"), timeout)
    doctor = _load_json_stdout(doctor_result)
    doctor_ok = (
        doctor_result["returncode"] == 0 and isinstance(doctor, dict) and
        doctor.get("recommended_profile") == "static" and
        "recommended_command" in doctor and
        "integrity" in (doctor.get("safe_layers_now") or [])
    )
    _add_command_check(
        checks,
        "usability.doctor.recommends_next_step",
        doctor_result,
        "Doctor classifies the fixture and prints a safe runnable command.",
        "Doctor did not return a usable safe recommendation.",
        lambda _r: doctor_ok,
    )

    validate_result = _run(_cmd(
        "validate",
        "--api-spec", os.path.join("scenarios", "joomla-yootheme-api.example.json"),
        "--scenarios", os.path.join("scenarios", "frontend-chatbot.json"),
        "--json",
    ), timeout)
    validate = _load_json_stdout(validate_result)
    validate_ok = validate_result["returncode"] == 0 and isinstance(validate, dict) and validate.get("status") == cc.PASS
    _add_command_check(
        checks,
        "usability.validate.json_only",
        validate_result,
        "JSON scenario/spec validation works without optional YAML dependencies.",
        "JSON validation did not complete cleanly.",
        lambda _r: validate_ok,
    )

    run_dir = os.path.join(out_dir, "static-run")
    run_result = _run(_cmd(
        "run", fixture,
        "--profile", "static",
        "--out-dir", run_dir,
        "--swarm",
        "--max-agents", "3",
        "--no-html",
    ), timeout)
    output_files = [
        "report.brief.md",
        "report.handoff.json",
        "summary.md",
        "report.json",
        "junit.xml",
        "sarif.json",
        os.path.join("handoff", "handoff.json"),
    ]
    missing = [rel for rel in output_files if not os.path.exists(os.path.join(run_dir, rel))]
    run_ok = run_result["returncode"] == 0 and not missing
    _add_command_check(
        checks,
        "usability.run.static_happy_path",
        run_result,
        "Static profile completes and writes brief, JSON, CI and swarm handoff outputs.",
        "Static profile failed or missed expected outputs.",
        lambda _r: run_ok,
    )
    if run_ok:
        artifacts.append(cc.artifact("report", os.path.join(run_dir, "report.brief.md"), "usability static brief"))
        artifacts.append(cc.artifact("json", os.path.join(run_dir, "report.handoff.json"), "usability handoff json"))
        try:
            with open(os.path.join(run_dir, "report.handoff.json"), encoding="utf-8") as fh:
                handoff = json.load(fh)
            handoff_ok = (
                handoff.get("read_next") and
                str(handoff["read_next"][0]).endswith("report.brief.md") and
                os.path.getsize(os.path.join(run_dir, "report.handoff.json")) < 20000
            )
        except (OSError, ValueError):
            handoff_ok = False
        checks.append(cc.check(
            "usability.handoff.low_token",
            cc.PASS if handoff_ok else cc.FAIL,
            "Handoff prioritizes report.brief.md and stays under the compact budget."
            if handoff_ok else "Handoff is missing read order or exceeds compact budget.",
        ))

    matrix_dir = os.path.join(out_dir, "matrix")
    matrix_result = _run(_cmd(
        "matrix", fixture,
        "--profile", "static",
        "--php", "8.2,8.3",
        "--cms", "latest",
        "--viewports", "desktop",
        "--max-cases", "2",
        "--out-dir", matrix_dir,
        "--json",
    ), timeout)
    matrix = _load_json_stdout(matrix_result)
    matrix_ok = (
        matrix_result["returncode"] == 0 and isinstance(matrix, dict) and
        matrix.get("case_count") == 2 and
        os.path.exists(os.path.join(matrix_dir, "matrix-plan.json"))
    )
    _add_command_check(
        checks,
        "usability.matrix.discoverable_plan",
        matrix_result,
        "Matrix command creates a bounded CI-ready plan.",
        "Matrix command did not produce a bounded plan.",
        lambda _r: matrix_ok,
    )

    bad_result = _run(_cmd("run", fixture, "--profile", "not-a-profile"), timeout)
    bad_text = (bad_result["stdout"] + "\n" + bad_result["stderr"]).lower()
    bad_ok = bad_result["returncode"] != 0 and ("invalid choice" in bad_text or "choose one" in bad_text)
    _add_command_check(
        checks,
        "usability.errors.invalid_profile",
        bad_result,
        "Invalid profile fails fast with a readable argparse message.",
        "Invalid profile did not produce a clear operator error.",
        lambda _r: bad_ok,
    )

    if fixture_tmp and not keep_fixture:
        try:
            shutil.rmtree(fixture_tmp)
        except OSError:
            pass

    status = cc.rollup_status(checks)
    report = cc.layer_result(
        "usability",
        checks,
        summary="usability: {} check(s), {}".format(len(checks), status),
        artifacts=artifacts,
        meta={"fixture": fixture if keep_fixture or target else "", "out_dir": out_dir},
        started_at=started,
    )
    json_path = os.path.join(out_dir, "usability-report.json")
    md_path = os.path.join(out_dir, "usability-report.md")
    cc.write_json(json_path, report)
    with open(md_path, "w", encoding="utf-8") as fh:
        fh.write("# CMS component tester usability smoke\n\n")
        fh.write("Overall: `{}`\n\n".format(status))
        for check in checks:
            fh.write("- `{}` {}: {}\n".format(check["status"], check["name"], check.get("detail", "")))
    report["artifacts"].append(cc.artifact("json", json_path, "usability report json"))
    report["artifacts"].append(cc.artifact("report", md_path, "usability report markdown"))
    cc.write_json(json_path, report)
    return report


def main(argv=None):
    p = argparse.ArgumentParser(description="Run CLI logic/usability smoke checks.")
    p.add_argument("--target", default=None, help="optional CMS target; defaults to a temporary WordPress fixture")
    p.add_argument("--out-dir", default="cms-test-report/usability")
    p.add_argument("--keep-fixture", action="store_true")
    p.add_argument("--timeout", type=int, default=90)
    p.add_argument("--json", action="store_true")
    args = p.parse_args(argv)
    result = run_usability_smoke(args.out_dir, args.target, args.keep_fixture, args.timeout)
    cc.emit(result, args.json)
    return cc.status_to_exit(result["status"])


if __name__ == "__main__":
    sys.exit(main())
