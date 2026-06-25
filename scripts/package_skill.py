#!/usr/bin/env python3
"""Build a clean installable cms-component-tester skill folder or zip."""

import argparse
import fnmatch
import hashlib
import os
import shutil
import sys
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import cms_common as cc  # noqa: E402

SKILL_NAME = "cms-component-tester"
DEFAULT_EXCLUDES = {
    ".git", ".gitignore", ".codebase-memory", ".pytest_cache", ".venv", "venv",
    "__pycache__", "cms-test-report", "screenshots", "steps", "trace.zip",
    "README.md", "tests", "*.pyc", "*.pyo", "*.zip", "*.local.env", ".env",
    "report.md", "report.json", "report.brief.md", "report.handoff.json", "report.html",
    "dashboard.html", "history.json", "summary.md", "junit.xml", "sarif.json", "matrix-plan.json",
    "matrix-summary.md", "visual-metrics.json", "*-report",
}
DEFAULT_INCLUDES = {"SKILL.md", "requirements.txt", "agents", "assets", "references", "scenarios", "schemas", "scripts"}


def _matches(name, patterns):
    return any(fnmatch.fnmatch(name, pat) for pat in patterns)


def should_include(rel, include_readme=False, include_tests=False, extra_excludes=None):
    rel = rel.replace("\\", "/").strip("/")
    if not rel:
        return True
    parts = rel.split("/")
    excludes = set(DEFAULT_EXCLUDES)
    if include_readme:
        excludes.discard("README.md")
    if include_tests:
        excludes.discard("tests")
    if extra_excludes:
        excludes.update(extra_excludes)
    if any(_matches(part, excludes) for part in parts):
        return False
    root = parts[0]
    return root in DEFAULT_INCLUDES or (include_readme and root == "README.md") or (include_tests and root == "tests")


def list_package_files(source_root, include_readme=False, include_tests=False, extra_excludes=None):
    source_root = os.path.abspath(source_root)
    files = []
    for dirpath, dirnames, filenames in os.walk(source_root):
        rel_dir = os.path.relpath(dirpath, source_root)
        rel_dir = "" if rel_dir == "." else rel_dir
        dirnames[:] = [d for d in dirnames if should_include(os.path.join(rel_dir, d), include_readme, include_tests, extra_excludes)]
        for filename in sorted(filenames):
            rel = os.path.join(rel_dir, filename) if rel_dir else filename
            if not should_include(rel, include_readme, include_tests, extra_excludes):
                continue
            files.append(rel.replace("\\", "/"))
    return sorted(files)


def _file_hash(path):
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def package_skill(source_root, output_dir, include_readme=False, include_tests=False, extra_excludes=None):
    source_root = os.path.abspath(source_root)
    output_dir = os.path.abspath(output_dir)
    dest_root = os.path.join(output_dir, SKILL_NAME)
    if source_root == output_dir or output_dir.startswith(source_root + os.sep):
        raise cc.GuardError("Output directory must not be inside the source skill folder.")
    if os.path.exists(dest_root):
        shutil.rmtree(dest_root)
    copied = []
    for rel in list_package_files(source_root, include_readme, include_tests, extra_excludes):
        src = os.path.join(source_root, rel.replace("/", os.sep))
        if os.path.islink(src):
            continue
        dst = os.path.join(dest_root, rel.replace("/", os.sep))
        cc.ensure_dir(os.path.dirname(dst))
        shutil.copy2(src, dst)
        copied.append(rel)
    return dest_root, sorted(copied)


def zip_skill(skill_dir, zip_path):
    cc.ensure_dir(os.path.dirname(zip_path) or ".")
    with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        paths = []
        for dirpath, _, filenames in os.walk(skill_dir):
            for filename in filenames:
                paths.append(os.path.join(dirpath, filename))
        for path in sorted(paths):
            arcname = os.path.relpath(path, os.path.dirname(skill_dir)).replace("\\", "/")
            info = zipfile.ZipInfo(arcname)
            info.date_time = (2024, 1, 1, 0, 0, 0)
            info.compress_type = zipfile.ZIP_DEFLATED
            with open(path, "rb") as fh:
                zf.writestr(info, fh.read())
    return zip_path


def main(argv=None):
    parser = argparse.ArgumentParser(description="Package a clean installable cms-component-tester skill.")
    parser.add_argument("--source", default=ROOT, help="source skill folder (default: repo root)")
    parser.add_argument("--out-dir", required=True, help="output parent directory")
    parser.add_argument("--zip", dest="zip_path", default=None, help="optional zip path to create")
    parser.add_argument("--include-readme", action="store_true", help="include README.md in the package")
    parser.add_argument("--include-tests", action="store_true", help="include tests in the package")
    parser.add_argument("--exclude", action="append", default=[], help="extra exclude glob (repeatable)")
    parser.add_argument("--dry-run", action="store_true", help="show package file list without copying")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    try:
        files = list_package_files(args.source, args.include_readme, args.include_tests, args.exclude)
        if args.dry_run:
            skill_dir, zip_path, zip_sha256 = None, None, None
        else:
            skill_dir, files = package_skill(args.source, args.out_dir, args.include_readme, args.include_tests, args.exclude)
            zip_path = zip_skill(skill_dir, args.zip_path) if args.zip_path else None
            zip_sha256 = _file_hash(zip_path) if zip_path else None
    except cc.GuardError as exc:
        sys.stderr.write(str(exc) + "\n")
        return cc.EXIT_USAGE
    result = {"skill_dir": skill_dir, "files": files, "zip": zip_path, "zip_sha256": zip_sha256}
    if args.json:
        action = "Would package" if args.dry_run else "Packaged"
        cc.emit(cc.layer_result("package", [cc.check("package", cc.PASS, "{} {} file(s).".format(action, len(files)))], meta=result), True)
    else:
        sys.stdout.write("{} {} file(s){}\n".format(
            "Would package" if args.dry_run else "Packaged",
            len(files),
            "" if args.dry_run else " into " + skill_dir,
        ))
        if zip_path:
            sys.stdout.write("Zip: {}\n".format(zip_path))
            sys.stdout.write("SHA256: {}\n".format(zip_sha256))
    return cc.EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
