#!/usr/bin/env python3
import argparse
import asyncio
import json
import os
import re
from datetime import datetime
from pathlib import Path

from ampapi import AMPControllerInstance, APIParams, Bridge
from ampapi.modules import ActionResultError


class SafeAMPControllerInstance(AMPControllerInstance):
    def __init__(self) -> None:
        super().__init__()
        self._tracked_instances: list[object] = []
        self._tracked_instance_ids: set[str] = set()

    def __del__(self) -> None:
        return

    async def get_instance(self, instance_id: str, format_data: bool | None = None) -> object:
        instance_obj = await super().get_instance(instance_id=instance_id, format_data=format_data)
        tracked_id = str(getattr(instance_obj, "instance_id", "")).strip()
        if not isinstance(instance_obj, ActionResultError) and tracked_id and tracked_id not in self._tracked_instance_ids:
            self._tracked_instances.append(instance_obj)
            self._tracked_instance_ids.add(tracked_id)
        return instance_obj

    async def close_all(self) -> None:
        for instance_obj in reversed(self._tracked_instances):
            try:
                await instance_obj.logout()
            except Exception:
                pass
            try:
                await instance_obj.__adel__()
            except Exception:
                pass
        self._tracked_instances.clear()
        self._tracked_instance_ids.clear()
        try:
            await self.logout()
        except Exception:
            pass
        await self.__adel__()


def _extract_template_group(friendly_name: str) -> str | None:
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


async def _discover_group_instances(
    ads: SafeAMPControllerInstance,
    instances_by_id: dict[str, object],
    template_group: str,
    template_instance_name: str,
) -> dict[str, object]:
    print("\nDiscovering instances for backup cleanup:")
    selected: dict[str, object] = {}
    for instance in instances_by_id.values():
        instance_name = getattr(instance, "instance_name", "<unknown>")
        friendly_name = str(getattr(instance, "friendly_name", ""))
        module = getattr(instance, "module", "<unknown>")
        if _is_ads_instance(module=module, instance_name=instance_name):
            continue

        include = False
        if str(instance_name) == template_instance_name:
            include = True
        elif _has_destination_group(friendly_name=friendly_name, group=template_group):
            include = True

        if not include:
            continue

        instance_id = getattr(instance, "instance_id", "")
        instance_obj = await ads.get_instance(instance_id=instance_id, format_data=True)
        if isinstance(instance_obj, ActionResultError):
            print(f"- skip {instance_name}: get_instance failed ({instance_obj})")
            continue

        selected[instance_id] = instance_obj
        print(f"- included: {getattr(instance_obj, 'friendly_name', instance_name)} ({instance_name})")

    return selected


def _format_bytes(size: object) -> str:
    try:
        value = float(size)
    except Exception:
        return str(size)
    units = ["B", "KiB", "MiB", "GiB", "TiB"]
    unit_idx = 0
    while value >= 1024 and unit_idx < len(units) - 1:
        value /= 1024
        unit_idx += 1
    return f"{value:.1f} {units[unit_idx]}"


def _backup_field(backup: object, key: str, default: object = None) -> object:
    if isinstance(backup, dict):
        return backup.get(key, default)
    return getattr(backup, key, default)


def _backup_timestamp_value(backup: object) -> datetime:
    raw = _backup_field(backup, "timestamp")
    if isinstance(raw, datetime):
        return raw
    text = str(raw or "").strip()
    if not text:
        return datetime.min
    if text.startswith("/Date(") and text.endswith(")/"):
        try:
            millis = int(text[6:-2])
            return datetime.fromtimestamp(millis / 1000)
        except Exception:
            return datetime.min
    try:
        return datetime.fromisoformat(text)
    except Exception:
        return datetime.min


async def _delete_backup(instance_obj: object, backup: object) -> list[str]:
    actions: list[str] = []
    backup_id = str(_backup_field(backup, "id", "")).strip()
    if not backup_id:
        return ["missing backup id"]

    if bool(_backup_field(backup, "stored_locally", False)):
        try:
            await instance_obj.delete_local_backup(backup_id=backup_id)
            actions.append("local deleted")
        except Exception as exc:
            actions.append(f"local delete failed ({exc})")

    if bool(_backup_field(backup, "stored_remotely", False)):
        result = await instance_obj.delete_from_s3(backup_id=backup_id, format_data=True)
        if isinstance(result, ActionResultError):
            actions.append(f"remote delete failed ({result})")
        else:
            actions.append("remote deleted")

    if not actions:
        actions.append("no storage flags set")
    return actions


async def _cleanup_instance_backups(instance_obj: object, apply: bool) -> None:
    name = str(getattr(instance_obj, "instance_name", "<unknown>"))
    friendly = str(getattr(instance_obj, "friendly_name", name))

    backups = await instance_obj.get_backups(format_data=False)
    if isinstance(backups, ActionResultError):
        print(f"\n{friendly} ({name})")
        print(f"- failed to load backups: {backups}")
        return

    backup_list = list(backups or [])
    if not backup_list:
        print(f"\n{friendly} ({name})")
        print("- no backups found")
        return

    backup_list.sort(key=_backup_timestamp_value, reverse=True)
    keep = backup_list[0]
    delete_candidates = backup_list[1:]

    print(f"\n{friendly} ({name})")
    print(
        f"- keeping latest: {_backup_field(keep, 'name', '<unknown>')} "
        f"id={_backup_field(keep, 'id', '<unknown>')} "
        f"timestamp={_backup_field(keep, 'timestamp', '<unknown>')} "
        f"size={_format_bytes(_backup_field(keep, 'total_size_bytes', 0))}"
    )

    if not delete_candidates:
        print("- nothing else to delete")
        return

    print(f"- backups to delete: {len(delete_candidates)}")
    for backup in delete_candidates:
        print(
            f"  - delete candidate: {_backup_field(backup, 'name', '<unknown>')} "
            f"id={_backup_field(backup, 'id', '<unknown>')} "
            f"timestamp={_backup_field(backup, 'timestamp', '<unknown>')} "
            f"local={_backup_field(backup, 'stored_locally', '<unknown>')} "
            f"remote={_backup_field(backup, 'stored_remotely', '<unknown>')}"
        )

    if not apply:
        print("- dry run only: no backups deleted")
        return

    for backup in delete_candidates:
        actions = await _delete_backup(instance_obj=instance_obj, backup=backup)
        print(
            f"  - deleted {_backup_field(backup, 'name', '<unknown>')} "
            f"id={_backup_field(backup, 'id', '<unknown>')}: {', '.join(actions)}"
        )


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Delete all but the latest backup for each template-group server.")
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Actually delete older backups. Without this flag the script only prints the plan.",
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
    logged_in = False
    try:
        login_result = await ads.login(amp_user=amp_user, amp_password=amp_pass)
        if isinstance(login_result, ActionResultError):
            print(f"Login failed: {login_result}")
            return 2
        logged_in = True

        instances = await ads.get_instances(format_data=True)
        if isinstance(instances, ActionResultError):
            print(f"Instance list query failed: {instances}")
            return 3

        instances_by_id: dict[str, object] = {
            getattr(instance, "instance_id", ""): instance for instance in instances if getattr(instance, "instance_id", "")
        }
        master_template, template_group = _find_master_template_instance(instances_by_id=instances_by_id)
        if master_template is None:
            print("Master template not found. Friendly name must match pattern '-TEMPLATE <GROUP>-'.")
            return 4
        if not template_group:
            print("Master template selected but no template group parsed from friendly name.")
            return 4

        template_instance_name = str(getattr(master_template, "instance_name", ""))
        template_friendly = str(getattr(master_template, "friendly_name", template_instance_name))
        mode = "APPLY" if args.apply else "DRY RUN"
        print(f"Clear old backups, keep latest only ({mode})")
        print(f"Master template selected: {template_friendly} ({template_instance_name})")
        print(f"Template group: {template_group} (destinations require '-{template_group}-' in friendly name)")

        selected_instances = await _discover_group_instances(
            ads=ads,
            instances_by_id=instances_by_id,
            template_group=template_group,
            template_instance_name=template_instance_name,
        )
        if not selected_instances:
            print("- No matching instances found.")
            return 0

        for instance_obj in selected_instances.values():
            await _cleanup_instance_backups(instance_obj=instance_obj, apply=args.apply)

        return 0
    finally:
        if logged_in:
            await ads.close_all()
        else:
            await ads.__adel__()


if __name__ == "__main__":
    try:
        raise SystemExit(asyncio.run(main()))
    except RuntimeError as exc:
        print(str(exc))
        raise SystemExit(1)
