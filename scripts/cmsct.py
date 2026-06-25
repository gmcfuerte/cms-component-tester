#!/usr/bin/env python3
"""Front-door CLI for cms-component-tester.

The layer scripts stay composable; this wrapper adds intent-based profiles so
operators do not have to remember every flag during routine QA.
"""

import argparse
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import cms_common as cc          # noqa: E402
import detect_target as dt       # noqa: E402
import matrix_runner             # noqa: E402
import package_skill             # noqa: E402
import playground_blueprint      # noqa: E402
import run_tests                 # noqa: E402
import scenario_generator        # noqa: E402
import self_test                 # noqa: E402
import swarm_orchestrator        # noqa: E402
import usability_smoke           # noqa: E402
import validate_specs            # noqa: E402
import visual_baseline           # noqa: E402

PROFILES = {
    "static": ["phpunit", "integrity", "quality", "visual", "security"],
    "api": ["api"],
    "human": ["human", "visual"],
    "full": ["phpunit", "integrity", "api", "human", "quality", "visual", "security"],
    "release": ["phpunit", "integrity", "quality", "visual", "security"],
    "swarm": ["phpunit", "integrity", "api", "human", "quality", "visual", "security"],
}


def profile_layers(profile):
    if profile not in PROFILES:
        raise cc.GuardError("Unknown profile '{}'. Choose one of: {}.".format(
            profile, ", ".join(sorted(PROFILES))))
    return ",".join(PROFILES[profile])


def doctor(target, base_url=None, scenarios=None, api_spec=None):
    desc = dt.detect(target)
    is_live = desc.get("kind") == "url-live"
    site_url = base_url or ((desc.get("entrypoints") or {}).get("base_url") if is_live else None)
    site_ok = bool(site_url) and not cc.looks_like_production(site_url)
    source_ok = desc.get("kind") in ("source-tree", "zip")

    safe_layers = []
    if source_ok:
        safe_layers.extend(["phpunit", "integrity", "quality", "visual", "security"])
    if site_ok:
        safe_layers.extend(["api", "human", "visual"])
    safe_layers = list(dict.fromkeys(safe_layers))

    missing = []
    if not source_ok:
        missing.append("source tree or .zip for phpunit/integrity/quality")
    if not site_url:
        missing.append("staging --base-url for api/human")
    elif not site_ok:
        missing.append("non-production staging --base-url for api/human")
    if "api" in safe_layers and not api_spec:
        missing.append("--api-spec for deterministic endpoint assertions")
    if "human" in safe_layers and not scenarios:
        missing.append("--scenarios for deterministic browser flows")

    yootheme = (desc.get("entrypoints") or {}).get("yootheme") or {}
    profile = "full" if site_ok and source_ok else ("static" if source_ok else ("human" if site_ok else "static"))
    command = [
        "python", os.path.join("scripts", "cmsct.py"), "run", target,
        "--profile", profile,
    ]
    if base_url:
        command.extend(["--base-url", base_url])
    if scenarios:
        command.extend(["--scenarios", scenarios])
    if api_spec:
        command.extend(["--api-spec", api_spec])

    return {
        "target": desc,
        "safe_layers_now": safe_layers,
        "missing_inputs": missing,
        "recommended_profile": profile,
        "recommended_command": " ".join(command),
        "yootheme": {
            "detected": bool(yootheme.get("detected")),
            "elements": len(yootheme.get("elements") or []),
            "reference": "references/yootheme-pro.md" if yootheme.get("detected") else "",
        },
    }


def _print_doctor_text(info):
    target = info["target"]
    sys.stdout.write("Target: {} ({}, {}, {})\n".format(
        target.get("input"), target.get("platform"), target.get("kind"), target.get("confidence")))
    sys.stdout.write("Safe layers now: {}\n".format(", ".join(info["safe_layers_now"]) or "none"))
    if info["missing_inputs"]:
        sys.stdout.write("Missing inputs:\n")
        for item in info["missing_inputs"]:
            sys.stdout.write("- {}\n".format(item))
    if info["yootheme"]["detected"]:
        sys.stdout.write("YOOtheme Pro: detected ({} custom element(s)); read {}\n".format(
            info["yootheme"]["elements"], info["yootheme"]["reference"]))
    sys.stdout.write("Recommended: {}\n".format(info["recommended_command"]))


def _run_profile(args):
    argv = [args.target, "--layers", profile_layers(args.profile), "--out-dir", args.out_dir]
    for opt, value in (
        ("--base-url", args.base_url),
        ("--scenarios", args.scenarios),
        ("--api-spec", args.api_spec),
        ("--platform", args.platform),
        ("--previous-report", args.previous_report),
    ):
        if value:
            argv.extend([opt, value])
    for flag in (
        "run", "run_quality", "write_scaffold", "allow_install",
        "allow_production", "headed", "brief", "no_html", "no_ci", "swarm",
    ):
        if getattr(args, flag):
            argv.append("--" + flag.replace("_", "-"))
    if args.visual_baseline:
        argv.extend(["--visual-baseline", args.visual_baseline])
    if args.handoff_dir:
        argv.extend(["--handoff-dir", args.handoff_dir])
    if args.max_agents:
        argv.extend(["--max-agents", str(args.max_agents)])
    if args.timeout:
        argv.extend(["--timeout", str(args.timeout)])
    return run_tests.main(argv)


def main(argv=None):
    parser = argparse.ArgumentParser(description="cms-component-tester command profiles.")
    sub = parser.add_subparsers(dest="command", required=True)

    p_doc = sub.add_parser("doctor", help="inspect target and recommend a safe profile")
    p_doc.add_argument("target")
    p_doc.add_argument("--base-url", default=None)
    p_doc.add_argument("--scenarios", default=None)
    p_doc.add_argument("--api-spec", default=None)
    p_doc.add_argument("--json", action="store_true")

    p_run = sub.add_parser("run", help="run a named QA profile")
    p_run.add_argument("target")
    p_run.add_argument("--profile", choices=sorted(PROFILES), default="static")
    p_run.add_argument("--base-url", default=None)
    p_run.add_argument("--scenarios", default=None)
    p_run.add_argument("--api-spec", default=None)
    p_run.add_argument("--platform", choices=[dt.JOOMLA, dt.WORDPRESS], default=None)
    p_run.add_argument("--out-dir", default="cms-test-report")
    p_run.add_argument("--run", action="store_true")
    p_run.add_argument("--run-quality", action="store_true")
    p_run.add_argument("--write-scaffold", action="store_true")
    p_run.add_argument("--allow-install", action="store_true")
    p_run.add_argument("--allow-production", action="store_true")
    p_run.add_argument("--headed", action="store_true")
    p_run.add_argument("--brief", action="store_true")
    p_run.add_argument("--no-html", action="store_true")
    p_run.add_argument("--no-ci", action="store_true")
    p_run.add_argument("--previous-report", default=None)
    p_run.add_argument("--visual-baseline", default=None)
    p_run.add_argument("--swarm", action="store_true", help="write compact vassal handoff prompts after the run")
    p_run.add_argument("--handoff-dir", default=None)
    p_run.add_argument("--max-agents", type=int, default=6)
    p_run.add_argument("--timeout", type=int, default=30)

    p_swarm = sub.add_parser("swarm", help="plan or execute token-efficient microtasks for vassal subagents")
    p_swarm.add_argument("target")
    p_swarm.add_argument("--base-url", default=None)
    p_swarm.add_argument("--scenarios", default=None)
    p_swarm.add_argument("--api-spec", default=None)
    p_swarm.add_argument("--platform", choices=[dt.JOOMLA, dt.WORDPRESS], default=None)
    p_swarm.add_argument("--out-dir", default="cms-test-report/swarm")
    p_swarm.add_argument("--execute", action="store_true", help="run local subprocess microtasks")
    p_swarm.add_argument("--max-workers", type=int, default=4)
    p_swarm.add_argument("--no-package", action="store_true")
    p_swarm.add_argument("--json", action="store_true")

    p_val = sub.add_parser("validate", help="validate API specs and human scenarios")
    p_val.add_argument("--api-spec", action="append", default=[])
    p_val.add_argument("--scenarios", action="append", default=[])
    p_val.add_argument("--json", action="store_true")

    p_pkg = sub.add_parser("package", help="build a clean installable skill bundle")
    p_pkg.add_argument("--source", default=ROOT)
    p_pkg.add_argument("--out-dir", required=True)
    p_pkg.add_argument("--zip", dest="zip_path", default=None)
    p_pkg.add_argument("--include-readme", action="store_true")
    p_pkg.add_argument("--include-tests", action="store_true")
    p_pkg.add_argument("--exclude", action="append", default=[])
    p_pkg.add_argument("--dry-run", action="store_true")
    p_pkg.add_argument("--json", action="store_true")

    p_bp = sub.add_parser("blueprint", help="generate a WordPress Playground blueprint")
    p_bp.add_argument("--plugin-url", default=None)
    p_bp.add_argument("--plugin-slug", default=None)
    p_bp.add_argument("--php", default="8.2")
    p_bp.add_argument("--wp", default="latest")
    p_bp.add_argument("--landing-page", default="/wp-admin/plugins.php")
    p_bp.add_argument("--out", default=None)

    p_gen = sub.add_parser("generate", help="generate starter API and human scenario files from detection")
    p_gen.add_argument("target")
    p_gen.add_argument("--out-dir", default="cms-test-report/generated")
    p_gen.add_argument("--json", action="store_true")

    p_base = sub.add_parser("baseline", help="create/update visual screenshot baselines from a report directory")
    p_base.add_argument("report_dir")
    p_base.add_argument("--baseline-dir", required=True)
    p_base.add_argument("--prune", action="store_true")
    p_base.add_argument("--json", action="store_true")

    p_matrix = sub.add_parser("matrix", help="generate or execute a CI/staging QA matrix")
    p_matrix.add_argument("target")
    p_matrix.add_argument("--out-dir", default="cms-test-report/matrix")
    p_matrix.add_argument("--profile", choices=sorted(PROFILES), default="static")
    p_matrix.add_argument("--base-url", default=None)
    p_matrix.add_argument("--scenarios", default=None)
    p_matrix.add_argument("--api-spec", default=None)
    p_matrix.add_argument("--platform", choices=[dt.JOOMLA, dt.WORDPRESS], default=None)
    p_matrix.add_argument("--php", default=None)
    p_matrix.add_argument("--cms", default=None)
    p_matrix.add_argument("--browsers", default=None)
    p_matrix.add_argument("--viewports", default=None)
    p_matrix.add_argument("--max-cases", type=int, default=64)
    p_matrix.add_argument("--execute", action="store_true")
    p_matrix.add_argument("--timeout", type=int, default=1800)
    p_matrix.add_argument("--json", action="store_true")

    p_ux = sub.add_parser("usability", help="run CLI logic/usability smoke checks")
    p_ux.add_argument("--target", default=None)
    p_ux.add_argument("--out-dir", default="cms-test-report/usability")
    p_ux.add_argument("--keep-fixture", action="store_true")
    p_ux.add_argument("--timeout", type=int, default=90)
    p_ux.add_argument("--json", action="store_true")

    p_self = sub.add_parser("self-test", help="run the skill's final validation checklist")
    p_self.add_argument("--out-dir", default="cms-test-report/self-test")
    p_self.add_argument("--no-unit", action="store_true")
    p_self.add_argument("--timeout", type=int, default=240)
    p_self.add_argument("--json", action="store_true")

    args = parser.parse_args(argv)
    try:
        if args.command == "doctor":
            info = doctor(args.target, args.base_url, args.scenarios, args.api_spec)
            if args.json:
                json.dump(cc.redact_tree(info), sys.stdout, indent=2, ensure_ascii=False)
                sys.stdout.write("\n")
            else:
                _print_doctor_text(cc.redact_tree(info))
            return cc.EXIT_OK
        if args.command == "run":
            if args.profile == "swarm":
                args.swarm = True
            return _run_profile(args)
        if args.command == "swarm":
            argv2 = [args.target, "--out-dir", args.out_dir, "--max-workers", str(args.max_workers)]
            for opt, value in (
                ("--base-url", args.base_url),
                ("--scenarios", args.scenarios),
                ("--api-spec", args.api_spec),
                ("--platform", args.platform),
            ):
                if value:
                    argv2.extend([opt, value])
            for flag in ("execute", "no_package", "json"):
                if getattr(args, flag):
                    argv2.append("--" + flag.replace("_", "-"))
            return swarm_orchestrator.main(argv2)
        if args.command == "validate":
            argv2 = []
            for path in args.api_spec:
                argv2.extend(["--api-spec", path])
            for path in args.scenarios:
                argv2.extend(["--scenarios", path])
            if args.json:
                argv2.append("--json")
            return validate_specs.main(argv2)
        if args.command == "package":
            argv2 = ["--source", args.source, "--out-dir", args.out_dir]
            if args.zip_path:
                argv2.extend(["--zip", args.zip_path])
            for flag in ("include_readme", "include_tests", "dry_run", "json"):
                if getattr(args, flag):
                    argv2.append("--" + flag.replace("_", "-"))
            for pattern in args.exclude:
                argv2.extend(["--exclude", pattern])
            return package_skill.main(argv2)
        if args.command == "blueprint":
            argv2 = ["--php", args.php, "--wp", args.wp, "--landing-page", args.landing_page]
            if args.plugin_url:
                argv2.extend(["--plugin-url", args.plugin_url])
            if args.plugin_slug:
                argv2.extend(["--plugin-slug", args.plugin_slug])
            if args.out:
                argv2.extend(["--out", args.out])
            return playground_blueprint.main(argv2)
        if args.command == "generate":
            argv2 = [args.target, "--out-dir", args.out_dir]
            if args.json:
                argv2.append("--json")
            return scenario_generator.main(argv2)
        if args.command == "baseline":
            argv2 = [args.report_dir, "--baseline-dir", args.baseline_dir]
            if args.prune:
                argv2.append("--prune")
            if args.json:
                argv2.append("--json")
            return visual_baseline.main(argv2)
        if args.command == "matrix":
            argv2 = [args.target, "--out-dir", args.out_dir, "--profile", args.profile,
                     "--max-cases", str(args.max_cases), "--timeout", str(args.timeout)]
            for opt, value in (
                ("--base-url", args.base_url),
                ("--scenarios", args.scenarios),
                ("--api-spec", args.api_spec),
                ("--platform", args.platform),
                ("--php", args.php),
                ("--cms", args.cms),
                ("--browsers", args.browsers),
                ("--viewports", args.viewports),
            ):
                if value:
                    argv2.extend([opt, value])
            for flag in ("execute", "json"):
                if getattr(args, flag):
                    argv2.append("--" + flag.replace("_", "-"))
            return matrix_runner.main(argv2)
        if args.command == "usability":
            argv2 = ["--out-dir", args.out_dir, "--timeout", str(args.timeout)]
            if args.target:
                argv2.extend(["--target", args.target])
            for flag in ("keep_fixture", "json"):
                if getattr(args, flag):
                    argv2.append("--" + flag.replace("_", "-"))
            return usability_smoke.main(argv2)
        if args.command == "self-test":
            argv2 = ["--out-dir", args.out_dir, "--timeout", str(args.timeout)]
            for flag in ("no_unit", "json"):
                if getattr(args, flag):
                    argv2.append("--" + flag.replace("_", "-"))
            return self_test.main(argv2)
    except cc.GuardError as exc:
        sys.stderr.write(str(exc) + "\n")
        return cc.EXIT_USAGE
    return cc.EXIT_USAGE


if __name__ == "__main__":
    sys.exit(main())
