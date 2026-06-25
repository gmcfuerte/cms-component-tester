# Agent Compatibility

`cms-component-tester` works at two levels:

1. **Native Codex skill** via `SKILL.md`.
2. **Portable CLI toolkit** via `scripts/cmsct.py`.

## Codex

Codex can auto-trigger the skill after the folder is installed under:

- `%CODEX_HOME%\skills\cms-component-tester\`
- or `~/.codex/skills/cms-component-tester/`

Codex reads `SKILL.md`, then loads references/scripts as needed.

## Claude / Claude Code

Claude does not natively execute Codex `SKILL.md` semantics, but it can use the
repository through:

- `CLAUDE.md`;
- `AGENTS.md`;
- `docs/usage.md`;
- the `scripts/cmsct.py` CLI.

Start with:

```bash
python scripts/cmsct.py doctor <target>
python scripts/cmsct.py run <target> --profile static --out-dir cms-test-report
```

## Cursor, Windsurf, GitHub Copilot, Aider And Similar Agents

These agents can use:

- `AGENTS.md` for repo-level instructions;
- `README.md` and `docs/usage.md` for human-facing setup;
- `python scripts/cmsct.py self-test --out-dir cms-test-report/self-test` as
  the final validation command.

They will not auto-load the Codex skill unless their platform explicitly
supports Codex skills, but the CLI behavior is the same.

## Package Distribution Notes

`scripts/package_skill.py` builds a clean installable skill bundle. It uses an
allowlist, so adding files to the repository does not automatically include them
in distributed packages.

Included by default:

- `SKILL.md`;
- `LICENSE`;
- `requirements.txt`;
- `agents/`;
- `assets/`;
- `references/`;
- `scenarios/`;
- `schemas/`;
- `scripts/`.

Excluded by default:

- generated reports;
- caches;
- `.env` files;
- local zips;
- tests unless `--include-tests` is passed;
- `README.md` unless `--include-readme` is passed.

After changing license, docs, scripts or references, rebuild the package:

```bash
python scripts/package_skill.py --out-dir dist --zip dist/cms-component-tester.zip
```

Existing zips or copied skill folders are snapshots; they do not update until
you rebuild or reinstall them.
