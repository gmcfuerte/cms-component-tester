# Layer 1 — PHPUnit (unit / functional)

How to run and scaffold PHPUnit tests for Joomla 4/5 extensions and WordPress
plugins. The `scripts/layer_phpunit.py` layer automates the common path; this
file is the deeper reference to consult when wiring the full harness.

## Contents
- [Joomla 4/5](#joomla-45)
- [WordPress](#wordpress)
- [Activation hook = the WP analogue of the Joomla install script](#activation-hook)
- [What the layer does](#what-the-layer-does)
- [Sources](#sources)

---

## Joomla 4/5

**Important correction.** The standalone `joomla/test-unit` Composer package is
**archived (read-only since 2019)** and is *not* the modern path, despite older
guides. In Joomla 4/5 the canonical unit-test infrastructure lives **inside the
joomla-cms repo** under `tests/Unit`, driven by the root `phpunit.xml.dist`.

Key facts:
- Joomla 4 and 5 pin `phpunit/phpunit: ^9.6` and require `php: ^8.1`. **Do not
  author PHPUnit 10-only syntax** — it breaks against the pinned ^9.6.
- Root `phpunit.xml.dist` sets `bootstrap="tests/Unit/bootstrap.php"` and
  defines two suites: `Unit` (mocks everything, no DB) and `Integration` (real
  DB via `JTEST_DB_*` constants in the `<php>` block).
- Base classes: `Joomla\Tests\Unit\UnitTestCase` (extends
  `PHPUnit\Framework\TestCase`) for unit tests;
  `Joomla\Tests\Integration\IntegrationTestCase` for integration tests.
- `tests/Unit/bootstrap.php` defines `_JEXEC` and the `JPATH_*` constants
  (`JPATH_BASE = getcwd()`, `JPATH_ROOT`, `JPATH_LIBRARIES`,
  `JPATH_ADMINISTRATOR`, …) because extensions reference these globals but unit
  tests do not run inside a full CMS.

Minimal config (what the layer scaffolds):
```xml
<phpunit bootstrap="tests/Unit/bootstrap.php" colors="true" failOnWarning="true">
  <testsuites>
    <testsuite name="Unit">
      <directory suffix="Test.php">./tests/Unit</directory>
    </testsuite>
  </testsuites>
</phpunit>
```
A com_* unit test:
```php
namespace Joomla\Component\Foo\Tests\Unit;
use Joomla\Tests\Unit\UnitTestCase;
class MyServiceTest extends UnitTestCase {
    public function testItAddsNumbers(): void { $this->assertSame(3, 1 + 2); }
}
```
Run: `vendor/bin/phpunit --testsuite Unit` (from the Joomla root, or override the
`JPATH_*` consts in your own `phpunit.xml`).

Pitfalls:
- Undefined `_JEXEC` / `JPATH_*` ⇒ the bootstrap isn't wired. `JPATH_BASE`
  defaults to `getcwd()`, so run phpunit from the Joomla root or set the consts.
- Integration tests need a real MySQL DB matching `JTEST_DB_*`; the dist file
  targets a docker host `mysql` / db `test_joomla`. Leaving defaults outside
  that setup makes integration tests error on connect.

## WordPress

Two coexisting setups in 2025/2026, both loading the same WP test library and
`WP_UnitTestCase`:
1. **Classic WP-CLI flow** — `wp scaffold plugin-tests <plugin> --ci=github`
   generates `bin/install-wp-tests.sh`, `phpunit.xml.dist`, `tests/bootstrap.php`,
   `tests/test-sample.php`. Install the test lib with:
   `bash bin/install-wp-tests.sh <db-name> <db-user> <db-pass> [db-host] [wp-version]`
   (needs the `mysql` **and** `mysqladmin` CLI clients).
2. **Composer flow** — `composer require --dev yoast/phpunit-polyfills
   wp-phpunit/wp-phpunit phpunit/phpunit`, using `WP_PHPUNIT__DIR`.

Mandatory dependency: since WP 5.8.2 the test bootstrap **fatal-exits without
`yoast/phpunit-polyfills`** (^1.0 for PHPUnit 9; ^2.0/^3.0 for 9/10/11). Most
plugins still pin PHPUnit ^9.6.

The scaffolded `tests/bootstrap.php` reads `WP_TESTS_DIR`, requires the test
library's `functions.php`, registers the plugin via
`tests_add_filter('muplugins_loaded', '_manually_load_plugin')`, then requires
the library's `bootstrap.php`.

Gotcha: the default scaffolded suite only matches files with the **`test-`
prefix** (e.g. `test-foo.php`). PSR-style `tests/Unit/*Test.php` won't be picked
up until you change the `<directory prefix=… suffix=…>` rule.

`WP_UnitTestCase` is really an **integration** harness — it boots full WP + DB
and rolls back per test. For pure no-WP unit tests, extend PHPUnit's `TestCase`
directly.

<a name="activation-hook"></a>
## Activation hook = the WP analogue of the Joomla install script

`register_activation_hook()` callbacks do **not** fire just because the plugin
file is loaded by `_manually_load_plugin()`. Exercise activation explicitly:
```php
class ActivationTest extends WP_UnitTestCase {
    public function test_activation_creates_table() {
        global $wpdb;
        activate_plugin('my-plugin/my-plugin.php');   // fires the activation hook
        $t = $wpdb->prefix . 'my_plugin_data';
        $this->assertSame($t, $wpdb->get_var("SHOW TABLES LIKE '$t'"));
    }
}
```
This mirrors validating a Joomla `<scriptfile>` install lifecycle
(preflight/postflight, install/uninstall).

<a name="what-the-layer-does"></a>
## What the layer does

- **Existing suite:** if a `phpunit.xml(.dist)` is detected, `--run` executes it
  via `vendor/bin/phpunit` (or `phpunit` on PATH), logs JUnit XML, and reports
  test/failure/error/skip counts.
- **No suite:** scaffolds a minimal, runnable suite into
  `<out-dir>/phpunit-scaffold/` (Joomla: `phpunit.xml.dist` +
  `tests/Unit/bootstrap.php` + a manifest-sanity `TestCase`; WordPress:
  `phpunit.xml.dist` + `tests/bootstrap.php` + a `WP_UnitTestCase` activation
  test). `--write-scaffold` additionally copies files into the source tree
  (never overwriting existing files).
- Running real WP tests still needs `composer install`, the WP test library and
  a MySQL test DB; when those are absent the layer SKIPs the *run* but still
  emits the scaffold.

## Sources
- Joomla Programmers Manual — Unit testing setup: https://manual.joomla.org/docs/testing/automated/unit/setup/
- joomla-cms `phpunit.xml.dist`: https://github.com/joomla/joomla-cms/blob/5.3-dev/phpunit.xml.dist
- WP-CLI Handbook — Plugin Integration Tests: https://make.wordpress.org/cli/handbook/how-to/plugin-unit-tests/
- `wp scaffold plugin-tests`: https://developer.wordpress.org/cli/commands/scaffold/plugin-tests/
- Yoast/PHPUnit-Polyfills: https://github.com/Yoast/PHPUnit-Polyfills
- Using the WordPress Test Suite (Felix Arntz): https://felix-arntz.me/blog/using-the-wordpress-test-suite/
