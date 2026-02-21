#!/usr/bin/env python3
import argparse
import asyncio
import json
import os
from pathlib import Path
import re
import sys

from ampapi import AMPControllerInstance, APIParams, Bridge
from ampapi.modules import ActionResultError

ARKSA_GROUP_KEY = "arksa:stadiacontroller"
# Per-instance identity/location fields that should not be cloned from master.
ARKSA_SKIP_NODES = {
    "Meta.GenericModule.SessionName",
    "Meta.GenericModule.Map",
    "Meta.GenericModule.CustomMap",
}
# Force specific config values on all destination targets.
ARKSA_FORCED_NODE_VALUES = {
    "GenericModule.App.UseRandomAdminPassword": "false",
}


class SafeAMPControllerInstance(AMPControllerInstance):
    # ampapi's __del__ may invoke asyncio.run() during interpreter teardown.
    # We explicitly close the session in main(), so this no-op avoids warning noise.
    def __del__(self) -> None:
        return


def _extract_template_group(friendly_name: str) -> str | None:
    # Expected pattern example: "Some Name -TEMPLATE GROUP-"
    match = re.search(r"-\s*template\s+([^-]+?)\s*-", friendly_name, flags=re.IGNORECASE)
    if match is None:
        return None
    group = match.group(1).strip()
    return group or None


def _has_destination_group(friendly_name: str, group: str) -> bool:
    return f"-{group.lower()}-" in friendly_name.lower()


def _is_template_instance_friendly(friendly_name: object) -> bool:
    return _extract_template_group(str(friendly_name)) is not None


def _read_config() -> dict[str, str]:
    config_path = Path(__file__).resolve().parent / "amp_config.json"
    if not config_path.exists():
        return {}
    with config_path.open("r", encoding="utf-8") as fh:
        data = json.load(fh)
    if not isinstance(data, dict):
        raise RuntimeError("amp_config.json must contain a JSON object")
    return {
        "url": str(data.get("url", "")).strip(),
        "username": str(data.get("username", "")).strip(),
        "password": str(data.get("password", "")).strip(),
    }


def _require_value(name: str, env_name: str, config: dict[str, str]) -> str:
    value = os.getenv(env_name, "").strip() or config.get(name, "").strip()
    if not value:
        raise RuntimeError(f"Missing required value for '{name}' ({env_name} or amp_config.json)")
    return value


def _is_ads_instance(module: object, instance_name: object) -> bool:
    return str(module) == "ADS" or str(instance_name).startswith("ADS")


def _find_master_template_instance(instances_by_id: dict[str, object]) -> tuple[object | None, str | None]:
    matches: list[object] = []
    for instance in instances_by_id.values():
        instance_name = str(getattr(instance, "instance_name", ""))
        module = str(getattr(instance, "module", ""))
        if _is_ads_instance(module=module, instance_name=instance_name):
            continue
        friendly_name = str(getattr(instance, "friendly_name", ""))
        if _extract_template_group(friendly_name) is not None:
            matches.append(instance)

    if not matches:
        return None, None
    matches.sort(key=lambda i: str(getattr(i, "instance_name", "")))
    selected = matches[0]
    selected_group = _extract_template_group(str(getattr(selected, "friendly_name", "")))
    return selected, selected_group


def _iter_settings_from_spec(settings_spec: object) -> list[object]:
    collected: list[object] = []
    if settings_spec is None:
        return collected
    for value in vars(settings_spec).values():
        if isinstance(value, list):
            collected.extend(value)
    return collected


def _is_game_related_setting(setting: object) -> bool:
    node = str(getattr(setting, "node", "") or "")
    node_prefixes = (
        "GenericModule.",
        "steamcmdplugin.",
        "RCONPlugin.",
        "Meta.GenericModule.",
    )
    return node.startswith(node_prefixes)


def _print_controller_status(ctrl_status: object) -> None:
    if isinstance(ctrl_status, ActionResultError):
        print(f"Controller status query failed: {ctrl_status}")
        return
    print("Controller status:")
    print(
        f"- state={getattr(ctrl_status, 'state', '<unknown>')} "
        f"uptime={getattr(ctrl_status, 'uptime', '<unknown>')} "
        f"active_users={getattr(ctrl_status, 'active_users', '<unknown>')}"
    )


def _print_instance_statuses(statuses: object, instances_by_id: dict[str, object]) -> None:
    if isinstance(statuses, list) and statuses:
        printed = 0
        print("Instance statuses (non-ADS):")
        for entry in statuses:
            instance_id = getattr(entry, "instance_id", "<unknown>")
            running = getattr(entry, "running", "<unknown>")
            instance = instances_by_id.get(instance_id)
            instance_name = getattr(instance, "instance_name", "<unknown>")
            friendly_name = getattr(instance, "friendly_name", "<unknown>")
            module = getattr(instance, "module", "<unknown>")
            app_state = getattr(instance, "app_state", "<unknown>")
            if _is_ads_instance(module=module, instance_name=instance_name):
                continue
            if _is_template_instance_friendly(friendly_name):
                continue
            printed += 1
            print(
                f"- instance_id={instance_id} friendly_name={friendly_name} "
                f"instance_name={instance_name} module={module} "
                f"running={running} app_state={app_state}"
            )
        if printed == 0:
            print("- No non-ADS instances found.")
    else:
        print("No instance status list returned.")


async def _print_application_statuses(ads: SafeAMPControllerInstance, statuses: object) -> None:
    print("\nApplication status (running instances):")
    if not isinstance(statuses, list) or not statuses:
        print("- No statuses available to query applications.")
        return

    any_printed = False
    for entry in statuses:
        instance_id = getattr(entry, "instance_id", "")
        running = bool(getattr(entry, "running", False))
        if not instance_id or not running:
            continue

        instance_obj = await ads.get_instance(instance_id=instance_id, format_data=True)
        if isinstance(instance_obj, ActionResultError):
            print(f"- instance_id={instance_id} error=get_instance failed ({instance_obj})")
            any_printed = True
            continue

        friendly_name = getattr(instance_obj, "friendly_name", "<unknown>")
        instance_name = getattr(instance_obj, "instance_name", "<unknown>")
        module = getattr(instance_obj, "module", "<unknown>")
        if _is_ads_instance(module=module, instance_name=instance_name):
            continue
        if _is_template_instance_friendly(friendly_name):
            continue
        app_status = await instance_obj.get_application_status(format_data=True)
        if isinstance(app_status, ActionResultError):
            print(
                f"- {friendly_name} ({instance_name}) instance_id={instance_id} "
                f"error=get_application_status failed ({app_status})"
            )
            any_printed = True
            continue

        metrics = getattr(app_status, "metrics", None)
        active_users = getattr(getattr(metrics, "active_users", None), "raw_value", "<unknown>")
        cpu_percent = getattr(getattr(metrics, "cpu_usage", None), "percent", "<unknown>")
        mem_percent = getattr(getattr(metrics, "memory_usage", None), "percent", "<unknown>")
        any_printed = True
        print(
            f"- {friendly_name} ({instance_name}) instance_id={instance_id} "
            f"state={getattr(app_status, 'state', '<unknown>')} "
            f"uptime={getattr(app_status, 'uptime', '<unknown>')} "
            f"active_users={active_users} cpu={cpu_percent}% mem={mem_percent}%"
        )

    if not any_printed:
        print("- No running non-ADS instances.")


async def _print_template_game_settings(
    ads: SafeAMPControllerInstance,
    instances_by_id: dict[str, object],
    template_instance_name: str,
) -> None:
    print(f"\nTemplate game settings ({template_instance_name}):")
    template_meta = next(
        (
            instance
            for instance in instances_by_id.values()
            if getattr(instance, "instance_name", "") == template_instance_name
        ),
        None,
    )
    if template_meta is None:
        print("- Template instance not found.")
        return

    template_id = getattr(template_meta, "instance_id", "")
    template_obj = await ads.get_instance(instance_id=template_id, format_data=True)
    if isinstance(template_obj, ActionResultError):
        print(f"- Failed to load template instance: {template_obj}")
        return

    settings_spec = await template_obj.get_setting_spec(format_data=True)
    if isinstance(settings_spec, ActionResultError):
        print(f"- Failed to query setting spec: {settings_spec}")
        return

    settings = _iter_settings_from_spec(settings_spec)
    game_settings = [setting for setting in settings if _is_game_related_setting(setting)]
    if not game_settings:
        print("- No game-related settings found.")
        return

    print(f"- Found {len(game_settings)} game-related settings")
    for setting in sorted(game_settings, key=lambda s: str(getattr(s, "node", ""))):
        print(
            f"- node={getattr(setting, 'node', '<unknown>')} "
            f"name={getattr(setting, 'name', '<unknown>')} "
            f"value={getattr(setting, 'current_value', '<unknown>')} "
            f"requires_restart={getattr(setting, 'requires_restart', '<unknown>')}"
        )


def _arksa_bucket_from_subcategory(subcategory: str) -> str:
    lower = subcategory.lower()
    if lower.startswith("server:"):
        return "Server"
    if lower.startswith("gameplay:"):
        return "Gameplay"
    if lower.startswith("multipliers:"):
        return "Multipliers"
    if lower.startswith("structures:"):
        return "Structures"
    if lower.startswith("clusters:"):
        return "Clusters"
    return "Overall"


async def _print_arksa_menu_configuration_settings(
    ads: SafeAMPControllerInstance,
    template_meta: object,
) -> None:
    template_instance_name = str(getattr(template_meta, "instance_name", "<unknown>"))
    print(f"\nTemplate menu configuration ({template_instance_name}):")
    if not bool(getattr(template_meta, "running", False)):
        print("- Template instance unavailable/offline; menu settings not queried.")
        return

    template_id = getattr(template_meta, "instance_id", "")
    template_obj = await ads.get_instance(instance_id=template_id, format_data=True)
    if isinstance(template_obj, ActionResultError):
        print(f"- Failed to load template instance: {template_obj}")
        return

    settings_spec = await template_obj.get_setting_spec(format_data=False)
    if isinstance(settings_spec, ActionResultError):
        print(f"- Failed to query setting spec: {settings_spec}")
        return
    if not isinstance(settings_spec, dict):
        print("- Unexpected setting spec format.")
        return

    arksa_settings = settings_spec.get("arksa:stadiacontroller", [])
    if not isinstance(arksa_settings, list) or not arksa_settings:
        print("- No template menu settings returned.")
        return

    buckets: dict[str, list[dict]] = {
        "Server": [],
        "Gameplay": [],
        "Multipliers": [],
        "Structures": [],
        "Clusters": [],
        "Overall": [],
    }
    for item in arksa_settings:
        if not isinstance(item, dict):
            continue
        bucket = _arksa_bucket_from_subcategory(str(item.get("subcategory", "")))
        buckets[bucket].append(item)

    for section in ["Server", "Gameplay", "Multipliers", "Structures", "Clusters", "Overall"]:
        items = buckets[section]
        if not items:
            continue
        print(f"\n[{section}] ({len(items)})")
        for setting in sorted(items, key=lambda x: (int(x.get("order", 0)), str(x.get("node", "")))):
            print(
                f"- {setting.get('name', '<unknown>')} | "
                f"node={setting.get('node', '<unknown>')} | "
                f"value={setting.get('current_value', '<unknown>')} | "
                f"requires_restart={setting.get('requires_restart', '<unknown>')}"
            )


async def _discover_arksa_instances(
    ads: SafeAMPControllerInstance,
    instances_by_id: dict[str, object],
    template_group: str,
) -> dict[str, object]:
    print("\nDiscovering destination instances (group marker check):")
    arksa: dict[str, object] = {}
    for instance in instances_by_id.values():
        instance_name = getattr(instance, "instance_name", "<unknown>")
        friendly_name = str(getattr(instance, "friendly_name", ""))
        module = getattr(instance, "module", "<unknown>")
        if _is_ads_instance(module=module, instance_name=instance_name):
            continue
        if not _has_destination_group(friendly_name=friendly_name, group=template_group):
            print(f"- skip {instance_name}: missing destination marker -{template_group}-")
            continue
        if not bool(getattr(instance, "running", False)):
            print(f"- skip {instance_name}: instance unavailable/offline")
            continue
        instance_id = getattr(instance, "instance_id", "")
        instance_obj = await ads.get_instance(instance_id=instance_id, format_data=True)
        if isinstance(instance_obj, ActionResultError):
            print(f"- skip {instance_name}: get_instance failed")
            continue
        arksa[instance_id] = instance_obj
        print(f"- destination confirmed: {getattr(instance_obj, 'friendly_name', instance_name)} ({instance_name})")
    return arksa


def _build_master_arksa_value_map(arksa_settings: list[dict]) -> dict[str, str]:
    values: dict[str, str] = {}
    for item in arksa_settings:
        if not isinstance(item, dict):
            continue
        node = str(item.get("node", "")).strip()
        if not node or node in ARKSA_SKIP_NODES:
            continue
        if bool(item.get("read_only", False)):
            continue
        raw = item.get("current_value")
        if raw is None:
            continue
        if isinstance(raw, bool):
            values[node] = "true" if raw else "false"
        elif isinstance(raw, (int, float, str)):
            values[node] = str(raw)
        else:
            values[node] = json.dumps(raw, separators=(",", ":"))
    return values


def _build_writable_node_current_values(settings_spec: dict) -> dict[str, str]:
    values: dict[str, str] = {}
    for group_items in settings_spec.values():
        if not isinstance(group_items, list):
            continue
        for item in group_items:
            if not isinstance(item, dict):
                continue
            node = str(item.get("node", "")).strip()
            if not node:
                continue
            if bool(item.get("read_only", False)):
                continue
            values[node] = str(item.get("current_value", ""))
    return values


def _normalize_value(value: object) -> str:
    text = str(value).strip()
    lower = text.lower()
    if lower in {"true", "false"}:
        return lower
    return text


def _application_type(instance_obj: object) -> str:
    # Prefer app-specific image source (e.g. steam:2399830). Fallback to module.
    app_type = str(getattr(instance_obj, "display_image_source", "")).strip()
    if app_type:
        return app_type
    return str(getattr(instance_obj, "module", "")).strip()


async def _wait_for_application_stop(target: object, timeout_seconds: int = 60, interval_seconds: int = 1) -> bool:
    elapsed = 0
    while elapsed <= timeout_seconds:
        refreshed = await target.get_instance_status()
        if isinstance(refreshed, ActionResultError):
            await asyncio.sleep(interval_seconds)
            elapsed += interval_seconds
            continue

        if not bool(getattr(refreshed, "running", True)):
            return True

        await asyncio.sleep(interval_seconds)
        elapsed += interval_seconds
    return False


async def _sync_arksa_settings_from_master(
    ads: SafeAMPControllerInstance,
    instances_by_id: dict[str, object],
    master_instance_name: str,
    template_group: str,
    dry_run: bool,
) -> None:
    mode = "DRY RUN" if dry_run else "APPLY"
    print(f"\nSync game settings from template: {master_instance_name} ({mode})")
    arksa_instances = await _discover_arksa_instances(
        ads=ads,
        instances_by_id=instances_by_id,
        template_group=template_group,
    )
    if not arksa_instances:
        print("- No destination instances discovered. Sync skipped.")
        return

    master = next(
        (inst for inst in arksa_instances.values() if getattr(inst, "instance_name", "") == master_instance_name),
        None,
    )
    if master is None:
        print("- Template instance not found in destination set. Sync skipped.")
        return

    master_spec = await master.get_setting_spec(format_data=False)
    if isinstance(master_spec, ActionResultError) or not isinstance(master_spec, dict):
        print("- Failed to load master setting spec. Sync skipped.")
        return
    master_arksa_settings = master_spec.get(ARKSA_GROUP_KEY, [])
    if not isinstance(master_arksa_settings, list) or not master_arksa_settings:
        print("- Template settings group missing. Sync skipped.")
        return

    master_values = _build_master_arksa_value_map(master_arksa_settings)
    print(f"- Master settings candidates: {len(master_values)}")
    print(f"- Explicitly skipped nodes: {len(ARKSA_SKIP_NODES)}")

    targets = [inst for inst in arksa_instances.values() if getattr(inst, "instance_name", "") != master_instance_name]
    if not targets:
        print("- No target destination instances (only template exists).")
        return
    print(f"- Target destination instances: {len(targets)}")

    template_app_type = _application_type(master)
    mismatches: list[str] = []
    for target in targets:
        target_type = _application_type(target)
        if target_type != template_app_type:
            mismatches.append(
                f"- {getattr(target, 'friendly_name', '<unknown>')} "
                f"({getattr(target, 'instance_name', '<unknown>')}): "
                f"template_type={template_app_type} target_type={target_type}"
            )
    if mismatches:
        print("\nApplication type mismatch detected. Halting sync.")
        for line in mismatches:
            print(line)
        return

    target_reports: list[dict[str, object]] = []
    for target in targets:
        target_name = getattr(target, "instance_name", "<unknown>")
        target_friendly = getattr(target, "friendly_name", target_name)

        target_spec = await target.get_setting_spec(format_data=False)
        if isinstance(target_spec, ActionResultError) or not isinstance(target_spec, dict):
            target_reports.append(
                {
                    "target": target,
                    "name": target_name,
                    "friendly": target_friendly,
                    "error": "failed to get target settings spec",
                    "same": {},
                    "diff": {},
                }
            )
            continue
        target_arksa_settings = target_spec.get(ARKSA_GROUP_KEY, [])
        if not isinstance(target_arksa_settings, list):
            target_reports.append(
                {
                    "target": target,
                    "name": target_name,
                    "friendly": target_friendly,
                    "error": "target has no matching settings group at apply time",
                    "same": {},
                    "diff": {},
                }
            )
            continue

        target_current_values = _build_writable_node_current_values(target_spec)
        target_allowed_nodes = set(target_current_values.keys())
        payload_all = {node: value for node, value in master_values.items() if node in target_allowed_nodes}
        for forced_node, forced_value in ARKSA_FORCED_NODE_VALUES.items():
            if forced_node in target_allowed_nodes:
                payload_all[forced_node] = forced_value

        payload_diff = {
            node: new_value
            for node, new_value in payload_all.items()
            if _normalize_value(target_current_values.get(node, "")) != _normalize_value(new_value)
        }
        payload_same = {
            node: value
            for node, value in payload_all.items()
            if _normalize_value(target_current_values.get(node, "")) == _normalize_value(value)
        }
        target_reports.append(
            {
                "target": target,
                "name": target_name,
                "friendly": target_friendly,
                "error": None,
                "same": payload_same,
                "diff": payload_diff,
                "current": target_current_values,
            }
        )

    print("\nNo Update Needed By Server:")
    for report in target_reports:
        name = str(report["name"])
        friendly = str(report["friendly"])
        same = report["same"]
        error = report["error"]
        if error is not None:
            print(f"- {friendly} ({name}): unable to evaluate ({error})")
            continue
        assert isinstance(same, dict)
        print(f"- {friendly} ({name}): {len(same)} setting(s) already aligned")
        for node in sorted(same.keys()):
            print(f"  - {node}: {same[node]}")

    print("\nUpdate Required By Server:")
    for report in target_reports:
        name = str(report["name"])
        friendly = str(report["friendly"])
        diff = report["diff"]
        error = report["error"]
        if error is not None:
            print(f"- {friendly} ({name}): unable to evaluate ({error})")
            continue
        assert isinstance(diff, dict)
        print(f"- {friendly} ({name}): {len(diff)} setting(s) need update")
        current = report.get("current", {})
        if not isinstance(current, dict):
            current = {}
        for node in sorted(diff.keys()):
            old_value = current.get(node, "<unset>")
            print(f"  - {node}: {old_value} -> {diff[node]}")

    if dry_run:
        print("- dry run only: no stop/apply/start executed")
        return

    for report in target_reports:
        error = report["error"]
        if error is not None:
            continue
        diff = report["diff"]
        if not isinstance(diff, dict) or not diff:
            continue
        target = report["target"]
        name = str(report["name"])
        friendly = str(report["friendly"])
        print(f"\nApplying changes: {friendly} ({name})")
        if bool(getattr(target, "running", False)):
            stop_res = await target.stop_application()
            if isinstance(stop_res, ActionResultError):
                print(f"- stop failed: {stop_res}")
                continue
            print("- application stopped")
            stopped = await _wait_for_application_stop(target=target, timeout_seconds=60, interval_seconds=1)
            if not stopped:
                print("- timeout waiting for application to stop (60s), skipping apply")
                continue
            print("- confirmed stopped")
        else:
            print("- application already stopped")

        apply_res = await target.set_configs(data=diff, format_data=False)
        if isinstance(apply_res, ActionResultError):
            print(f"- set_configs failed: {apply_res}")
            continue
        print(f"- updated settings count: {len(diff)}")

        start_res = await target.start_application(format_data=False)
        if isinstance(start_res, ActionResultError):
            print(f"- start failed: {start_res}")
            continue
        print("- application start requested")


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Query and sync AMP game settings.")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what settings would change on destination targets without stopping or updating instances.",
    )
    return parser.parse_args()


async def main() -> int:
    args = _parse_args()
    config = _read_config()
    amp_url = _require_value("url", "AMP_URL", config)
    amp_user = _require_value("username", "AMP_USER", config)
    amp_pass = _require_value("password", "AMP_PASS", config)

    params = APIParams(url=amp_url, user=amp_user, password=amp_pass)
    Bridge(api_params=params)

    ads = SafeAMPControllerInstance()

    try:
        login_result = await ads.login(amp_user=amp_user, amp_password=amp_pass)
        if isinstance(login_result, ActionResultError):
            print(f"Login failed: {login_result}")
            return 2

        ctrl_status = await ads.get_status()
        _print_controller_status(ctrl_status)

        instances = await ads.get_instances(format_data=True)
        if isinstance(instances, ActionResultError):
            print(f"Instance list query failed: {instances}")
            return 4
        instances_by_id: dict[str, object] = {
            getattr(instance, "instance_id", ""): instance for instance in instances if getattr(instance, "instance_id", "")
        }
        master_template, template_group = _find_master_template_instance(instances_by_id=instances_by_id)
        if master_template is None:
            print("\nMaster template not found. Friendly name must match pattern '-TEMPLATE <GROUP>-'.")
            return 5
        if not template_group:
            print("\nMaster template selected but no template group parsed from friendly name.")
            return 5
        print(
            f"\nMaster template selected: {getattr(master_template, 'friendly_name', '<unknown>')} "
            f"({getattr(master_template, 'instance_name', '<unknown>')})"
        )
        print(f"Template group: {template_group} (destinations require '-{template_group}-' in friendly name)")

        # ADS list of instance statuses
        statuses = await ads.get_instance_statuses(format_data=True)
        if isinstance(statuses, ActionResultError):
            print(f"Instance status query failed: {statuses}")
            return 3

        _print_instance_statuses(statuses, instances_by_id)
        await _print_application_statuses(ads, statuses)
        # Old generic template settings output intentionally disabled for now.
        # await _print_template_game_settings(
        #     ads=ads,
        #     instances_by_id=instances_by_id,
        #     template_instance_name="ARKSurvivalAscended02",
        # )
        await _print_arksa_menu_configuration_settings(
            ads=ads,
            template_meta=master_template,
        )
        await _sync_arksa_settings_from_master(
            ads=ads,
            instances_by_id=instances_by_id,
            master_instance_name=str(getattr(master_template, "instance_name", "")),
            template_group=template_group,
            dry_run=args.dry_run,
        )
        return 0
    finally:
        await ads.__adel__()


if __name__ == "__main__":
    try:
        raise SystemExit(asyncio.run(main()))
    except RuntimeError as exc:
        print(str(exc))
        raise SystemExit(1)
