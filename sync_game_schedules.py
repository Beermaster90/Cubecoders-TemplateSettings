#!/usr/bin/env python3
import argparse
import asyncio
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
from typing import Any

from ampapi import AMPControllerInstance, APIParams, Bridge
from ampapi.modules import ActionResultError

class SafeAMPControllerInstance(AMPControllerInstance):
    # ampapi's __del__ may invoke asyncio.run() during interpreter teardown.
    # We explicitly close the session in main(), so this no-op avoids warning noise.
    def __del__(self) -> None:
        return


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


def _extract_template_group(friendly_name: str) -> str | None:
    # Expected pattern example: "Some Name -TEMPLATE GROUP-"
    match = re.search(r"-\s*template\s+([^-]+?)\s*-", friendly_name, flags=re.IGNORECASE)
    if match is None:
        return None
    group = match.group(1).strip()
    return group or None


def _has_destination_group(friendly_name: str, group: str) -> bool:
    return f"-{group.lower()}-" in friendly_name.lower()


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


def _serialize_value(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if value is None:
        return ""
    if isinstance(value, (int, float, str)):
        return str(value)
    return json.dumps(value, separators=(",", ":"))


def _v(item: object, key: str, default: Any = None) -> Any:
    if isinstance(item, dict):
        return item.get(key, default)
    return getattr(item, key, default)


def _iter_trigger_tasks(trigger: object) -> list[object]:
    tasks = _v(trigger, "tasks", []) or []
    if isinstance(tasks, dict):
        return list(tasks.values())
    if isinstance(tasks, list):
        return tasks
    return []


def _normalize_param_key(name: str) -> str:
    return "".join(ch for ch in name.lower() if ch.isalnum())


def _build_method_consumes_map(schedule_data: object) -> dict[str, list[str]]:
    methods = _v(schedule_data, "available_methods", []) or []
    result: dict[str, list[str]] = {}
    for method in methods:
        method_id = str(_v(method, "id", "")).strip()
        if not method_id:
            continue
        consumes = _v(method, "consumes", []) or []
        names: list[str] = []
        for consume in consumes:
            name = str(_v(consume, "name", "")).strip()
            if name:
                names.append(name)
        result[method_id] = names
    return result


def _remap_parameter_mapping_for_method(
    method_id: str,
    mapping: dict[str, str],
    consumes_map: dict[str, list[str]],
) -> dict[str, str]:
    expected = consumes_map.get(method_id, [])
    if not expected:
        return mapping

    source_by_norm: dict[str, str] = {}
    for key, value in mapping.items():
        source_by_norm[_normalize_param_key(str(key))] = str(value)

    remapped: dict[str, str] = {}
    for target_name in expected:
        source_value = source_by_norm.get(_normalize_param_key(target_name))
        if source_value is not None:
            remapped[target_name] = source_value

    # If we successfully mapped at least one expected key, send only canonical keys.
    # This avoids AMP picking a conflicting alias (e.g. valuetocheck vs value_to_check).
    if remapped:
        return remapped
    return mapping


def _parameter_mapping_to_dict(mapping: object) -> dict[str, str]:
    if mapping is None:
        return {}
    if isinstance(mapping, dict):
        return {str(k): _serialize_value(v) for k, v in mapping.items()}
    if hasattr(mapping, "__dict__"):
        return {
            str(k): _serialize_value(v)
            for k, v in vars(mapping).items()
            if not str(k).startswith("_")
        }
    return {}


def _is_interval_trigger(trigger: object) -> bool:
    trigger_type = str(_v(trigger, "type", "") or _v(trigger, "trigger_type", ""))
    return "interval" in trigger_type.lower()


def _is_backup_task(task: object) -> bool:
    method = str(_v(task, "task_method_name", "")).lower()
    return "backup" in method


def _trigger_has_backup_task(trigger: object) -> bool:
    tasks = _iter_trigger_tasks(trigger)
    return any(_is_backup_task(task) for task in tasks)


def _distributed_minute(index: int, total: int) -> int:
    if total <= 0:
        return 0
    return int((index * 60) / total) % 60


def _distributed_minute_avoiding(index: int, total: int, blocked_minute: int) -> int:
    allowed = [m for m in range(60) if m != blocked_minute]
    if not allowed:
        return blocked_minute
    if total <= 0:
        return allowed[0]
    pos = int((index * len(allowed)) / total)
    if pos >= len(allowed):
        pos = len(allowed) - 1
    return allowed[pos]


def _build_replicated_description(base_description: str, template_name: str, run_stamp: str) -> str:
    base = base_description.strip() or "Scheduled Trigger"
    return f"{base} | replicated from {template_name} {run_stamp}"


def _trigger_summary(trigger: object) -> str:
    return (
        f"{_v(trigger, 'description', '<unknown>')} "
        f"(id={_v(trigger, 'id', '<unknown>')} type={_v(trigger, 'type', '<unknown>')})"
    )


async def _get_schedule(instance_obj: object) -> object | ActionResultError:
    return await instance_obj.get_schedule_data(format_data=True)


async def _get_target_instances(
    ads: SafeAMPControllerInstance,
    instances_by_id: dict[str, object],
    master_instance_name: str,
    template_group: str,
) -> list[object]:
    targets: list[object] = []
    for meta in sorted(instances_by_id.values(), key=lambda x: str(getattr(x, "instance_name", ""))):
        instance_name = str(getattr(meta, "instance_name", ""))
        module = str(getattr(meta, "module", ""))
        if _is_ads_instance(module=module, instance_name=instance_name):
            continue
        if instance_name == master_instance_name:
            continue
        friendly_name = str(getattr(meta, "friendly_name", ""))
        if not _has_destination_group(friendly_name=friendly_name, group=template_group):
            print(f"- skip {instance_name}: missing destination marker -{template_group}-")
            continue
        if not bool(getattr(meta, "running", False)):
            print(f"- skip {instance_name}: unavailable/offline")
            continue
        instance_id = str(getattr(meta, "instance_id", ""))
        if not instance_id:
            continue
        instance_obj = await ads.get_instance(instance_id=instance_id, format_data=True)
        if isinstance(instance_obj, ActionResultError):
            print(f"- skip {instance_name}: failed to load instance ({instance_obj})")
            continue
        targets.append(instance_obj)
    return targets


async def _load_template_interval_details(template_obj: object, template_trigger: object) -> dict[str, Any] | None:
    trigger_id = str(_v(template_trigger, "id", ""))
    if not trigger_id:
        return None
    timed = await template_obj.get_time_interval_trigger(trigger_id=trigger_id, format_data=False)
    if isinstance(timed, ActionResultError) or not isinstance(timed, dict):
        return None

    def _first_int(items: object, default: int) -> int:
        if isinstance(items, list) and items:
            try:
                return int(items[0])
            except (TypeError, ValueError):
                return default
        return default

    # ampapi currently exposes EditIntervalTrigger with single int values, so use first matched value.
    return {
        "months": _first_int(timed.get("match_months", []), 1),
        "days": _first_int(timed.get("match_days", []), 0),
        "hours": _first_int(timed.get("match_hours", []), 0),
        "minutes": _first_int(timed.get("match_minutes", []), 0),
        "days_of_month": _first_int(timed.get("match_days_of_month", []), 1),
        "description": str(timed.get("description", _v(template_trigger, "description", "Interval Trigger"))),
    }


async def _find_new_trigger_id_after_create(
    target_obj: object,
    before_ids: set[str],
    expected_description: str,
    preferred_id: str | None = None,
) -> str | None:
    schedule_after = await _get_schedule(target_obj)
    if isinstance(schedule_after, ActionResultError):
        return None

    populated_after = getattr(schedule_after, "populated_triggers", []) or []
    if preferred_id and any(str(getattr(t, "id", "")) == preferred_id for t in populated_after):
        if preferred_id not in before_ids:
            return preferred_id
    new_triggers = [t for t in populated_after if str(getattr(t, "id", "")) not in before_ids]
    if not new_triggers:
        return None

    for trg in new_triggers:
        if str(getattr(trg, "description", "")) == expected_description:
            return str(getattr(trg, "id", "")) or None
    return str(getattr(new_triggers[0], "id", "")) or None


async def _clear_trigger_tasks(target_obj: object, trigger_id: str) -> None:
    schedule = await target_obj.get_schedule_data(format_data=False)
    if isinstance(schedule, ActionResultError) or not isinstance(schedule, dict):
        return
    populated = schedule.get("populated_triggers", []) or []
    trigger = next((t for t in populated if str(t.get("id", "")) == trigger_id), None)
    if not isinstance(trigger, dict):
        return
    tasks = trigger.get("tasks", []) or []
    if isinstance(tasks, dict):
        task_items = list(tasks.values())
    elif isinstance(tasks, list):
        task_items = tasks
    else:
        task_items = []

    for task in task_items:
        task_id = str(_v(task, "id", "")).strip()
        if not task_id:
            continue
        res = await target_obj.delete_task(trigger_id=trigger_id, task_id=task_id, format_data=True)
        if isinstance(res, ActionResultError):
            print(f"  - existing task delete failed: task_id={task_id} ({res})")


async def _add_interval_trigger_raw(target_obj: object, interval_cfg: dict[str, Any]) -> object | ActionResultError:
    # Raw endpoint required for IntervalTrigger creation.
    params = {
        "months": [int(interval_cfg["months"])],
        "days": [int(interval_cfg["days"])],
        "hours": [int(interval_cfg["hours"])],
        "minutes": [int(interval_cfg["minutes"])],
        "daysOfMonth": [int(interval_cfg["days_of_month"])],
        "description": str(interval_cfg["description"]),
    }
    return await target_obj._call_api(api="Core/AddIntervalTrigger", parameters=params)


async def _sync_schedule_for_target(
    target_obj: object,
    template_obj: object,
    template_schedule: object,
    dry_run: bool,
    target_index: int,
    target_total: int,
    template_name: str,
    run_stamp: str,
) -> None:
    target_name = str(getattr(target_obj, "instance_name", "<unknown>"))
    target_friendly = str(getattr(target_obj, "friendly_name", target_name))
    print(f"\nTarget: {target_friendly} ({target_name})")

    target_schedule = await _get_schedule(target_obj)
    if isinstance(target_schedule, ActionResultError):
        print(f"- failed to read target schedule: {target_schedule}")
        return

    target_populated = list(getattr(target_schedule, "populated_triggers", []) or [])
    template_populated = list(_v(template_schedule, "populated_triggers", []) or [])
    print(f"- existing triggers on target: {len(target_populated)}")
    print(f"- template triggers to clone: {len(template_populated)}")

    for trigger in target_populated:
        trigger_id = str(getattr(trigger, "id", ""))
        print(f"- delete trigger: {_trigger_summary(trigger)}")
        if dry_run:
            continue
        result = await target_obj.delete_trigger(trigger_id=trigger_id, format_data=True)
        if isinstance(result, ActionResultError):
            print(f"  - delete failed: {result}")

    if dry_run:
        for template_trigger in template_populated:
            if not _is_interval_trigger(template_trigger):
                continue
            if not _trigger_has_backup_task(template_trigger):
                continue
            template_minute = 0
            interval_cfg = await _load_template_interval_details(template_obj, template_trigger)
            if interval_cfg is not None:
                template_minute = int(interval_cfg["minutes"])
            planned_minute = _distributed_minute_avoiding(
                index=target_index,
                total=target_total,
                blocked_minute=template_minute,
            )
            print(
                f"- dry-run backup minute plan: "
                f"{_v(template_trigger, 'description', '<unknown>')} -> {planned_minute:02d}"
            )
        print("- dry-run: skip create/apply steps")
        return

    # Refresh once after deletes.
    target_schedule = await _get_schedule(target_obj)
    if isinstance(target_schedule, ActionResultError):
        print(f"- failed to refresh target schedule after delete: {target_schedule}")
        return

    target_available = list(getattr(target_schedule, "available_triggers", []) or [])
    method_consumes = _build_method_consumes_map(target_schedule)

    for template_trigger in template_populated:
        trigger_desc = str(_v(template_trigger, "description", ""))
        trigger_enabled = bool(_v(template_trigger, "enabled_state", False))
        trigger_tasks = _iter_trigger_tasks(template_trigger)
        is_interval = _is_interval_trigger(template_trigger)

        before_schedule = await _get_schedule(target_obj)
        if isinstance(before_schedule, ActionResultError):
            print(f"- failed to read target schedule before create: {before_schedule}")
            continue
        before_ids = {str(getattr(t, "id", "")) for t in (getattr(before_schedule, "populated_triggers", []) or [])}

        source_trigger_id: str | None = None
        if is_interval:
            interval_cfg = await _load_template_interval_details(template_obj, template_trigger)
            if interval_cfg is None:
                print(f"- create interval trigger skipped (could not read template interval details): {trigger_desc}")
                continue
            interval_cfg["description"] = _build_replicated_description(
                base_description=trigger_desc,
                template_name=template_name,
                run_stamp=run_stamp,
            )
            if _trigger_has_backup_task(template_trigger):
                old_minutes = int(interval_cfg["minutes"])
                new_minutes = _distributed_minute_avoiding(
                    index=target_index,
                    total=target_total,
                    blocked_minute=old_minutes,
                )
                interval_cfg["minutes"] = new_minutes
                print(
                    f"  - backup interval minute adjusted for spread: "
                    f"{trigger_desc} {old_minutes:02d} -> {new_minutes:02d} "
                    f"(template minute blocked)"
                )
            create_result = await _add_interval_trigger_raw(target_obj=target_obj, interval_cfg=interval_cfg)
            if isinstance(create_result, ActionResultError):
                print(f"- create interval trigger failed: {trigger_desc} ({create_result})")
                continue
        else:
            # Map template event trigger description to target available trigger ID.
            match = next(
                (
                    t
                    for t in target_available
                    if str(getattr(t, "description", "")) == trigger_desc
                    and not _is_interval_trigger(t)
                ),
                None,
            )
            if match is None:
                print(f"- create event trigger skipped (no target match): {trigger_desc}")
                continue
            source_trigger_id = str(getattr(match, "id", ""))
            create_result = await target_obj.add_event_trigger(trigger_id=source_trigger_id, format_data=True)
            if isinstance(create_result, ActionResultError):
                print(f"- create event trigger failed: {trigger_desc} ({create_result})")
                continue

        new_trigger_id = await _find_new_trigger_id_after_create(
            target_obj=target_obj,
            before_ids=before_ids,
            expected_description=(
                str(interval_cfg["description"]) if is_interval else trigger_desc
            ),
            preferred_id=source_trigger_id,
        )
        if not new_trigger_id:
            print(f"- trigger created but new trigger id not found: {trigger_desc}")
            continue

        print(f"- created trigger: {trigger_desc} -> {new_trigger_id}")

        if is_interval:
            print(f"  - interval trigger configured: {interval_cfg['description']}")
        else:
            print(
                "  - event trigger naming unchanged by AMP API "
                f"(template trigger: {trigger_desc})"
            )
            await _clear_trigger_tasks(target_obj=target_obj, trigger_id=new_trigger_id)

        sorted_tasks = sorted(trigger_tasks, key=lambda x: int(_v(x, "order", 0)))
        for task in sorted_tasks:
            method_id = str(_v(task, "task_method_name", "")).strip()
            if not method_id:
                print("  - task skipped: missing method")
                continue
            mapping = _parameter_mapping_to_dict(_v(task, "parameter_mapping", {}))
            mapping = _remap_parameter_mapping_for_method(
                method_id=method_id,
                mapping=mapping,
                consumes_map=method_consumes,
            )
            add_task_result = await target_obj.add_task(
                trigger_id=new_trigger_id,
                method_id=method_id,
                parameter_mapping=mapping,
                format_data=True,
            )
            if isinstance(add_task_result, ActionResultError):
                print(f"  - task add failed: method={method_id} ({add_task_result})")
                continue
            print(f"  - task added: method={method_id}")

        set_enabled_result = await target_obj.set_trigger_enabled(
            trigger_id=new_trigger_id,
            enabled=trigger_enabled,
            format_data=True,
        )
        if isinstance(set_enabled_result, ActionResultError):
            print(f"  - set trigger enabled failed: {set_enabled_result}")
        else:
            print(f"  - trigger enabled state set: {trigger_enabled}")


async def _run_schedule_sync(ads: SafeAMPControllerInstance, instances_by_id: dict[str, object], dry_run: bool) -> int:
    master_template, template_group = _find_master_template_instance(instances_by_id=instances_by_id)
    if master_template is None:
        print("Master template not found. Friendly name must match pattern '-TEMPLATE <GROUP>-'.")
        return 5
    if not template_group:
        print("Master template selected but no template group parsed from friendly name.")
        return 5

    template_name = str(getattr(master_template, "instance_name", "<unknown>"))
    template_friendly = str(getattr(master_template, "friendly_name", "<unknown>"))
    template_id = str(getattr(master_template, "instance_id", ""))
    print(f"Master template selected: {template_friendly} ({template_name})")
    print(f"Template group: {template_group} (destinations require '-{template_group}-' in friendly name)")

    if not template_id:
        print("Template instance missing instance_id.")
        return 6

    template_obj = await ads.get_instance(instance_id=template_id, format_data=True)
    if isinstance(template_obj, ActionResultError):
        print(f"Failed to load template instance: {template_obj}")
        return 7
    template_schedule = await template_obj.get_schedule_data(format_data=False)
    if isinstance(template_schedule, ActionResultError) or not isinstance(template_schedule, dict):
        print(f"Failed to query template schedule data: {template_schedule}")
        return 8

    template_populated = list(template_schedule.get("populated_triggers", []) or [])
    print(f"Template populated triggers: {len(template_populated)}")
    for trigger in template_populated:
        tasks = _iter_trigger_tasks(trigger)
        print(f"- {_trigger_summary(trigger)} tasks={len(tasks)}")

    targets = await _get_target_instances(
        ads=ads,
        instances_by_id=instances_by_id,
        master_instance_name=template_name,
        template_group=template_group,
    )
    if not targets:
        print("No target instances found (only template or ADS instances present).")
        return 0

    mode = "DRY RUN" if dry_run else "APPLY"
    run_stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    print(f"\nSchedule sync mode: {mode}")
    print(f"Replication stamp (UTC): {run_stamp}")
    print(f"Target instances: {len(targets)}")

    for idx, target in enumerate(targets):
        await _sync_schedule_for_target(
            target_obj=target,
            template_obj=template_obj,
            template_schedule=template_schedule,
            dry_run=dry_run,
            target_index=idx,
            target_total=len(targets),
            template_name=template_name,
            run_stamp=run_stamp,
        )

    return 0


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Replace target schedules with template schedule.")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Preview deletes/creates without applying schedule changes.",
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

        instances = await ads.get_instances(format_data=True)
        if isinstance(instances, ActionResultError):
            print(f"Instance list query failed: {instances}")
            return 4

        instances_by_id: dict[str, object] = {
            getattr(instance, "instance_id", ""): instance
            for instance in instances
            if getattr(instance, "instance_id", "")
        }
        return await _run_schedule_sync(ads=ads, instances_by_id=instances_by_id, dry_run=args.dry_run)
    finally:
        await ads.__adel__()


if __name__ == "__main__":
    try:
        raise SystemExit(asyncio.run(main()))
    except RuntimeError as exc:
        print(str(exc))
        raise SystemExit(1)
