# Future Improvements

## Reliability / Robustness

### Automatic reconnection in monitor mode

The `monitor` command runs indefinitely but if the BLE connection drops, it
prints "No update received" and eventually hangs. The MQTT command has full
exponential-backoff reconnection logic — `monitor` should have the same.

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
CLI flags for daily users.

### Configurable timeout flag

`--timeout` for `temps` and `connect` — let the user control how long to wait
before giving up. Currently hardcoded to 10 seconds.

### Multi-device support

Run one process that connects to multiple thermometers simultaneously
(separate BleakClient instances). The MQTT command especially would benefit —
one systemd service instead of N. Significant scope expansion but frequently
requested for BBQ setups with multiple thermometers.

---

## Code Quality / Maintainability

### Remove vestigial --poll flag

The `--poll` flag is accepted by argparse but `ThermoproClient.use_polling` is
only used in `poll_temperature()`, which is never called by any command.
Either wire it up or remove the dead code path.

### Deduplicate scan logic

`cmd_scan()` reimplements the same filter+dedup that
`scan_thermopro_devices()` already does. Should call
`scan_thermopro_devices()` directly and only handle the "show all devices"
fallback separately.

### Clean up argparse dispatch

Every command dispatch in `main()` uses `getattr(args, ...)` defensively
because argparse subparsers share a namespace. Using `set_defaults(func=...)`
on each subparser and calling `args.func(**vars(args))` would be cleaner.

### Structured logging

Debug output mixes `print(..., file=sys.stderr)` with a `debug` bool threaded
through every function. A single `logging.getLogger()` with `--debug` setting
the level to DEBUG would be cleaner and let users do `--debug 2>debug.log`
more naturally.

### Dynamic MQTT model name

`publish_discovery()` always reports `"model": "TP25W"` regardless of which
device is actually connected. If the scan already identifies the device name
(e.g. "TP25", "TP920"), that should propagate into the MQTT device info.

---

## UX Polish

### Clean signal handling in monitor

Ctrl+C currently prints "Stopping..." but Python sometimes also dumps a
KeyboardInterrupt traceback depending on where the signal hits the event loop.
A proper signal handler (`loop.add_signal_handler`) would make exit cleaner.

### Formalize exit codes

Exit codes are inconsistent — some paths return None (implicit 0), some
return 0/1. Formalizing exit codes (0=success, 1=connection error, 2=no device
found, etc.) would help scripting.

### Add --version flag

Minor but expected for CLI tools.

### Scan progress feedback

The 3-second auto-scan is silent about progress. A spinner or countdown on
stderr would signal it's working, not hung.

---

## Priority

**High impact, low effort:**
- Remove dead --poll code
- Deduplicate scan logic
- --version flag

**High impact, medium effort:**
- Reconnection in monitor mode
- Temperature alerts
- Config file

**Worth discussing:**
- Multi-device support (significant scope, but common request)
