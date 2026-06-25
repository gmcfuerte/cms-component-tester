#!/usr/bin/env python3
"""Layer 2 - install / uninstall + file integrity.

What it does by DEFAULT (static, safe, no side effects):
  * Validate the manifest/header (Joomla <extension type=...>; WordPress
    "Plugin Name:" header + readme.txt "Stable tag:").
  * Cross-check every file declared in the manifest against what is actually on
    disk / in the zip, and flag declared-but-missing files and undeclared
    orphans.
  * Verify version consistency (WP: readme Stable tag == plugin Version header).

Real install / uninstall is OPT-IN only (`--allow-install`) and only ever runs
against a disposable staging instance (`--base-url`). It is never attempted in
the default static mode. Dropping tables / deleting files is out of scope.

Standalone:
    python3 layer_integrity.py <path-or-zip> [--json] [--allow-install --base-url URL]
"""

import argparse
import os
import re
import sys
import xml.etree.ElementTree as ET
import urllib.error
import urllib.request
from urllib.parse import urlsplit
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import cms_common as cc          # noqa: E402
import detect_target as dt       # noqa: E402

LAYER = "integrity"
_CODE_EXT = (".php", ".js", ".css", ".xml", ".ini", ".sql", ".html", ".tpl")
_IGNORE = {"index.html", ".gitignore", ".ds_store", "thumbs.db"}
_HTTP_UA = "cms-component-tester/1.0"


def _norm_rel(path):
    return str(path or "").replace("\\", "/").lstrip("./")


def _unsafe_relpath(path):
    raw = str(path or "").replace("\\", "/")
    if not raw:
        return False
    if raw.startswith("/") or (len(raw) > 1 and raw[1] == ":"):
        return True
    return any(part == ".." for part in raw.split("/"))


def _read_rel_text(ctx, rel, cap=200000):
    """Read a target-relative text file without escaping the target root."""
    rel = _norm_rel(rel)
    if not rel:
        return ""
    if ctx["target"]["kind"] == "zip":
        try:
            with zipfile.ZipFile(ctx["target_path"]) as zf:
                info = zf.getinfo(rel)
                if info.file_size > cap * 10:
                    return ""
                with zf.open(rel) as fh:
                    return fh.read(cap).decode("utf-8", "replace")
        except (KeyError, OSError, zipfile.BadZipFile):
            return ""
    root = ctx["target_path"]
    if os.path.isfile(root) and os.path.basename(root) == os.path.basename(rel):
        path = os.path.abspath(root)
        root_abs = os.path.dirname(path)
    else:
        root_abs = os.path.abspath(root)
        path = os.path.abspath(os.path.join(root_abs, rel))
    if not (path == root_abs or path.startswith(root_abs + os.sep)):
        return ""
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            return fh.read(cap)
    except OSError:
        return ""


def _append_suspicious_content_check(checks, name, text, source):
    findings = cc.suspicious_instruction_findings(text, source)
    if findings:
        checks.append(cc.check(name, cc.WARN,
                               "{} contains instruction-like text; reported as data only.".format(source),
                               evidence=findings))


def _strip_php_comments(text):
    """Remove PHP comments enough for static hook/guard presence checks."""
    text = re.sub(r"/\*.*?\*/", "", text or "", flags=re.S)
    text = re.sub(r"(?m)//.*$", "", text)
    text = re.sub(r"(?m)#.*$", "", text)
    return text


def _has_wp_uninstall_guard(text):
    code = _strip_php_comments(text)
    return (
        "WP_UNINSTALL_PLUGIN" in code
        and re.search(r"\b(defined|constant)\s*\(\s*['\"]WP_UNINSTALL_PLUGIN['\"]", code) is not None
    )


def _xml_local(tag):
    """Return an XML tag name without a namespace."""
    return str(tag or "").rsplit("}", 1)[-1]


def _xml_child_text(node, name):
    if node is None:
        return ""
    for child in list(node):
        if _xml_local(child.tag) == name:
            return (child.text or "").strip()
    return ""


def _xml_children(node, name):
    if node is None:
        return []
    return [child for child in list(node) if _xml_local(child.tag) == name]


def _http_get_text(url, timeout=10, cap=250000):
    req = urllib.request.Request(url, headers={"User-Agent": _HTTP_UA})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as res:
            raw = res.read(cap + 1)
            body = raw[:cap].decode("utf-8", "replace")
            return {"ok": 200 <= res.status < 300, "status": res.status, "body": body, "error": ""}
    except urllib.error.HTTPError as exc:
        body = ""
        try:
            body = exc.read(min(cap, 4096)).decode("utf-8", "replace")
        except Exception:
            body = ""
        return {"ok": False, "status": exc.code, "body": body, "error": str(exc)}
    except Exception as exc:
        return {"ok": False, "status": None, "body": "", "error": str(exc)}


def _http_probe(url, timeout=10):
    headers = {"User-Agent": _HTTP_UA}
    for method, extra_headers in (("HEAD", {}), ("GET", {"Range": "bytes=0-0"})):
        req = urllib.request.Request(url, headers=dict(headers, **extra_headers), method=method)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as res:
                return {"ok": 200 <= res.status < 300, "status": res.status, "error": ""}
        except urllib.error.HTTPError as exc:
            # Some servers reject HEAD for files but allow GET. Retry once with
            # a byte range before deciding the URL is actually inaccessible.
            if method == "HEAD" and exc.code in (403, 405, 501):
                continue
            return {"ok": False, "status": exc.code, "error": str(exc)}
        except Exception as exc:
            if method == "HEAD":
                continue
            return {"ok": False, "status": None, "error": str(exc)}
    return {"ok": False, "status": None, "error": "probe failed"}


def _manifest_update_servers(manifest_text):
    try:
        root = ET.fromstring(manifest_text or "")
    except ET.ParseError:
        return []
    servers = []
    for updateservers in root.iter():
        if _xml_local(updateservers.tag) != "updateservers":
            continue
        for server in _xml_children(updateservers, "server"):
            url = (server.text or "").strip()
            if url:
                servers.append({
                    "type": server.get("type", "extension"),
                    "name": server.get("name", ""),
                    "priority": server.get("priority", ""),
                    "url": url,
                })
    return servers


def _joomla_manifest_identity(manifest_text, manifest):
    try:
        root = ET.fromstring(manifest_text or "")
    except ET.ParseError:
        root = None
    ext_type = (manifest.get("type") or "").strip()
    name = (manifest.get("name") or "").strip()
    element = (manifest.get("element") or "").strip()
    folder = ""
    client = (manifest.get("client") or "").strip()
    if root is not None:
        if ext_type == "plugin":
            folder = (root.get("group") or "").strip()
            for filename in root.iter():
                if _xml_local(filename.tag) == "filename" and filename.get("plugin"):
                    element = filename.get("plugin", "").strip()
                    break
        if not client:
            client = (root.get("client") or "").strip()
    if not element:
        if name.startswith(("com_", "pkg_", "mod_", "plg_")):
            element = name
        elif manifest.get("path"):
            element = os.path.splitext(os.path.basename(manifest["path"]))[0]
    if ext_type == "plugin" and not client:
        client = "site"
    return {"type": ext_type, "element": element, "folder": folder, "client": client}


def _is_http_url(url):
    try:
        return urlsplit(url).scheme.lower() in ("http", "https")
    except Exception:
        return False


def _is_direct_zip_url(url):
    try:
        return urlsplit(url).path.lower().endswith(".zip")
    except Exception:
        return False


def _looks_like_license_gateway(url):
    low = url.lower()
    return "option=com_gmclicenses" in low and "task=api.download" in low


def _version_key(version):
    parts = re.findall(r"\d+|[A-Za-z]+", str(version or ""))
    key = []
    for part in parts:
        if part.isdigit():
            key.append((0, int(part)))
        else:
            key.append((1, part.lower()))
    return key


def _joomla_update_feed_checks(ctx, manifest, manifest_text):
    checks = []
    identity = _joomla_manifest_identity(manifest_text, manifest)
    servers = _manifest_update_servers(manifest_text)
    if not servers:
        checks.append(cc.check(
            "updateservers.present",
            cc.WARN,
            "No <updateservers> declared; Joomla will not discover automatic updates for this extension.",
        ))
        return checks

    checks.append(cc.check(
        "updateservers.present",
        cc.PASS,
        "{} Joomla update server(s) declared.".format(len(servers)),
        evidence=[s["url"] for s in servers[:5]],
    ))

    timeout = min(int(ctx.get("timeout") or 10), 15)
    for idx, server in enumerate(servers, start=1):
        prefix = "updateserver.{}".format(idx)
        url = server["url"]
        if not _is_http_url(url):
            checks.append(cc.check(prefix + ".url", cc.WARN,
                                   "Update server is not an http(s) URL; remote feed check skipped.",
                                   evidence=url))
            continue
        if server.get("type") not in ("extension", "collection", ""):
            checks.append(cc.check(prefix + ".type", cc.WARN,
                                   "Unusual Joomla update server type '{}'.".format(server.get("type")),
                                   evidence=url))

        feed = _http_get_text(url, timeout=timeout)
        if not feed["ok"]:
            detail = "Update feed returned HTTP {}.".format(feed["status"]) if feed["status"] else feed["error"]
            checks.append(cc.check(prefix + ".fetch", cc.FAIL, detail, evidence=url))
            continue
        checks.append(cc.check(prefix + ".fetch", cc.PASS,
                               "Update feed fetched successfully (HTTP {}).".format(feed["status"]),
                               evidence=url))
        try:
            root = ET.fromstring(feed["body"])
        except ET.ParseError as exc:
            checks.append(cc.check(prefix + ".xml", cc.FAIL,
                                   "Update feed is not well-formed XML: {}.".format(exc),
                                   evidence=url))
            continue

        root_name = _xml_local(root.tag)
        if server.get("type") == "collection" or root_name == "extensionset":
            extensions = _xml_children(root, "extension")
            checks.append(cc.check(prefix + ".collection",
                                   cc.PASS if extensions else cc.FAIL,
                                   "Collection feed contains {} extension reference(s).".format(len(extensions)),
                                   evidence=url))
            continue
        if root_name != "updates":
            checks.append(cc.check(prefix + ".root", cc.FAIL,
                                   "Extension update feed root should be <updates>, got <{}>.".format(root_name),
                                   evidence=url))
            continue
        updates = _xml_children(root, "update")
        if not updates:
            checks.append(cc.check(prefix + ".update", cc.FAIL,
                                   "Extension update feed has no <update> entry.", evidence=url))
            continue
        update = updates[0]
        feed_type = _xml_child_text(update, "type")
        feed_element = _xml_child_text(update, "element")
        feed_folder = _xml_child_text(update, "folder")
        feed_client = _xml_child_text(update, "client")
        feed_version = _xml_child_text(update, "version")

        expected_type = identity["type"]
        if expected_type and feed_type != expected_type:
            checks.append(cc.check(prefix + ".identity.type", cc.FAIL,
                                   "Update feed type '{}' does not match manifest type '{}'.".format(
                                       feed_type, expected_type),
                                   evidence=url))
        else:
            checks.append(cc.check(prefix + ".identity.type", cc.PASS,
                                   "Update feed type matches manifest ({}).".format(feed_type or expected_type)))
        expected_element = identity["element"]
        if expected_element and feed_element != expected_element:
            checks.append(cc.check(prefix + ".identity.element", cc.FAIL,
                                   "Update feed element '{}' does not match installed element '{}'.".format(
                                       feed_element, expected_element),
                                   evidence=url))
        elif expected_element:
            checks.append(cc.check(prefix + ".identity.element", cc.PASS,
                                   "Update feed element matches installed element ({}).".format(feed_element)))

        if expected_type == "plugin":
            if identity["folder"] and feed_folder != identity["folder"]:
                checks.append(cc.check(prefix + ".identity.folder", cc.FAIL,
                                       "Plugin update feed folder '{}' does not match group '{}'.".format(
                                           feed_folder, identity["folder"]),
                                       evidence=url))
            else:
                checks.append(cc.check(prefix + ".identity.folder", cc.PASS,
                                       "Plugin folder is present ({}).".format(feed_folder)))
            if not feed_client:
                checks.append(cc.check(prefix + ".identity.client", cc.FAIL,
                                       "Plugin update feed is missing <client>site</client>; Joomla may not match it.",
                                       evidence=url))
            elif feed_client != identity["client"]:
                checks.append(cc.check(prefix + ".identity.client", cc.FAIL,
                                       "Plugin update feed client '{}' does not match installed client '{}'.".format(
                                           feed_client, identity["client"]),
                                       evidence=url))
            else:
                checks.append(cc.check(prefix + ".identity.client", cc.PASS,
                                       "Plugin client matches installed client ({}).".format(feed_client)))

        manifest_version = manifest.get("version") or ""
        if not feed_version:
            checks.append(cc.check(prefix + ".version", cc.FAIL,
                                   "Update feed version is missing.", evidence=url))
        elif manifest_version and _version_key(feed_version) < _version_key(manifest_version):
            checks.append(cc.check(prefix + ".version", cc.FAIL,
                                   "Update feed version {} is lower than packaged version {}; live feed is stale.".format(
                                       feed_version, manifest_version),
                                   evidence=url))
        else:
            detail = "Update feed version: {}.".format(feed_version)
            if manifest_version and feed_version != manifest_version:
                detail += " Packaged version is {}; feed offers a newer update.".format(manifest_version)
            checks.append(cc.check(prefix + ".version", cc.PASS, detail))

        downloads = []
        for downloads_el in _xml_children(update, "downloads"):
            for dl in _xml_children(downloads_el, "downloadurl"):
                text = dl.text or ""
                downloads.append((text.strip(), text))
        if not downloads:
            checks.append(cc.check(prefix + ".downloadurl", cc.FAIL,
                                   "Update feed has no <downloads><downloadurl>.", evidence=url))
            continue
        dl_url, raw_dl_url = downloads[0]
        if raw_dl_url != dl_url:
            checks.append(cc.check(prefix + ".downloadurl.whitespace", cc.FAIL,
                                   "downloadurl contains surrounding whitespace/newlines; Joomla can treat this as malformed.",
                                   evidence=raw_dl_url))
        if not _is_http_url(dl_url):
            checks.append(cc.check(prefix + ".downloadurl", cc.FAIL,
                                   "downloadurl is not an http(s) URL.", evidence=dl_url))
            continue
        dlid = any(_xml_local(child.tag) == "dlid" for child in list(update))
        probe = _http_probe(dl_url, timeout=timeout)
        if probe["ok"]:
            checks.append(cc.check(prefix + ".download", cc.PASS,
                                   "Update package URL is reachable (HTTP {}).".format(probe["status"]),
                                   evidence=dl_url))
        elif probe["status"] in (401, 403) and dlid and _looks_like_license_gateway(dl_url):
            checks.append(cc.check(prefix + ".download", cc.PASS,
                                   "Licensed download gateway rejects unauthenticated probes as expected (HTTP {}).".format(
                                       probe["status"]),
                                   evidence=dl_url))
        elif probe["status"] in (401, 403) and _is_direct_zip_url(dl_url):
            checks.append(cc.check(prefix + ".download", cc.FAIL,
                                   "Update feed points directly to a zip that is HTTP {}; Joomla cannot download it.".format(
                                       probe["status"]),
                                   evidence=dl_url))
        else:
            detail = "Update package probe failed"
            if probe["status"]:
                detail += " with HTTP {}".format(probe["status"])
            elif probe["error"]:
                detail += ": {}".format(probe["error"])
            checks.append(cc.check(prefix + ".download", cc.FAIL, detail + ".", evidence=dl_url))
    return checks


# --- file presence abstraction (works for source tree and zip) -------------


def _build_file_index(ctx):
    """Return (all_rel_files:set, exists(rel)->bool, find_by_name(name)->list)."""
    desc = ctx["target"]
    kind = desc["kind"]
    if kind == "zip":
        zf = zipfile.ZipFile(ctx["target_path"])
        members = [m for m in zf.namelist() if not m.endswith("/")]
        rels = set(members)
        names = {}
        for m in members:
            names.setdefault(os.path.basename(m).lower(), []).append(m)
        return rels, (lambda r: r in rels or r.lstrip("./") in rels), (lambda n: names.get(n.lower(), []))
    # source tree
    root = ctx["target_path"]
    if not os.path.isdir(root):
        # Bare manifest / single file: no tree to index.
        return set(), (lambda r: False), (lambda n: [])
    rels = set()
    names = {}
    for dirpath, dirnames, filenames in os.walk(root):
        if ".git" in dirnames:
            dirnames.remove(".git")
        for f in filenames:
            rel = os.path.relpath(os.path.join(dirpath, f), root).replace(os.sep, "/")
            rels.add(rel)
            names.setdefault(f.lower(), []).append(rel)

    def _exists(r):
        r = r.lstrip("./").replace(os.sep, "/")
        if r in rels:
            return True
        return os.path.exists(os.path.join(root, r))

    return rels, _exists, (lambda n: names.get(n.lower(), []))


def _has_file_index(ctx):
    """True when there is a real tree or archive to cross-check files against."""
    return ctx["target"]["kind"] == "zip" or os.path.isdir(ctx["target_path"])


# --- Joomla integrity -------------------------------------------------------


def _joomla_integrity(ctx):
    desc = ctx["target"]
    manifest = desc.get("manifest") or {}
    checks = []

    # Manifest validity
    if not manifest.get("path"):
        checks.append(cc.check("manifest.present", cc.FAIL,
                               "No Joomla installation manifest (<extension>) found."))
        return checks, {}
    checks.append(cc.check("manifest.present", cc.PASS,
                           "Manifest: " + manifest["path"]))

    ext_type = manifest.get("type", "")
    if ext_type:
        checks.append(cc.check("manifest.type", cc.PASS,
                               "extension type=\"{}\"".format(ext_type)))
    else:
        checks.append(cc.check("manifest.type", cc.FAIL,
                               "<extension> is missing the required type attribute."))
    for field in ("name", "version"):
        if manifest.get(field):
            checks.append(cc.check("manifest." + field, cc.PASS, manifest[field]))
        else:
            checks.append(cc.check("manifest." + field, cc.FAIL,
                                   "Manifest <{}> is empty or missing.".format(field)))

    manifest_text = _read_rel_text(ctx, manifest.get("path", ""))
    checks.extend(_joomla_update_feed_checks(ctx, manifest, manifest_text))

    # File cross-check (needs an unpacked tree or a .zip).
    if not _has_file_index(ctx):
        checks.append(cc.check("files.crosscheck", cc.SKIP,
                               "Bare manifest: no unpacked source tree or .zip to cross-check files "
                               "against. Point the target at the extension folder or its .zip."))
        return checks, {"extension_type": ext_type, "declared": 0, "missing": 0}
    rels, exists, find_by_name = _build_file_index(ctx)
    declared = []
    for key in ("declared_files", "admin_files"):
        declared += manifest.get(key, [])
    declared += manifest.get("language_files", [])
    if manifest.get("scriptfile"):
        declared.append(manifest["scriptfile"])
    declared_folders = list(manifest.get("declared_folders", [])) + list(manifest.get("media_folders", []))

    _append_suspicious_content_check(
        checks, "manifest.untrusted_content",
        manifest_text, "manifest"
    )

    unsafe = sorted({p for p in declared + declared_folders if _unsafe_relpath(p)})
    if unsafe:
        checks.append(cc.check("files.paths", cc.FAIL,
                               "Manifest declares unsafe absolute or parent-traversal paths.",
                               evidence=unsafe[:25]))

    missing, relocated, present = [], [], 0
    for d in declared:
        d_norm = d.replace(os.sep, "/").lstrip("./")
        if exists(d_norm):
            present += 1
        elif find_by_name(os.path.basename(d_norm)):
            relocated.append(d_norm)
        else:
            missing.append(d_norm)

    if not declared:
        checks.append(cc.check("files.declared", cc.WARN,
                               "Manifest declares no <files>; nothing to cross-check."))
    else:
        if missing:
            checks.append(cc.check("files.missing", cc.FAIL,
                                   "{} declared file(s) not found on disk.".format(len(missing)),
                                   evidence=missing[:25]))
        else:
            checks.append(cc.check("files.missing", cc.PASS,
                                   "All {} declared files are present.".format(present + len(relocated))))
        if relocated:
            checks.append(cc.check("files.relocated", cc.WARN,
                                   "{} declared file(s) found under a different path "
                                   "than declared (package layout may differ from source layout).".format(len(relocated)),
                                   evidence=relocated[:25]))

    missing_folders = []
    for folder in declared_folders:
        f_norm = _norm_rel(folder).rstrip("/")
        if not f_norm:
            continue
        if not any(r == f_norm or r.startswith(f_norm + "/") for r in rels):
            missing_folders.append(f_norm)
    if declared_folders:
        if missing_folders:
            checks.append(cc.check("folders.missing", cc.FAIL,
                                   "{} declared folder(s) not found.".format(len(missing_folders)),
                                   evidence=missing_folders[:25]))
        else:
            checks.append(cc.check("folders.missing", cc.PASS,
                                   "All {} declared folder(s) are present.".format(len(declared_folders))))

    # Orphan detection (advisory)
    declared_basenames = {os.path.basename(d).lower() for d in declared}
    declared_basenames.add(os.path.basename(manifest["path"]).lower())
    orphans = []
    for r in rels:
        base = os.path.basename(r).lower()
        if base in _IGNORE or base in declared_basenames:
            continue
        if r.lower().endswith(_CODE_EXT) and ("test" not in r.lower()):
            orphans.append(r)
    if orphans:
        checks.append(cc.check("files.orphans", cc.WARN,
                               "{} code file(s) present but not declared in the manifest "
                               "(may be intentional includes, or packaging gaps).".format(len(orphans)),
                               evidence=sorted(orphans)[:25]))
    else:
        checks.append(cc.check("files.orphans", cc.PASS, "No undeclared code files detected."))

    # SQL install/uninstall scripts declared?
    if manifest.get("sql_files"):
        present_sql = [s for s in manifest["sql_files"] if find_by_name(os.path.basename(s))]
        checks.append(cc.check("sql.scripts", cc.PASS if present_sql else cc.WARN,
                               "Declared SQL scripts: {} found / {} declared.".format(
                                   len(present_sql), len(manifest["sql_files"]))))

    yootheme = (desc.get("entrypoints") or {}).get("yootheme") or {}
    checks.extend(_yootheme_integrity_checks(yootheme))

    meta = {
        "extension_type": ext_type,
        "declared": len(declared),
        "missing": len(missing),
        "yootheme": {
            "detected": bool(yootheme.get("detected")),
            "elements": len(yootheme.get("elements") or []),
        },
    }
    return checks, meta


def _check_name_fragment(value):
    return re.sub(r"[^a-zA-Z0-9_.-]+", "-", str(value or "unknown"))[:60]


def _yootheme_integrity_checks(yootheme):
    """Static YOOtheme Pro checks for custom elements/child-theme structure."""
    checks = []
    if not yootheme.get("detected"):
        return checks

    elements = yootheme.get("elements") or []
    modules = yootheme.get("modules") or []
    styles = yootheme.get("styles") or []
    config_files = yootheme.get("config_files") or []
    overrides = yootheme.get("overrides") or []
    custom_assets = yootheme.get("custom_assets") or []
    checks.append(cc.check(
        "yootheme.detected",
        cc.PASS,
        "YOOtheme Pro customization detected: {} element(s), {} module bootstrap(s), {} style file(s)."
        .format(len(elements), len(modules), len(styles)),
        evidence={"config": config_files, "overrides": overrides[:10], "assets": custom_assets[:10]},
    ))

    names = [e.get("name") for e in elements if e.get("name")]
    duplicates = sorted({name for name in names if names.count(name) > 1})
    if duplicates:
        checks.append(cc.check("yootheme.elements.unique_names", cc.FAIL,
                               "Custom element names must be unique.", evidence=duplicates))
    elif elements:
        checks.append(cc.check("yootheme.elements.unique_names", cc.PASS,
                               "All detected custom element names are unique."))

    for idx, element in enumerate(elements):
        label = _check_name_fragment(element.get("name") or os.path.basename(element.get("dir", "")) or idx)
        path = element.get("path", "")
        if element.get("name"):
            checks.append(cc.check("yootheme.element.{}.name".format(label), cc.PASS,
                                   "Element '{}' declared in {}.".format(element["name"], path)))
        else:
            checks.append(cc.check("yootheme.element.{}.name".format(label), cc.FAIL,
                                   "element.php is missing the required 'name' property.", evidence=path))
        checks.append(cc.check("yootheme.element.{}.template".format(label),
                               cc.PASS if element.get("has_template") else cc.FAIL,
                               "templates/template.php {}.".format(
                                   "present" if element.get("has_template") else "missing"),
                               evidence=path))
        checks.append(cc.check("yootheme.element.{}.content".format(label),
                               cc.PASS if element.get("has_content") else cc.WARN,
                               "templates/content.php {}. It keeps searchable fallback content when YOOtheme is not rendering."
                               .format("present" if element.get("has_content") else "missing"),
                               evidence=path))
        icons_ok = element.get("has_icon") and element.get("has_icon_small")
        checks.append(cc.check("yootheme.element.{}.icons".format(label),
                               cc.PASS if icons_ok else cc.WARN,
                               "Element icon.svg and iconSmall.svg {}.".format(
                                   "present" if icons_ok else "not both present"),
                               evidence=path))
        if element.get("has_fields") and not element.get("has_fieldset"):
            checks.append(cc.check("yootheme.element.{}.fieldset".format(label), cc.WARN,
                                   "Element declares fields but no fieldset; builder UI ordering may be incomplete.",
                                   evidence=path))

    if modules and not config_files:
        checks.append(cc.check("yootheme.modules.config", cc.WARN,
                               "Module bootstraps detected but no YOOtheme config.php loader was detected.",
                               evidence=[m.get("path") for m in modules[:10]]))
    elif modules:
        checks.append(cc.check("yootheme.modules.config", cc.PASS,
                               "YOOtheme module bootstrap(s) have a detected config.php loader."))

    for style in styles:
        path = style.get("path", "")
        has_name = bool(style.get("name"))
        checks.append(cc.check("yootheme.style." + _check_name_fragment(path),
                               cc.PASS if has_name else cc.WARN,
                               "Style file {} a header Name.".format("has" if has_name else "is missing"),
                               evidence=path))
    return checks


# --- WordPress integrity ----------------------------------------------------


def _wp_integrity(ctx):
    desc = ctx["target"]
    manifest = desc.get("manifest") or {}
    checks = []

    if not manifest.get("path"):
        checks.append(cc.check("header.present", cc.FAIL,
                               "No 'Plugin Name:' header found in any PHP file."))
        return checks, {}
    checks.append(cc.check("header.present", cc.PASS, "Main plugin file: " + manifest["path"]))

    if manifest.get("name"):
        checks.append(cc.check("header.plugin_name", cc.PASS, manifest["name"]))
    else:
        checks.append(cc.check("header.plugin_name", cc.FAIL, "Missing 'Plugin Name:' value."))

    version = manifest.get("version", "")
    if version:
        checks.append(cc.check("header.version", cc.PASS, version))
    else:
        checks.append(cc.check("header.version", cc.WARN,
                               "No 'Version:' header (recommended for releases)."))

    path_norm = _norm_rel(manifest["path"])
    slug = path_norm.split("/", 1)[0] if "/" in path_norm else os.path.splitext(os.path.basename(path_norm))[0]
    text_domain = manifest.get("text_domain")
    if text_domain:
        checks.append(cc.check("header.text_domain", cc.PASS if text_domain == slug else cc.FAIL,
                               "Text Domain: {} (expected plugin slug: {})".format(text_domain, slug)))
    else:
        checks.append(cc.check("header.text_domain", cc.WARN,
                               "Missing recommended header 'Text Domain'."))

    for label, key in (("Requires PHP", "requires_php"), ("Requires at least", "requires_wp")):
        if manifest.get(key):
            checks.append(cc.check("header." + key, cc.PASS, "{}: {}".format(label, manifest[key])))
        else:
            checks.append(cc.check("header." + key, cc.WARN, "Missing recommended header '{}'.".format(label)))
    if manifest.get("requires_php") and not re.match(r"^\d+(?:\.\d+){0,2}$", manifest["requires_php"]):
        checks.append(cc.check("header.requires_php.format", cc.FAIL,
                               "Requires PHP should be a bare version like 8.1, not '{}'.".format(
                                   manifest["requires_php"])))

    # readme.txt Stable tag vs Version
    readme = manifest.get("readme") or {}
    stable = readme.get("stable_tag")
    if stable is None:
        checks.append(cc.check("readme.stable_tag", cc.WARN,
                               "No readme.txt 'Stable tag:' found (required for the WP.org directory)."))
    elif stable.lower() == "trunk":
        checks.append(cc.check("readme.stable_tag", cc.WARN, "Stable tag is 'trunk'."))
    elif version and stable != version:
        checks.append(cc.check("readme.stable_tag", cc.FAIL,
                               "readme.txt Stable tag ({}) does not match plugin Version ({}). "
                               "WP.org serves the Stable tag, so a release mismatch ships the wrong code."
                               .format(stable, version)))
    elif version:
        checks.append(cc.check("readme.stable_tag", cc.PASS,
                               "Stable tag matches Version ({}).".format(version)))

    # Cross-check: main file present, uninstall hook discoverable
    if not _has_file_index(ctx):
        checks.append(cc.check("files.crosscheck", cc.SKIP,
                               "Bare header file: no unpacked source tree or .zip to cross-check against."))
        return checks, {"version": version, "stable_tag": stable}
    rels, exists, _ = _build_file_index(ctx)
    if exists(manifest["path"]):
        checks.append(cc.check("files.main", cc.PASS, "Main plugin file present."))
    else:
        checks.append(cc.check("files.main", cc.FAIL, "Declared main file not found."))

    main_text = _read_rel_text(ctx, manifest["path"])
    main_code = _strip_php_comments(main_text)
    _append_suspicious_content_check(checks, "header.untrusted_content", main_text, "main plugin file")
    if main_text:
        checks.append(cc.check("activation.hook",
                               cc.PASS if "register_activation_hook" in main_code else cc.WARN,
                               "register_activation_hook() {}.".format(
                                   "found" if "register_activation_hook" in main_code else "not found")))

    ep = desc.get("entrypoints", {})
    uninstall_files = [r for r in rels if r.lower().endswith("uninstall.php")]
    if ep.get("uninstall") or uninstall_files:
        checks.append(cc.check("uninstall.handler", cc.PASS, "uninstall.php present."))
        uninstall_text = _read_rel_text(ctx, uninstall_files[0] if uninstall_files else ep.get("uninstall"))
        has_guard = _has_wp_uninstall_guard(uninstall_text)
        checks.append(cc.check("uninstall.guard",
                               cc.PASS if has_guard else cc.FAIL,
                               "uninstall.php {} WP_UNINSTALL_PLUGIN guard.".format(
                                   "contains" if has_guard else "is missing")))
    elif "register_uninstall_hook" in main_code:
        checks.append(cc.check("uninstall.handler", cc.PASS, "register_uninstall_hook() found."))
    else:
        checks.append(cc.check("uninstall.handler", cc.WARN,
                               "No uninstall.php found; confirm register_uninstall_hook() cleans up options/tables."))

    meta = {"version": version, "stable_tag": stable}
    return checks, meta


# --- Real install / uninstall (opt-in, disposable instance only) -----------


def _wp_plugin_slugs(wp):
    res = cc.run_cmd([wp, "plugin", "list", "--field=name"])
    if not res["ok"]:
        return set()
    return {line.strip() for line in res["stdout"].splitlines() if line.strip()}


def _wp_slug_from_manifest(manifest):
    path = (manifest or {}).get("path", "")
    if "/" in path:
        return path.split("/")[0]
    base = os.path.basename(path)
    if base.lower().endswith(".php"):
        return base[:-4]
    return (manifest or {}).get("text_domain", "")


def _real_install_check(ctx, checks):
    """Append a check describing the real install/uninstall path.

    By default this is a SKIP that explains how to enable it. When enabled it
    only proceeds against a clearly-staging base URL, and for WordPress it uses
    WP-CLI on a disposable instance.
    """
    if not ctx.get("allow_install"):
        checks.append(cc.check("install.runtime", cc.SKIP,
                               "Static mode (default). Real install/uninstall is opt-in: pass "
                               "--allow-install with --base-url pointing at a DISPOSABLE staging "
                               "instance, after confirming with the operator."))
        return
    base = ctx.get("base_url")
    if not base:
        checks.append(cc.check("install.runtime", cc.ERROR,
                               "Real install requires --base-url so the production guard can verify the target."))
        return
    try:
        cc.guard_target(base, ctx.get("allow_production", False), "real install")
    except cc.GuardError as exc:
        checks.append(cc.check("install.runtime", cc.ERROR, str(exc)))
        return

    platform = ctx["target"]["platform"]
    if platform == dt.WORDPRESS:
        wp = cc.which("wp")
        if not wp:
            checks.append(cc.check("install.runtime", cc.SKIP,
                                   "WP-CLI ('wp') not on PATH; cannot run real install. "
                                   "Install on the disposable instance and re-run."))
            return
        if ctx["target"]["kind"] != "zip":
            checks.append(cc.check("install.runtime", cc.SKIP,
                                   "Real WP install expects a packaged .zip target."))
            return
        zip_path = ctx["target_path"]
        # NOTE: paths come from the operator, never from response/manifest content.
        before = _wp_plugin_slugs(wp)
        inst = cc.run_cmd([wp, "plugin", "install", zip_path, "--force", "--activate"],
                          timeout=ctx.get("timeout", 300))
        if inst["ok"]:
            checks.append(cc.check("install.activate", cc.PASS, "wp plugin install --activate succeeded."))
            # Derive the real slug from what WP-CLI actually added (the directory
            # slug, which often differs from Text Domain), falling back to the
            # manifest path. Using Text Domain alone silently no-ops uninstall.
            after = _wp_plugin_slugs(wp)
            added = sorted(after - before)
            slug = added[0] if added else _wp_slug_from_manifest(ctx["target"]["manifest"])
            if slug:
                un = cc.run_cmd([wp, "plugin", "deactivate", slug])
                unin = cc.run_cmd([wp, "plugin", "uninstall", slug])
                ok = un["ok"] and unin["ok"]
                checks.append(cc.check("install.uninstall", cc.PASS if ok else cc.WARN,
                                       "Deactivate/uninstall ran for slug '{}' (deactivate={}, uninstall={})."
                                       .format(slug, un["returncode"], unin["returncode"])))
            else:
                checks.append(cc.check("install.uninstall", cc.WARN,
                                       "Could not infer plugin slug for uninstall; do it manually."))
        else:
            checks.append(cc.check("install.activate", cc.FAIL,
                                   "wp plugin install failed.", evidence=inst["stderr"][:500]))
    else:
        checks.append(cc.check("install.runtime", cc.SKIP,
                               "Real Joomla install via CLI is not automated here (the Joomla "
                               "installer is GUI/Discover-driven). Use the human-emulation layer "
                               "to drive Extensions > Install on a disposable instance."))


# --- Entry points -----------------------------------------------------------


def run(ctx):
    started = cc.now_iso()
    desc = ctx["target"]
    platform = desc["platform"]
    if desc["kind"] == "url-live":
        return cc.layer_result(LAYER, [cc.check("applicability", cc.SKIP,
                               "Integrity needs a source tree or .zip; a live URL has no manifest to check.")],
                               summary="Skipped (live URL).", started_at=started)
    try:
        if platform == dt.JOOMLA:
            checks, meta = _joomla_integrity(ctx)
        elif platform == dt.WORDPRESS:
            checks, meta = _wp_integrity(ctx)
        else:
            return cc.layer_result(LAYER, [cc.check("platform", cc.SKIP,
                                   "Platform unknown; cannot pick manifest rules.")],
                                   summary="Skipped (unknown platform).", started_at=started)
        _real_install_check(ctx, checks)
        status = cc.rollup_status(checks)
        summary = "{} integrity: {}".format(platform, status)
        return cc.layer_result(LAYER, checks, summary=summary, meta=meta, started_at=started)
    except cc.GuardError as exc:
        return cc.error_result(LAYER, str(exc), started_at=started)


def _ctx_from_args(args):
    desc = dt.detect(args.target)
    return {
        "target": desc,
        "target_path": os.path.abspath(args.target) if not args.target.lower().startswith("http") else args.target,
        "base_url": args.base_url,
        "allow_install": args.allow_install,
        "allow_production": args.allow_production,
        "timeout": args.timeout,
    }


def main(argv=None):
    p = argparse.ArgumentParser(description="Manifest + file integrity checks.")
    p.add_argument("target", help="source tree, .zip, or .xml manifest")
    p.add_argument("--base-url", default=None)
    p.add_argument("--allow-install", action="store_true")
    p.add_argument("--allow-production", action="store_true")
    p.add_argument("--timeout", type=int, default=300)
    p.add_argument("--json", action="store_true")
    args = p.parse_args(argv)
    ctx = _ctx_from_args(args)
    result = run(ctx)
    cc.emit(result, args.json)
    return cc.status_to_exit(result["status"])


if __name__ == "__main__":
    sys.exit(main())
