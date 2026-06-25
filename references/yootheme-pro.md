# YOOtheme Pro Testing Notes

Use this reference when a Joomla target contains YOOtheme Pro custom elements,
child-theme files, builder modules, module positions, or dynamic-content mapping.
Keep it out of routine non-YOOtheme runs to save context.

## Source Patterns

Detect these without executing PHP:

- `builder/<element>/element.php` or `builder/elements/<element>/element.php`
- `builder/<element>/templates/template.php` and `templates/content.php`
- `builder/<element>/images/icon.svg` and `images/iconSmall.svg`
- `config.php` loading module definitions, commonly via `$app->load(...)`
- `modules/<name>/bootstrap.php`
- `less/theme.NAME.less`, `css/custom.css`, `fonts/*`
- `html/*` and `templates/*` overrides in a child theme

YOOtheme documents custom builder elements as directories containing
`element.php`, template files and icon files. Element `name` values must be
unique. Third-party elements should set a `group` so they do not disappear into
the generic Custom group.

## Static Checks

For each custom element:

- Fail when `element.php` has no static `name` property.
- Fail duplicate element names.
- Fail missing `templates/template.php`.
- Warn missing `templates/content.php`; this is the searchable content fallback
  for Joomla and matters if the site later stops rendering through YOOtheme.
- Warn when `icon.svg` or `iconSmall.svg` is missing.
- Warn when `fields` exists without a `fieldset` declaration.

For child themes:

- Warn if `modules/*/bootstrap.php` exists but no `config.php` loader is
  detected.
- Warn if a `less/theme.*.less` file lacks a header `Name:` value.
- Treat `html/*` and `templates/*` overrides as review hotspots; compare them
  against the installed YOOtheme Pro version during manual QA because upstream
  template changes can silently break overrides.

## Human Smoke Flow

Use `scenarios/yootheme-builder-smoke.json` as a starting point on disposable
Joomla instances. The flow logs into `/administrator`, opens the template styles
screen, optionally opens the YOOtheme customizer, checks for builder/customizer
UI, then verifies frontend rendering. Adapt selectors to the installed YOOtheme
version and site language.

For custom elements, add one dedicated step sequence per element:

1. Open a page/template/module built with YOOtheme.
2. Insert or locate the custom element.
3. Set each required field with a non-empty value.
4. Save layout.
5. Reload frontend with cache disabled or a cache-busting query string.
6. Assert visible output and searchable fallback text if `content.php` exists.

When the builder/customizer renders inside a preview iframe, set
`frame_selector` on the relevant steps so selectors target the iframe document
instead of the Joomla admin shell.

## API And Dynamic Content

YOOtheme Pro can map Joomla Article, Category, Tag, User and custom fields into
builder element fields. For components that provide custom fields or sources,
pair the browser scenario with `scenarios/joomla-yootheme-api.example.json` or a
custom API spec that verifies:

- the Joomla item exists and is published,
- custom fields are returned in the expected type/shape,
- AJAX/source endpoints return a logical success flag, not only HTTP 200,
- empty source data makes the element collapse gracefully instead of rendering
  broken markup.

## Links

- Builder elements: https://yootheme.com/support/yootheme-pro/joomla/developers-elements
- Child themes: https://yootheme.com/support/yootheme-pro/joomla/developers-child-themes
- Dynamic content: https://yootheme.com/support/yootheme-pro/joomla/dynamic-content
- Modules and positions: https://yootheme.com/support/yootheme-pro/joomla/widgets-and-modules
