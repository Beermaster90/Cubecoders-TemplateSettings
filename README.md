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
- Prints per-server aligned vs changed settings
- Apply mode: stops app, applies diff, starts app

Safety checks:

- Halts if template app type and destination app type do not match
  - Uses `display_image_source` (fallback: `module`)

Not copied intentionally:

- `Meta.GenericModule.SessionName`
- `Meta.GenericModule.Map`
- `Meta.GenericModule.CustomMap`

Forced value:

- `GenericModule.App.UseRandomAdminPassword = false`

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
