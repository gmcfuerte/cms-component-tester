"""Compare a cms-component-tester report with a previous report."""

import json
import os

import cms_common as cc


def _finding_key(item):
    return "{}:{}".format(item.get("layer", ""), item.get("name", ""))


def _findings(report):
    out = {}
    for result in report.get("results", []) or []:
        layer = result.get("layer", "")
        for check in result.get("checks", []) or []:
            if check.get("status") in (cc.FAIL, cc.ERROR, cc.WARN):
                item = {
                    "layer": layer,
                    "name": check.get("name"),
                    "status": check.get("status"),
                    "detail": check.get("detail", ""),
                }
                out[_finding_key(item)] = item
    return out


def compare_reports(current_report, previous_report):
    current = _findings(current_report)
    previous = _findings(previous_report)
    current_keys = set(current)
    previous_keys = set(previous)
    return {
        "tool": "cms-component-tester",
        "generated": cc.now_iso(),
        "current_status": current_report.get("meta", {}).get("overall_status"),
        "previous_status": previous_report.get("meta", {}).get("overall_status"),
        "new": [current[k] for k in sorted(current_keys - previous_keys)],
        "fixed": [previous[k] for k in sorted(previous_keys - current_keys)],
        "persisting": [current[k] for k in sorted(current_keys & previous_keys)],
    }


def compare_report_files(current_path, previous_path):
    with open(current_path, "r", encoding="utf-8") as fh:
        current = json.load(fh)
    with open(previous_path, "r", encoding="utf-8") as fh:
        previous = json.load(fh)
    return compare_reports(current, previous)


def write_history(current_report, previous_report_path, out_dir):
    if not previous_report_path:
        return None
    if not os.path.isfile(previous_report_path):
        raise cc.GuardError("Previous report not found: " + previous_report_path)
    with open(previous_report_path, "r", encoding="utf-8") as fh:
        previous = json.load(fh)
    history = compare_reports(current_report, previous)
    path = os.path.join(out_dir, "history.json")
    cc.write_json(path, cc.redact_tree(history))
    return path
