#!/usr/bin/env python3
"""
Clone one hand-made Apprise notification across all *arr apps and severities.

Contains no secrets - API keys are read at runtime from each app's config.xml
under CONFIG_ROOT, so this file is safe to commit.

Prerequisite: create ONE notification by hand in Sonarr named exactly "P1"
(type Apprise, Server URL http://apprise:8000, Configuration Key p1,
 trigger: On Health Issue only, Include Health Warnings OFF).

This script reads that as a template, then creates/updates the equivalent
notifications in every app listed in APPS. It only sets trigger flags that
each app's own schema declares, so per-app differences are handled by the
apps themselves rather than assumed here.

Idempotent: re-running updates existing notifications matched by name.
No third-party dependencies.

Usage:
    python3 host/provision-arr-notifications.py            # dry run
    python3 host/provision-arr-notifications.py --apply    # write changes
"""

import json
import os
import sys
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET

# ---------------------------------------------------------------------------
# CONFIG
# ---------------------------------------------------------------------------

# Where the bind-mounted app configs live on the host.
CONFIG_ROOT = os.environ.get("MEDIA_CONFIG_ROOT", "/opt/media-stack/config")

# Ports are as published on the host by gluetun. Run this script FROM THE HOST.
# 'config' is the directory name under CONFIG_ROOT holding config.xml.
APPS = {
    "sonarr":   {"url": "http://localhost:8989", "api": 3, "config": "sonarr"},
    "radarr":   {"url": "http://localhost:7878", "api": 3, "config": "radarr"},
    "lidarr":   {"url": "http://localhost:8686", "api": 1, "config": "lidarr"},
    "prowlarr": {"url": "http://localhost:9696", "api": 1, "config": "prowlarr"},
}

# The app + notification name holding the hand-made template.
TEMPLATE_APP = "sonarr"
TEMPLATE_NAME = "P1"

# Severity -> (apprise config key, list of candidate trigger flags).
# Flags not present in a given app's schema are silently skipped.
SEVERITIES = {
    "P1": (
        "p1",
        ["onHealthIssue"],
    ),
    "P2": (
        "p2",
        ["onManualInteractionRequired", "onImportFailure", "onDownloadFailure"],
    ),
    "INFO": (
        "info",
        ["onGrab", "onDownload", "onReleaseImport", "onUpgrade", "onHealthRestored"],
    ),
}

# Health warnings are transient behind a VPN (indexer/download-client
# unavailable on every reconnect). Errors only.
INCLUDE_HEALTH_WARNINGS = False

DRY_RUN = "--apply" not in sys.argv

# ---------------------------------------------------------------------------

_keys = {}


def api_key(app):
    """Read the app's API key from its config.xml. Cached."""
    if app in _keys:
        return _keys[app]
    path = os.path.join(CONFIG_ROOT, APPS[app]["config"], "config.xml")
    try:
        key = ET.parse(path).getroot().findtext("ApiKey")
    except FileNotFoundError:
        sys.exit(f"No config.xml at {path}. Set MEDIA_CONFIG_ROOT if it lives elsewhere.")
    except PermissionError:
        sys.exit(f"Cannot read {path}. Try running with sudo, or fix ownership.")
    except ET.ParseError as e:
        sys.exit(f"{path} is not valid XML: {e}")
    if not key:
        sys.exit(f"No <ApiKey> element in {path}. Has {app} finished first-run setup?")
    _keys[app] = key.strip()
    return _keys[app]


def call(app, path, method="GET", body=None):
    cfg = APPS[app]
    url = f"{cfg['url']}/api/v{cfg['api']}/{path}"
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("X-Api-Key", api_key(app))
    req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            raw = r.read()
            return json.loads(raw) if raw else None
    except urllib.error.HTTPError as e:
        detail = e.read()[:300].decode(errors="replace")
        print(f"  ! {app} {method} {path} -> HTTP {e.code}: {detail}")
        raise
    except urllib.error.URLError as e:
        print(f"  ! {app} unreachable at {cfg['url']}: {e.reason}")
        raise


def get_template():
    """Fetch the hand-made notification and work out which field holds the key."""
    for n in call(TEMPLATE_APP, "notification"):
        if n.get("name") == TEMPLATE_NAME:
            key_field = None
            for f in n.get("fields", []):
                if str(f.get("value", "")).strip().lower() == "p1":
                    key_field = f["name"]
            if not key_field:
                sys.exit(
                    f"Could not find a field on '{TEMPLATE_NAME}' whose value is 'p1'. "
                    "Check the Configuration Key is set to exactly 'p1'."
                )
            print(f"Template found in {TEMPLATE_APP}. Config-key field is '{key_field}'.")
            return n, key_field
    sys.exit(f"No notification named '{TEMPLATE_NAME}' in {TEMPLATE_APP}. Create it first.")


def supported_flags(app, implementation):
    """Ask the app which onX flags its Apprise notification actually supports."""
    for s in call(app, "notification/schema"):
        if s.get("implementation") == implementation:
            return {k for k in s if k.startswith("on") and isinstance(s[k], bool)}, s
    sys.exit(f"{app} has no '{implementation}' notification type. Is it too old?")


def build(template, key_field, schema, flags_on, name, config_key):
    payload = {
        "name": name,
        "implementation": template["implementation"],
        "implementationName": template.get("implementationName"),
        "configContract": template["configContract"],
        "fields": [dict(f) for f in template["fields"]],
        "tags": [],
    }
    for f in payload["fields"]:
        if f["name"] == key_field:
            f["value"] = config_key

    # Start from the app's own schema defaults, then enable what we want.
    for k, v in schema.items():
        if k.startswith("on") and isinstance(v, bool):
            payload[k] = False
    for flag in flags_on:
        if flag in schema:
            payload[flag] = True
    if "includeHealthWarnings" in schema:
        payload["includeHealthWarnings"] = INCLUDE_HEALTH_WARNINGS
    return payload


def main():
    print(f"Config root: {CONFIG_ROOT}")
    template, key_field = get_template()
    impl = template["implementation"]

    for app in APPS:
        print(f"\n=== {app} ===")
        try:
            available, schema = supported_flags(app, impl)
            existing = {n["name"]: n["id"] for n in call(app, "notification")}
        except Exception:
            print("  skipped (see error above)")
            continue

        for name, (config_key, wanted) in SEVERITIES.items():
            usable = [f for f in wanted if f in available]
            skipped = [f for f in wanted if f not in available]
            if not usable:
                print(f"  {name}: no supported triggers here, skipping "
                      f"(schema lacks: {', '.join(skipped)})")
                continue

            payload = build(template, key_field, schema, usable, name, config_key)
            action = "update" if name in existing else "create"
            note = f"  (schema lacks: {', '.join(skipped)})" if skipped else ""
            print(f"  {name}: {action} -> {', '.join(usable)}{note}")

            if DRY_RUN:
                continue
            if action == "update":
                payload["id"] = existing[name]
                call(app, f"notification/{existing[name]}", "PUT", payload)
            else:
                call(app, "notification", "POST", payload)

    if DRY_RUN:
        print("\nDRY RUN - nothing written. Re-run with --apply to write.")
    else:
        print("\nApplied.")


if __name__ == "__main__":
    main()
