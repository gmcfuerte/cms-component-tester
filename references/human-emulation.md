# Layer 4 — Human emulation (real-user simulation)

Drive Joomla and WordPress through a headless browser (Playwright) exactly as a
real user would: log in, navigate, fill and submit forms, click admin toolbar
buttons, and verify system messages and chatbot replies — screenshotting every
step. Scenarios are DATA files; credentials come **only** from the environment;
production is never a target.

## Contents
- [Setup](#setup)
- [Scenario file format](#format)
- [Verified selectors — Joomla](#joomla-selectors)
- [Verified selectors — WordPress](#wp-selectors)
- [Waiting & screenshots](#waiting)
- [Pitfalls](#pitfalls)
- [Selenium fallback](#selenium)
- [Sources](#sources)

---

<a name="setup"></a>
## Setup
```bash
pip install playwright
playwright install chromium          # or: playwright install --with-deps chromium  (CI/Linux)
export CMS_ADMIN_USER='admin'
export CMS_ADMIN_PASS='…'            # never inline; redacted from all output
```
If Playwright isn't installed, the layer does not fail — it writes the parsed
scenario plan to `<out-dir>/human/scenario-plan.json` and SKIPs.

<a name="format"></a>
## Scenario file format

A YAML or JSON file is one scenario, a list of scenarios, or `{scenarios: [...]}`.
Scenario keys: `name`, `platform`, `description`, `requires_auth`,
`continue_on_failure`, `viewport`, `storage_state` (reuse a saved login —
confined to `<out-dir>/human/state/`), `save_storage_state`, `env`,
`secret_env`, and `steps`.

Placeholders expanded from the environment — **allowlisted, not the whole
environment**, so unrelated secrets (`DB_PASSWORD`, `AWS_*`, …) can never be
interpolated into a URL, form value, check detail or screenshot. By default only
`${BASE_URL}` (from `--base-url`), `${ADMIN_USER}` (`CMS_ADMIN_USER`),
`${ADMIN_PASS}` (`CMS_ADMIN_PASS`), `${API_TOKEN}` (`CMS_API_TOKEN`) are
available. A scenario may opt extra vars in with `env: [VAR, …]` (non-secret) or
`secret_env: [VAR, …]` (masked + redacted). `ADMIN_PASS`, `API_TOKEN` and any
`secret_env` var are treated as secrets (never logged/screenshotted, redacted
from the report).

Step actions:

| Action | Params | Asserts? |
|---|---|---|
| `goto` | `url` | navigates |
| `fill` / `type` | `selector`, `value`/`text`, `secret` | sets input |
| `click` | `selector` | clicks |
| `upload` | `selector`, `path` | sets a file input from an allowed lab upload root |
| `press` | `selector`, `key` | key press |
| `select` | `selector`, `value` | dropdown |
| `check` / `uncheck` | `selector` | checkbox |
| `wait_for` | `selector`+`state`, or `state`, or `ms` | waits |
| `expect_visible` / `expect_selector` | `selector` | ✅ element visible |
| `expect_text` | `selector`(opt), `text` | ✅ text present |
| `expect_text_regex` | `selector`(opt), `pattern`/`text` | ✅ text matches regex |
| `expect_nonempty_text` | `selector`(opt), `min_length` | ✅ text is not blank |
| `expect_not_text` | `selector`(opt), `text` | ✅ text absent |
| `expect_url` | `pattern` (regex) | ✅ URL matches |
| `screenshot` | `name` | captures |

Every step also accepts `name` (screenshot label), `optional: true` (a failure
becomes a WARN and doesn't halt the scenario), `secret: true`, `timeout_ms`, and
`frame_selector`. `frame_selector` runs selector-based actions inside an iframe,
useful for YOOtheme customizer/preview panes:

```json
{"action": "expect_text", "frame_selector": "iframe#preview", "selector": "body", "text": "Hello"}
```

**A screenshot is captured after every step regardless** — that's the point of
human emulation — into `<out-dir>/human/<scenario>/`. For `fill` / `type` steps
marked `secret: true` or using a secret placeholder, the field is masked before
the screenshot; if masking fails the screenshot for that step is skipped with a
WARN rather than risking a secret leak.

Each scenario also writes `browser-events.jsonl` with redacted console messages,
failed requests, and HTTP 4xx/5xx responses.

`upload` is intended for disposable labs, especially Joomla extension installs.
The file path must resolve under `${CMSCT_UPLOAD_ROOT}`, `${CMSCT_UPLOAD_ZIP}`'s
directory, or `${OUT_DIR}`; otherwise the step errors instead of exposing local
files to a scenario.

See `scenarios/joomla-admin-crud.yml`, `scenarios/wordpress-settings-roundtrip.yml`,
and `scenarios/frontend-chatbot.json`.

<a name="joomla-selectors"></a>
## Verified selectors — Joomla 4/5 (Atum admin)

- **Login** (`#form-login`): username `#mod-login-username` (`name=username`),
  password `#mod-login-password` (`name=passwd`), submit `#btn-login-submit`.
- **CSRF token:** a hidden input with a *random per-session name* and
  `value="1"`. When driving a real browser you don't read it — submitting the
  form carries it. Only raw POSTs must scrape `input[type=hidden][value='1']`.
- **List/edit URLs:** `…/administrator/index.php?option=com_<name>&view=<plural>`;
  edit `&task=<singular>.edit&id=N`; row checkbox `input[name='cid[]']`.
- **Toolbar** (web components, id `{toolbar}-{button}`): `#toolbar-save`,
  `#toolbar-apply` (stays in edit), `#toolbar-save-new`, `#toolbar-cancel`,
  `#toolbar-delete`, `#toolbar-trash`, `#toolbar-new`. Click the inner button:
  `joomla-toolbar-button#toolbar-save button` (it appears only after the custom
  element hydrates — wait for it).
- **System messages:** `#system-message-container` containing
  `joomla-alert[type='success']` → `.alert-success`.
- **Frontend** (SEF off): `/index.php?option=com_<name>&view=<view>&id=N`.

<a name="wp-selectors"></a>
## Verified selectors — WordPress

- **Login** (`#loginform`): `#user_login` (`name=log`), `#user_pass`
  (`name=pwd`), submit `#wp-submit`. The browser handles the `wordpress_test_cookie`.
- **Plugin activate/deactivate:** nonce-protected GET links on `plugins.php`.
  Click the DOM anchor — it already carries the live `&_wpnonce`:
  `tr[data-plugin='my-plugin/my-plugin.php'] .activate a` (`.deactivate a`).
  **Never hardcode the nonce** (per-user, time-limited).
- **Settings API page:** `form[action='options.php']`; submit
  `#submit.button-primary`. After save WP redirects to `?settings-updated=true`
  and renders `#setting-error-settings_updated.notice-success` ("Settings saved.").
- **Generic nonce field:** `input#_wpnonce[name='_wpnonce']` +
  `input[name='_wp_http_referer']`.

<a name="waiting"></a>
## Waiting & screenshots
- Prefer **web assertions** (`expect_visible`, `expect_url`) over network idle.
  **`networkidle` is discouraged** by current Playwright docs — admin dashboards
  long-poll (heartbeat/autosave) and it hangs. The layer waits for the `load`
  state before each screenshot.
- Reuse login via `storage_state` / `save_storage_state` to avoid re-login (and
  brute-force lockouts) across scenarios.
- Screenshots are `full_page=True`, one per step, attached to the report.

<a name="pitfalls"></a>
## Pitfalls
- **Never production.** Saves/trashes (Joomla) and activation/option-saves (WP)
  are irreversible mid-flight. The layer hard-refuses non-staging hosts.
- **2FA / WebAuthn / SSO** add a step selector scripts won't pass; use a
  password-only test account on the clone, or Playwright's virtual authenticator.
- **Rate limiting / security plugins** (Wordfence, RSFirewall, fail2ban) lock
  out repeated logins — disable on the clone; reuse `storage_state`.
- **Joomla list actions** (delete/trash) need a row checkbox selected first.
- **Joomla edit lock:** an item left open in edit is checked out — always Close
  (`#toolbar-cancel`) to release it.
- **SEF / permalinks** change frontend URLs — disable SEF on the clone or
  navigate via rendered links.
- **Caching plugins** (WP-Optimize/W3TC) serve stale frontend pages — verify
  logged-in or with a preview/`?nocache` URL.
- **Headless hydration:** Joomla web components / Gutenberg may need a real
  viewport — run `--headed` and wait for the element to upgrade before clicking.

<a name="selenium"></a>
## Selenium fallback
If Playwright cannot be installed, the same scenarios map to Selenium 4:
`webdriver.Chrome(options=Options().add_argument('--headless=new'))`,
`driver.find_element(By.CSS_SELECTOR, sel)`, `.send_keys()`, `.click()`,
`WebDriverWait(driver, t).until(EC.visibility_of_element_located(...))`,
`driver.save_screenshot(path)`. The selector tables above are identical. Keep
the same env-only credentials and production guard.

## Sources
- Joomla mod_login template: https://github.com/joomla/joomla-cms/blob/5.3-dev/administrator/modules/mod_login/tmpl/default.php
- Joomla toolbar / system messages: https://docs.joomla.org/Creating_a_toolbar_for_your_component
- WP login form / nonces: https://developer.wordpress.org/apis/security/nonces/
- WP `settings_errors()`: https://developer.wordpress.org/reference/functions/settings_errors/
- Playwright Python (waiting, screenshots, auth reuse): https://playwright.dev/python/docs/auth
