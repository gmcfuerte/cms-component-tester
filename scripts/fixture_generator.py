#!/usr/bin/env python3
"""Generate disposable Joomla/WordPress extension fixtures for tests and demos."""

import argparse
import os
import sys
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import cms_common as cc  # noqa: E402


def _write(path, text):
    cc.ensure_dir(os.path.dirname(path))
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text)


def _zip_dir(src_dir, zip_path):
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for dirpath, _, filenames in os.walk(src_dir):
            for filename in filenames:
                path = os.path.join(dirpath, filename)
                zf.write(path, os.path.relpath(path, src_dir).replace("\\", "/"))


def create_wordpress_fixture(base_dir, name="sample-plugin", valid=True, make_zip=True):
    root = os.path.join(base_dir, name)
    cc.ensure_dir(root)
    version = "1.2.3"
    stable = version if valid else "1.0.0"
    text_domain = name if valid else "wrong-domain"
    requires_php = "8.1" if valid else "PHP 8.1"
    _write(os.path.join(root, name + ".php"), """<?php
/*
Plugin Name: Sample Plugin
Version: {version}
Text Domain: {text_domain}
Requires PHP: {requires_php}
Requires at least: 6.4
*/
register_activation_hook(__FILE__, 'sample_activate');
function sample_activate() {{}}
add_shortcode('sample_fixture', function() {{ return 'fixture'; }});
add_action('wp_ajax_nopriv_sample_fixture', function() {{ wp_send_json_success(['reply' => 'ok']); }});
add_action('rest_api_init', function() {{
    register_rest_route('sample/v1', '/chat', [
        'methods' => 'POST',
        'callback' => '__return_true',
        'permission_callback' => '__return_true',
    ]);
}});
""".format(version=version, text_domain=text_domain, requires_php=requires_php))
    _write(os.path.join(root, "readme.txt"), "=== Sample Plugin ===\nStable tag: {}\nRequires PHP: 8.1\n".format(stable))
    uninstall = "if ( ! defined('WP_UNINSTALL_PLUGIN') ) { die; }\n" if valid else "// missing WP_UNINSTALL_PLUGIN guard\n"
    _write(os.path.join(root, "uninstall.php"), "<?php\n" + uninstall)
    if make_zip:
        _zip_dir(root, os.path.join(base_dir, name + ".zip"))
    return root


def create_joomla_fixture(base_dir, name="com_sample", valid=True, make_zip=True):
    root = os.path.join(base_dir, name)
    cc.ensure_dir(root)
    _write(os.path.join(root, "site", "sample.php"), "<?php defined('_JEXEC') or die;\n")
    _write(os.path.join(root, "admin", "sample.php"), "<?php defined('_JEXEC') or die;\n")
    _write(os.path.join(root, "script.php"), "<?php class com_sampleInstallerScript {}\n")
    missing = "\n      <filename>site/missing.php</filename>" if not valid else ""
    _write(os.path.join(root, name + ".xml"), """<?xml version="1.0" encoding="utf-8"?>
<extension type="component" method="upgrade">
  <name>com_sample</name>
  <version>1.2.3</version>
  <element>com_sample</element>
  <scriptfile>script.php</scriptfile>
  <files>
    <filename>site/sample.php</filename>{missing}
  </files>
  <administration>
    <files>
      <filename>admin/sample.php</filename>
    </files>
  </administration>
</extension>
""".format(missing=missing))
    if make_zip:
        _zip_dir(root, os.path.join(base_dir, name + ".zip"))
    return root


def create_all(base_dir):
    cc.ensure_dir(base_dir)
    return {
        "wordpress_valid": create_wordpress_fixture(base_dir, "sample-plugin", True),
        "wordpress_invalid": create_wordpress_fixture(base_dir, "bad-plugin", False),
        "joomla_valid": create_joomla_fixture(base_dir, "com_sample", True),
        "joomla_invalid": create_joomla_fixture(base_dir, "com_bad", False),
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description="Generate disposable CMS extension fixtures.")
    parser.add_argument("out_dir")
    parser.add_argument("--platform", choices=["all", "wordpress", "joomla"], default="all")
    parser.add_argument("--invalid", action="store_true", help="generate invalid fixture variant for a single platform")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    if args.platform == "all":
        result = create_all(args.out_dir)
    elif args.platform == "wordpress":
        result = {"wordpress": create_wordpress_fixture(args.out_dir, valid=not args.invalid)}
    else:
        result = {"joomla": create_joomla_fixture(args.out_dir, valid=not args.invalid)}
    if args.json:
        cc.emit(cc.layer_result("fixtures", [cc.check("fixtures", cc.PASS, "Generated fixtures.")], meta=result), True)
    else:
        for key, path in result.items():
            sys.stdout.write("{}: {}\n".format(key, path))
    return cc.EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
