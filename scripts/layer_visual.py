#!/usr/bin/env python3
"""Layer 6 - visual artifact QA.

This layer is intentionally stdlib-first. It inspects screenshots and report
artifacts already produced by the human/API layers, instead of opening a browser
again or loading image libraries. The goal is to catch cheap, high-signal visual
failures: blank/tiny screenshots, invalid PNGs, unsafe artifact paths, and
secret-looking filenames.
"""

import argparse
import glob
import hashlib
import json
import math
import os
import struct
import sys
import zlib

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import cms_common as cc          # noqa: E402
import detect_target as dt       # noqa: E402

LAYER = "visual"
PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"


def _under_dir(path, root):
    try:
        abs_path = os.path.abspath(path)
        abs_root = os.path.abspath(root)
        return abs_path == abs_root or abs_path.startswith(abs_root + os.sep)
    except (TypeError, ValueError):
        return False


def _png_info(path):
    with open(path, "rb") as fh:
        head = fh.read(33)
    if len(head) < 33 or not head.startswith(PNG_SIGNATURE):
        return None
    length = struct.unpack(">I", head[8:12])[0]
    chunk = head[12:16]
    if length < 13 or chunk != b"IHDR":
        return None
    width, height = struct.unpack(">II", head[16:24])
    bit_depth = head[24]
    color_type = head[25]
    compression = head[26]
    filter_method = head[27]
    interlace = head[28]
    return {
        "width": width,
        "height": height,
        "bit_depth": bit_depth,
        "color_type": color_type,
        "compression": compression,
        "filter": filter_method,
        "interlace": interlace,
    }


def _png_chunks(path):
    chunks = []
    with open(path, "rb") as fh:
        if fh.read(8) != PNG_SIGNATURE:
            return []
        while True:
            raw_len = fh.read(4)
            if len(raw_len) != 4:
                break
            length = struct.unpack(">I", raw_len)[0]
            kind = fh.read(4)
            data = fh.read(length)
            crc = fh.read(4)
            if len(kind) != 4 or len(data) != length or len(crc) != 4:
                break
            chunks.append((kind, data))
            if kind == b"IEND":
                break
    return chunks


def _png_idat(path):
    return b"".join(data for kind, data in _png_chunks(path) if kind == b"IDAT")


def _sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _entropy(path, cap=262144):
    with open(path, "rb") as fh:
        data = fh.read(cap)
    if not data:
        return 0.0
    counts = {}
    for b in data:
        counts[b] = counts.get(b, 0) + 1
    total = float(len(data))
    return -sum((n / total) * math.log(n / total, 2) for n in counts.values())


def _byte_entropy(data):
    if not data:
        return 0.0
    counts = {}
    for b in data:
        counts[b] = counts.get(b, 0) + 1
    total = float(len(data))
    return -sum((n / total) * math.log(n / total, 2) for n in counts.values())


def _png_pixel_entropy(path, cap=262144):
    try:
        idat = _png_idat(path)
        if not idat:
            return 0.0
        decomp = zlib.decompressobj()
        sample = decomp.decompress(idat, cap)
        return _byte_entropy(sample)
    except (OSError, zlib.error, struct.error):
        return 0.0


def _channels_for_color_type(color_type):
    return {0: 1, 2: 3, 6: 4}.get(color_type)


def _paeth(a, b, c):
    p = a + b - c
    pa = abs(p - a)
    pb = abs(p - b)
    pc = abs(p - c)
    if pa <= pb and pa <= pc:
        return a
    if pb <= pc:
        return b
    return c


def _decode_png_rows(path, max_pixels=12000000):
    """Decode common non-interlaced 8-bit PNGs without third-party libraries."""
    info = _png_info(path)
    if not info:
        return None
    width = info["width"]
    height = info["height"]
    if width < 1 or height < 1 or width * height > max_pixels:
        return None
    channels = _channels_for_color_type(info["color_type"])
    if not channels or info["bit_depth"] != 8 or info.get("interlace") != 0:
        return None
    try:
        raw = zlib.decompress(_png_idat(path))
    except (OSError, zlib.error, struct.error):
        return None
    stride = width * channels
    needed = height * (stride + 1)
    if len(raw) < needed:
        return None
    rows = []
    prev = [0] * stride
    pos = 0
    for _ in range(height):
        filter_type = raw[pos]
        pos += 1
        scan = list(raw[pos:pos + stride])
        pos += stride
        if filter_type == 1:  # Sub
            for i in range(stride):
                left = scan[i - channels] if i >= channels else 0
                scan[i] = (scan[i] + left) & 0xFF
        elif filter_type == 2:  # Up
            for i in range(stride):
                scan[i] = (scan[i] + prev[i]) & 0xFF
        elif filter_type == 3:  # Average
            for i in range(stride):
                left = scan[i - channels] if i >= channels else 0
                up = prev[i]
                scan[i] = (scan[i] + ((left + up) // 2)) & 0xFF
        elif filter_type == 4:  # Paeth
            for i in range(stride):
                left = scan[i - channels] if i >= channels else 0
                up = prev[i]
                up_left = prev[i - channels] if i >= channels else 0
                scan[i] = (scan[i] + _paeth(left, up, up_left)) & 0xFF
        elif filter_type != 0:
            return None
        rows.append(scan)
        prev = scan
    return {"info": info, "rows": rows, "channels": channels}


def _pixel_luma(row, idx, channels):
    if channels == 1:
        return row[idx]
    r, g, b = row[idx], row[idx + 1], row[idx + 2]
    if channels == 4:
        alpha = row[idx + 3] / 255.0
        r = int((r * alpha) + (255 * (1.0 - alpha)))
        g = int((g * alpha) + (255 * (1.0 - alpha)))
        b = int((b * alpha) + (255 * (1.0 - alpha)))
    return int((0.299 * r) + (0.587 * g) + (0.114 * b))


def _png_ahash(path, grid=8):
    decoded = _decode_png_rows(path)
    if not decoded:
        return None
    info = decoded["info"]
    rows = decoded["rows"]
    channels = decoded["channels"]
    values = []
    for gy in range(grid):
        y = min(info["height"] - 1, int((gy + 0.5) * info["height"] / grid))
        row = rows[y]
        for gx in range(grid):
            x = min(info["width"] - 1, int((gx + 0.5) * info["width"] / grid))
            values.append(_pixel_luma(row, x * channels, channels))
    if not values:
        return None
    avg = sum(values) / float(len(values))
    bits = 0
    for value in values:
        bits = (bits << 1) | (1 if value >= avg else 0)
    return {
        "ahash": "{:016x}".format(bits),
        "luma_mean": round(avg, 2),
        "luma_min": min(values),
        "luma_max": max(values),
    }


def _hamming_hex(left, right):
    if not left or not right:
        return None
    try:
        return bin(int(left, 16) ^ int(right, 16)).count("1")
    except ValueError:
        return None


def visual_fingerprint(path):
    """Return a stable screenshot fingerprint for baseline and drift checks."""
    size = os.path.getsize(path)
    info = _png_info(path) or {}
    fp = {
        "sha256": _sha256(path),
        "size": size,
        "width": info.get("width"),
        "height": info.get("height"),
        "bit_depth": info.get("bit_depth"),
        "color_type": info.get("color_type"),
        "file_entropy": round(_entropy(path), 4),
        "pixel_entropy": round(_png_pixel_entropy(path), 4),
        "ahash": "",
        "luma_mean": None,
        "luma_min": None,
        "luma_max": None,
    }
    phash = _png_ahash(path)
    if phash:
        fp.update(phash)
    return fp


def _screenshot_artifacts(prior_results):
    artifacts = []
    for result in prior_results or []:
        for artifact in result.get("artifacts", []) or []:
            if artifact.get("type") == "screenshot" or str(artifact.get("path", "")).lower().endswith(".png"):
                artifacts.append(artifact)
    return artifacts


def _all_artifacts(prior_results):
    artifacts = []
    for result in prior_results or []:
        artifacts.extend(result.get("artifacts", []) or [])
    return artifacts


def _scan_pngs(out_dir):
    if not out_dir or not os.path.isdir(out_dir):
        return []
    artifacts = []
    for path in glob.glob(os.path.join(out_dir, "**", "*.png"), recursive=True):
        artifacts.append({"type": "screenshot", "path": path, "label": os.path.basename(path)})
    return artifacts


def _baseline_match(path, baseline_dir, out_dir):
    if not baseline_dir:
        return None
    if out_dir and _under_dir(path, out_dir):
        rel = os.path.relpath(path, out_dir)
        candidate = os.path.join(baseline_dir, rel)
        if os.path.isfile(candidate):
            return candidate
        return None
    matches = glob.glob(os.path.join(baseline_dir, "**", os.path.basename(path)), recursive=True)
    if len(matches) == 1:
        return matches[0]
    return None


def _load_baseline_index(baseline_dir):
    if not baseline_dir:
        return {}
    path = os.path.join(baseline_dir, "baseline-index.json")
    if not os.path.isfile(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _baseline_fingerprint(path, baseline_dir, out_dir):
    index = _load_baseline_index(baseline_dir)
    rel = None
    if out_dir and _under_dir(path, out_dir):
        rel = os.path.relpath(path, out_dir).replace("\\", "/")
    fingerprints = index.get("fingerprints") or {}
    keys = [rel] if rel else [os.path.basename(path)]
    for key in keys:
        if key and isinstance(fingerprints.get(key), dict):
            match = _baseline_match(path, baseline_dir, out_dir)
            return {"path": match or key, "fingerprint": fingerprints[key], "source": "index"}
    match = _baseline_match(path, baseline_dir, out_dir)
    if not match:
        return None
    return {"path": match, "fingerprint": visual_fingerprint(match), "source": "png"}


def _env_int(name, default):
    try:
        return int(os.environ.get(name, str(default)))
    except ValueError:
        return default


def _env_float(name, default):
    try:
        return float(os.environ.get(name, str(default)))
    except ValueError:
        return default


def _strict_visual_status():
    value = os.environ.get("CMSCT_VISUAL_STRICT", "true").lower()
    return cc.WARN if value in ("0", "false", "no", "warn") else cc.FAIL


def _expected_screenshot_rels(artifacts, out_dir):
    rels = set()
    for artifact in artifacts:
        path = str(artifact.get("path", ""))
        if path and out_dir and _under_dir(path, out_dir):
            rels.add(os.path.relpath(path, out_dir).replace("\\", "/"))
        elif path:
            rels.add(os.path.basename(path))
    return rels


def analyze_baseline_inventory(screenshots, baseline_dir, out_dir):
    if not baseline_dir:
        return []
    if not os.path.isdir(baseline_dir):
        return [cc.check("visual.baseline.inventory", cc.FAIL,
                         "Visual baseline directory does not exist.", evidence=baseline_dir)]
    expected = _expected_screenshot_rels(screenshots, out_dir)
    baseline = set()
    by_name = {}
    for path in glob.glob(os.path.join(baseline_dir, "**", "*.png"), recursive=True):
        rel = os.path.relpath(path, baseline_dir).replace("\\", "/")
        baseline.add(rel)
        by_name.setdefault(os.path.basename(path), []).append(rel)
    duplicates = {name: rels for name, rels in by_name.items() if len(rels) > 1}
    stale = sorted(baseline - expected)
    missing = sorted(expected - baseline)
    checks = []
    if duplicates:
        checks.append(cc.check("visual.baseline.duplicate_names", cc.WARN,
                               "{} duplicate baseline screenshot filename(s); matching uses relative paths."
                               .format(len(duplicates)),
                               evidence=duplicates))
    if missing:
        checks.append(cc.check("visual.baseline.missing", cc.WARN,
                               "{} current screenshot(s) missing from baseline.".format(len(missing)),
                               evidence=missing[:20]))
    if stale:
        checks.append(cc.check("visual.baseline.stale", cc.WARN,
                               "{} stale baseline screenshot(s) not produced in this run.".format(len(stale)),
                               evidence=stale[:20]))
    if not checks:
        checks.append(cc.check("visual.baseline.inventory", cc.PASS,
                               "{} baseline screenshot(s) align with current artifact paths.".format(len(baseline))))
    return checks


def analyze_screenshots(artifacts, out_dir, baseline_dir=None, metrics=None):
    checks = []
    seen = set()
    secret_values = cc.secret_values()
    valid = 0
    max_distance = _env_int("CMSCT_VISUAL_MAX_DISTANCE", 6)
    max_luma_delta = _env_float("CMSCT_VISUAL_MAX_LUMA_DELTA", 8.0)
    for idx, artifact in enumerate(artifacts):
        path = artifact.get("path", "")
        label = artifact.get("label") or "screenshot{}".format(idx)
        name = "visual.screenshot." + "".join(c if c.isalnum() else "-" for c in str(label))[:60]
        if not path or path in seen:
            continue
        seen.add(path)
        if out_dir and not _under_dir(path, out_dir):
            checks.append(cc.check(name + ".scope", cc.FAIL,
                                   "Screenshot artifact path is outside the report directory.",
                                   evidence=path))
            continue
        if any(secret in str(path) for secret in secret_values):
            checks.append(cc.check(name + ".secret_path", cc.FAIL,
                                   "Screenshot path contains a configured secret value."))
        if not os.path.isfile(path):
            checks.append(cc.check(name + ".exists", cc.FAIL, "Screenshot file missing.", evidence=path))
            continue
        size = os.path.getsize(path)
        info = _png_info(path)
        if not info:
            checks.append(cc.check(name + ".png", cc.FAIL, "Screenshot is not a valid PNG.", evidence=path))
            continue
        valid += 1
        dims = "{}x{}".format(info["width"], info["height"])
        checks.append(cc.check(name + ".png", cc.PASS,
                               "Valid PNG screenshot: {}, {} bytes.".format(dims, size)))
        if info["width"] < 80 or info["height"] < 80:
            checks.append(cc.check(name + ".dimensions", cc.WARN,
                                   "Screenshot is very small ({}); selector/page may not have rendered fully."
                                   .format(dims)))
        pixels = max(1, info["width"] * info["height"])
        bytes_per_pixel = size / float(pixels)
        entropy = _entropy(path)
        pixel_entropy = _png_pixel_entropy(path)
        fingerprint = visual_fingerprint(path)
        if metrics is not None:
            metrics.append({
                "label": label,
                "path": path,
                "fingerprint": fingerprint,
            })
        if bytes_per_pixel < 0.03 or pixel_entropy < 1.5 or entropy < 2.0:
            checks.append(cc.check(name + ".blank_suspect", cc.FAIL,
                                   "Screenshot looks highly compressed/low entropy; possible blank or single-color render "
                                   "(file entropy {:.2f}, pixel entropy {:.2f}, {:.4f} bytes/pixel)."
                                   .format(entropy, pixel_entropy, bytes_per_pixel)))
        baseline = _baseline_fingerprint(path, baseline_dir, out_dir)
        if baseline:
            base_fp = baseline["fingerprint"]
            same_hash = fingerprint.get("sha256") == base_fp.get("sha256")
            if same_hash:
                checks.append(cc.check(name + ".baseline", cc.PASS, "Screenshot matches baseline exactly."))
            else:
                distance = _hamming_hex(fingerprint.get("ahash"), base_fp.get("ahash"))
                luma_delta = None
                if fingerprint.get("luma_mean") is not None and base_fp.get("luma_mean") is not None:
                    luma_delta = abs(float(fingerprint["luma_mean"]) - float(base_fp["luma_mean"]))
                byte_delta = size - int(base_fp.get("size") or 0)
                evidence = {
                    "current": path,
                    "baseline": baseline["path"],
                    "ahash_distance": distance,
                    "luma_delta": round(luma_delta, 2) if luma_delta is not None else None,
                    "byte_delta": byte_delta,
                }
                drift = (distance is None or distance > max_distance or
                         (luma_delta is not None and luma_delta > max_luma_delta))
                if drift:
                    checks.append(cc.check(
                        name + ".baseline",
                        _strict_visual_status(),
                        "Screenshot perceptually differs from baseline "
                        "(aHash distance {}, luma delta {}, byte delta {}; thresholds: {}, {})."
                        .format(distance, evidence["luma_delta"], byte_delta, max_distance, max_luma_delta),
                        evidence=evidence,
                    ))
                else:
                    checks.append(cc.check(
                        name + ".baseline", cc.PASS,
                        "Screenshot byte hash changed but perceptual delta is within threshold "
                        "(aHash distance {}, luma delta {}).".format(distance, evidence["luma_delta"]),
                        evidence=evidence,
                    ))
        elif baseline_dir:
            checks.append(cc.check(name + ".baseline", cc.WARN,
                                   "No matching baseline screenshot found.", evidence=path))
    if valid:
        checks.insert(0, cc.check("visual.screenshots", cc.PASS,
                                  "{} screenshot artifact(s) inspected.".format(valid)))
    else:
        checks.append(cc.check("visual.screenshots", cc.SKIP,
                               "No screenshots found. Run the human layer first or point at a report directory."))
    return checks


def _human_layer_ran(prior_results):
    for result in prior_results or []:
        if result.get("layer") != "human":
            continue
        if result.get("status") in (cc.PASS, cc.FAIL, cc.ERROR):
            return True
        if any(a.get("type") == "browser-log" for a in result.get("artifacts", []) or []):
            return True
    return False


def analyze_screenshot_expectation(prior_results, screenshots):
    if screenshots:
        return []
    if _human_layer_ran(prior_results):
        return [cc.check("visual.screenshots.expected", cc.FAIL,
                         "Human emulation ran but produced no screenshot artifacts.")]
    return []


def _browser_log_artifacts(artifacts):
    logs = []
    for artifact in artifacts:
        path = str(artifact.get("path", ""))
        if artifact.get("type") == "browser-log" or os.path.basename(path).lower() == "browser-events.jsonl":
            logs.append(artifact)
    return logs


def _read_browser_events(path, max_lines=2000):
    events = []
    try:
        with open(path, "r", encoding="utf-8") as fh:
            for line_no, line in enumerate(fh, 1):
                if line_no > max_lines:
                    events.append({"type": "truncated", "line": line_no})
                    break
                line = line.strip()
                if not line:
                    continue
                try:
                    event = json.loads(line)
                except ValueError:
                    event = {"type": "malformed", "line": line_no, "text": line[:300]}
                events.append(event)
    except OSError as exc:
        events.append({"type": "read-error", "text": str(exc)})
    return events


def analyze_browser_events(artifacts, out_dir):
    logs = _browser_log_artifacts(artifacts)
    if not logs:
        return []
    hard = []
    soft = []
    scanned = 0
    for artifact in logs:
        path = str(artifact.get("path", ""))
        if not path or not os.path.isfile(path):
            hard.append({"event": "missing-log", "path": path})
            continue
        if out_dir and not _under_dir(path, out_dir):
            hard.append({"event": "unsafe-log-path", "path": path})
            continue
        for event in _read_browser_events(path):
            scanned += 1
            kind = str(event.get("type", ""))
            level = str(event.get("level", "")).lower()
            status = int(event.get("status") or 0)
            item = {"type": kind, "level": level, "status": status,
                    "url": event.get("url", ""), "text": event.get("text", "")}
            if kind in ("pageerror", "requestfailed", "read-error", "malformed"):
                hard.append(item)
            elif kind == "response" and status >= 500:
                hard.append(item)
            elif kind == "console" and level in ("error", "assert"):
                hard.append(item)
            elif kind == "response" and status >= 400:
                soft.append(item)
            elif kind == "console" and level == "warning":
                soft.append(item)
            elif kind == "truncated":
                soft.append(item)
    if hard:
        return [cc.check(
            "visual.browser_events", cc.FAIL,
            "{} blocking browser/runtime event(s) found across {} log(s)."
            .format(len(hard), len(logs)),
            evidence=cc.redact_tree(hard[:20]),
        )]
    if soft:
        return [cc.check(
            "visual.browser_events", cc.WARN,
            "{} advisory browser/runtime event(s) found across {} log(s)."
            .format(len(soft), len(logs)),
            evidence=cc.redact_tree(soft[:20]),
        )]
    return [cc.check("visual.browser_events", cc.PASS,
                     "{} browser/runtime event(s) scanned across {} log(s).".format(scanned, len(logs)))]


def analyze_artifact_safety(artifacts, out_dir):
    if not artifacts:
        return []
    issues = []
    inspected = 0
    for artifact in artifacts:
        path = str(artifact.get("path", ""))
        if not path:
            continue
        inspected += 1
        if path.lower().startswith(("javascript:", "data:")):
            issues.append({"issue": "unsafe-url", "path": path, "type": artifact.get("type")})
            continue
        if out_dir and not _under_dir(path, out_dir):
            issues.append({"issue": "outside-report-dir", "path": path, "type": artifact.get("type")})
            continue
        if not os.path.exists(path):
            issues.append({"issue": "missing-file", "path": path, "type": artifact.get("type")})
            continue
        if os.path.isdir(path):
            issues.append({"issue": "directory-artifact", "path": path, "type": artifact.get("type")})
    if issues:
        return [cc.check("visual.artifacts.safety", cc.FAIL,
                         "{} unsafe/missing artifact reference(s) found.".format(len(issues)),
                         evidence=issues[:30])]
    return [cc.check("visual.artifacts.safety", cc.PASS,
                     "{} artifact path(s) are local, present, and report-scoped.".format(inspected))]


def _file_contains_secret(path, secret_bytes, chunk_size=262144):
    if not secret_bytes:
        return False
    longest = max(len(secret) for secret in secret_bytes)
    tail = b""
    try:
        with open(path, "rb") as fh:
            while True:
                chunk = fh.read(chunk_size)
                if not chunk:
                    break
                data = tail + chunk
                if any(secret in data for secret in secret_bytes):
                    return True
                tail = data[-max(0, longest - 1):]
    except OSError:
        return False
    return False


def analyze_artifact_leaks(artifacts, out_dir):
    checks = []
    secrets = cc.secret_values()
    if not artifacts:
        return checks
    if not secrets:
        checks.append(cc.check("visual.artifacts.secrets", cc.PASS,
                               "No configured secret values to scan for in artifacts."))
        return checks
    leaked = []
    for artifact in artifacts:
        path = str(artifact.get("path", ""))
        label = str(artifact.get("label", ""))
        kind = str(artifact.get("type", "artifact"))
        haystack = "\n".join([path, label, kind])
        for secret in secrets:
            if secret and secret in haystack:
                leaked.append({"where": "artifact metadata", "artifact": cc.redact(label or path)})
        if not path or not os.path.isfile(path) or (out_dir and not _under_dir(path, out_dir)):
            continue
        secret_bytes = [secret.encode("utf-8", "ignore") for secret in secrets if secret]
        if _file_contains_secret(path, secret_bytes):
            leaked.append({"where": "artifact content", "artifact": cc.redact(label or path)})
    if leaked:
        checks.append(cc.check("visual.artifacts.secrets", cc.FAIL,
                               "{} artifact secret leak(s) detected.".format(len(leaked)),
                               evidence=leaked[:20]))
    else:
        checks.append(cc.check("visual.artifacts.secrets", cc.PASS,
                               "{} artifact(s) scanned for configured secret leaks.".format(len(artifacts))))
    return checks


def run(ctx):
    started = cc.now_iso()
    out_dir = ctx.get("out_dir")
    baseline_dir = ctx.get("visual_baseline")
    all_artifacts = _all_artifacts(ctx.get("prior_results"))
    screenshots = _screenshot_artifacts(ctx.get("prior_results"))
    if not screenshots:
        screenshots = _scan_pngs(out_dir)
        all_artifacts.extend(screenshots)
    metrics = []
    artifacts = []
    checks = analyze_artifact_safety(all_artifacts, out_dir)
    checks.extend(analyze_artifact_leaks(all_artifacts, out_dir))
    checks.extend(analyze_browser_events(all_artifacts, out_dir))
    checks.extend(analyze_screenshot_expectation(ctx.get("prior_results"), screenshots))
    checks.extend(analyze_baseline_inventory(screenshots, baseline_dir, out_dir))
    checks.extend(analyze_screenshots(screenshots, out_dir, baseline_dir, metrics))
    metrics_path = None
    if out_dir and metrics:
        metrics_path = os.path.join(out_dir, "visual", "visual-metrics.json")
        cc.write_json(metrics_path, {"tool": "cms-component-tester", "screenshots": metrics})
        artifacts.append(cc.artifact("visual-metrics", metrics_path, "visual fingerprints/metrics"))
    status = cc.rollup_status(checks)
    return cc.layer_result(LAYER, checks,
                           summary="visual: {} screenshot/artifact check(s), {}".format(len(checks), status),
                           artifacts=artifacts,
                           meta={"screenshots": len(screenshots), "artifacts": len(all_artifacts),
                                 "baseline": baseline_dir or "", "metrics": metrics_path or ""},
                           started_at=started)


def _ctx_from_args(args):
    desc = dt.detect(args.target) if args.target else {"platform": dt.UNKNOWN, "kind": "url-live", "entrypoints": {}}
    return {
        "target": desc,
        "target_path": os.path.abspath(args.target) if args.target and not args.target.lower().startswith("http") else args.target,
        "out_dir": args.out_dir,
        "visual_baseline": args.baseline,
        "prior_results": [],
    }


def main(argv=None):
    p = argparse.ArgumentParser(description="Visual artifact QA layer.")
    p.add_argument("target", nargs="?", help="optional source/zip/url for metadata")
    p.add_argument("--out-dir", default="cms-test-report", help="report directory to scan")
    p.add_argument("--baseline", default=None, help="optional baseline screenshot directory")
    p.add_argument("--json", action="store_true")
    args = p.parse_args(argv)
    result = run(_ctx_from_args(args))
    cc.emit(result, args.json)
    return cc.status_to_exit(result["status"])


if __name__ == "__main__":
    sys.exit(main())
