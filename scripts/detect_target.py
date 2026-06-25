#!/usr/bin/env python3
"""Classify a test target for the cms-component-tester skill.

Given a path or URL, decide:
  * platform : joomla | wordpress | unknown
  * kind     : source-tree | zip | url-live
  * manifest : the parsed manifest/header metadata (type, name, version, ...)
  * entrypoints : the files/routes the other layers need

Everything the manifest or header contains is treated as DATA: we parse it with
the XML/text parsers, we never execute it.

Usage:
    python3 detect_target.py <path-or-url> [--json]

Prints a JSON descriptor on stdout. This same descriptor is what run_tests.py
passes to each layer (and writes to <out-dir>/target.json).
"""

import argparse
import io
import json
import os
import re
import sys
import xml.etree.ElementTree as ET
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import cms_common as cc  # noqa: E402

JOOMLA = "joomla"
WORDPRESS = "wordpress"
UNKNOWN = "unknown"

# --- WordPress header parsing ----------------------------------------------

WP_HEADERS = {
    "name": "Plugin Name",
    "plugin_uri": "Plugin URI",
    "version": "Version",
    "description": "Description",
    "author": "Author",
    "author_uri": "Author URI",
    "text_domain": "Text Domain",
    "domain_path": "Domain Path",
    "requires_wp": "Requires at least",
    "requires_php": "Requires PHP",
    "requires_plugins": "Requires Plugins",
    "license": "License",
    "license_uri": "License URI",
    "network": "Network",
    "update_uri": "Update URI",
}


def _read_head(path, size=16384):
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            return fh.read(size)
    except OSError:
        return ""


def parse_wp_plugin_header(text):
    """Parse the WordPress plugin file header block. Returns {} if not a plugin."""
    if "Plugin Name:" not in text:
        return {}
    headers = {}
    for key, label in WP_HEADERS.items():
        m = re.search(r"^[ \t/*#@]*" + re.escape(label) + r":(.*)$", text, re.M | re.I)
        if m:
            headers[key] = m.group(1).strip()
    return headers if headers.get("name") else {}


def parse_wp_readme(text):
    """Parse readme.txt headers; the Stable tag rule matters for releases."""
    info = {}
    m = re.search(r"^Stable tag:\s*(.+)$", text, re.M | re.I)
    if m:
        info["stable_tag"] = m.group(1).strip()
    for label, key in (("Requires at least", "requires_wp"),
                       ("Requires PHP", "requires_php"),
                       ("Tested up to", "tested_up_to")):
        mm = re.search(r"^" + re.escape(label) + r":\s*(.+)$", text, re.M | re.I)
        if mm:
            info[key] = mm.group(1).strip()
    return info


# --- Joomla manifest parsing ------------------------------------------------


def parse_joomla_manifest(xml_text):
    """Parse a Joomla installation manifest. Returns metadata or None.

    Recognised by a root <extension type="..."> element. We collect declared
    files so the integrity layer can cross-check them against disk.
    """
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return None
    if root.tag != "extension":
        return None
    info = {
        "type": root.get("type", ""),
        "method": root.get("method", ""),
        "client": root.get("client", ""),
        "name": (root.findtext("name") or "").strip(),
        "version": (root.findtext("version") or "").strip(),
        "element": (root.findtext("element") or "").strip(),
        "author": (root.findtext("author") or "").strip(),
        "scriptfile": (root.findtext("scriptfile") or "").strip(),
        "declared_files": [],
        "declared_folders": [],
        "media_folders": [],
        "language_files": [],
        "sql_files": [],
        "admin_files": [],
    }

    def _collect_files(files_el, bucket_files, bucket_folders):
        if files_el is None:
            return
        base = files_el.get("folder", "")
        for child in files_el:
            tag = child.tag.lower()
            text = (child.text or "").strip()
            if not text:
                continue
            rel = base + "/" + text if base else text
            if tag == "folder":
                bucket_folders.append(rel)
            elif tag in ("filename", "file"):
                bucket_files.append(rel)

    # Site files
    _collect_files(root.find("files"), info["declared_files"], info["declared_folders"])
    # Admin files (component)
    admin = root.find("administration")
    if admin is not None:
        _collect_files(admin.find("files"), info["admin_files"], info["declared_folders"])
        menu = admin.findtext("menu")
        if menu:
            info["admin_menu"] = menu.strip()
    # Media
    for media in root.findall("media"):
        folder = media.get("folder", "")
        if folder:
            info["media_folders"].append(folder)
        _collect_files(media, info["declared_files"], info["declared_folders"])
    # Languages
    for langs in root.findall("languages"):
        base = langs.get("folder", "")
        for lf in langs.findall("language"):
            text = (lf.text or "").strip()
            if text:
                info["language_files"].append((base + "/" + text) if base else text)
    # SQL install/uninstall
    for tag in ("install", "uninstall", "update"):
        node = root.find(tag)
        if node is not None:
            for sql in node.findall(".//file"):
                text = (sql.text or "").strip()
                if text:
                    info["sql_files"].append(text)
    return info


# --- Source tree detection --------------------------------------------------

_SKIP_DIRS = {".git", "node_modules", "vendor", ".github"}


def _walk(root, max_files=20000):
    out = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in _SKIP_DIRS]
        for f in filenames:
            out.append(os.path.join(dirpath, f))
            if len(out) >= max_files:
                return out
    return out


def detect_source_tree(root):
    files = _walk(root)
    rels = [os.path.relpath(f, root) for f in files]

    # Joomla: any *.xml whose root is <extension type=...>
    joomla_manifest = None
    joomla_manifest_path = None
    for f in files:
        if f.lower().endswith(".xml"):
            info = parse_joomla_manifest(_read_head(f, 65536))
            if info:
                # Prefer the manifest closest to the root.
                depth = os.path.relpath(f, root).count(os.sep)
                if joomla_manifest is None or depth < joomla_manifest.get("_depth", 99):
                    info["_depth"] = depth
                    joomla_manifest = info
                    joomla_manifest_path = f
    if joomla_manifest:
        joomla_manifest.pop("_depth", None)
        ep = _joomla_entrypoints(root, rels, joomla_manifest)
        return _descriptor(root, "source-tree", JOOMLA, "high",
                           manifest=_join_manifest_path(joomla_manifest, os.path.relpath(joomla_manifest_path, root)),
                           entrypoints=ep)

    # WordPress: a php file with a Plugin Name header.
    wp_main = None
    wp_header = {}
    for f in files:
        if f.lower().endswith(".php"):
            header = parse_wp_plugin_header(_read_head(f))
            if header:
                depth = os.path.relpath(f, root).count(os.sep)
                if wp_main is None or depth < wp_main[1]:
                    wp_main = (f, depth)
                    wp_header = header
    if wp_main:
        readme = None
        readme_info = {}
        for cand in ("readme.txt", "README.txt", "Readme.txt"):
            p = os.path.join(root, cand)
            if os.path.isfile(p):
                readme = p
                readme_info = parse_wp_readme(_read_head(p))
                break
        manifest = dict(wp_header)
        manifest.update({"readme": readme_info})
        ep = _wp_entrypoints(root, rels, os.path.relpath(wp_main[0], root), readme)
        return _descriptor(root, "source-tree", WORDPRESS, "high",
                           manifest=_join_manifest_path(manifest, os.path.relpath(wp_main[0], root)),
                           entrypoints=ep)

    return _descriptor(root, "source-tree", UNKNOWN, "low",
                       manifest=None, entrypoints={"declared_files": rels[:200]},
                       notes=["No Joomla <extension> manifest or WordPress 'Plugin Name:' header found."])


def _join_manifest_path(manifest, rel):
    manifest = dict(manifest)
    manifest["path"] = rel
    return manifest


def _joomla_entrypoints(root, rels, manifest):
    ep = {
        "platform_root": ".",
        "declared_files": manifest.get("declared_files", []) + manifest.get("admin_files", []),
        "scriptfile": manifest.get("scriptfile", ""),
        "sql_files": manifest.get("sql_files", []),
        "language_files": manifest.get("language_files", []),
        "endpoints_hint": [],
    }
    yootheme = _scan_yootheme_project(rels, read_text=lambda rel: _read_head(os.path.join(root, rel), 200000))
    if yootheme.get("detected"):
        ep["yootheme"] = yootheme
    if manifest.get("element"):
        opt = manifest["element"]
        if not opt.startswith("com_") and manifest.get("type") == "component":
            opt = "com_" + opt
        ep["endpoints_hint"].append({
            "kind": "com_ajax",
            "example": "index.php?option=com_ajax&{}=&format=json".format(opt),
        })
        ep["endpoints_hint"].append({
            "kind": "webservice",
            "example": "/api/index.php/v1/{}".format(opt.replace("com_", "")),
        })
    # Existing tests / phpunit config
    for r in rels:
        low = r.lower()
        if low.endswith("phpunit.xml.dist") or low.endswith("phpunit.xml"):
            ep["phpunit_config"] = r
        if low.startswith("tests" + os.sep) or (os.sep + "tests" + os.sep) in low:
            ep.setdefault("test_dir", "tests")
    return ep


def _php_array_string(text, key):
    m = re.search(r"['\"]" + re.escape(key) + r"['\"]\s*=>\s*['\"]([^'\"]+)['\"]", text or "")
    return m.group(1).strip() if m else ""


def _scan_yootheme_project(rels, read_text):
    """Detect YOOtheme Pro child-theme/custom-element files as data only.

    YOOtheme support is intentionally best-effort. We only extract static
    structure that helps choose tests and integrity checks; no PHP is executed.
    """
    rels = sorted(r.replace("\\", "/").lstrip("./") for r in rels)
    rel_set = set(rels)
    elements = []
    seen_element_dirs = set()
    for rel in rels:
        m = re.search(r"(^|/)builder/(?:elements/)?([^/]+)/element\.php$", rel, re.I)
        if not m:
            continue
        elem_dir = rel.rsplit("/", 1)[0]
        if elem_dir in seen_element_dirs:
            continue
        seen_element_dirs.add(elem_dir)
        text = read_text(rel)
        element = {
            "path": rel,
            "dir": elem_dir,
            "name": _php_array_string(text, "name"),
            "title": _php_array_string(text, "title"),
            "group": _php_array_string(text, "group"),
            "has_fields": "'fields'" in text or '"fields"' in text,
            "has_fieldset": "'fieldset'" in text or '"fieldset"' in text,
            "has_template": elem_dir + "/templates/template.php" in rel_set,
            "has_content": elem_dir + "/templates/content.php" in rel_set,
            "has_icon": elem_dir + "/images/icon.svg" in rel_set,
            "has_icon_small": elem_dir + "/images/iconSmall.svg" in rel_set,
        }
        elements.append(element)

    modules = []
    for rel in rels:
        m = re.search(r"(^|/)modules/([^/]+)/bootstrap\.php$", rel, re.I)
        if m:
            modules.append({"name": m.group(2), "path": rel})

    styles = []
    for rel in rels:
        if not re.search(r"(^|/)less/theme\.[^/]+\.less$", rel, re.I):
            continue
        text = read_text(rel)
        styles.append({
            "path": rel,
            "name": _less_header_value(text, "Name"),
            "background": _less_header_value(text, "Background"),
            "color": _less_header_value(text, "Color"),
            "type": _less_header_value(text, "Type"),
            "preview": _less_header_value(text, "Preview"),
        })

    config_files = []
    for rel in rels:
        if os.path.basename(rel).lower() != "config.php":
            continue
        text = read_text(rel)
        if "$app->load" in text or "/modules/*/bootstrap.php" in text or "YOOtheme" in text or elements or modules or styles:
            config_files.append(rel)

    overrides = [r for r in rels if r.startswith("html/") or r.startswith("templates/")]
    custom_assets = [r for r in rels if r.startswith("css/custom.") or r.startswith("fonts/")]
    detected = bool(elements or modules or styles or config_files or overrides or custom_assets)
    if not detected:
        return {"detected": False}

    warnings = []
    names = [e.get("name") for e in elements if e.get("name")]
    duplicates = sorted({n for n in names if names.count(n) > 1})
    if duplicates:
        warnings.append({"kind": "duplicate_element_names", "items": duplicates})
    for element in elements:
        if not element.get("name"):
            warnings.append({"kind": "element_missing_name", "path": element["path"]})
        if not element.get("has_template"):
            warnings.append({"kind": "element_missing_template", "path": element["path"]})

    return {
        "detected": True,
        "kind": "yootheme-pro-customization",
        "elements": elements,
        "modules": modules,
        "config_files": config_files,
        "styles": styles,
        "overrides": overrides[:200],
        "custom_assets": custom_assets[:200],
        "warnings": warnings,
    }


def _less_header_value(text, key):
    m = re.search(r"^\s*" + re.escape(key) + r"\s*:\s*(.+?)\s*$", text or "", re.M)
    return m.group(1).strip() if m else ""


def _wp_entrypoints(root, rels, main_php, readme):
    ep = {
        "platform_root": ".",
        "php_main": main_php,
        "readme": os.path.relpath(readme, root) if readme else "",
        "declared_files": rels[:500],
        "endpoints_hint": [],
        "shortcodes": [],
        "rest_namespaces": [],
        "routes": {"rest": [], "ajax": [], "admin_post": []},
        "forms": [],
        "hooks": [],
        "blocks": [],
        "assets": {"scripts": [], "styles": []},
        "activation_hooks": [],
    }
    # Scan source for hooks/routes (as data — regex only, never executed).
    blob = ""
    for r in rels:
        if r.lower().endswith(".php"):
            blob += _read_head(os.path.join(root, r), 40000) + "\n"
    if len(blob) > 4_000_000:
        blob = blob[:4_000_000]
    _scan_wp_blob(blob, ep)
    _scan_wp_blocks(root, rels, ep)
    return _dedupe_wp_entrypoints(ep)


def _scan_wp_blob(blob, ep):
    for m in re.finditer(r"register_rest_route\(\s*['\"]([^'\"]+)['\"]\s*,\s*['\"]([^'\"]+)['\"](?:\s*,\s*(.*?))?\)\s*;", blob, re.S):
        namespace, route, args = m.group(1), m.group(2), m.group(3) or ""
        methods = "GET"
        mm = re.search(r"['\"]methods['\"]\s*=>\s*([^,\]\)]+)", args)
        if mm:
            methods = mm.group(1).strip().strip("'\"")
        ep["rest_namespaces"].append(namespace + route)
        ep["routes"]["rest"].append({"namespace": namespace, "route": route, "methods": methods})
        ep["endpoints_hint"].append({"kind": "rest", "example": "/wp-json/" + namespace.strip("/") + route})
    for m in re.finditer(r"add_shortcode\(\s*['\"]([^'\"]+)['\"]", blob):
        ep["shortcodes"].append(m.group(1))
    for m in re.finditer(r"add_action\(\s*['\"]wp_ajax(_nopriv)?_([a-zA-Z0-9_]+)['\"]", blob):
        public = bool(m.group(1))
        action = m.group(2)
        ep["routes"]["ajax"].append({"action": action, "public": public})
        ep["endpoints_hint"].append({"kind": "admin-ajax", "example": "/wp-admin/admin-ajax.php?action=" + action})
    for m in re.finditer(r"add_action\(\s*['\"]admin_post(_nopriv)?_([a-zA-Z0-9_]+)['\"]", blob):
        public = bool(m.group(1))
        action = m.group(2)
        ep["routes"]["admin_post"].append({"action": action, "public": public})
        ep["forms"].append({"kind": "admin_post", "action": action, "public": public})
    for m in re.finditer(r"add_(action|filter)\(\s*['\"]([^'\"]+)['\"]\s*,\s*([^,\)]+)", blob):
        ep["hooks"].append({"type": m.group(1), "hook": m.group(2), "callback": m.group(3).strip()[:120]})
    for kind, key, regex in (
        ("script", "scripts", r"wp_(?:enqueue|register)_script\(\s*['\"]([^'\"]+)['\"]\s*,\s*([^,\)]+)"),
        ("style", "styles", r"wp_(?:enqueue|register)_style\(\s*['\"]([^'\"]+)['\"]\s*,\s*([^,\)]+)"),
    ):
        for m in re.finditer(regex, blob):
            ep["assets"][key].append({"handle": m.group(1), "path": m.group(2).strip().strip("'\"")[:200]})
    for hook_name in ("register_activation_hook", "register_deactivation_hook", "register_uninstall_hook"):
        for m in re.finditer(hook_name + r"\(\s*([^,\)]+)\s*,\s*([^\)]+)\)", blob):
            ep["activation_hooks"].append({"type": hook_name.replace("register_", "").replace("_hook", ""),
                                           "target": m.group(1).strip()[:120],
                                           "callback": m.group(2).strip()[:120]})


def _scan_wp_blocks(root, rels, ep, read_text=None):
    for r in rels:
        if os.path.basename(r).lower() != "block.json":
            continue
        try:
            raw = read_text(r) if read_text else _read_head(os.path.join(root, r), 200000)
            data = json.loads(raw)
        except (ValueError, TypeError, OSError):
            continue
        if not isinstance(data, dict):
            continue
        assets = []
        for key in ("editorScript", "script", "viewScript", "style", "editorStyle", "viewStyle"):
            value = data.get(key)
            if isinstance(value, str):
                assets.append(value)
            elif isinstance(value, list):
                assets.extend(x for x in value if isinstance(x, str))
        ep["blocks"].append({"name": data.get("name", ""), "path": r, "assets": assets})


def _dedupe_wp_entrypoints(ep):
    if any(r.lower().endswith("uninstall.php") for r in ep.get("declared_files", [])):
        ep["uninstall"] = "uninstall.php"
    for r in ep.get("declared_files", []):
        if r.lower().endswith(("phpunit.xml.dist", "phpunit.xml")):
            ep["phpunit_config"] = r
        if "tests" in r.lower().split(os.sep):
            ep.setdefault("test_dir", "tests")
    # De-dup hints
    seen = set()
    ded = []
    for h in ep["endpoints_hint"]:
        k = (h["kind"], h["example"])
        if k not in seen:
            seen.add(k)
            ded.append(h)
    ep["endpoints_hint"] = ded
    for key in ("shortcodes", "rest_namespaces"):
        ep[key] = sorted(set(ep[key]))
    for bucket in ("rest", "ajax", "admin_post"):
        seen = set()
        out = []
        for item in ep["routes"][bucket]:
            sig = tuple(sorted(item.items()))
            if sig not in seen:
                seen.add(sig)
                out.append(item)
        ep["routes"][bucket] = out
    return ep


# --- Zip detection ----------------------------------------------------------


# Bound how much we pull out of an (untrusted) archive member, so a zip-bomb
# manifest/readme can't exhaust memory before we ever parse or guard it.
_ZIP_READ_CAP = 1_048_576          # 1 MB decompressed read per member
_ZIP_DECLARED_MAX = 50_000_000     # skip members declaring > 50 MB decompressed


def _zread(zf, name, cap=_ZIP_READ_CAP):
    try:
        info = zf.getinfo(name)
    except KeyError:
        return ""
    if info.file_size > _ZIP_DECLARED_MAX:
        return ""
    try:
        with zf.open(name) as fh:
            return fh.read(cap).decode("utf-8", "replace")
    except (KeyError, OSError, zipfile.BadZipFile):
        return ""


def detect_zip(path):
    try:
        zf = zipfile.ZipFile(path)
    except zipfile.BadZipFile:
        return _descriptor(path, "zip", UNKNOWN, "low", manifest=None,
                           entrypoints={}, notes=["Not a valid zip archive."])
    names = zf.namelist()
    # Joomla manifest inside the zip
    for n in names:
        if n.lower().endswith(".xml") and not n.endswith("/"):
            info = parse_joomla_manifest(_zread(zf, n))
            if info:
                ep = {
                    "declared_files": info.get("declared_files", []) + info.get("admin_files", []),
                    "scriptfile": info.get("scriptfile", ""),
                    "archive_members": names,
                }
                yootheme = _scan_yootheme_project(names, read_text=lambda member: _zread(zf, member, cap=200000))
                if yootheme.get("detected"):
                    ep["yootheme"] = yootheme
                return _descriptor(path, "zip", JOOMLA, "high",
                                   manifest=_join_manifest_path(info, n), entrypoints=ep)
    # WordPress plugin header inside the zip
    for n in names:
        if n.lower().endswith(".php"):
            head = _zread(zf, n, cap=16384)
            if not head:
                continue
            header = parse_wp_plugin_header(head)
            if header:
                readme_info = {}
                for rn in names:
                    if rn.lower().endswith("readme.txt"):
                        readme_info = parse_wp_readme(_zread(zf, rn))
                        break
                manifest = dict(header)
                manifest["readme"] = readme_info
                ep = {
                    "platform_root": ".",
                    "php_main": n,
                    "archive_members": names,
                    "declared_files": names[:500],
                    "endpoints_hint": [],
                    "shortcodes": [],
                    "rest_namespaces": [],
                    "routes": {"rest": [], "ajax": [], "admin_post": []},
                    "forms": [],
                    "hooks": [],
                    "blocks": [],
                    "assets": {"scripts": [], "styles": []},
                    "activation_hooks": [],
                }
                blob = ""
                for member in names:
                    if member.lower().endswith(".php"):
                        blob += _zread(zf, member, cap=40000) + "\n"
                _scan_wp_blob(blob[:4_000_000], ep)
                _scan_wp_blocks("", names, ep, read_text=lambda member: _zread(zf, member, cap=200000))
                ep = _dedupe_wp_entrypoints(ep)
                return _descriptor(path, "zip", WORDPRESS, "high",
                                   manifest=_join_manifest_path(manifest, n), entrypoints=ep)
    return _descriptor(path, "zip", UNKNOWN, "low", manifest=None,
                       entrypoints={"archive_members": names[:200]},
                       notes=["Zip contains no Joomla manifest or WP plugin header."])


# --- URL detection ----------------------------------------------------------


def detect_url(url):
    low = url.lower()
    platform = UNKNOWN
    confidence = "low"
    notes = []
    if any(s in low for s in ("/administrator", "option=com_", "/index.php?option=", "joomla")):
        platform, confidence = JOOMLA, "medium"
    elif any(s in low for s in ("/wp-admin", "/wp-json", "/wp-content", "/wp-login", "wordpress")):
        platform, confidence = WORDPRESS, "medium"
    else:
        notes.append("Platform not obvious from URL; pass --platform or let the "
                     "api/human layers probe a known endpoint.")
    return {
        "input": url,
        "kind": "url-live",
        "platform": platform,
        "confidence": confidence,
        "manifest": None,
        "entrypoints": {"base_url": url.rstrip("/")},
        "notes": notes + ["Live URL: only the api and human layers apply; "
                          "integrity/phpunit need the source tree or zip."],
    }


# --- Descriptor assembly ----------------------------------------------------


def _descriptor(input_path, kind, platform, confidence, manifest, entrypoints, notes=None):
    return {
        "input": input_path,
        "kind": kind,
        "platform": platform,
        "confidence": confidence,
        "manifest": manifest,
        "entrypoints": entrypoints or {},
        "notes": notes or [],
    }


def detect(target):
    """Top-level dispatch used by run_tests.py and the CLI."""
    if re.match(r"^https?://", target, re.I):
        return detect_url(target)
    if os.path.isdir(target):
        return detect_source_tree(os.path.abspath(target))
    if os.path.isfile(target) and target.lower().endswith(".zip"):
        return detect_zip(target)
    if os.path.isfile(target) and target.lower().endswith(".xml"):
        # A bare manifest file.
        info = parse_joomla_manifest(_read_head(target, 65536))
        if info:
            return _descriptor(target, "source-tree", JOOMLA, "medium",
                               manifest=_join_manifest_path(info, os.path.basename(target)),
                               entrypoints={"note": "Bare manifest; point at the full source tree for integrity checks."})
    raise cc.GuardError("Cannot classify target: {} (expected a directory, a .zip, "
                        ".xml manifest, or an http(s) URL).".format(target))


def main(argv=None):
    parser = argparse.ArgumentParser(description="Detect a Joomla/WordPress test target.")
    parser.add_argument("target", help="path to source tree, .zip, .xml manifest, or http(s) URL")
    parser.add_argument("--json", action="store_true", default=True, help="emit JSON (default)")
    args = parser.parse_args(argv)
    try:
        desc = detect(args.target)
    except cc.GuardError as exc:
        sys.stderr.write(str(exc) + "\n")
        return cc.EXIT_USAGE
    json.dump(desc, sys.stdout, indent=2, ensure_ascii=False)
    sys.stdout.write("\n")
    return cc.EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
