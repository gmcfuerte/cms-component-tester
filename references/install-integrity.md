# Layer 2 — Install / uninstall + file integrity

Validate the manifest, cross-check declared files against what's on disk, and —
only on a disposable instance, only with explicit opt-in — run a real
install/uninstall. The default mode is **static** (no side effects).

## Contents
- [Joomla installation manifest](#joomla)
- [WordPress headers + readme.txt](#wordpress)
- [Cross-check rules](#cross-check)
- [Real install / uninstall (opt-in)](#real-install)
- [Sources](#sources)

---

<a name="joomla"></a>
## Joomla installation manifest

Root element `<extension type="...">` with `type ∈ {component, module, plugin,
template, library, package, file, language}`. Attributes: `type` (required),
`method` (`install` default = abort if files exist; `upgrade` = overwrite),
`client` (`site|administrator`, modules), `group` (plugins).

Metadata children: `<name>`, `<version>`, `<creationDate>`, `<author>`,
`<license>`, `<description>`, optional `<element>`, `<namespace path="src">`.

File payload:
- `<files folder="src">` containing `<filename>` and `<folder>` children. The
  `folder` attribute names the source subdirectory *inside the zip*; each child
  path is relative to it.
- Backend (component) files: `<administration><files folder="admin">…</files>
  <menu>…</menu></administration>` → `/administrator/components/com_xxx/`.
  Frontend `<files>` → `/components/com_xxx/`.
- `<media folder="media" destination="com_xxx">` → `/media/com_xxx/`.
- `<languages folder="language">` with `<language tag="en-GB">com_xxx.ini</language>`.
- `<scriptfile>script.php</scriptfile>` runs PHP install lifecycle methods.
- DB: `<install><sql><file driver="mysql" charset="utf8">sql/install.mysql.utf8.sql
  </file></sql></install>` and matching `<uninstall><sql>`.

Manifest filename conventions the installer uses to *find* the manifest:
`com_xxx.xml`/`xxx.xml` (component), `mod_xxx.xml` (module), `xxx.xml` (plugin,
needs `group`), `templateDetails.xml` (template), `pkg_xxx.xml` (package).

<a name="wordpress"></a>
## WordPress headers + readme.txt

A plugin is detected by a header comment block in the main PHP file. **Only
`Plugin Name` is required.** Recognised fields: `Plugin Name`, `Plugin URI`,
`Description`, `Version`, `Requires at least`, `Requires PHP`, `Author`,
`Author URI`, `License`, `License URI`, `Text Domain`, `Domain Path`, `Network`,
`Update URI`, `Requires Plugins`.

Rules:
- `Text Domain` **must equal the plugin slug** (folder name / single-file
  basename).
- `Requires PHP` must be **digits only** (`7.4`, not `PHP 7.4`).
- `readme.txt` header lives between `=== Plugin Name ===` and the short
  description: `Contributors`, `Tags`, `Requires at least`, `Tested up to`,
  **`Stable tag`**, `Requires PHP`, `License`.

**The Stable tag rule.** WP.org serves the version named by `Stable tag` in
`/trunk/readme.txt` (e.g. `Stable tag: 1.2.3` → `/tags/1.2.3/`). The version
*shown to users* comes from the main PHP file's `Version:` header. **Best
practice: `Stable tag` == main-file `Version`.** A mismatch ships whatever code
is under the tag named by `Stable tag` — possibly stale or a different version.
`Stable tag: trunk` serves `/trunk` directly (discouraged).

Activation/uninstall:
- `register_activation_hook(__FILE__, 'cb')` / `register_deactivation_hook(...)`
  at the top level of the main file (`__FILE__` of the header file).
- Uninstall: `uninstall.php` in the plugin root runs on delete and **must**
  guard with `if ( ! defined('WP_UNINSTALL_PLUGIN') ) { die; }`. If
  `uninstall.php` exists it **bypasses** any `register_uninstall_hook` callback.
  `WP_UNINSTALL_PLUGIN` is *not* defined inside a `register_uninstall_hook`
  callback — a common bug.

<a name="cross-check"></a>
## Cross-check rules (what the layer flags)

- **Missing:** a `<filename>`/`<folder>` declared in the manifest but absent on
  disk / in the zip → the installer simply won't copy it; the feature silently
  breaks. Reported as **FAIL**.
- **Relocated:** declared file found by basename but at a different path than
  declared (source layout ≠ package layout). Reported as **WARN**.
- **Orphan:** code files present but *not* declared. Joomla never tracks them,
  so they're left behind on uninstall; for WP they may be dead code. Reported as
  **WARN** (often intentional includes — review, don't panic).
- **WordPress version consistency:** readme `Stable tag` vs main-file `Version`
  → **FAIL** on mismatch.
- **Joomla runtime orphan** (informational): a `#__extensions` row whose
  manifest `.xml` is gone from disk → "Package Uninstall: Missing manifest file"
  blocks UI uninstall. Caused by deleting files manually or using sub-extension
  uninstallers instead of the package uninstaller.

<a name="real-install"></a>
## Real install / uninstall (opt-in, disposable instance only)

Default is static. Enabling the runtime path requires **all** of:
- `--allow-install`,
- `--base-url` pointing at a clearly-staging/local host (the production guard
  refuses otherwise; override only with `--allow-production`),
- explicit operator confirmation.

When enabled:
- **WordPress:** uses WP-CLI on the disposable instance —
  `wp plugin install <zip> --force --activate`, then
  `wp plugin deactivate <slug>` + `wp plugin uninstall <slug>`.
- **Joomla:** the installer is GUI/Discover-driven and not automated here; use
  the **human-emulation layer** to drive *Extensions → Install* on a disposable
  instance.

Destructive operations (dropping tables, deleting arbitrary files) are **out of
scope** by design.

## Sources
- Joomla Manifest Files: https://manual.joomla.org/docs/5.4/building-extensions/install-update/installation/manifest/
- WP Header Requirements: https://developer.wordpress.org/plugins/plugin-basics/header-requirements/
- WP readme.txt / Stable tag: https://developer.wordpress.org/plugins/wordpress-org/how-your-readme-txt-works/
- WP Uninstall Methods: https://developer.wordpress.org/plugins/plugin-basics/uninstall-methods/
- WordPress Plugin Check tool: `wp plugin install plugin-check && wp plugin check <slug>`
- readme validator: https://wordpress.org/plugins/developers/readme-validator/
