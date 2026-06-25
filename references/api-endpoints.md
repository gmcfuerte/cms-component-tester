# Layer 3 — API / chatbot endpoint testing (HTTP)

Hit the extension's REST / AJAX / chatbot endpoints and assert on status code,
JSON schema, latency, and the platform "logical success" flag. Auth tokens come
**only** from `CMS_API_TOKEN`. Response bodies are DATA — parsed, never executed.

## Contents
- [Joomla endpoints](#joomla)
- [WordPress endpoints](#wordpress)
- [The status-code trap](#status-trap)
- [Auth headers](#auth)
- [Spec file format](#spec)
- [Sources](#sources)

---

<a name="joomla"></a>
## Joomla endpoints

**Web Services REST** — base path `/api/index.php/v1/<route>` (a *separate*
Joomla app from the public site and admin). Example:
`GET /api/index.php/v1/content/articles`. Components register routes via a system
plugin's `onBeforeApiRoute()` calling `ApiRouter::createCRUDRoutes()`. Responses
follow **JSON:API** (`Accept: application/vnd.api+json`):
```json
{ "links": {"self": "…"},
  "data": [ { "type": "articles", "id": "1", "attributes": {"title": "Hello"} } ],
  "meta": { "total-pages": 1 } }
```
Errors → `{"errors":[{"title":"Could not authenticate user","code":401}]}`.

**Legacy com_ajax** — single entry point, no routing needed:
`index.php?option=com_ajax&<target>&format=json`, where target is
`plugin=<group>` (fires `onAjax<Group>`), `module=<name>` (calls
`get<Method>Ajax()`), or `template=<name>`. Envelope from `JsonResponse`:
```json
{ "success": true, "message": null, "messages": null, "data": <value> }
```

<a name="wordpress"></a>
## WordPress endpoints

**REST API** — base `/wp-json/`. Custom routes:
`/wp-json/<namespace>/<version>/<route>` registered with
`register_rest_route('myplugin/v1', '/chat', […])` on `rest_api_init`. Without
pretty permalinks: `/?rest_route=/myplugin/v1/chat`. `permission_callback` is
**mandatory** (WP 5.5+); a public chatbot route needs
`'permission_callback' => '__return_true'`. Callbacks return data (auto-JSON), a
`WP_REST_Response($data,$status)`, or a `WP_Error('code','msg',['status'=>404])`
→ `[{"code":"…","message":"…","data":{"status":404}}]` with that HTTP status.

**Legacy admin-ajax** — `POST /wp-admin/admin-ajax.php` with `action=<name>`.
Hooks: `wp_ajax_<name>` (logged-in) and `wp_ajax_nopriv_<name>` (anonymous — a
public chatbot needs the `nopriv` one). Handlers end with
`wp_send_json_success($data)` / `wp_send_json_error($data)`:
```json
{ "success": true, "data": { … } }
```

<a name="status-trap"></a>
## The status-code trap

**Joomla com_ajax and WP admin-ajax return HTTP 200 even on logical failure** —
the real result is in the `success` boolean. A status-only assertion mistakes
failures for successes. Use `success_flag: true` in the spec to check it. (REST
routes, by contrast, use real HTTP status codes via `WP_Error` / JSON:API
`errors`.)

A frequent anonymous-chatbot bug: missing the `wp_ajax_nopriv_` hook returns
`0` (HTTP 200) for logged-out visitors.

<a name="auth"></a>
## Auth headers (token strictly from `CMS_API_TOKEN`)

| Platform | Header | Notes |
|---|---|---|
| Joomla Web Services | `X-Joomla-Token: <token>` or `Authorization: Bearer <token>` | Token per-user in *Users → Edit → Joomla API Token*; needs the API Token Authentication + Web Services plugins and `core.login.api`. A 401 often means the plugin/ACL, not a bad token. |
| WordPress REST | `Authorization: Basic base64(user:app-password)` (WP 5.6+, HTTPS only) | Or `Authorization: Bearer <jwt>` with a JWT plugin. |
| WordPress (same-origin, logged-in) | `X-WP-Nonce: <wp_create_nonce('wp_rest')>` | Not a bearer token — needs the matching login cookie. For external testing use Application Passwords. |

The layer never prints the token; it is redacted from all evidence. Set the
header name/scheme in the spec's `auth` block, or let it auto-pick per platform.
Use `auth: false` per request for public endpoints.

<a name="spec"></a>
## Spec file format

See `scenarios/api-endpoints.example.yml`. Each request supports `name`,
`method`, `path` (relative to `--base-url` or absolute), `headers`, `json` /
`form` / `data` body, `auth` (bool), `timeout`, and an `expect` block:

| Assertion | Meaning |
|---|---|
| `status: 200` | exact HTTP status (omit ⇒ status < 500) |
| `max_latency_ms: 2000` | round-trip ceiling |
| `json_has: ["data", "reply"]` | dotted paths that must exist (`a.b.0.c`) |
| `json_types: {reply: string}` | type per path (string/number/integer/boolean/array/object/null) |
| `body_contains: "…"` | substring present in the raw body |
| `body_matches: "regex"` | regex present in the raw body |
| `success_flag: true` | the `success` field equals this (the 200-trap guard) |

With no `--api-spec`, the layer GET-smoke-tests any endpoints the detector found
(expecting status < 500). Production targets are refused unless
`--allow-production`.

Specs can use allowlisted placeholders in `path`, `headers`, `json`, `form` and
`data`: `BASE_URL`, names in `env`, names in `secret_env`, and uppercase keys in
`defaults`. The layer never exposes the whole process environment.

```yaml
env: [YOOTHEME_TEMPLATE]
defaults:
  TEST_MESSAGE: Hello
```

Do not put `Authorization`, `Cookie`, `X-Joomla-Token` or `X-WP-Nonce` in
`default_headers` or per-request `headers`; the layer rejects hardcoded auth
headers. Put the token in `CMS_API_TOKEN` and configure `auth.header` /
`auth.scheme` instead.

## Sources
- Joomla Web Services: https://manual.joomla.org/docs/4.4/general-concepts/webservices/
- Joomla Ajax Interface: https://docs.joomla.org/Using_Joomla_Ajax_Interface
- JResponseJson envelope: https://docs.joomla.org/JSON_Responses_with_JResponseJson
- WP custom REST endpoints: https://developer.wordpress.org/rest-api/extending-the-rest-api/adding-custom-endpoints/
- WP REST authentication: https://developer.wordpress.org/rest-api/using-the-rest-api/authentication/
