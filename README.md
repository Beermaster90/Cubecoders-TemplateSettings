# AMP ARK SA Settings Sync

This script uses the AMP API wrapper from:

- https://github.com/k8thekat/AMPAPI_Python

## TL;DR

1. Set your master ARK instance **friendly name** to include `-template-` (case-insensitive).
2. Run dry-run first:

```bash
./.venv/bin/python update_arkappsettings.py --dry-run
```

3. If output looks good, run apply:

```bash
./.venv/bin/python update_arkappsettings.py
```

## What this script does

`update_arkappsettings.py`:

- Connects to AMP with `cc-ampapi`
- Finds master instance by friendly name containing `-template-`
- Detects ARK SA instances only (`arksa:stadiacontroller`)
- Compares master settings to each ARK target
- Shows per-server report:
  - settings already aligned
  - settings requiring update
- In apply mode: stop target app -> apply diff -> start target app

## Settings intentionally not copied

These remain per-instance and are skipped:

- `Meta.GenericModule.SessionName`
- `Meta.GenericModule.Map`
- `Meta.GenericModule.CustomMap`

## Forced setting

- `GenericModule.App.UseRandomAdminPassword = false`

## Dry-run safety

`--dry-run` is read-only. It does **not**:

- stop applications
- apply settings
- start applications

## Setup from scratch

## 1) Create folder

```bash
mkdir -p amp-arksa-sync
cd amp-arksa-sync
```

## 2) Create venv

```bash
python3 -m venv .venv
```

## 3) Install API package

```bash
./.venv/bin/python -m pip install --upgrade pip setuptools wheel
./.venv/bin/pip install cc-ampapi
```

## 4) Create account config

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

## 5) Run

Dry run:

```bash
./.venv/bin/python update_arkappsettings.py --dry-run
```

Apply:

```bash
./.venv/bin/python update_arkappsettings.py
```
