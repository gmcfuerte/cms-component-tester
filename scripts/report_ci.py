"""JUnit and SARIF exporters for cms-component-tester reports."""

import json
import os
import xml.etree.ElementTree as ET

import cms_common as cc


def _checks(report_json):
    for result in report_json.get("results", []) or []:
        layer = result.get("layer", "unknown")
        for check in result.get("checks", []) or []:
            yield layer, check


def write_junit(report_json, path):
    checks = list(_checks(report_json))
    suite = ET.Element("testsuite", {
        "name": "cms-component-tester",
        "tests": str(len(checks)),
        "failures": str(sum(1 for _, c in checks if c.get("status") == cc.FAIL)),
        "errors": str(sum(1 for _, c in checks if c.get("status") == cc.ERROR)),
        "skipped": str(sum(1 for _, c in checks if c.get("status") == cc.SKIP)),
    })
    for layer, check in checks:
        case = ET.SubElement(suite, "testcase", {
            "classname": layer,
            "name": str(check.get("name", "check")),
        })
        status = check.get("status")
        detail = str(check.get("detail", ""))
        if status == cc.FAIL:
            ET.SubElement(case, "failure", {"message": detail}).text = detail
        elif status == cc.ERROR:
            ET.SubElement(case, "error", {"message": detail}).text = detail
        elif status == cc.SKIP:
            ET.SubElement(case, "skipped", {"message": detail}).text = detail
        elif status == cc.WARN:
            ET.SubElement(case, "system-out").text = "WARN: " + detail
    cc.ensure_dir(os.path.dirname(path) or ".")
    ET.ElementTree(suite).write(path, encoding="utf-8", xml_declaration=True)
    return path


def _sarif_level(status):
    if status in (cc.FAIL, cc.ERROR):
        return "error"
    if status == cc.WARN:
        return "warning"
    return "note"


def write_sarif(report_json, path):
    rules = {}
    results = []
    for layer, check in _checks(report_json):
        status = check.get("status")
        if status not in (cc.FAIL, cc.ERROR, cc.WARN):
            continue
        rule_id = str(check.get("name", layer))
        rules.setdefault(rule_id, {
            "id": rule_id,
            "name": rule_id,
            "shortDescription": {"text": "{} {}".format(layer, rule_id)},
        })
        result = {
            "ruleId": rule_id,
            "level": _sarif_level(status),
            "message": {"text": str(check.get("detail", ""))[:1000]},
            "properties": {"layer": layer, "status": status},
        }
        evidence = check.get("evidence")
        if isinstance(evidence, str) and evidence:
            result["locations"] = [{
                "physicalLocation": {
                    "artifactLocation": {"uri": evidence.replace("\\", "/")}
                }
            }]
        results.append(result)
    sarif = {
        "$schema": "https://json.schemastore.org/sarif-2.1.0.json",
        "version": "2.1.0",
        "runs": [{
            "tool": {
                "driver": {
                    "name": "cms-component-tester",
                    "informationUri": "https://example.invalid/cms-component-tester",
                    "rules": list(rules.values()),
                }
            },
            "results": results,
        }],
    }
    cc.write_json(path, sarif)
    return path


def write_ci_reports(report_json, out_dir):
    junit = os.path.join(out_dir, "junit.xml")
    sarif = os.path.join(out_dir, "sarif.json")
    write_junit(report_json, junit)
    write_sarif(report_json, sarif)
    return {"junit": junit, "sarif": sarif}
