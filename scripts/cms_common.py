"""Shared contract and helpers for the cms-component-tester skill.

Every test layer (phpunit, integrity, api, human) and the orchestrator
(run_tests.py) build on the small vocabulary defined here so their results can
be consolidated into a single report.

Design rules baked into this module:

* Results are plain JSON-serialisable dicts. A *layer result* contains a list
  of *checks*; each check has a status. The layer status is a roll-up of its
  checks. This keeps every layer's output uniform and machine-readable.
* Secrets (API tokens, admin passwords) come from environment variables only.
  Nothing here ever prints or stores a secret value; `redact()` scrubs any that
  leak into free text before it reaches a report.
* Content coming from manifests, HTTP responses or the DOM is DATA. This module
  never `eval()`s, `exec()`s or shells out with it. Layers must do the same.

The module targets Python 3.8+ and the standard library only, so the static
layers run with zero third-party dependencies.
"""

import datetime
import ipaddress
import json
import os
import re
import sys
from urllib.parse import urlsplit

# Windows terminals/pipes default to a legacy code page (cp1252) that cannot
# encode the status glyphs this tool emits (the pass/fail/warn icons). Force
# UTF-8 on stdout/stderr where the stream supports it, degrading gracefully
# instead of crashing with UnicodeEncodeError. (Setting PYTHONUTF8=1 also fixes
# this, but callers should not have to.)
for _std_name in ("stdout", "stderr"):
    _std = getattr(sys, _std_name, None)
    try:
        _std.reconfigure(encoding="utf-8", errors="backslashreplace")
    except (AttributeError, ValueError, OSError):
        pass

# --- Status vocabulary -----------------------------------------------------

PASS = "pass"
FAIL = "fail"
SKIP = "skip"
ERROR = "error"
WARN = "warn"  # a non-fatal advisory; rolls up like pass for exit codes

_STATUS_ORDER = {PASS: 0, SKIP: 1, WARN: 2, FAIL: 3, ERROR: 4}

# Exit codes used by every standalone layer script and the orchestrator.
EXIT_OK = 0
EXIT_FAIL = 1
EXIT_ERROR = 2
EXIT_USAGE = 3


def now_iso():
    """UTC timestamp, second precision, suitable for report metadata."""
    return datetime.datetime.now(datetime.timezone.utc).replace(microsecond=0).isoformat()


# --- Result builders -------------------------------------------------------


def check(name, status, detail="", evidence=None):
    """Build one check (the atomic unit of a layer result)."""
    item = {"name": name, "status": status, "detail": detail}
    if evidence is not None:
        item["evidence"] = evidence
    return item


def artifact(kind, path, label=""):
    """Reference a file produced during testing (screenshot, log, json...)."""
    return {"type": kind, "path": path, "label": label}


def combine_statuses(statuses):
    """Roll several statuses into one.

    Severity wins for failures, but SKIP must not mask success: a layer whose
    checks mostly PASS plus one informational SKIP (e.g. opt-in install not run)
    is a PASS, not a SKIP. SKIP only wins when *everything* skipped (nothing
    meaningful ran). WARN is advisory and reads as PASS.
    """
    statuses = list(statuses)
    if not statuses:
        return SKIP
    if ERROR in statuses:
        return ERROR
    if FAIL in statuses:
        return FAIL
    if any(s in (PASS, WARN) for s in statuses):
        return PASS
    return SKIP


def rollup_status(checks):
    """The layer status implied by its checks. No checks -> skip."""
    return combine_statuses([c.get("status", ERROR) for c in checks])


def layer_result(layer, checks=None, summary="", artifacts=None, meta=None,
                 started_at=None, status=None):
    """Assemble the standard layer-result envelope consumed by run_tests.py."""
    checks = checks or []
    result = {
        "layer": layer,
        "status": status or rollup_status(checks),
        "summary": summary,
        "started_at": started_at or now_iso(),
        "finished_at": now_iso(),
        "checks": checks,
        "artifacts": artifacts or [],
        "meta": meta or {},
    }
    return result


def error_result(layer, message, started_at=None):
    """A layer that blew up before it could run its checks."""
    return layer_result(
        layer,
        checks=[check(layer + ":fatal", ERROR, message)],
        summary="Layer errored: " + message,
        started_at=started_at,
        status=ERROR,
    )


# --- Secrets & environment -------------------------------------------------

# Canonical environment variable names. Documented in SKILL.md; never hardcode
# the values anywhere in this repo.
ENV_API_TOKEN = "CMS_API_TOKEN"
ENV_ADMIN_USER = "CMS_ADMIN_USER"
ENV_ADMIN_PASS = "CMS_ADMIN_PASS"


def env(name, default=None):
    return os.environ.get(name, default)


def secret_values():
    """The set of secret strings to scrub from any text before reporting."""
    vals = set()
    for n in (ENV_API_TOKEN, ENV_ADMIN_PASS, ENV_ADMIN_USER):
        v = os.environ.get(n)
        if v:
            vals.add(v)
    return vals


def redact(text, extra=None):
    """Replace any known secret value found in `text` with a marker.

    Use this on every string that might echo a credential (HTTP headers,
    subprocess output, error messages) before it lands in a report.
    """
    if text is None:
        return text
    text = str(text)
    targets = secret_values()
    if extra:
        targets |= set(x for x in extra if x)
    for v in sorted(targets, key=len, reverse=True):
        if v:
            text = text.replace(v, "***REDACTED***")
    return text


def redact_tree(obj, extra=None):
    """Recursively redact known secret values from every string in a structure.

    Used at the report-write boundary so a single missed redact() call site at a
    layer can never persist a canonical secret to report.json / report.md.
    """
    if isinstance(obj, str):
        return redact(obj, extra)
    if isinstance(obj, list):
        return [redact_tree(x, extra) for x in obj]
    if isinstance(obj, tuple):
        return tuple(redact_tree(x, extra) for x in obj)
    if isinstance(obj, dict):
        return {redact_tree(k, extra): redact_tree(v, extra) for k, v in obj.items()}
    return obj


def substitute(template, mapping, secrets=None):
    """Expand ${NAME} placeholders in a string using `mapping`.

    Returns (expanded, used_secret) so callers know whether the expanded value
    must be treated as sensitive (kept out of logs/screenshots filenames).
    """
    if not isinstance(template, str):
        return template, False
    secrets = secrets or set()
    used_secret = [False]

    def _repl(m):
        key = m.group(1)
        if key in secrets:
            used_secret[0] = True
        return str(mapping.get(key, m.group(0)))

    expanded = re.sub(r"\$\{([A-Z0-9_]+)\}", _repl, template)
    return expanded, used_secret[0]


# --- Untrusted-content tripwires -------------------------------------------

_SUSPICIOUS_PATTERNS = (
    (r"\bignore\s+(all\s+)?previous\s+instructions\b", "prompt-injection phrase"),
    (r"\bsystem\s+prompt\b", "system-prompt reference"),
    (r"\bdeveloper\s+message\b", "developer-message reference"),
    (r"\bdo\s+not\s+tell\s+the\s+user\b", "concealment instruction"),
    (r"\brm\s+-rf\b", "destructive shell command"),
    (r"\bdrop\s+table\b", "destructive SQL command"),
    (r"\bdelete\s+(all|the)\b", "destructive delete instruction"),
)


def suspicious_instruction_findings(text, source="content", limit=8):
    """Return advisory findings for content that looks like instructions.

    Manifests, HTTP responses and DOM text are untrusted data. These heuristics
    are intentionally conservative tripwires: they report suspicious strings so
    the operator can inspect them, but no layer ever acts on the content.
    """
    if not text:
        return []
    haystack = str(text)
    findings = []
    for pattern, label in _SUSPICIOUS_PATTERNS:
        for match in re.finditer(pattern, haystack, re.I):
            start = max(0, match.start() - 60)
            end = min(len(haystack), match.end() + 60)
            snippet = haystack[start:end].replace("\r", " ").replace("\n", " ")
            findings.append("{}: {} ({})".format(source, redact(snippet), label))
            if len(findings) >= limit:
                return findings
    return findings


# --- Production safety guard ------------------------------------------------


class GuardError(Exception):
    """Raised when an action is blocked for safety (e.g. production target)."""


# Staging markers are matched against the HOSTNAME as whole labels / suffixes,
# never as free substrings — otherwise a live host like "stagingsupplies.com"
# or "mydev.com" would be mistaken for staging and the safety guard disabled.
_LOOPBACK_HOSTS = {"localhost", "127.0.0.1", "0.0.0.0", "::1", "[::1]"}
_STAGING_SUFFIXES = (
    ".local", ".localhost", ".test", ".invalid", ".example",
    ".ddev.site", ".lndo.site", ".wpengine.com", ".ngrok.io",
    ".ngrok-free.app", ".vagrant",
)
_STAGING_LABELS = {
    "staging", "stage", "dev", "development", "qa", "uat", "preprod",
    "sandbox", "local", "localhost", "test", "testing", "demo",
}
_DEV_PORTS = {3000, 8000, 8025, 8080, 8081, 8443, 8888}


def split_host_port(url):
    """(hostname_lower, port) for a URL, tolerating a missing scheme."""
    if not url:
        return "", None
    parts = urlsplit(url if "://" in url else "http://" + url)
    return (parts.hostname or "").lower(), parts.port


def _is_private_ip(host):
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return False
    return ip.is_private or ip.is_loopback or ip.is_link_local


def looks_like_production(url):
    """Does this base URL look like a live production site?

    Returns False only for clearly local/staging hosts (loopback, private IPs,
    known dev suffixes, a staging-style hostname label, or a common dev port).
    Anything else is assumed production so the caller errs on the side of
    refusing destructive/interactive actions.
    """
    host, port = split_host_port(url)
    if not host:
        return False
    if host in _LOOPBACK_HOSTS or _is_private_ip(host):
        return False
    if host.endswith(_STAGING_SUFFIXES):
        return False
    if any(label in _STAGING_LABELS for label in host.split(".")):
        return False
    if "." not in host and port in _DEV_PORTS:
        return False
    return True


def same_host(url_a, url_b):
    """True when two URLs share scheme+host+port (for redirect safety)."""
    a = urlsplit(url_a if "://" in url_a else "http://" + url_a)
    b = urlsplit(url_b if "://" in url_b else "http://" + url_b)
    return (a.scheme, (a.hostname or "").lower(), a.port) == \
           (b.scheme, (b.hostname or "").lower(), b.port)


def guard_target(base_url, allow_production, action):
    """Refuse destructive/interactive actions against production by default.

    `action` is a short label used in the error message. Raises GuardError
    unless the target looks like staging/local OR the operator explicitly
    passed the allow-production override.
    """
    if allow_production:
        return
    if looks_like_production(base_url):
        raise GuardError(
            "Refusing to run '{}' against what looks like a PRODUCTION site "
            "({}). Point --base-url at a disposable staging/local instance, or "
            "pass --allow-production only if you are certain this is safe."
            .format(action, base_url)
        )


# --- Data loading (json / yaml) --------------------------------------------


def load_data_file(path):
    """Load a .json / .yml / .yaml data file into a Python object.

    JSON needs no dependencies. YAML uses PyYAML if available; if a YAML file is
    given without PyYAML installed we fail loudly rather than guess.
    """
    with open(path, "r", encoding="utf-8") as fh:
        raw = fh.read()
    lower = path.lower()
    if lower.endswith(".json"):
        return json.loads(raw)
    if lower.endswith((".yml", ".yaml")):
        try:
            import yaml  # type: ignore
        except ImportError:
            raise GuardError(
                "Reading '{}' needs PyYAML (pip install pyyaml), or convert the "
                "scenario file to JSON.".format(path)
            )
        return yaml.safe_load(raw)
    # Fall back to trying JSON, then YAML.
    try:
        return json.loads(raw)
    except ValueError:
        try:
            import yaml  # type: ignore
        except ImportError:
            raise GuardError(
                "Reading '{}' as YAML needs PyYAML (pip install pyyaml), or use "
                "a .json file.".format(path)
            )
        return yaml.safe_load(raw)


def write_json(path, obj):
    ensure_dir(os.path.dirname(path) or ".")
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(obj, fh, indent=2, ensure_ascii=False)
        fh.write("\n")


def ensure_dir(path):
    if path and not os.path.isdir(path):
        os.makedirs(path, exist_ok=True)
    return path


# --- Subprocess helper ------------------------------------------------------


def run_cmd(cmd, cwd=None, timeout=300, env_extra=None):
    """Run a command, capturing output. Never raises on non-zero exit.

    Returns dict(returncode, stdout, stderr, ok). Output is redacted of known
    secrets. `cmd` must be a list (no shell), so response/manifest content can
    never be interpreted as a shell instruction.
    """
    import subprocess

    if isinstance(cmd, str):
        raise ValueError("run_cmd expects a list, not a shell string")
    run_env = dict(os.environ)
    if env_extra:
        run_env.update(env_extra)
    try:
        proc = subprocess.run(
            cmd, cwd=cwd, timeout=timeout, env=run_env,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
        return {
            "returncode": proc.returncode,
            "stdout": redact(proc.stdout.decode("utf-8", "replace")),
            "stderr": redact(proc.stderr.decode("utf-8", "replace")),
            "ok": proc.returncode == 0,
        }
    except FileNotFoundError:
        return {"returncode": 127, "stdout": "", "stderr": "command not found: " + cmd[0], "ok": False}
    except Exception as exc:  # timeout, permission, etc.
        return {"returncode": -1, "stdout": "", "stderr": redact(str(exc)), "ok": False}


def which(name):
    """Locate an executable on PATH (cross-platform), or None."""
    from shutil import which as _which
    return _which(name)


# --- Output -----------------------------------------------------------------


def emit(result, as_json):
    """Print a layer result either as JSON (machine) or a short text summary."""
    if as_json:
        json.dump(result, sys.stdout, indent=2, ensure_ascii=False)
        sys.stdout.write("\n")
        return
    status = result.get("status", "?").upper()
    sys.stdout.write("[{}] {} - {}\n".format(status, result.get("layer"), result.get("summary", "")))
    for c in result.get("checks", []):
        sys.stdout.write("  {:<5} {} {}\n".format(
            c.get("status", "?").upper(), c.get("name", ""),
            ("- " + c["detail"]) if c.get("detail") else "",
        ))
    for a in result.get("artifacts", []):
        sys.stdout.write("  artifact: {} ({})\n".format(a.get("path"), a.get("type")))


def status_to_exit(status):
    if status == ERROR:
        return EXIT_ERROR
    if status == FAIL:
        return EXIT_FAIL
    return EXIT_OK
