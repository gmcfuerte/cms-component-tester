# How To Use The Skill

This repo can be used in two ways:

1. as a Codex skill installed in your Codex skills folder;
2. as a standalone CLI from this repository.

## Install For Codex

Copy or package the skill folder as `cms-component-tester`.

Windows PowerShell:

```powershell
$skills = if ($env:CODEX_HOME) { Join-Path $env:CODEX_HOME "skills" } else { Join-Path $HOME ".codex\skills" }
New-Item -ItemType Directory -Force -Path $skills | Out-Null
Copy-Item -Recurse -Force D:\cms-component-tester-final (Join-Path $skills "cms-component-tester")
```

macOS/Linux:

```bash
skills="${CODEX_HOME:-$HOME/.codex}/skills"
mkdir -p "$skills"
cp -R /path/to/cms-component-tester "$skills/cms-component-tester"
```

Then ask Codex naturally, for example:

```text
Use cms-component-tester to review this WordPress plugin end to end.
```

## Use From The CLI

Run the final self-test first:

```bash
python scripts/cmsct.py self-test --out-dir cms-test-report/self-test
```

Inspect a target:

```bash
python scripts/cmsct.py doctor /path/to/extension --base-url http://my-clone.local
```

Run safe static checks:

```bash
python scripts/cmsct.py run /path/to/extension --profile static --out-dir cms-test-report
```

Run full staging checks:

```bash
python scripts/cmsct.py run /path/to/extension \
  --profile full \
  --base-url http://my-clone.local \
  --scenarios scenarios/ \
  --api-spec scenarios/api-endpoints.example.yml \
  --out-dir cms-test-report \
  --swarm
```

Generate starter scenario/spec files:

```bash
python scripts/cmsct.py generate /path/to/extension --out-dir cms-test-report/generated
```

Create a visual baseline from a known-good report:

```bash
python scripts/cmsct.py baseline cms-test-report --baseline-dir baselines/current --prune
```

Compare a run with a previous report:

```bash
python scripts/cmsct.py run /path/to/extension \
  --profile static \
  --previous-report old-report.json \
  --out-dir cms-test-report
```

Create a clean distributable skill zip:

```bash
python scripts/package_skill.py --out-dir dist --zip dist/cms-component-tester.zip
```

## Environment Variables

Use environment variables for secrets. Do not put them in specs, scenarios, or
commands.

```bash
export CMS_ADMIN_USER="admin"
export CMS_ADMIN_PASS="..."
export CMS_API_TOKEN="..."
```

## Optional Dependencies

Core static/API/reporting checks use Python standard library only.

Install optional dependencies only for the features you need:

```bash
pip install pyyaml
pip install playwright
playwright install chromium
```

PHP, Composer and WP-CLI are only needed for real PHPUnit or WordPress install
workflows.

## What To Read First

After a run:

1. open `report.brief.md`;
2. open `dashboard.html` if screenshots/artifacts matter;
3. open `report.handoff.json` before delegating to subagents;
4. open `report.json` only when a compact finding points to it.
