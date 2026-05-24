# Future Improvements

## Reliability / Robustness

### Generate handshake bytes properly

`create_handshake_command()` returns a static byte sequence captured from one
Android app session. The decompiled APK generates random bytes for part of the
handshake. If the device ever validates those bytes more strictly (firmware
update, different model), the connection will silently fail.

### Explicit BLE connection timeout

bleak's default connection timeout varies by platform. An explicit timeout
(e.g. 10s) would make failure faster and more predictable, especially on
flaky adapters.

### Replace fixed post-handshake sleep

The 2-second `asyncio.sleep` after handshake is fragile. Could instead wait
for the first 0x30 packet with a timeout — faster on responsive devices, less
likely to miss slow ones.

---

## Feature Additions

### Temperature alerts

`monitor` could accept `--alert-high` / `--alert-low` thresholds and emit a
line (or run a command via `--alert-cmd`) when crossed. Useful for "notify me
when the brisket hits 195F" without piping to a separate script.

### CSV output mode

`--csv` alongside `--json` for easy import into spreadsheets or Grafana.
Header row followed by: timestamp, battery, p1, p2, p3, p4.

### Config file

A `~/.config/thermopro/config.toml` storing device address, preferred unit,
MQTT credentials, probe names, and alert thresholds. Eliminates repetitive
CLI flags for daily users. (Note: .env file now handles MQTT credentials.)

### Configurable timeout flag

`--timeout` for `temps` and `connect` — let the user control how long to wait
before giving up. Currently hardcoded to 10 seconds.

### Multi-device support

Run one process that connects to multiple thermometers simultaneously
(separate BleakClient instances). The MQTT command especially would benefit —
one systemd service instead of N. Significant scope expansion but frequently
requested for BBQ setups with multiple thermometers.

### Dynamic MQTT model name

`publish_discovery()` always reports `"model": "TP25W"` regardless of which
device is actually connected. If the scan already identifies the device name
(e.g. "TP25", "TP920"), that should propagate into the MQTT device info.

---

## UX Polish

### Formalize exit codes

Exit codes are inconsistent — some paths return None (implicit 0), some
return 0/1. Formalizing exit codes (0=success, 1=connection error, 2=no device
found, etc.) would help scripting.

### Scan progress feedback

The 3-second auto-scan is silent about progress. A spinner or countdown on
stderr would signal it's working, not hung.

---

## Priority

**High impact, low effort:**
- Explicit BLE connection timeout
- Configurable --timeout flag
- Dynamic MQTT model name

**High impact, medium effort:**
- Temperature alerts
- Config file

**Worth discussing:**
- Multi-device support (significant scope, but common request)
