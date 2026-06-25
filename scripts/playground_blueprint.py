#!/usr/bin/env python3
"""Generate a WordPress Playground Blueprint for plugin smoke testing."""

import argparse
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import cms_common as cc  # noqa: E402

SCHEMA = "https://playground.wordpress.net/blueprint-schema.json"


def build_blueprint(plugin_url=None, plugin_slug=None, php="8.2", wp="latest", landing_page="/wp-admin/plugins.php"):
    plugins = []
    if plugin_url:
        plugins.append(plugin_url)
    if plugin_slug:
        plugins.append(plugin_slug)
    if not plugins:
        raise cc.GuardError("Provide --plugin-url or --plugin-slug.")
    return {
        "$schema": SCHEMA,
        "landingPage": landing_page,
        "login": True,
        "preferredVersions": {
            "php": str(php),
            "wp": str(wp),
        },
        "features": {
            "networking": True,
        },
        "plugins": plugins,
        "steps": [
            {
                "step": "runPHP",
                "code": "<?php update_option('blog_public', '0');",
            }
        ],
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description="Generate a WordPress Playground Blueprint JSON file.")
    parser.add_argument("--plugin-url", default=None, help="download URL for a plugin zip")
    parser.add_argument("--plugin-slug", default=None, help="WordPress.org plugin slug")
    parser.add_argument("--php", default="8.2")
    parser.add_argument("--wp", default="latest")
    parser.add_argument("--landing-page", default="/wp-admin/plugins.php")
    parser.add_argument("--out", default=None, help="write JSON to this path instead of stdout")
    args = parser.parse_args(argv)
    try:
        data = build_blueprint(args.plugin_url, args.plugin_slug, args.php, args.wp, args.landing_page)
    except cc.GuardError as exc:
        sys.stderr.write(str(exc) + "\n")
        return cc.EXIT_USAGE
    text = json.dumps(data, indent=2, ensure_ascii=False) + "\n"
    if args.out:
        cc.ensure_dir(os.path.dirname(args.out) or ".")
        with open(args.out, "w", encoding="utf-8") as fh:
            fh.write(text)
    else:
        sys.stdout.write(text)
    return cc.EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
