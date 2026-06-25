#!/usr/bin/env python3
"""Generate disposable Docker Compose labs for CMS extension testing.

The script writes a lab folder with docker-compose.yml and helper notes. It can
optionally run `docker compose up -d` when Docker is available, but it never
targets a production site and never asks for real credentials.
"""

import argparse
import json
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import cms_common as cc  # noqa: E402


WP_COMPOSE = """services:
  db:
    image: mariadb:10.11
    environment:
      MYSQL_DATABASE: wordpress
      MYSQL_USER: wordpress
      MYSQL_PASSWORD: wordpress
      MYSQL_ROOT_PASSWORD: root
    volumes:
      - db:/var/lib/mysql
  wordpress:
    image: wordpress:latest
    depends_on: [db]
    ports:
      - "127.0.0.1:{port}:80"
    environment:
      WORDPRESS_DB_HOST: db
      WORDPRESS_DB_NAME: wordpress
      WORDPRESS_DB_USER: wordpress
      WORDPRESS_DB_PASSWORD: wordpress
    volumes:
      - wp:/var/www/html
      - ./extensions:/extensions
volumes:
  db:
  wp:
"""

JOOMLA_COMPOSE = """services:
  db:
    image: mariadb:10.11
    environment:
      MYSQL_DATABASE: joomla
      MYSQL_USER: joomla
      MYSQL_PASSWORD: joomla
      MYSQL_ROOT_PASSWORD: root
    volumes:
      - db:/var/lib/mysql
  joomla:
    image: joomla:latest
    depends_on: [db]
    ports:
      - "127.0.0.1:{port}:80"
    environment:
      JOOMLA_DB_HOST: db
      JOOMLA_DB_NAME: joomla
      JOOMLA_DB_USER: joomla
      JOOMLA_DB_PASSWORD: joomla
    volumes:
      - joomla:/var/www/html
      - ./extensions:/extensions
volumes:
  db:
  joomla:
"""


def _write_json(path, obj):
    cc.write_json(path, obj)


def _copy_extension(extension, ext_dir):
    if not extension:
        return None
    src = os.path.abspath(extension)
    dest = os.path.join(ext_dir, os.path.basename(extension))
    if os.path.islink(src):
        raise cc.GuardError("Refusing symlink extension path.")
    if src != os.path.abspath(dest):
        import shutil
        shutil.copy2(src, dest)
    return dest


def write_joomla_install_scenario(out_dir, base_url, upload_zip):
    scenario_dir = cc.ensure_dir(os.path.join(out_dir, "scenarios"))
    scenario_path = os.path.join(scenario_dir, "joomla-real-install.json")
    scenario = {
        "name": "joomla-real-install",
        "platform": "joomla",
        "description": "Login to Joomla admin and upload an extension package on a disposable lab.",
        "requires_auth": True,
        "secret_env": ["CMSCT_UPLOAD_ZIP"],
        "steps": [
            {"action": "goto", "url": "${BASE_URL}/administrator/index.php", "name": "01-login"},
            {"action": "fill", "selector": "#mod-login-username", "value": "${ADMIN_USER}"},
            {"action": "fill", "selector": "#mod-login-password", "value": "${ADMIN_PASS}", "secret": True},
            {"action": "click", "selector": "#btn-login-submit", "name": "02-submit-login"},
            {"action": "goto", "url": "${BASE_URL}/administrator/index.php?option=com_installer", "name": "03-installer"},
            {
                "action": "upload",
                "selector": "input[type='file'][name='install_package'], #install_package",
                "path": "${CMSCT_UPLOAD_ZIP}",
                "name": "04-upload-package",
            },
            {"action": "click", "selector": "#installbutton_package, button[type='submit']", "name": "05-install"},
            {
                "action": "expect_text_regex",
                "selector": "#system-message-container, body",
                "pattern": "(?i)(install|success|installed)",
                "name": "06-install-result",
            },
        ],
    }
    _write_json(scenario_path, scenario)
    return scenario_path


def write_lab(platform, out_dir, port, extension=None):
    cc.ensure_dir(out_dir)
    ext_dir = cc.ensure_dir(os.path.join(out_dir, "extensions"))
    upload_path = _copy_extension(extension, ext_dir)
    compose = WP_COMPOSE if platform == "wordpress" else JOOMLA_COMPOSE
    compose_path = os.path.join(out_dir, "docker-compose.yml")
    with open(compose_path, "w", encoding="utf-8") as fh:
        fh.write(compose.format(port=port))
    base_url = "http://localhost:{}".format(port)
    env_path = os.path.join(out_dir, "lab.local.env")
    with open(env_path, "w", encoding="utf-8") as fh:
        fh.write("CMSCT_OUT_DIR={}\n".format(os.path.abspath(out_dir)))
        fh.write("CMSCT_UPLOAD_ROOT={}\n".format(os.path.abspath(ext_dir)))
        if upload_path:
            fh.write("CMSCT_UPLOAD_ZIP={}\n".format(os.path.abspath(upload_path)))
        fh.write("CMS_ADMIN_USER=admin\n")
        fh.write("CMS_ADMIN_PASS=change-me-in-lab\n")
    marker = {
        "tool": "cms-component-tester",
        "platform": platform,
        "created_at": cc.now_iso(),
        "project": "cmsct-{}-{}".format(platform, int(time.time())),
        "base_url": base_url,
        "out_dir": os.path.abspath(out_dir),
        "upload": os.path.abspath(upload_path) if upload_path else None,
    }
    marker_path = os.path.join(out_dir, ".cmsct-lab.json")
    _write_json(marker_path, marker)
    scenario_path = None
    if platform == "joomla" and upload_path:
        scenario_path = write_joomla_install_scenario(out_dir, base_url, upload_path)
    notes = [
        "# Disposable {} lab".format(platform),
        "",
        "Base URL: `{}`".format(base_url),
        "",
        "Start:",
        "```bash",
        "docker compose -p {project} --env-file lab.local.env up -d".format(project=marker["project"]),
        "```",
        "",
        "Stop and destroy:",
        "```bash",
        "docker compose -p {project} --env-file lab.local.env down -v".format(project=marker["project"]),
        "```",
        "",
        "Use only test credentials created inside this lab.",
    ]
    notes_path = os.path.join(out_dir, "README-lab.md")
    with open(notes_path, "w", encoding="utf-8") as fh:
        fh.write("\n".join(notes) + "\n")
    return {
        "compose": compose_path,
        "notes": notes_path,
        "env": env_path,
        "marker": marker_path,
        "scenario": scenario_path,
        "base_url": base_url,
        "project": marker["project"],
    }


def docker_available():
    docker = cc.which("docker")
    if not docker:
        return None
    res = cc.run_cmd([docker, "compose", "version"], timeout=30)
    return docker if res["ok"] else None


def main(argv=None):
    parser = argparse.ArgumentParser(description="Create a disposable Docker CMS lab.")
    parser.add_argument("platform", choices=["wordpress", "joomla"])
    parser.add_argument("--out-dir", required=True)
    parser.add_argument("--port", type=int, default=None)
    parser.add_argument("--extension", default=None, help="optional extension zip/source artifact to copy into lab")
    parser.add_argument("--up", action="store_true", help="run docker compose up -d after writing files")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    port = args.port or (8088 if args.platform == "wordpress" else 8089)
    result = write_lab(args.platform, args.out_dir, port, args.extension)
    checks = [cc.check("lab.scaffold", cc.PASS, "Docker lab files written.")]
    if args.up:
        docker = docker_available()
        if not docker:
            checks.append(cc.check("docker", cc.SKIP, "Docker compose is not available. Lab files are ready."))
        else:
            res = cc.run_cmd([docker, "compose", "-p", result["project"], "--env-file", "lab.local.env", "up", "-d"],
                             cwd=args.out_dir, timeout=300)
            checks.append(cc.check("docker.up", cc.PASS if res["ok"] else cc.FAIL,
                                   "docker compose up exited {}.".format(res["returncode"]),
                                   evidence=(res["stdout"] or res["stderr"])[-1000:]))
    layer = cc.layer_result("dev_lab", checks, summary="{} lab at {}".format(args.platform, result["base_url"]), meta=result)
    if args.json:
        cc.emit(layer, True)
    else:
        sys.stdout.write("{}\n{}\n".format(layer["summary"], result["compose"]))
    return cc.status_to_exit(layer["status"])


if __name__ == "__main__":
    sys.exit(main())
