# cms-component-tester

`cms-component-tester` is a Codex skill and CLI toolkit for reviewing Joomla,
YOOtheme Pro, and WordPress CMS extensions before they reach production.

It is built for practical release gates: static integrity checks, API smoke
tests, browser-driven human scenarios, visual regression evidence, security
heuristics, CI exports, and compact subagent handoffs.

## What It Solves

CMS extensions often fail outside normal unit tests:

- manifests declare files that are missing from the package;
- WordPress release metadata drifts from `readme.txt`;
- Joomla/WordPress AJAX endpoints return HTTP 200 while the payload is failed;
- admin UI flows save nothing, miss notices, or break inside iframes;
- screenshots are blank, stale, or contain hidden console/network errors;
- review agents waste tokens by opening full reports before compact summaries.

This project turns those problems into deterministic checks and readable
artifacts.

## Core Capabilities

| Area | Capability |
|---|---|
| Detection | Classify source trees, zips, manifests and staging URLs |
| Integrity | Joomla manifest and WordPress plugin metadata checks |
| PHPUnit | Discover, run, or scaffold starter test suites |
| API | Validate REST, `com_ajax`, `admin-ajax`, `wp-json`, chatbot endpoints |
| Human | Drive Playwright scenarios with screenshots at every step |
| Visual | Detect blank PNGs, baseline drift, browser errors, unsafe artifacts |
| Security | Flag nonce, ACL, SQL, upload and hardcoded-secret smells |
| CI | Export JUnit, SARIF, GitHub Step Summary, dashboards and matrix plans |
| Swarm | Produce low-token handoffs for specialized subagents |
| Self-test | Validate the skill itself with one command |

## Outputs

Each run can produce:

- `report.brief.md` for quick human/subagent triage;
- `report.handoff.json` for low-token delegation;
- `summary.md` for GitHub Step Summary;
- `dashboard.html` and `report.html` for visual review;
- `report.json` for machines;
- `junit.xml` and `sarif.json` for CI/code scanning;
- `history.json` for new/fixed/persisting finding comparison.

## Design Principles

- Default static checks are side-effect free.
- Production-looking URLs are blocked for install/human/API flows unless
  explicitly overridden.
- Secrets are environment-only and redacted at report boundaries.
- Manifest, HTTP and DOM content are treated as data, not instructions.
- Optional dependencies skip gracefully instead of crashing unrelated layers.
- Subagents read compact briefs first, then only named artifacts.

## Best Fit

Use this project for:

- pre-release review of WordPress plugins;
- Joomla component/module/plugin/template QA;
- YOOtheme Pro child theme/custom element smoke testing;
- CMS chatbot/widget endpoint and UI validation;
- CI quality gates before deploying to staging or production.
