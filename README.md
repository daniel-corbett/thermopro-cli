# ThermoPro Bluetooth Thermometer CLI

A Linux command-line tool for reading temperatures from ThermoPro Bluetooth BBQ thermometers. Supports the TP25W model (and potentially TP920/TP960 models).

**Monitor your BBQ from the command line!** 🍖

```bash
$ python3 thermopro_cli.py temps
Scanning for ThermoPro devices...
Auto-selected: Thermopro (E3:5E:A8:FA:2F:2C)
Battery: 90%
Unit: F
Probe 1: 266.2°F
Probe 2: not connected
Probe 3: not connected
Probe 4: not connected
```

## Features

- ✅ **Real-time temperature monitoring** - Get live temperature updates from up to 4 probes
- ✅ **Battery monitoring** - Check remaining battery level
- ✅ **Home Assistant integration** - MQTT auto-discovery for seamless smart home integration
- ✅ **Automatic reconnection** - Handles device disconnections with exponential backoff
- ✅ **Systemd service** - Run continuously as a background service
- ✅ **JSON output** - Easy integration with scripts and monitoring tools
- ✅ **Continuous monitoring** - Stream temperatures with configurable intervals
- ✅ **Celsius/Fahrenheit** - Display temperatures in your preferred unit
- ✅ **Auto-discovery** - Automatically finds your device, no address needed
- ✅ **Clean CLI** - Simple, intuitive command-line interface
- ✅ **No Android required** - Direct Bluetooth communication from Linux

## Supported Devices

**Confirmed working:**
- ThermoPro TP25W (4-probe wireless thermometer)
- ThermoPro TP25

**Should work (untested):**
- ThermoPro TP920
- ThermoPro TP930
- ThermoPro TP960

The protocol was reverse-engineered from the official Android app and tested on real hardware.

## Requirements

- **Linux** with Bluetooth support (BlueZ)
- **Python 3.8+**
- **Bluetooth adapter** (built-in or USB dongle)

### Required Permissions

Your user needs Bluetooth permissions. Either:
- Add your user to the `bluetooth` group: `sudo usermod -a -G bluetooth $USER`
- Or run with appropriate capabilities (not recommended for regular use)

## Installation

```bash
# Clone the repository
git clone https://github.com/yourusername/thermopro-cli.git
cd thermopro-cli

# Create virtual environment
python3 -m venv venv
source venv/bin/activate

# Install dependencies
pip install -r requirements.txt
```

## Quick Start

### 1. Find Your Device

```bash
python3 thermopro_cli.py scan
```

Output:
```
Found: Thermopro (E3:5E:A8:FA:2F:2C)
```

### 2. Get Temperatures

```bash
# Auto-discovers your device — no address needed!
python3 thermopro_cli.py temps

# Or specify the address directly
python3 thermopro_cli.py temps --addr E3:5E:A8:FA:2F:2C
```

### 3. Monitor Continuously

```bash
python3 thermopro_cli.py monitor --interval 2
```

Output:
```
[2025-12-14 10:30:00] Battery: 90% | P1:135.2°F | P2:142.8°F | P3:--- | P4:---
[2025-12-14 10:30:02] Battery: 90% | P1:136.1°F | P2:143.5°F | P3:--- | P4:---
[2025-12-14 10:30:04] Battery: 90% | P1:137.0°F | P2:144.2°F | P3:--- | P4:---
```

Press `Ctrl+C` to stop monitoring.

## Usage

### Commands

#### `scan` - Find ThermoPro devices
```bash
python3 thermopro_cli.py scan
```

Scans for nearby Bluetooth devices and lists any ThermoPro thermometers found.

#### `temps` - Get current temperatures
```bash
python3 thermopro_cli.py temps [--addr <BLUETOOTH_ADDR>] [options]
```

If `--addr` is omitted, the tool auto-scans for ThermoPro devices.

**Options:**
- `--addr ADDR` - Bluetooth address (auto-scans if not provided)
- `--json` - Output as JSON
- `--unit C|F` - Set temperature unit (default: Fahrenheit)

**Examples:**
```bash
# Auto-discover and read
python3 thermopro_cli.py temps

# Specify address directly
python3 thermopro_cli.py temps --addr E3:5E:A8:FA:2F:2C

# JSON output for scripting
python3 thermopro_cli.py temps --json

# Celsius
python3 thermopro_cli.py temps --unit C
```

#### `monitor` - Continuous temperature monitoring
```bash
python3 thermopro_cli.py monitor [--addr <BLUETOOTH_ADDR>] [options]
```

If `--addr` is omitted, the tool auto-scans for ThermoPro devices.

**Options:**
- `--addr ADDR` - Bluetooth address (auto-scans if not provided)
- `--interval N` - Update interval in seconds (default: 1)
- `--json` - Output each reading as JSON
- `--unit C|F` - Temperature unit (default: Fahrenheit)

**Examples:**
```bash
# Monitor with 2-second updates
python3 thermopro_cli.py monitor --interval 2

# Monitor with JSON output for logging
python3 thermopro_cli.py monitor --json >> cook_log.jsonl
```

#### `mqtt` - Publish to MQTT for Home Assistant
```bash
python3 thermopro_cli.py mqtt --addr <BLUETOOTH_ADDR> [options]
```

Continuously publish temperature readings to an MQTT broker with Home Assistant auto-discovery support. The `--addr` flag is **required** for this command since it runs as an unattended service where deterministic device selection is important.

**Options:**
- `--addr ADDR` - Bluetooth address (**required**)
- `--broker HOST` - MQTT broker address (or set `MQTT_BROKER` env var)
- `--port PORT` - MQTT broker port (default: 1883)
- `--username USER` - MQTT username (or set `MQTT_USERNAME` env var)
- `--password PASS` - MQTT password (or set `MQTT_PASSWORD` env var)
- `--interval N` - Update interval in seconds (default: 5)
- `--unit C|F` - Temperature unit (default: Fahrenheit)
- `--device-name NAME` - Custom device name for MQTT topics (default: thermopro)

**Examples:**
```bash
# Basic MQTT publishing (credentials from environment)
export MQTT_BROKER=192.168.1.100
export MQTT_USERNAME=homeassistant
export MQTT_PASSWORD=secret
python3 thermopro_cli.py mqtt --addr E3:5E:A8:FA:2F:2C

# With command-line options
python3 thermopro_cli.py mqtt --addr E3:5E:A8:FA:2F:2C \
  --broker 192.168.1.100 --username homeassistant --password secret

# As systemd service (see SYSTEMD_SETUP.md)
sudo systemctl start thermopro-mqtt.service
```

The MQTT command automatically:
- Publishes Home Assistant auto-discovery configurations
- Creates 5 sensors: 4 probe temperatures + battery level
- Reconnects automatically if connection is lost
- Uses exponential backoff for retry attempts

See [SYSTEMD_SETUP.md](SYSTEMD_SETUP.md) for running as a background service.

#### `connect` - Test connection
```bash
python3 thermopro_cli.py connect [--addr <BLUETOOTH_ADDR>]
```

Tests the connection and displays device information and current temperatures. Auto-scans if `--addr` is omitted.

### JSON Output Format

```json
{
  "battery": 90,
  "unit": "F",
  "probe_count": 4,
  "temperatures": [266.2, -999.0, -999.0, -999.0],
  "last_update": "2025-12-14T10:30:00.123456",
  "connected": true
}
```

**Note:** Temperature value of `-999.0` indicates probe not connected.

## Use Cases

### Log Temperatures to File
```bash
python3 thermopro_cli.py monitor --addr E3:5E:A8:FA:2F:2C --json >> bbq_log.jsonl
```

### Monitor Multiple Metrics
```bash
while true; do
    python3 thermopro_cli.py temps --addr E3:5E:A8:FA:2F:2C --json | \
    jq '{time: now|strftime("%Y-%m-%d %H:%M:%S"), temps: .temperatures}'
    sleep 5
done
```

### Alert on Temperature
```bash
python3 thermopro_cli.py monitor --addr E3:5E:A8:FA:2F:2C --json | \
while read line; do
    temp=$(echo $line | jq -r '.temperatures[0]')
    if (( $(echo "$temp > 65" | bc -l) )); then
        notify-send "BBQ Alert" "Probe 1 reached ${temp}°C!"
    fi
done
```

### Integration with Home Assistant

**Recommended: MQTT Integration with Auto-Discovery**

```bash
# Set up environment variables
export MQTT_BROKER=192.168.1.100
export MQTT_USERNAME=homeassistant
export MQTT_PASSWORD=secret

# Run as systemd service for continuous operation
python3 thermopro_cli.py mqtt --addr E3:5E:A8:FA:2F:2C
```

Sensors will automatically appear in Home Assistant:
- `sensor.thermopro_probe1` through `sensor.thermopro_probe4`
- `sensor.thermopro_battery`

See [SYSTEMD_SETUP.md](SYSTEMD_SETUP.md) for production setup.

**Alternative: Command Line Sensor** (polling method)

```yaml
sensor:
  - platform: command_line
    name: "BBQ Probe 1"
    command: "python3 /path/to/thermopro_cli.py temps --addr E3:5E:A8:FA:2F:2C --json"
    value_template: "{{ value_json.temperatures[0] }}"
    unit_of_measurement: "°C"
    scan_interval: 10
```

## How It Works

This tool communicates directly with the ThermoPro thermometer over Bluetooth Low Energy (BLE). The protocol was reverse-engineered by:

1. **Decompiling the Android APK** using JADX
2. **Analyzing BLE traffic** from the Android app using HCI snoop logs
3. **Identifying the GATT characteristics** and command structure
4. **Replicating the handshake and notification protocol**

### Protocol Overview

The TP25W uses a simple BLE notification-based protocol:

1. Connect to device over BLE
2. Enable notifications on characteristic `FFF2`
3. Send handshake command to characteristic `FFF1`
4. Device responds with handshake acknowledgment
5. Device automatically streams temperature notifications (~1/second)

Temperatures are encoded in a custom BCD (Binary-Coded Decimal) format, identical to the TP920/TP960 models.

For technical details, see [PROTOCOL.md](PROTOCOL.md).

## Troubleshooting

### Device not found during scan
- Ensure the thermometer is powered on
- Make sure Bluetooth is enabled: `bluetoothctl power on`
- Check the thermometer isn't already connected to another device (Android app, etc.)
- Try moving closer to the thermometer

### Connection fails or times out
- The thermometer may be paired to another device - unpair from phone/tablet
- Check Bluetooth permissions (see Requirements section)
- Restart the Bluetooth service: `sudo systemctl restart bluetooth`
- Try disconnecting and reconnecting the probes

### "No temperature data received"
- Ensure at least one probe is properly connected to the thermometer
- The device may need to be reset (remove and reinsert batteries)
- Check that you're using the correct Bluetooth address

### Stale/incorrect readings
- The tool always shows live data from the device
- Make sure probe tips are making good contact with the meat
- Verify probes aren't damaged or shorting

### Permission denied errors
- Add user to bluetooth group: `sudo usermod -a -G bluetooth $USER`
- Log out and back in for group changes to take effect
- Alternatively, run with `sudo` (not recommended)

## Development

### Project Structure

```
.
├── thermopro_cli.py          # Main CLI tool
├── parse_btsnoop.py           # HCI snoop log parser
├── test_exact_sequence.py     # Protocol test script
├── PROTOCOL.md                # Detailed protocol documentation
├── CURRENT_STATUS.md          # Development notes and findings
└── sources/                   # Decompiled APK source code
```

### Running Tests

Test the connection and protocol:
```bash
python3 test_exact_sequence.py E3:5E:A8:FA:2F:2C
```

Analyze BLE captures:
```bash
# Enable HCI snoop on Android, capture traffic, pull log
python3 parse_btsnoop.py btsnoop_hci.log
```

### Contributing

Contributions are welcome! Areas for improvement:

- **Support for more models** (TP901, TP902, etc.)
- **Temperature alerts** with desktop notifications
- **Graphing/visualization** of temperature history
- **Configuration file** for storing device addresses
- **Unit tests** for protocol encoding/decoding

Please open an issue before starting work on major features.

## Technical Documentation

- **[PROTOCOL.md](PROTOCOL.md)** - Complete BLE protocol specification
- **[CURRENT_STATUS.md](CURRENT_STATUS.md)** - Reverse engineering process and findings
- **[sources/](sources/)** - Decompiled Android APK source code (for reference)

## FAQ

**Q: Does this work on macOS or Windows?**
A: Not tested. The `bleak` library supports both platforms, so it should work with minor modifications. The biggest challenge would be Bluetooth permissions/setup.

**Q: Can I connect to multiple thermometers at once?**
A: Not currently, but it would be straightforward to add. Each thermometer needs its own connection instance.

**Q: Does this work with the official app running?**
A: No - the thermometer can only maintain one BLE connection at a time. Close the official app before using this tool.

**Q: Will this drain the thermometer battery faster?**
A: No - the connection uses the same amount of power as the official app. Battery life should be identical.

**Q: Is this affiliated with ThermoPro?**
A: No, this is an independent reverse engineering project. Not endorsed by or affiliated with ThermoPro.

**Q: Can I use this commercially?**
A: This tool is for personal use. See the License section.

## Legal

This project is for **educational and personal use only**. It is the result of reverse engineering the ThermoPro Android application for the purpose of interoperability under fair use provisions.

- **No warranty** - Use at your own risk
- **No official support** - This is not an official ThermoPro product
- **Interoperability** - Created solely for interfacing with legally purchased hardware
- **No redistribution of proprietary code** - The included decompiled sources are for reference only

ThermoPro is a trademark of ThermoPro. This project is not affiliated with, endorsed by, or supported by ThermoPro.

## License

MIT License - See LICENSE file for details.

Copyright (c) 2025

Permission is hereby granted, free of charge, to any person obtaining a copy of this software and associated documentation files (the "Software"), to deal in the Software without restriction, including without limitation the rights to use, copy, modify, merge, publish, distribute, sublicense, and/or sell copies of the Software, and to permit persons to whom the Software is furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY, FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM, OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE SOFTWARE.

## Acknowledgments

- **JADX** - Excellent APK decompilation tool
- **Bleak** - Clean, cross-platform Python BLE library
- **Nordic Semiconductor** - BLE SDK used in the ThermoPro app
- **ThermoPro** - For making hackable Bluetooth thermometers

## Support

If you find this tool useful, please star the repository! ⭐

For bugs and feature requests, please [open an issue](https://github.com/yourusername/thermopro-cli/issues).

---

**Happy grilling!** 🔥🥩
