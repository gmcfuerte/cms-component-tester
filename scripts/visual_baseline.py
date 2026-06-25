#!/usr/bin/env python3
"""Manage visual screenshot baselines for cms-component-tester."""

import argparse
import os
import shutil
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import cms_common as cc          # noqa: E402
import layer_visual as visual    # noqa: E402


def _pngs(root):
    out = []
    for dirpath, _, filenames in os.walk(root):
        for filename in filenames:
            if filename.lower().endswith(".png"):
                out.append(os.path.join(dirpath, filename))
    return sorted(out)


def create_baseline(report_dir, baseline_dir, prune=False):
    report_dir = os.path.abspath(report_dir)
    baseline_dir = os.path.abspath(baseline_dir)
    if not os.path.isdir(report_dir):
        raise cc.GuardError("Report directory not found: " + report_dir)
    copied = []
    seen = set()
    for src in _pngs(report_dir):
        rel = os.path.relpath(src, report_dir)
        seen.add(rel.replace("\\", "/"))
        dst = os.path.join(baseline_dir, rel)
        cc.ensure_dir(os.path.dirname(dst))
        shutil.copy2(src, dst)
        copied.append(rel.replace("\\", "/"))
    pruned = []
    if prune and os.path.isdir(baseline_dir):
        for dst in _pngs(baseline_dir):
            rel = os.path.relpath(dst, baseline_dir).replace("\\", "/")
            if rel not in seen:
                os.remove(dst)
                pruned.append(rel)
    fingerprints = {}
    for dst in _pngs(baseline_dir):
        rel = os.path.relpath(dst, baseline_dir).replace("\\", "/")
        try:
            fingerprints[rel] = visual.visual_fingerprint(dst)
        except OSError:
            continue
    cc.write_json(os.path.join(baseline_dir, "baseline-index.json"), {
        "tool": "cms-component-tester",
        "generated": cc.now_iso(),
        "source_report_dir": report_dir,
        "screenshots": copied,
        "pruned": pruned,
        "fingerprints": fingerprints,
    })
    return {"baseline_dir": baseline_dir, "screenshots": copied, "pruned": pruned,
            "fingerprints": len(fingerprints)}


def main(argv=None):
    p = argparse.ArgumentParser(description="Create/update visual baselines from a report directory.")
    p.add_argument("report_dir")
    p.add_argument("--baseline-dir", required=True)
    p.add_argument("--prune", action="store_true")
    p.add_argument("--json", action="store_true")
    args = p.parse_args(argv)
    try:
        result = create_baseline(args.report_dir, args.baseline_dir, args.prune)
    except cc.GuardError as exc:
        sys.stderr.write(str(exc) + "\n")
        return cc.EXIT_USAGE
    if args.json:
        import json
        json.dump(result, sys.stdout, indent=2, ensure_ascii=False)
        sys.stdout.write("\n")
    else:
        sys.stdout.write("Baseline: {} ({} screenshot(s))\n".format(result["baseline_dir"], len(result["screenshots"])))
    return cc.EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
