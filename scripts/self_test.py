#!/usr/bin/env python3
"""Run the skill's own final validation checklist."""

import argparse
import json
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import cms_common as cc  # noqa: E402


def _run(cmd, timeout=180):
    proc = subprocess.run(
        cmd,
        cwd=ROOT,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=timeout,
    )
    return {
        "cmd": cmd,
        "returncode": proc.returncode,
        "stdout": cc.redact(proc.stdout.decode("utf-8", "replace")),
        "stderr": cc.redact(proc.stderr.decode("utf-8", "replace")),
    }


def _cmd_text(cmd):
    return " ".join(str(part) for part in cmd)


def _check_from_run(name, result, detail):
    if result["returncode"] == 0:
        return cc.check(name, cc.PASS, detail)
    return cc.check(name, cc.FAIL, detail + " failed.", evidence={
        "cmd": _cmd_text(result["cmd"]),
        "returncode": result["returncode"],
        "stdout": result["stdout"][-1200:],
        "stderr": result["stderr"][-1200:],
    })


def build_commands(out_dir, include_unit=True):
    commands = [
        ("self.compileall", [sys.executable, "-m", "compileall", "scripts", "tests"],
         "Python files compile cleanly."),
        ("self.validate_json", [
            sys.executable, os.path.join("scripts", "cmsct.py"), "validate",
            "--api-spec", os.path.join("scenarios", "joomla-yootheme-api.example.json"),
            "--scenarios", os.path.join("scenarios", "frontend-chatbot.json"),
            "--json",
        ], "Bundled JSON scenario/spec files validate without optional dependencies."),
        ("self.usability", [
            sys.executable, os.path.join("scripts", "cmsct.py"), "usability",
            "--out-dir", os.path.join(out_dir, "usability"),
            "--json",
        ], "CLI logic/usability smoke passes."),
        ("self.package", [
            sys.executable, os.path.join("scripts", "package_skill.py"),
            "--out-dir", os.path.join(out_dir, "package"),
            "--include-tests",
            "--json",
        ], "Clean package can be built."),
    ]
    if include_unit:
        commands.insert(0, ("self.unit", [sys.executable, "-m", "unittest", "discover", "-s", "tests", "-v"],
                            "Unit regression suite passes."))
    return commands


def run_self_test(out_dir, include_unit=True, timeout=240):
    started = cc.now_iso()
    out_dir = os.path.abspath(out_dir)
    cc.ensure_dir(out_dir)
    checks = []
    raw = []
    for name, cmd, detail in build_commands(out_dir, include_unit):
        result = _run(cmd, timeout)
        raw.append(result)
        checks.append(_check_from_run(name, result, detail))

    quick_validate = os.path.join(os.path.expanduser("~"), ".codex", "skills", ".system",
                                  "skill-creator", "scripts", "quick_validate.py")
    if os.path.isfile(quick_validate):
        result = _run([sys.executable, quick_validate, ROOT], timeout)
        raw.append(result)
        status = cc.PASS if result["returncode"] == 0 else cc.WARN
        checks.append(cc.check(
            "self.skill_validate",
            status,
            "Skill quick_validate.py completed." if status == cc.PASS
            else "Skill quick_validate.py did not complete; often this means PyYAML is not installed in the base Python.",
            evidence=None if status == cc.PASS else {
                "cmd": _cmd_text(result["cmd"]),
                "stderr": result["stderr"][-800:],
            },
        ))
    else:
        checks.append(cc.check("self.skill_validate", cc.SKIP, "quick_validate.py not found."))

    status = cc.rollup_status(checks)
    result = cc.layer_result(
        "self-test",
        checks,
        summary="self-test: {} check(s), {}".format(len(checks), status),
        meta={"out_dir": out_dir},
        started_at=started,
    )
    cc.write_json(os.path.join(out_dir, "self-test-commands.json"), raw)
    cc.write_json(os.path.join(out_dir, "self-test-report.json"), result)
    with open(os.path.join(out_dir, "self-test-report.md"), "w", encoding="utf-8") as fh:
        fh.write("# cms-component-tester self-test\n\n")
        fh.write("Overall: `{}`\n\n".format(status))
        for check in checks:
            fh.write("- `{}` {}: {}\n".format(check["status"], check["name"], check.get("detail", "")))
    return result


def main(argv=None):
    p = argparse.ArgumentParser(description="Run final validation checks for this skill.")
    p.add_argument("--out-dir", default="cms-test-report/self-test")
    p.add_argument("--no-unit", action="store_true", help="skip unittest discovery")
    p.add_argument("--timeout", type=int, default=240)
    p.add_argument("--json", action="store_true")
    args = p.parse_args(argv)
    result = run_self_test(args.out_dir, include_unit=not args.no_unit, timeout=args.timeout)
    cc.emit(result, args.json)
    return cc.status_to_exit(result["status"])


if __name__ == "__main__":
    sys.exit(main())
