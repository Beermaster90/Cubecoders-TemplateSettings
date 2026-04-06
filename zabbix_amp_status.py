#!/usr/bin/env python3
import argparse
import asyncio
import json
import os
from pathlib import Path
from typing import Any

from ampapi import AMPControllerInstance, APIParams, Bridge
from ampapi.modules import ActionResultError

# Easy toggle for Zabbix discovery scope.
# True: include only ARK instances.
# False: include all non-ADS instances.
ARK_ONLY_DISCOVERY = True


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


def _is_ads_instance(instance: object) -> bool:
    module = str(getattr(instance, "module", ""))
    instance_name = str(getattr(instance, "instance_name", ""))
    return module == "ADS" or instance_name.startswith("ADS")


def _is_ark_instance(instance: object) -> bool:
    instance_name = str(getattr(instance, "instance_name", "")).strip().lower()
    friendly_name = str(getattr(instance, "friendly_name", "")).strip().lower()
    module = str(getattr(instance, "module", "")).strip().lower()
    if "-ark-" in friendly_name or "-template ark-" in friendly_name:
        return True
    if "ark" in instance_name:
        return True
    if "ark" in friendly_name:
        return True
    return "ark" in module


def _is_monitorable_instance(instance: object) -> bool:
    if _is_ads_instance(instance):
        return False
    if ARK_ONLY_DISCOVERY and not _is_ark_instance(instance):
        return False
    return True


def _metric_value(metric: object, *attrs: str, default: Any = None) -> Any:
    current = metric
    for attr in attrs:
        if current is None:
            return default
        current = getattr(current, attr, None)
    if current is None:
        return default
    return current


def _num(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return float(default)


def _int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return int(default)


def _state_name(value: Any) -> str:
    text = str(value or "").strip()
    if "." in text:
        text = text.rsplit(".", 1)[-1]
    return text.lower()


def _app_state_is_running(app_status: object | None) -> bool:
    if app_status is None:
        return False
    explicit_running = getattr(app_status, "running", None)
    if explicit_running is not None:
        return bool(explicit_running)
    return _state_name(getattr(app_status, "state", "")) in {"ready", "running"}


async def _connect() -> SafeAMPControllerInstance:
    config = _read_config()
    amp_url = _require_value("url", "AMP_URL", config)
    amp_user = _require_value("username", "AMP_USER", config)
    amp_pass = _require_value("password", "AMP_PASS", config)

    params = APIParams(url=amp_url, user=amp_user, password=amp_pass)
    Bridge(api_params=params)

    ads = SafeAMPControllerInstance()
    login_result = await ads.login(amp_user=amp_user, amp_password=amp_pass)
    if isinstance(login_result, ActionResultError):
        raise RuntimeError(f"Login failed: {login_result}")
    return ads


async def _get_instances(ads: SafeAMPControllerInstance) -> list[object]:
    instances = await ads.get_instances(format_data=True)
    if isinstance(instances, ActionResultError):
        raise RuntimeError(f"Instance list query failed: {instances}")
    return list(instances)


async def _get_instance_by_id(ads: SafeAMPControllerInstance, instance_id: str) -> object:
    instances = await _get_instances(ads)
    meta = next((entry for entry in instances if str(getattr(entry, "instance_id", "")) == instance_id), None)
    if meta is None:
        raise RuntimeError(f"Instance not found: {instance_id}")
    if _is_ads_instance(meta):
        raise RuntimeError(f"Instance is ADS and not monitorable by this check: {instance_id}")
    if not _is_monitorable_instance(meta):
        raise RuntimeError(f"Instance is filtered out and is not monitorable by this check: {instance_id}")
    instance_obj = await ads.get_instance(instance_id=instance_id, format_data=True)
    if isinstance(instance_obj, ActionResultError):
        raise RuntimeError(f"get_instance failed: {instance_obj}")
    return instance_obj


async def _controller_json(ads: SafeAMPControllerInstance) -> dict[str, Any]:
    status = await ads.get_status()
    if isinstance(status, ActionResultError):
        raise RuntimeError(f"Controller status query failed: {status}")
    return {
        "state": str(getattr(status, "state", "")),
        "uptime": str(getattr(status, "uptime", "")),
        "active_users": _int(getattr(status, "active_users", 0), 0),
    }


async def _discovery_json(ads: SafeAMPControllerInstance) -> dict[str, list[dict[str, Any]]]:
    items: list[dict[str, Any]] = []
    instances = await _get_instances(ads)
    for instance in sorted(instances, key=lambda x: str(getattr(x, "instance_name", ""))):
        if not _is_monitorable_instance(instance):
            continue
        items.append(
            {
                "{#AMP.INSTANCE_ID}": str(getattr(instance, "instance_id", "")),
                "{#AMP.INSTANCE_NAME}": str(getattr(instance, "instance_name", "")),
                "{#AMP.FRIENDLY_NAME}": str(getattr(instance, "friendly_name", "")),
                "{#AMP.MODULE}": str(getattr(instance, "module", "")),
            }
        )
    return {"data": items}


async def _instance_json(ads: SafeAMPControllerInstance, instance_id: str) -> dict[str, Any]:
    instance_obj = await _get_instance_by_id(ads=ads, instance_id=instance_id)

    try:
        instance_status = await instance_obj.get_instance_status()
    except Exception as exc:
        raise RuntimeError(f"get_instance_status failed: {exc}") from exc
    if isinstance(instance_status, ActionResultError):
        raise RuntimeError(f"get_instance_status failed: {instance_status}")

    try:
        app_status = await instance_obj.get_application_status(format_data=True)
    except Exception as exc:
        app_error = str(exc)
        app_status = None
    else:
        app_error = None
    if isinstance(app_status, ActionResultError):
        app_error = str(app_status)
        app_status = None

    metrics = getattr(app_status, "metrics", None) if app_status is not None else None
    instance_running = 1 if bool(getattr(instance_status, "running", False)) else 0
    app_running = 1 if _app_state_is_running(app_status) else 0
    app_status_error_flag = 1 if app_error else 0
    instance_stuck = 1 if instance_running == 1 and app_running == 0 and not app_error else 0
    return {
        "instance_id": str(getattr(instance_obj, "instance_id", instance_id)),
        "instance_name": str(getattr(instance_obj, "instance_name", "")),
        "friendly_name": str(getattr(instance_obj, "friendly_name", "")),
        "module": str(getattr(instance_obj, "module", "")),
        "instance_running": instance_running,
        "instance_state": str(getattr(instance_status, "state", "")),
        "app_running": app_running,
        "app_state": str(getattr(app_status, "state", "")) if app_status is not None else "",
        "app_status_error_flag": app_status_error_flag,
        "instance_stuck": instance_stuck,
        "active_users": _int(_metric_value(getattr(metrics, "active_users", None), "raw_value", default=0), 0),
        "cpu_percent": _num(_metric_value(getattr(metrics, "cpu_usage", None), "percent", default=0), 0.0),
        "memory_percent": _num(_metric_value(getattr(metrics, "memory_usage", None), "percent", default=0), 0.0),
        "uptime": str(getattr(app_status, "uptime", "")) if app_status is not None else "",
        "app_status_error": app_error or "",
    }


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="AMP instance/application status checks for Zabbix.")
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("discovery", help="LLD discovery JSON for ARK non-ADS instances.")
    subparsers.add_parser("controller-json", help="Controller-level status JSON.")

    instance_json = subparsers.add_parser("instance-json", help="Per-instance JSON for dependent items.")
    instance_json.add_argument("--instance-id", required=True, help="AMP instance_id.")

    instance_running = subparsers.add_parser("instance_running", help="Print 1 if instance is running, else 0.")
    instance_running.add_argument("--instance-id", required=True, help="AMP instance_id.")

    app_running = subparsers.add_parser("app_running", help="Print 1 if game application is running, else 0.")
    app_running.add_argument("--instance-id", required=True, help="AMP instance_id.")

    stuck = subparsers.add_parser(
        "instance_stuck",
        help="Print 1 if instance is running but application is not running, else 0.",
    )
    stuck.add_argument("--instance-id", required=True, help="AMP instance_id.")

    return parser.parse_args()


async def main() -> int:
    args = _parse_args()
    ads = await _connect()
    try:
        if args.command == "discovery":
            print(json.dumps(await _discovery_json(ads), separators=(",", ":")))
            return 0
        if args.command == "controller-json":
            print(json.dumps(await _controller_json(ads), separators=(",", ":")))
            return 0
        if args.command == "instance-json":
            print(json.dumps(await _instance_json(ads, args.instance_id), separators=(",", ":")))
            return 0
        if args.command == "instance_running":
            payload = await _instance_json(ads, args.instance_id)
            print(payload["instance_running"])
            return 0
        if args.command == "app_running":
            payload = await _instance_json(ads, args.instance_id)
            print(payload["app_running"])
            return 0
        if args.command == "instance_stuck":
            payload = await _instance_json(ads, args.instance_id)
            print(payload["instance_stuck"])
            return 0
        raise RuntimeError(f"Unsupported command: {args.command}")
    finally:
        await ads.__adel__()


if __name__ == "__main__":
    try:
        raise SystemExit(asyncio.run(main()))
    except RuntimeError as exc:
        print(str(exc))
        raise SystemExit(1)
