# Zabbix Monitoring Setup

This repo includes a Zabbix-oriented AMP status collector:

- `zabbix_amp_status.py`
- `run-zabbix-amp-status.sh`

The intended design is:

- Zabbix agent runs the script on the AMP host
- one discovery rule finds ARK AMP instances
- one master item prototype fetches JSON per instance
- dependent item prototypes extract fields from that JSON

This keeps AMP credentials on the AMP host and avoids many duplicate API calls.

## 1. Prerequisites

On the AMP host, make sure these exist and work:

- repo checkout present
- Python virtualenv present
- `amp_config.json` contains valid AMP credentials

Test locally:

```bash
<repo-path>/run-zabbix-amp-status.sh discovery
<repo-path>/run-zabbix-amp-status.sh controller-json
```

The script reads credentials from:

- `amp_config.json`
- or environment variables:
  - `AMP_URL`
  - `AMP_USER`
  - `AMP_PASS`

No passwords are hardcoded in the Python source.

## 2. Zabbix Agent Configuration

Add `UserParameter` entries on the AMP host in your agent config file.

Typical paths:

- `/etc/zabbix/zabbix_agent2.conf`
- `/etc/zabbix/zabbix_agentd.conf`

Add:

```ini
UserParameter=amp.discovery,<repo-path>/run-zabbix-amp-status.sh discovery
UserParameter=amp.controller.json,<repo-path>/run-zabbix-amp-status.sh controller-json
UserParameter=amp.instance.json[*],<repo-path>/run-zabbix-amp-status.sh instance-json --instance-id "$1"
```

Replace `<repo-path>` with the actual installation path of this repo on the AMP host.

Restart the agent after editing:

```bash
sudo systemctl restart zabbix-agent2
```

or:

```bash
sudo systemctl restart zabbix-agent
```

Optional test through the agent:

```bash
zabbix_agent2 -t amp.discovery
zabbix_agent2 -t 'amp.instance.json[de38ba11-2f9c-431d-a4dc-6f5408c90f84]'
```

If using classic agent, replace `zabbix_agent2` with `zabbix_agentd`.

## 3. Host In Zabbix

Create or use one Zabbix host that represents the AMP controller machine.

Go to:

- `Data collection -> Hosts`

That host should use:

- a Zabbix agent interface
- the correct proxy, if you use one

This monitoring is host-based. You do not need a separate Zabbix host for each AMP game instance.

## 4. Discovery Rule

Open the AMP host and create a discovery rule:

- `Data collection -> Hosts -> <AMP host> -> Discovery rules -> Create discovery rule`

Use:

- `Name`: `AMP instance discovery`
- `Type`: `Zabbix agent`
- `Key`: `amp.discovery`
- `Update interval`: `5m`
- `Enabled`: `Yes`

This returns discovery JSON like:

```json
{
  "data": [
    {
      "{#AMP.INSTANCE_ID}": "6c99913e-62d2-4540-b3cc-8b013d370d36",
      "{#AMP.INSTANCE_NAME}": "ARKSurvivalAscended01",
      "{#AMP.FRIENDLY_NAME}": "ARK Lost Colony -ARK-",
      "{#AMP.MODULE}": "GenericModule"
    }
  ]
}
```

Current behavior:

- discovery is intentionally limited to ARK instances only
- non-ARK instances such as Valheim are ignored by this collector

Easy toggle:

- in `zabbix_amp_status.py`, set `ARK_ONLY_DISCOVERY = True` to keep ARK-only monitoring
- set `ARK_ONLY_DISCOVERY = False` to include all non-ADS instances

## 5. Master Item Prototype

Inside `AMP instance discovery`, create this item prototype first:

- `Name`: `AMP instance JSON {#AMP.FRIENDLY_NAME}`
- `Type`: `Zabbix agent`
- `Key`: `amp.instance.json[{#AMP.INSTANCE_ID}]`
- `Type of information`: `Text`
- `Update interval`: `1m`
- `History`: `7d`
- `Trends`: `Do not keep trends`
- `Enabled`: `Yes`

This is the only per-instance item prototype that should call the agent directly.

## 6. Dependent Item Prototypes

After the master item prototype exists, create the following dependent item prototypes under the same discovery rule.

Every item below must use:

- `Type`: `Dependent item`
- `Master item`: `AMP instance JSON {#AMP.FRIENDLY_NAME}`

### AMP instance running

- `Name`: `AMP instance running {#AMP.FRIENDLY_NAME}`
- `Key`: `amp.instance_running[{#AMP.INSTANCE_ID}]`
- `Type of information`: `Numeric (unsigned)`
- `Preprocessing`: `JSONPath` = `$.instance_running`

### AMP app running

- `Name`: `AMP app running {#AMP.FRIENDLY_NAME}`
- `Key`: `amp.app_running[{#AMP.INSTANCE_ID}]`
- `Type of information`: `Numeric (unsigned)`
- `Preprocessing`: `JSONPath` = `$.app_running`

### AMP instance stuck

- `Name`: `AMP instance stuck {#AMP.FRIENDLY_NAME}`
- `Key`: `amp.instance_stuck[{#AMP.INSTANCE_ID}]`
- `Type of information`: `Numeric (unsigned)`
- `Preprocessing`: `JSONPath` = `$.instance_stuck`

### AMP active users

- `Name`: `AMP active users {#AMP.FRIENDLY_NAME}`
- `Key`: `amp.active_users[{#AMP.INSTANCE_ID}]`
- `Type of information`: `Numeric (unsigned)`
- `Preprocessing`: `JSONPath` = `$.active_users`

### AMP CPU percent

- `Name`: `AMP CPU percent {#AMP.FRIENDLY_NAME}`
- `Key`: `amp.cpu_percent[{#AMP.INSTANCE_ID}]`
- `Type of information`: `Numeric (float)`
- `Units`: `%`
- `Preprocessing`: `JSONPath` = `$.cpu_percent`

### AMP memory percent

- `Name`: `AMP memory percent {#AMP.FRIENDLY_NAME}`
- `Key`: `amp.memory_percent[{#AMP.INSTANCE_ID}]`
- `Type of information`: `Numeric (float)`
- `Units`: `%`
- `Preprocessing`: `JSONPath` = `$.memory_percent`

### AMP app state

- `Name`: `AMP app state {#AMP.FRIENDLY_NAME}`
- `Key`: `amp.app_state[{#AMP.INSTANCE_ID}]`
- `Type of information`: `Character`
- `Preprocessing`: `JSONPath` = `$.app_state`

### AMP uptime

- `Name`: `AMP uptime {#AMP.FRIENDLY_NAME}`
- `Key`: `amp.uptime[{#AMP.INSTANCE_ID}]`
- `Type of information`: `Character`
- `Preprocessing`: `JSONPath` = `$.uptime`

### AMP app status error

- `Name`: `AMP app status error {#AMP.FRIENDLY_NAME}`
- `Key`: `amp.app_status_error[{#AMP.INSTANCE_ID}]`
- `Type of information`: `Text`
- `Preprocessing`: `JSONPath` = `$.app_status_error`

## 7. Trigger Prototypes

After items are working, add trigger prototypes under the same discovery rule.

Replace `<HOST NAME>` with the exact Zabbix host name.

### Instance down

- `Name`: `AMP instance down: {#AMP.FRIENDLY_NAME}`
- `Expression`:

```text
last(/<HOST NAME>/amp.instance_running[{#AMP.INSTANCE_ID}])=0
```

Suggested severity:

- `High`

### Instance stuck

- `Name`: `AMP instance stuck: {#AMP.FRIENDLY_NAME}`
- `Expression`:

```text
last(/<HOST NAME>/amp.instance_stuck[{#AMP.INSTANCE_ID}])=1
```

Suggested severity:

- `High`

### App status error

- `Name`: `AMP app status error: {#AMP.FRIENDLY_NAME}`
- `Expression`:

```text
length(last(/<HOST NAME>/amp.app_status_error[{#AMP.INSTANCE_ID}]))>0
```

Suggested severity:

- `Warning`

## 8. Optional Controller-Level Item

You can also add a normal host item for controller-wide status.

Create on the host, not under the instance discovery rule:

- `Name`: `AMP controller JSON`
- `Type`: `Zabbix agent`
- `Key`: `amp.controller.json`
- `Type of information`: `Text`

You may then create dependent host items from it using:

- `$.state`
- `$.uptime`
- `$.active_users`

## 9. How It Works

Flow:

1. Zabbix server asks the AMP host agent for `amp.discovery`
2. The agent runs:

```bash
<repo-path>/run-zabbix-amp-status.sh discovery
```

3. Zabbix receives the discovered AMP instances
4. For each discovered instance, Zabbix asks the agent for:

```text
amp.instance.json[{#AMP.INSTANCE_ID}]
```

5. The agent runs:

```bash
<repo-path>/run-zabbix-amp-status.sh instance-json --instance-id "<instance_id>"
```

6. Zabbix receives one JSON document per instance
7. Dependent items extract individual values with JSONPath

Important:

- only `amp.discovery`, `amp.controller.json`, and `amp.instance.json[*]` are real agent keys
- `amp.app_running`, `amp.cpu_percent`, and the rest are not agent-side keys
- they must be dependent items

## 10. Common Mistakes

### Unknown metric `amp.app_running`

Cause:

- item was created as `Zabbix agent`

Fix:

- change it to `Dependent item`
- set master item to `AMP instance JSON {#AMP.FRIENDLY_NAME}`
- add `JSONPath` preprocessing

### Value of type string is not suitable for numeric item

Cause:

- dependent item received the full JSON blob instead of one field

Fix:

- verify the item is `Dependent item`
- verify `Master item` is set
- verify `Preprocessing -> JSONPath` is correct

### Master item not available in dependent item form

Cause:

- `AMP instance JSON {#AMP.FRIENDLY_NAME}` does not exist yet in the same discovery rule

Fix:

- create the master item prototype first
- save it
- then create dependent item prototypes

## 11. Recommended Verification

After setup:

1. Run `Check now` on `AMP instance discovery`
2. Open `Latest data` for the host
3. Filter with `amp.`
4. Confirm discovered items exist and values are populated

Quick checks:

- `amp.instance.json[...]` should contain JSON text
- `amp.instance_running[...]` should be `0` or `1`
- `amp.app_state[...]` should contain text like `AMPInstanceState.ready`

## 12. Template Or Host?

This guide uses host-level discovery rules and item prototypes because that was the path used during setup.

That means:

- monitoring works immediately on this host
- no template is required for this single host

If later you want reuse across multiple AMP hosts:

- move the same discovery rule, master item prototype, dependent item prototypes, and trigger prototypes into a template
- then link that template to hosts
