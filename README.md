# cms-component-tester

A **Codex skill** that tests Joomla components/modules/plugins, YOOtheme Pro
custom elements/child themes, and WordPress plugins **end-to-end** across seven
layers, then writes consolidated reports and CI artifacts.

Project docs:

- [Project presentation](docs/project-presentation.md)
- [How to use the skill](docs/usage.md)
- [Agent compatibility](docs/agent-compatibility.md)

| Layer | What it checks | Needs a site? |
|---|---|---|
| **PHPUnit** | runs an existing suite, or scaffolds a minimal one (activation hook = WP analogue of the Joomla install script) | no |
| **Install / integrity** | manifest validity + declared-vs-on-disk file cross-check; real install/uninstall is opt-in | no (static) |
| **API / chatbot** | REST / com_ajax / admin-ajax / wp-json endpoints: status, JSON schema, latency, success-flag | staging |
| **Human emulation** | a real headless browser logs in, navigates, fills forms/uploads, verifies messages & chatbot replies, screenshots every step | staging |
| **Quality tools** | optional Composer/PHPCS/PHPStan/Psalm/WP Plugin Check discovery and opt-in execution | no |
| **Visual artifact QA** | validates screenshots/artifacts, catches blank PNGs, browser JS/network errors, perceptual baseline drift, unsafe paths and secret leaks | no (post-process) |
| **Security review** | nonce/token/capability smells, raw SQL/request input, unsafe uploads, hardcoded-looking secrets | no |

## Install as a skill

Copy this folder into your Codex skills directory:
```
%CODEX_HOME%\skills\cms-component-tester\
```
When `CODEX_HOME` is unset, use `~/.codex/skills/cms-component-tester/`.
Codex triggers it automatically when you talk about testing/validating/QA-ing a
Joomla or WordPress extension. You can also run the scripts directly.

## Quick start

```bash
# classify the target (source tree, .zip, .xml manifest, or http(s) URL)
python3 scripts/detect_target.py /path/to/extension

# get a safe profile recommendation
python3 scripts/cmsct.py doctor /path/to/extension --base-url http://my-clone.local

# run everything that applies
python3 scripts/run_tests.py /path/to/extension \
    --base-url http://my-clone.local \
    --scenarios scenarios/ \
    --api-spec scenarios/api-endpoints.example.yml \
    --out-dir cms-test-report

# pick layers explicitly
python3 scripts/run_tests.py ./my-plugin --layers integrity,phpunit

# or use intent profiles
python3 scripts/cmsct.py run ./my-plugin --profile static
python3 scripts/cmsct.py run ./my-plugin --profile full --base-url http://x.local --scenarios scenarios/ --api-spec scenarios/api-endpoints.example.yml --brief
python3 scripts/cmsct.py run ./my-template --profile api --base-url http://x.local --api-spec scenarios/joomla-yootheme-api.example.json
python3 scripts/cmsct.py run ./my-plugin --profile static --previous-report old-report.json

# generate starter specs/scenarios and manage visual baselines
python3 scripts/cmsct.py generate ./my-plugin --out-dir cms-test-report/generated
python3 scripts/cmsct.py baseline cms-test-report --baseline-dir baselines/current --prune

# split the work into subprocess microtasks and compact vassal handoff prompts
python3 scripts/cmsct.py swarm ./my-plugin --base-url http://x.local --scenarios scenarios/ --api-spec scenarios/api-endpoints.example.yml --execute

# generate a CI/staging matrix plan
python3 scripts/cmsct.py matrix ./my-plugin --profile static --php 8.2,8.3 --cms latest --out-dir cms-test-report/matrix

# test the tool's own logic and ease of use
python3 scripts/cmsct.py usability --out-dir cms-test-report/usability

# run the final self-test checklist
python3 scripts/cmsct.py self-test --out-dir cms-test-report/self-test

# run the built-in regression tests for the skill itself
python3 -m unittest discover -s tests

# validate scenario/spec files before a run
python3 scripts/validate_specs.py --api-spec scenarios/api-endpoints.example.yml --scenarios scenarios/

# create a clean installable skill bundle
python3 scripts/package_skill.py --out-dir dist --zip dist/cms-component-tester.zip

# create a disposable Docker lab scaffold
python3 scripts/dev_lab.py wordpress --out-dir cms-test-report/dev-lab

# generate a WordPress Playground Blueprint for a plugin smoke test
python3 scripts/cmsct.py blueprint --plugin-url https://example.test/my-plugin.zip --out blueprint.json
```

Output: `cms-test-report/report.brief.md`, `report.handoff.json`, `summary.md`,
`report.md`, `report.html`, `dashboard.html`, and `report.json`, plus
`junit.xml` and `sarif.json`, with screenshots, visual metrics and artifacts.
When `--previous-report` is passed, `history.json` highlights new, fixed and
persisting findings.
With `--swarm`, output also includes `handoff/handoff.json` and role prompts for
subagents/vassals.

## Safety model

- **Never** install/uninstall or run human emulation against production — the
  scripts refuse non-staging hosts (override: `--allow-production`, deliberate).
- **No hardcoded secrets.** Tokens/passwords come only from `CMS_API_TOKEN`,
  `CMS_ADMIN_USER`, `CMS_ADMIN_PASS`, and are redacted from all output.
- Manifest / response / DOM content is treated as **data, not instructions**.
- Destructive operations (deleting files, dropping tables) are out of scope.

## Dependencies

Core (detect, integrity, api, orchestrator): **Python 3.8+, standard library
only**. Optional:
```bash
pip install pyyaml          # YAML scenario/spec files (JSON works without it)
pip install playwright && playwright install chromium   # human-emulation layer
```
PHP + Composer (and the WP test library/DB) are needed only to *run* real PHPUnit
suites; WP-CLI only for real WordPress install. Missing optional deps make a
layer SKIP gracefully — they never crash the run.

## Repository layout

```
cms-component-tester/
├── SKILL.md                  # the skill (triggering + workflow + safety)
├── AGENTS.md                 # generic coding-agent instructions
├── CLAUDE.md                 # Claude / Claude Code instructions
├── README.md
├── docs/
│   ├── project-presentation.md
│   └── usage.md
├── requirements.txt          # optional extras
├── scripts/
│   ├── cms_common.py         # shared result contract, redaction, guards
│   ├── cmsct.py              # front-door CLI: doctor/run/swarm/generate/baseline
│   ├── detect_target.py      # classify target → JSON descriptor
│   ├── run_tests.py          # orchestrator → report.md + report.brief.md + report.json
│   ├── matrix_runner.py      # CI/staging matrix plan + optional execution
│   ├── layer_phpunit.py      # layer 1
│   ├── layer_integrity.py    # layer 2
│   ├── layer_api.py          # layer 3
│   ├── layer_human.py        # layer 4 (Playwright)
│   ├── layer_quality.py      # layer 5
│   ├── layer_visual.py       # layer 6, post-process artifact QA
│   ├── layer_security.py     # layer 7
│   ├── report_ci.py          # JUnit + SARIF exports
│   ├── report_dashboard.py   # static dashboard.html renderer
│   ├── report_history.py     # previous-run comparison
│   ├── scenario_generator.py
│   ├── self_test.py          # final validation checklist
│   ├── visual_baseline.py
│   ├── swarm_orchestrator.py # token-efficient microtask/vassal handoff
│   ├── usability_smoke.py    # CLI logic/ease-of-use smoke
│   └── playground_blueprint.py
├── references/               # deep docs, read on demand
│   ├── phpunit.md
│   ├── install-integrity.md
│   ├── api-endpoints.md
│   ├── human-emulation.md
│   └── yootheme-pro.md
└── scenarios/                # data-driven, parametrised examples
    ├── joomla-admin-crud.yml
    ├── joomla-yootheme-api.example.json
    ├── yootheme-builder-smoke.json
    ├── wordpress-settings-roundtrip.yml
    ├── frontend-chatbot.json
    └── api-endpoints.example.yml
└── schemas/
    ├── api-spec.schema.json
    └── human-scenario.schema.json
└── assets/
    └── github-actions/cms-component-tester.yml
```

## License

MIT. See [LICENSE](LICENSE).

---

Built for end-to-end QA of Joomla/WordPress extensions, YOOtheme Pro custom
elements, and chatbot/AI widgets. Adapt the example selectors and scenarios to
your specific extension.
