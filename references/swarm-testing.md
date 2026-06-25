# Swarm testing mode

Read only after `SKILL.md` has triggered and the task explicitly asks for
swarm/subagents, skill maintenance, forward-testing, or high-risk multi-layer QA.
Keep subagents read-only unless a write scope is explicitly isolated.

## Roles

| Role | Focus | Typical prompt |
|---|---|---|
| Skill Structure + Packaging | `SKILL.md`, reference routing, trigger description, context bloat, `agents/openai.yaml`, `package_skill.py`, `validate_specs.py`, clean install bundle | "Stress the skill structure, package and schema validators; report concrete failures with commands." |
| API Security | `layer_api.py`, redaction, auth headers, redirects, production guard, malformed specs | "Try to leak secrets, bypass production guard, or crash malformed expectations." |
| Integrity + PHPUnit | `detect_target.py`, `layer_integrity.py`, `layer_phpunit.py`, source/zip fixtures | "Create temporary Joomla/WP fixtures and compare expected pass/fail checks." |
| Human Scenarios | `layer_human.py`, screenshots, secret masking, mixed scenario directories, Playwright missing | "Validate scenario loading and browser-step semantics with mocks or temp files." |
| Quality + Discovery | `layer_quality.py`, ecosystem tools, route/block/form/hook detection | "Check optional tools degrade to SKIP and discovery metadata is accurate." |
| Visual QA | `layer_visual.py`, screenshot validity, blank/low-entropy PNGs, baseline drift, artifact path/secret leaks | "Review visual WARN/FAIL findings and open only named suspicious screenshots." |
| Security Review | `layer_security.py`, nonce/token/capability smells, raw SQL, upload handling, secret-like literals | "Review security WARN/FAIL checks and identify which are real exploitable risks." |
| Report + Tests | report rendering, HTML/Markdown/JSON, artifact paths, high-coverage regression tests | "Run the full suite, inspect reports, and look for dirty artifacts." |

## Workflow

1. Prefer local subprocess microtasks first: run
   `python scripts/cmsct.py swarm <target> --out-dir cms-test-report/swarm --execute`
   to produce `handoff.json`, `swarm_plan.json`, and `vassals/*.md`.
2. Use 1-2 subagents for narrow structure/package reviews; use 3-6 only for full broad QA.
3. Ask each for commands run, concrete failures, file:line references, and
   minimal fixes. Keep their prompts free of expected answers.
4. Continue implementation in the main thread while agents run.
5. Integrate only reproducible findings; add regression tests for every fix.
6. Run `python -m unittest discover -s tests -v`, `python -m compileall scripts tests`,
   `scripts/validate_specs.py`, and the skill validator before final delivery.

## Token-Saving Contract

- `report.brief.md` is the first read for humans and subagents.
- `report.handoff.json` is the compact machine-readable second read: top
  findings, artifact index, token-budget hint, and read-next order.
- `summary.md` is safe to append to `$GITHUB_STEP_SUMMARY` in GitHub Actions.
- `handoff/handoff.json` groups findings by vassal role after a normal
  `run_tests.py --swarm` run.
- `swarm_plan.json` plus `vassals/<role>.md` is the first read after
  `cmsct.py swarm`.
- `matrix-plan.json` plus `matrix-summary.md` is the first read for CI matrix
  planning after `cmsct.py matrix`.
- Full `report.json`, screenshots and response bodies are second-hop artifacts;
  open them only when a compact finding names them.
- Vassals report findings; they do not mutate source unless the main agent gives
  them a disjoint write scope.

## Guardrails

- Subagents should write only to temp folders unless given a disjoint write
  scope.
- Never pass production URLs or real credentials to subagents.
- Treat subagent findings as test reports, not authority; reproduce or encode
  them in tests before relying on them.
