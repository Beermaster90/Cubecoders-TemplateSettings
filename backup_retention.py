#!/usr/bin/env python3
import argparse
import asyncio
import json
import logging
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

from ampapi import AMPControllerInstance, APIParams, Bridge
from ampapi.modules import ActionResultError


logging.getLogger("ampapi").setLevel(logging.CRITICAL)

KEEP_ALL_HOURS = 24


class SafeAMPControllerInstance(AMPControllerInstance):
    # ampapi's __del__ may invoke asyncio.run() during interpreter teardown.
    # We explicitly close sessions in main(), so this no-op avoids warning noise.
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


def _is_instance_unavailable(result: object) -> bool:
    return isinstance(result, ActionResultError) and str(getattr(result, "reason", "")) == "Instance Unavailable"


def _format_action_error(result: object) -> str:
    if _is_instance_unavailable(result):
        return "instance is down or unavailable"
    return str(result)


def _format_bytes(value: object) -> str:
    try:
        size = float(value)
    except (TypeError, ValueError):
        return "<unknown>"

    units = ["B", "KB", "MB", "GB", "TB"]
    idx = 0
    while size >= 1024 and idx < len(units) - 1:
        size /= 1024.0
        idx += 1
    if idx == 0:
        return f"{int(size)} {units[idx]}"
    return f"{size:.2f} {units[idx]}"


def _format_timestamp(value: object) -> str:
    if isinstance(value, datetime):
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc).isoformat()
        return value.astimezone(timezone.utc).isoformat()
    return str(value)


def _parse_timestamp(value: object) -> datetime | None:
    if isinstance(value, datetime):
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)

    text = str(value).strip()
    if not text:
        return None

    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None

    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


async def _get_backups_raw(instance_obj: object) -> list[object] | ActionResultError | None:
    backups = await instance_obj.get_backups(format_data=False)
    if isinstance(backups, ActionResultError):
        return backups
    if not isinstance(backups, list):
        return None
    return backups


async def _collect_backup_instances(
    ads: SafeAMPControllerInstance,
    instances_by_id: dict[str, object],
) -> list[object]:
    instances: list[object] = []
    for meta in sorted(instances_by_id.values(), key=lambda x: str(getattr(x, "instance_name", ""))):
        instance_name = str(getattr(meta, "instance_name", "<unknown>"))
        module = str(getattr(meta, "module", ""))
        if _is_ads_instance(module=module, instance_name=instance_name):
            continue

        friendly_name = str(getattr(meta, "friendly_name", instance_name))
        instance_id = str(getattr(meta, "instance_id", "")).strip()

        if not instance_id:
            print(f"\nInstance: {friendly_name} ({instance_name})")
            print("- missing instance_id; skipping backups")
            continue

        instance_obj = await ads.get_instance(instance_id=instance_id, format_data=True)
        if isinstance(instance_obj, ActionResultError):
            print(f"\nInstance: {friendly_name} ({instance_name})")
            print(f"- {_format_action_error(instance_obj)}; skipping backups")
            continue
        instances.append(instance_obj)

    return instances


def _backup_field(item: object, key: str, default: object = "") -> object:
    if isinstance(item, dict):
        return item.get(key, default)
    return getattr(item, key, default)


async def _print_backups_for_instance(instance_obj: object, sticky_only: bool) -> None:
    instance_name = str(getattr(instance_obj, "instance_name", "<unknown>"))
    friendly_name = str(getattr(instance_obj, "friendly_name", instance_name))
    print(f"\nInstance: {friendly_name} ({instance_name})")

    backups = await _get_backups_raw(instance_obj)
    if isinstance(backups, ActionResultError):
        if _is_instance_unavailable(backups):
            print("- instance is down or unavailable; skipping backups")
        else:
            print(f"- backup query failed: {backups}")
        return
    if backups is None:
        print("- unexpected backup response format")
        return

    if sticky_only:
        backups = [b for b in backups if _is_sticky_backup(b)]

    mode = "sticky backups" if sticky_only else "backups"
    print(f"- {mode} found: {len(backups)}")
    if not backups:
        return

    sorted_backups = sorted(backups, key=lambda b: str(_backup_field(b, "timestamp", "")), reverse=True)
    for backup in sorted_backups:
        backup_id = str(_backup_field(backup, "id", "<unknown>"))
        name = str(_backup_field(backup, "name", "<unnamed>"))
        timestamp = _format_timestamp(_backup_field(backup, "timestamp", "<unknown>"))
        sticky = bool(_backup_field(backup, "sticky", False))
        auto = bool(_backup_field(backup, "created_automatically", False))
        local = bool(_backup_field(backup, "stored_locally", False))
        remote = bool(_backup_field(backup, "stored_remotely", False))
        size = _format_bytes(_backup_field(backup, "total_size_bytes", 0))
        taken_by = str(_backup_field(backup, "taken_by", "<unknown>"))
        description = str(_backup_field(backup, "description", "") or "")

        print(
            f"- {name} | id={backup_id} | timestamp={timestamp} | size={size} "
            f"| sticky={sticky} | auto={auto} | local={local} | remote={remote} | by={taken_by}"
        )
        if description:
            print(f"  description={description}")


def _is_sticky_backup(backup: object) -> bool:
    return bool(_backup_field(backup, "sticky", False))


def _select_retention_keep_ids(
    backups: list[object],
    daily_days: int,
    weekly_months: int,
    now_utc: datetime,
) -> tuple[set[str], set[str], list[str]]:
    keep_ids: set[str] = set()
    delete_ids: set[str] = set()
    warnings: list[str] = []

    keep_all_cutoff = now_utc - timedelta(hours=KEEP_ALL_HOURS)
    daily_cutoff = keep_all_cutoff - timedelta(days=daily_days)
    weekly_cutoff = now_utc - timedelta(days=weekly_months * 30)

    daily_seen: set[str] = set()
    weekly_seen: set[str] = set()

    with_ts: list[tuple[datetime, object]] = []
    without_ts: list[object] = []
    for backup in backups:
        ts = _parse_timestamp(_backup_field(backup, "timestamp", ""))
        if ts is None:
            without_ts.append(backup)
            continue
        with_ts.append((ts, backup))

    with_ts.sort(key=lambda x: x[0], reverse=True)

    for ts, backup in with_ts:
        backup_id = str(_backup_field(backup, "id", "")).strip()
        if not backup_id:
            continue

        if ts >= keep_all_cutoff:
            keep_ids.add(backup_id)
            continue

        if ts >= daily_cutoff:
            day_key = ts.date().isoformat()
            if day_key not in daily_seen:
                daily_seen.add(day_key)
                keep_ids.add(backup_id)
            else:
                delete_ids.add(backup_id)
            continue

        if ts >= weekly_cutoff:
            iso_year, iso_week, _ = ts.isocalendar()
            week_key = f"{iso_year}-W{iso_week:02d}"
            if week_key not in weekly_seen:
                weekly_seen.add(week_key)
                keep_ids.add(backup_id)
            else:
                delete_ids.add(backup_id)
            continue

        delete_ids.add(backup_id)

    for backup in without_ts:
        backup_id = str(_backup_field(backup, "id", "<unknown>"))
        name = str(_backup_field(backup, "name", "<unnamed>"))
        warnings.append(f"backup kept due to invalid timestamp: {name} id={backup_id}")
        if backup_id != "<unknown>":
            keep_ids.add(backup_id)

    delete_ids = {bid for bid in delete_ids if bid not in keep_ids}
    return keep_ids, delete_ids, warnings


async def _delete_backup(instance_obj: object, backup: object) -> list[str]:
    messages: list[str] = []
    backup_id = str(_backup_field(backup, "id", "")).strip()
    if not backup_id:
        return ["skip delete: missing backup id"]

    stored_locally = bool(_backup_field(backup, "stored_locally", False))
    stored_remotely = bool(_backup_field(backup, "stored_remotely", False))

    if stored_locally or not stored_remotely:
        try:
            await instance_obj.delete_local_backup(backup_id=backup_id)
            messages.append("local deleted")
        except Exception as exc:
            messages.append(f"local delete failed ({exc})")

    if stored_remotely:
        res = await instance_obj.delete_from_s3(backup_id=backup_id, format_data=True)
        if isinstance(res, ActionResultError):
            messages.append(f"s3 delete failed ({res})")
        else:
            messages.append("s3 deleted")

    return messages


async def _cleanup_backups_for_instance(
    instance_obj: object,
    apply: bool,
    daily_days: int,
    weekly_months: int,
) -> None:
    instance_name = str(getattr(instance_obj, "instance_name", "<unknown>"))
    friendly_name = str(getattr(instance_obj, "friendly_name", instance_name))
    print(f"\nInstance: {friendly_name} ({instance_name})")

    backups = await _get_backups_raw(instance_obj)
    if isinstance(backups, ActionResultError):
        if _is_instance_unavailable(backups):
            print("- instance is down or unavailable; skipping backups")
        else:
            print(f"- backup query failed: {backups}")
        return
    if backups is None:
        print("- unexpected backup response format")
        return

    print(f"- backups found: {len(backups)}")
    if not backups:
        return

    keep_ids, delete_ids, warnings = _select_retention_keep_ids(
        backups=backups,
        daily_days=daily_days,
        weekly_months=weekly_months,
        now_utc=datetime.now(timezone.utc),
    )

    print(f"- retention keep: {len(keep_ids)}")
    print(f"- retention delete: {len(delete_ids)}")
    for warning in warnings:
        print(f"- warning: {warning}")

    if not delete_ids:
        return

    sorted_backups = sorted(backups, key=lambda b: str(_backup_field(b, "timestamp", "")), reverse=True)
    for backup in sorted_backups:
        backup_id = str(_backup_field(backup, "id", "")).strip()
        if backup_id not in delete_ids:
            continue

        name = str(_backup_field(backup, "name", "<unnamed>"))
        timestamp = _format_timestamp(_backup_field(backup, "timestamp", "<unknown>"))
        if not apply:
            print(f"- dry-run delete: {name} | id={backup_id} | timestamp={timestamp}")
            continue

        results = await _delete_backup(instance_obj=instance_obj, backup=backup)
        print(f"- deleted: {name} | id={backup_id} | timestamp={timestamp} | {'; '.join(results)}")


async def _run_list_backups(
    ads: SafeAMPControllerInstance,
    instances_by_id: dict[str, object],
    sticky_only: bool,
) -> int:
    scoped_instances = await _collect_backup_instances(
        ads=ads,
        instances_by_id=instances_by_id,
    )
    print(f"Scoped instances: {len(scoped_instances)} (all non-ADS instances)")
    if not scoped_instances:
        print("- no instances found")
        return 0

    for instance_obj in scoped_instances:
        await _print_backups_for_instance(instance_obj, sticky_only=sticky_only)

    return 0


async def _run_cleanup_backups(
    ads: SafeAMPControllerInstance,
    instances_by_id: dict[str, object],
    apply: bool,
    daily_days: int,
    weekly_months: int,
) -> int:
    mode = "APPLY" if apply else "DRY RUN"

    print(f"Cleanup mode: {mode}")
    print(
        f"Retention policy: keep all backups for {KEEP_ALL_HOURS}h, then 1/day for "
        f"{daily_days} days, then 1/week for {weekly_months} months."
    )

    scoped_instances = await _collect_backup_instances(
        ads=ads,
        instances_by_id=instances_by_id,
    )
    print(f"Scoped instances: {len(scoped_instances)} (all non-ADS instances)")
    if not scoped_instances:
        print("- no instances found")
        return 0

    for instance_obj in scoped_instances:
        await _cleanup_backups_for_instance(
            instance_obj=instance_obj,
            apply=apply,
            daily_days=daily_days,
            weekly_months=weekly_months,
        )

    return 0


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Manage AMP backups for all non-ADS instances."
    )
    parser.add_argument(
        "action",
        nargs="?",
        default="cleanup",
        choices=["list", "cleanup"],
        help="Backup action to run",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Apply destructive changes for cleanup. Without this flag cleanup is dry-run.",
    )
    parser.add_argument(
        "--daily-days",
        type=int,
        default=7,
        help="Retention: after the 24h keep-all window, keep one backup per day for this many days (default: 7).",
    )
    parser.add_argument(
        "--weekly-months",
        type=int,
        default=3,
        help="Retention: then keep one backup per ISO week this many months back (default: 3).",
    )
    parser.add_argument(
        "--sticky-only",
        action="store_true",
        help="List action: show only sticky backups. Cleanup always considers all backups.",
    )
    parser.add_argument(
        "--all-backups",
        action="store_true",
        help=argparse.SUPPRESS,
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

        if args.action == "list":
            return await _run_list_backups(
                ads=ads,
                instances_by_id=instances_by_id,
                sticky_only=args.sticky_only and not args.all_backups,
            )

        if args.action == "cleanup":
            if args.daily_days < 1:
                print("--daily-days must be >= 1")
                return 10
            if args.weekly_months < 1:
                print("--weekly-months must be >= 1")
                return 10
            return await _run_cleanup_backups(
                ads=ads,
                instances_by_id=instances_by_id,
                apply=args.apply,
                daily_days=args.daily_days,
                weekly_months=args.weekly_months,
            )

        print(f"Unsupported action: {args.action}")
        return 9
    finally:
        await ads.close_all()


if __name__ == "__main__":
    try:
        raise SystemExit(asyncio.run(main()))
    except RuntimeError as exc:
        print(str(exc))
        raise SystemExit(1)
