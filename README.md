# AMP Game Config Sync

This repo contains three scripts using the AMP API wrapper:

- https://github.com/k8thekat/AMPAPI_Python

## Purpose

This toolset is intended for cluster-style game server fleets where one server is the template source and a marked group of destination servers should stay aligned with it.

- `sync_game_settings.py` syncs game settings from template to destination group servers.
- `sync_game_schedules.py` syncs schedules/triggers/tasks from template to destination group servers.
- `backup_retention.py` lists backups on template and destination group servers.

## TL;DR

1. Name template with group marker:
   - `... -TEMPLATE <GROUP>-`
   - Example: `ARK Island -TEMPLATE ARK-`
2. Name destination servers with:
   - `... -<GROUP>-`
   - Example: `ARK Lost Colony -ARK-`
3. Purpose:
   - `sync_game_settings.py`: replicate game settings from template to destination group servers
   - `sync_game_schedules.py`: replicate schedules/triggers/tasks from template to destination group servers
4. Intended for ARK SA cluster setups; may work elsewhere, but verify manually.
5. Run game settings sync:
   - `./run-game-settings.sh --dry-run`
   - `./run-game-settings.sh`
6. Run game schedule sync:
   - `./run-game-schedules.sh --dry-run`
   - `./run-game-schedules.sh`
7. Run backup retention cleanup:
   - `./run-backup-retention.sh`

## Installation

Choose one:

1. Copy files manually from this folder
   - Copy these files to your target API folder:
     - `sync_game_settings.py`
     - `sync_game_schedules.py`
     - `backup_retention.py`
     - `run-game-settings.sh`
     - `run-game-schedules.sh`
     - `run-backup-retention.sh`
     - `README.md`
2. Clone the repo directly
   - `git clone <repo-url>`
   - `cd <repo-folder>`

## Scripts

- `sync_game_settings.py`
- `sync_game_schedules.py`
- `backup_retention.py`

Launchers:

- `./run-game-settings.sh`
- `./run-game-schedules.sh`
- `./run-backup-retention.sh`
- `./run-clear-old-backups-keep-latest.sh`
- `./run-zabbix-amp-status.sh`

## Instance Selection Logic (Both Scripts)

Template discovery and destination targeting are based on **friendly name** markers:

- Template must match: `-TEMPLATE <GROUP>-`
- Destinations must include: `-<GROUP>-`

Example:

- Template: `Main Server -TEMPLATE PVE-`
- Destinations: `... -PVE-`

## Game Settings Sync

Script: `sync_game_settings.py`

Run:

```bash
./run-game-settings.sh --dry-run
./run-game-settings.sh
```

What it does:

- Finds template from `-TEMPLATE <GROUP>-`
- Finds destination instances from `-<GROUP>-`
- Compares template settings group (`arksa:stadiacontroller`) to each destination
- Also includes detected backup-related settings groups for local/cloud backup configuration when present
- Prints per-server aligned vs changed settings
- Apply mode: applies diffs, then restarts only servers with game-setting changes

Safety checks:

- Halts if template app type and destination app type do not match
  - Uses `display_image_source` (fallback: `module`)

Not copied intentionally:

- `Meta.GenericModule.SessionName`
- `Meta.GenericModule.Map`
- `Meta.GenericModule.CustomMap`

Forced value:

- `GenericModule.App.UseRandomAdminPassword = false`

Backup settings notes:

- Backup-related settings are auto-detected from the template setting spec using group/key metadata
- Intended to catch backup configuration exposed in AMP local/cloud tabs
- Dry-run/apply output prints which backup settings groups were included
- Backup-only changes do not trigger a server restart

## Game Schedule Sync

Script: `sync_game_schedules.py`

Run:

```bash
./run-game-schedules.sh --dry-run
./run-game-schedules.sh
```

What it does:

- Uses same template/group marker logic
- Deletes existing populated triggers on each destination
- Recreates template triggers and tasks in order
- Copies full task parameter mappings (messages, waits, backup options, conditions, etc.)
- Keeps event trigger name as AMP-defined (not renameable by API)
- Adds replication stamp to interval trigger descriptions:
  - `... | replicated from <template_instance> <UTC timestamp>`
- Distributes backup trigger minutes across destination servers
- Avoids collision with the template backup minute

Notes:

- No app stop/start is done by schedule sync
- `--dry-run` prints planned operations only

Detailed behavior:

- Template source:
  - Reads template schedule with raw API data (`format_data=False`) so task parameters are not lost.
- Trigger types:
  - Interval triggers are created via `Core/AddIntervalTrigger`.
  - Event triggers are created via `AddEventTrigger`.
- Event trigger tasks:
  - Existing tasks on newly created event triggers are cleared first (prevents task duplication on repeated runs).
- Task parameter keys:
  - Parameter keys are remapped to method-consume names where required so AMP stores values correctly.
  - Example mappings handled: `value_to_check`, `seconds`, `dirty_only`, etc.

Backup scheduling logic:

- Trigger is treated as backup-related if any task method contains `backup`.
- For backup interval triggers:
  - Minute is spread across destination servers evenly over the hour.
  - Template backup minute is excluded from possible destination minutes.
- This reduces concurrent backup load spikes.

Recommended run flow:

1. Run dry-run first:
   - `./run-game-schedules.sh --dry-run`
2. Validate:
   - Destination list
   - Trigger delete/create plan
   - Backup minute plan lines
3. Apply:
   - `./run-game-schedules.sh`
4. Verify in AMP UI:
   - Interval trigger descriptions include replication stamp
   - Event trigger task count matches template
   - Backup trigger minute differs across destination servers

Known limitations:

- Event trigger **description/name** cannot be changed through current AMP API endpoints.
- Therefore, the event trigger `An update is available via SteamCMD` remains AMP-default text.

## Backup Retention

Script: `backup_retention.py`

Run:

```bash
./run-backup-retention.sh   # default: cleanup dry-run
./run-backup-retention.sh cleanup --daily-days 7 --weekly-months 3
./run-backup-retention.sh cleanup --daily-days 7 --weekly-months 3 --apply
```

What it does:

- Uses same template/group marker logic
- Includes template plus destination servers in that group
- Calls `LocalFileBackupPlugin/GetBackups` on each included instance
- `list` mode prints sticky backups by default (`--all-backups` to include non-sticky)
- `cleanup` mode (dry-run by default) applies sticky-only retention:
  - Keep one sticky backup per day for recent days (`--daily-days`, default `7`)
  - Then keep one sticky backup per ISO week for older backups (`--weekly-months`, default `3`)
- Older sticky backups outside retention are deleted only with `--apply`

## Keep Latest Backup Only

Script: `clear_old_backups_keep_latest.py`

Run:

```bash
./run-clear-old-backups-keep-latest.sh
./run-clear-old-backups-keep-latest.sh --apply
```

What it does:

- Uses same template/group marker logic
- Includes the template plus destination servers in that group
- Loads backups from each included instance
- Keeps the newest backup on each instance
- Deletes all older backups only with `--apply`

Notes:

- Default mode is dry-run
- If a backup exists both locally and remotely, the script deletes both copies

## Setup

Create `amp_config.json`:

```json
{
  "url": "http://127.0.0.1:8080",
  "username": "admin",
  "password": "yourpassword"
}
```

Optional env overrides:

- `AMP_URL`
- `AMP_USER`
- `AMP_PASS`

Install deps in venv:

```bash
python3 -m venv .venv
./.venv/bin/python -m pip install --upgrade pip setuptools wheel
./.venv/bin/pip install cc-ampapi
```

## Zabbix Monitoring

Script: `zabbix_amp_status.py`

Launcher:

```bash
./run-zabbix-amp-status.sh discovery
./run-zabbix-amp-status.sh controller-json
./run-zabbix-amp-status.sh instance-json --instance-id <AMP_INSTANCE_ID>
```

What it does:

- Connects to AMP directly with the same `amp_config.json` or `AMP_URL` / `AMP_USER` / `AMP_PASS`
- Returns low-level discovery JSON for ARK non-ADS instances only
- Returns a per-instance JSON payload suitable for Zabbix master item + dependent items

Per-instance JSON fields:

- `instance_running`
- `instance_state`
- `app_running`
- `app_state`
- `instance_stuck`
- `active_users`
- `cpu_percent`
- `memory_percent`
- `uptime`
- `app_status_error`

Recommended Zabbix design:

1. Put `run-zabbix-amp-status.sh` on the Zabbix server/proxy as an external script, or expose it through `UserParameter` on the AMP host.
2. Create an LLD rule using:
   - `run-zabbix-amp-status.sh discovery`
3. For each discovered instance, create one master item:
   - `run-zabbix-amp-status.sh instance-json --instance-id {#AMP.INSTANCE_ID}`
4. Create dependent items from the master item with JSONPath:
   - `$.instance_running`
   - `$.app_running`
   - `$.instance_stuck`
   - `$.active_users`
   - `$.cpu_percent`
   - `$.memory_percent`
   - `$.app_state`
   - `$.uptime`
5. Add triggers such as:
   - Instance stopped: `last(/<template>/amp.instance_running[{#AMP.INSTANCE_ID}])=0`
   - App stopped while instance is up: `last(/<template>/amp.instance_stuck[{#AMP.INSTANCE_ID}])=1`
   - App status query error: `length(last(/<template>/amp.app_status_error[{#AMP.INSTANCE_ID}]))>0`

Notes:

- Using direct AMP API calls is the right approach here.
- The preferred setup is one master JSON item per instance with dependent items, instead of many separate API calls.
- Current discovery is intentionally limited to ARK instances only.
- You can change this in `zabbix_amp_status.py` by setting `ARK_ONLY_DISCOVERY = False`.
- If Zabbix cannot reach AMP over the network, run the script on the AMP host and have the Zabbix agent execute it locally.
