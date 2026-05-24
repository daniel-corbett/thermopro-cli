#!/usr/bin/env python3
"""
ThermoPro Bluetooth Thermometer CLI
Reverse-engineered protocol implementation for Linux

Usage:
    thermopro_cli.py [--debug] scan
    thermopro_cli.py [--debug] connect [--addr ADDRESS] [--probe-names NAMES]
    thermopro_cli.py [--debug] temps [--addr ADDRESS] [--json] [--unit F|C] [--probe-names NAMES]
    thermopro_cli.py [--debug] monitor [--addr ADDRESS] [--interval 1] [--json] [--unit F|C] [--probe-names NAMES]
    thermopro_cli.py [--debug] mqtt --addr ADDRESS [--interval 5] [--unit F|C] [--broker HOST] [--probe-names NAMES]

    If --addr is omitted (except for mqtt), the tool auto-scans for ThermoPro
    devices and prompts for selection if multiple are found.

    --probe-names accepts comma-separated labels (e.g. 'Brisket,Ambient,,Ribs').
    Empty slots keep the default 'Probe N' naming.

Environment variables for MQTT:
    MQTT_BROKER   - MQTT broker address
    MQTT_PORT     - MQTT broker port (default: 1883)
    MQTT_USERNAME - MQTT username
    MQTT_PASSWORD - MQTT password
"""

import asyncio
import argparse
import json
import logging
import re
import signal
import sys
import time
import os
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional, List

try:
    from bleak import BleakClient, BleakScanner
except ImportError:
    print("Error: bleak library not installed. Run: pip install bleak")
    sys.exit(1)

try:
    import paho.mqtt.client as mqtt

    MQTT_AVAILABLE = True
except ImportError:
    MQTT_AVAILABLE = False

# BLE UUIDs
SERVICE_UUID = "1086FFF0-3343-4817-8BB2-B32206336CE8"
NOTIFY_UUID = "1086FFF2-3343-4817-8BB2-B32206336CE8"
WRITE_UUID = "1086FFF1-3343-4817-8BB2-B32206336CE8"

# Sentinel values from decode_temperature() that are not real readings
TEMP_SENTINELS = {-999.0, -100.0, 666.0}
MAX_PROBES = 4

log = logging.getLogger("thermopro")


def is_valid_temp(t: float) -> bool:
    return t not in TEMP_SENTINELS


def parse_probe_names(
    raw: Optional[str], max_probes: int = MAX_PROBES
) -> List[Optional[str]]:
    """Parse comma-separated probe names. Empty slots become None."""
    if not raw:
        return [None] * max_probes
    parts = raw.split(",")
    if len(parts) > max_probes:
        print(
            f"Warning: {len(parts)} names provided but max is {max_probes}, "
            f"ignoring extras",
            file=sys.stderr,
        )
        parts = parts[:max_probes]
    names = [(p.strip() or None) for p in parts]
    names.extend([None] * (max_probes - len(names)))
    return names


def get_probe_label(
    probe_names: List[Optional[str]], index: int, compact: bool = False
) -> str:
    """Return display label for a probe — custom name or default."""
    name = probe_names[index] if index < len(probe_names) else None
    if name:
        return name[:8] if compact else name
    return f"P{index+1}" if compact else f"Probe {index+1}"


@dataclass
class RunConfig:
    address: Optional[str] = None
    unit: str = "F"
    interval: int = 1
    as_json: bool = False
    probe_names: List[Optional[str]] = field(
        default_factory=lambda: [None] * MAX_PROBES
    )
    broker: Optional[str] = None
    port: int = 1883
    username: Optional[str] = None
    password: Optional[str] = None
    device_name: str = "thermopro"

    @classmethod
    def from_args(cls, args) -> "RunConfig":
        return cls(
            address=getattr(args, "addr", None),
            unit=(getattr(args, "unit", None) or "F").upper(),
            interval=getattr(args, "interval", 1),
            as_json=getattr(args, "json", False),
            probe_names=parse_probe_names(getattr(args, "probe_names", None)),
            broker=getattr(args, "broker", None) or os.environ.get("MQTT_BROKER"),
            port=getattr(args, "port", 1883),
            username=getattr(args, "username", None) or os.environ.get("MQTT_USERNAME"),
            password=getattr(args, "password", None) or os.environ.get("MQTT_PASSWORD"),
            device_name=getattr(args, "device_name", "thermopro"),
        )


class ThermoproState:
    def __init__(self):
        self.battery = 0
        self.device_unit = "C"
        self.probe_count = 4
        self.temperatures = [-999.0] * MAX_PROBES
        self.last_update = None
        self.connected = False

    def get_temperatures(self, unit: str) -> list:
        """Return temperatures converted to the requested display unit."""
        if self.device_unit == unit:
            return self.temperatures[:]
        temps = []
        for t in self.temperatures:
            if not is_valid_temp(t):
                temps.append(t)
            elif self.device_unit == "C" and unit == "F":
                temps.append(t * 9.0 / 5.0 + 32.0)
            elif self.device_unit == "F" and unit == "C":
                temps.append((t - 32.0) * 5.0 / 9.0)
            else:
                temps.append(t)
        return temps

    def to_dict(self, unit: str):
        temps = self.get_temperatures(unit)
        return {
            "battery": self.battery,
            "unit": unit,
            "probe_count": self.probe_count,
            "temperatures": [round(t, 1) if is_valid_temp(t) else t for t in temps],
            "last_update": self.last_update.isoformat() if self.last_update else None,
            "connected": self.connected,
        }


def decode_temperature(byte1: int, byte2: int) -> float:
    """
    Decode temperature from 2-byte BCD format.
    Based on q/c.java:88-99
    """
    if byte1 == 0xFF and byte2 == 0xFF:
        return -999.0
    if byte1 == 0xDD and byte2 == 0xDD:
        return -100.0
    if byte1 == 0xEE and byte2 == 0xEE:
        return 666.0

    is_negative = (byte1 & 0x80) != 0

    hundreds = ((byte1 & 0x70) // 16) * 100
    tens = (byte1 & 0x0F) * 10
    ones = (byte2 & 0xF0) // 16
    decimal = (byte2 & 0x0F) * 0.1

    temp = hundreds + tens + ones + decimal

    if is_negative:
        temp = -temp

    return temp


def calculate_checksum(data: List[int]) -> int:
    """Calculate checksum for command (sum of all bytes & 0xFF)"""
    return sum(data) & 0xFF


def create_handshake_command() -> bytes:
    """
    Create handshake/init command (0x01).
    Based on p/b.java:124-163

    For TP25W, using a known-good handshake from Android app capture.
    The exact random bytes don't seem critical for the protocol.
    """
    return bytes.fromhex("01098a7a13b73ed68b67c2a0")


def create_timestamp_sync_command() -> bytes:
    """
    Create timestamp sync command (0x28).
    Based on p/b.java:165-169
    """
    timestamp = int(time.time()) - 1577808000
    cmd = [
        0x28,
        0x04,
        (timestamp >> 24) & 0xFF,
        (timestamp >> 16) & 0xFF,
        (timestamp >> 8) & 0xFF,
        timestamp & 0xFF,
    ]
    checksum = calculate_checksum(cmd)
    return bytes(cmd + [checksum])


class ThermoproClient:
    def __init__(self, address: str):
        self.address = address
        self.client: Optional[BleakClient] = None
        self.state = ThermoproState()
        self.notification_event = asyncio.Event()

    def parse_temperature_packet(self, data: bytearray):
        """
        Parse temperature data from command 0x30.
        Handles both TP920 and TP25W formats.
        """
        if len(data) < 6:
            return

        self.state.battery = data[2]
        self.state.device_unit = "C"

        log.debug("0x30 raw: %s", " ".join(f"{b:02X}" for b in data))
        log.debug("byte[3] (unit?): 0x%02X", data[3])

        if data[4] == 0x00:
            self.state.probe_count = 4
            probe_offset = 5
        else:
            self.state.probe_count = data[4]
            probe_offset = 5

        temps = []
        for i in range(min(self.state.probe_count, MAX_PROBES)):
            offset = probe_offset + i * 2
            if offset + 1 < len(data):
                t1 = data[offset]
                t2 = data[offset + 1]
                temp = decode_temperature(t1, t2)
                temps.append(temp)

        while len(temps) < MAX_PROBES:
            temps.append(-999.0)

        self.state.temperatures = temps[:MAX_PROBES]
        self.state.last_update = datetime.now()
        self.notification_event.set()

    async def notification_handler(self, sender, data: bytearray):
        """
        Handle notifications from the device.
        Based on q/c.java protocol parsing
        """
        if len(data) < 2:
            return

        cmd = data[0]

        if cmd == 0x30:
            self.parse_temperature_packet(data)
        elif cmd == 0x01:
            if len(data) >= 4:
                model_code = data[3] if len(data) > 3 else 0
                log.debug("Device model code: 0x%02X", model_code)
            self.notification_event.set()
        elif cmd == 0x41:
            if len(data) >= 3:
                version_byte = data[2]
                version = (version_byte // 16) + ((version_byte % 16) / 10.0)
                log.debug("Device version: %s", version)
            self.notification_event.set()
        elif cmd == 0xE0:
            pass
        else:
            self.notification_event.set()

    async def connect(self) -> bool:
        """Connect to the thermometer and initialize."""
        try:
            log.debug("Connecting to %s...", self.address)
            self.client = BleakClient(self.address)
            await self.client.connect()
            self.state.connected = True
            log.debug("Connected!")

            log.debug("Enabling notifications...")
            await self.client.start_notify(NOTIFY_UUID, self.notification_handler)
            await asyncio.sleep(0.5)

            log.debug("Sending handshake...")
            handshake = create_handshake_command()
            await self.client.write_gatt_char(WRITE_UUID, handshake)

            log.debug("Waiting for temperature data...")
            await asyncio.sleep(2.0)

            log.debug("Initialization complete!")
            return True

        except Exception as e:
            print(f"Connection failed: {e}", file=sys.stderr)
            self.state.connected = False
            return False

    async def disconnect(self):
        """Disconnect from the thermometer"""
        if self.client and self.client.is_connected:
            await self.client.disconnect()
        self.state.connected = False

    async def wait_for_update(self, timeout: float = 5.0) -> bool:
        """Wait for a temperature update via notification"""
        self.notification_event.clear()
        try:
            await asyncio.wait_for(self.notification_event.wait(), timeout)
            return True
        except asyncio.TimeoutError:
            return False


def is_thermopro_device(name: str) -> bool:
    return "thermo" in name.lower() or bool(re.match(r"^TP\d", name))


async def scan_thermopro_devices(timeout: float = 3.0) -> List:
    """Scan for ThermoPro BLE devices, deduplicated by address."""
    print("Scanning for ThermoPro devices...", file=sys.stderr)
    try:
        devices = await BleakScanner.discover(timeout=timeout)
    except Exception as e:
        print(
            f"Error: BLE scan failed. Is Bluetooth enabled?\nDetails: {e}",
            file=sys.stderr,
        )
        sys.exit(1)

    seen = {}
    for d in devices:
        if is_thermopro_device(d.name or "") and d.address not in seen:
            seen[d.address] = d
    return list(seen.values())


async def resolve_address(addr: Optional[str]) -> str:
    """Resolve device address: use provided value or auto-discover via scan."""
    if addr:
        return addr

    thermopro_devices = await scan_thermopro_devices()

    if not thermopro_devices:
        print(
            "Error: No ThermoPro devices found. Specify --addr manually.",
            file=sys.stderr,
        )
        sys.exit(1)

    if len(thermopro_devices) == 1:
        device = thermopro_devices[0]
        print(f"Auto-selected: {device.name} ({device.address})", file=sys.stderr)
        return device.address

    if not sys.stdin.isatty():
        print(
            "Error: Multiple ThermoPro devices found but stdin is not interactive.\n"
            "Specify --addr to select a device:",
            file=sys.stderr,
        )
        for d in thermopro_devices:
            print(f"  {d.name}  ({d.address})", file=sys.stderr)
        sys.exit(1)

    print(f"\nFound {len(thermopro_devices)} ThermoPro devices:\n", file=sys.stderr)
    for i, device in enumerate(thermopro_devices, 1):
        print(f"  [{i}] {device.name}  ({device.address})", file=sys.stderr)
    print(file=sys.stderr)

    while True:
        try:
            sys.stderr.write(f"Select device [1-{len(thermopro_devices)}]: ")
            sys.stderr.flush()
            choice = input()
        except (EOFError, KeyboardInterrupt):
            print("\nAborted.", file=sys.stderr)
            sys.exit(1)

        try:
            index = int(choice) - 1
            if 0 <= index < len(thermopro_devices):
                selected = thermopro_devices[index]
                print(
                    f"Selected: {selected.name} ({selected.address})",
                    file=sys.stderr,
                )
                return selected.address
        except ValueError:
            pass

        print(
            f"Invalid choice. Enter a number between 1 and {len(thermopro_devices)}.",
            file=sys.stderr,
        )


def format_connect_output(
    state: ThermoproState, probe_names: List[Optional[str]], unit: str
) -> str:
    """Multi-line diagnostic output for 'connect' command."""
    lines = [
        "\nReceived temperature update!",
        f"Battery: {state.battery}%",
        f"Unit: {unit}",
        f"Probes: {state.probe_count}",
    ]
    temps = state.get_temperatures(unit)
    for i, temp in enumerate(temps[: state.probe_count]):
        label = get_probe_label(probe_names, i)
        if is_valid_temp(temp):
            lines.append(f"{label}: {temp:.1f}°{unit} (connected)")
        else:
            lines.append(f"{label}: not connected")
    return "\n".join(lines)


def format_temps_output(
    state: ThermoproState, probe_names: List[Optional[str]], unit: str
) -> str:
    """Multi-line output for 'temps' command."""
    lines = [
        f"Battery: {state.battery}%",
        f"Unit: {unit}",
    ]
    temps = state.get_temperatures(unit)
    for i, temp in enumerate(temps[: state.probe_count]):
        label = get_probe_label(probe_names, i)
        if is_valid_temp(temp):
            lines.append(f"{label}: {temp:.1f}°{unit}")
        else:
            lines.append(f"{label}: not connected")
    return "\n".join(lines)


def format_monitor_line(
    state: ThermoproState, probe_names: List[Optional[str]], unit: str
) -> str:
    """Compact one-liner for monitor command."""
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    temps_str = []
    temps = state.get_temperatures(unit)
    for i, temp in enumerate(temps[: state.probe_count]):
        label = get_probe_label(probe_names, i, compact=True)
        if is_valid_temp(temp):
            temps_str.append(f"{label}:{temp:.1f}°{unit}")
        else:
            temps_str.append(f"{label}:---")
    return f"[{timestamp}] Battery:{state.battery:3d}% | {' | '.join(temps_str)}"


def format_json(
    state: ThermoproState,
    probe_names: List[Optional[str]],
    unit: str,
    extra: dict = None,
    compact: bool = False,
) -> str:
    """JSON output with probe_names included."""
    output = state.to_dict(unit)
    output["probe_names"] = [
        get_probe_label(probe_names, i) for i in range(state.probe_count)
    ]
    if extra:
        output.update(extra)
    return json.dumps(output) if compact else json.dumps(output, indent=2)


async def cmd_scan(cfg: RunConfig) -> int:
    """Scan for ThermoPro devices"""
    print("Scanning for Bluetooth devices...", file=sys.stderr)
    try:
        devices = await BleakScanner.discover(timeout=5.0)
    except Exception as e:
        print(
            f"Error: BLE scan failed. Is Bluetooth enabled?\nDetails: {e}",
            file=sys.stderr,
        )
        return 1

    seen = {}
    for d in devices:
        if is_thermopro_device(d.name or "") and d.address not in seen:
            seen[d.address] = d
    thermopro_devices = list(seen.values())

    for device in thermopro_devices:
        print(f"Found: {device.name} ({device.address})")

    if not thermopro_devices:
        print("\nNo ThermoPro devices found.", file=sys.stderr)
        print("Showing all devices:", file=sys.stderr)
        for device in devices:
            print(f"  {device.name or 'Unknown'} ({device.address})")

    return 0 if thermopro_devices else 1


async def cmd_connect(cfg: RunConfig) -> int:
    """Test connection to a device"""
    address = await resolve_address(cfg.address)
    client = ThermoproClient(address)

    try:
        if not await client.connect():
            return 1

        if await client.wait_for_update(timeout=10.0):
            print(format_connect_output(client.state, cfg.probe_names, cfg.unit))
            return 0
        else:
            print("Timeout waiting for temperature data", file=sys.stderr)
            return 1
    finally:
        await client.disconnect()


async def cmd_temps(cfg: RunConfig) -> int:
    """Get current temperatures"""
    address = await resolve_address(cfg.address)
    client = ThermoproClient(address)

    try:
        if not await client.connect():
            return 1

        if await client.wait_for_update(timeout=10.0):
            if cfg.as_json:
                print(format_json(client.state, cfg.probe_names, cfg.unit))
            else:
                print(format_temps_output(client.state, cfg.probe_names, cfg.unit))
            return 0
        else:
            print("Error: Timeout waiting for temperature data", file=sys.stderr)
            return 1
    finally:
        await client.disconnect()


async def connection_loop(address: str):
    """Yields connected ThermoproClient instances. Reconnects on failure with backoff."""
    max_retry = 300
    delay = 5
    failures = 0
    while True:
        client = ThermoproClient(address)
        try:
            if not await client.connect():
                failures += 1
                delay = min(5 * (2 ** (failures - 1)), max_retry)
                log.warning("Connection failed, retry in %ds", delay)
                await asyncio.sleep(delay)
                continue
            failures = 0
            delay = 5
            yield client
        except Exception as e:
            failures += 1
            delay = min(5 * (2 ** (failures - 1)), max_retry)
            log.warning("%s, reconnecting in %ds", e, delay)
            await asyncio.sleep(delay)
        finally:
            try:
                await client.disconnect()
            except Exception:
                pass


async def cmd_monitor(cfg: RunConfig) -> int:
    """Monitor temperatures continuously"""
    address = await resolve_address(cfg.address)

    if not cfg.as_json:
        print("Monitoring temperatures (Ctrl+C to stop)...\n", file=sys.stderr)

    gen = connection_loop(address)
    try:
        async for client in gen:
            while True:
                if await client.wait_for_update(timeout=10.0):
                    if cfg.as_json:
                        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                        print(
                            format_json(
                                client.state,
                                cfg.probe_names,
                                cfg.unit,
                                extra={"timestamp": timestamp},
                                compact=True,
                            )
                        )
                    else:
                        print(
                            format_monitor_line(client.state, cfg.probe_names, cfg.unit)
                        )
                    sys.stdout.flush()
                else:
                    log.warning("No update received, reconnecting...")
                    break
                await asyncio.sleep(cfg.interval)
    except (KeyboardInterrupt, asyncio.CancelledError):
        pass
    finally:
        await gen.aclose()

    return 0


class MQTTPublisher:
    """MQTT publisher for Home Assistant integration"""

    def __init__(
        self,
        broker: str,
        port: int = 1883,
        username: Optional[str] = None,
        password: Optional[str] = None,
        device_name: str = "thermopro",
        discovery_prefix: str = "homeassistant",
    ):
        self.broker = broker
        self.port = port
        self.username = username
        self.password = password
        self.device_name = device_name
        self.discovery_prefix = discovery_prefix
        self.client = mqtt.Client()
        self.connected = False

        if username and password:
            self.client.username_pw_set(username, password)

        self.client.on_connect = self._on_connect
        self.client.on_disconnect = self._on_disconnect

    def _on_connect(self, client, userdata, flags, rc):
        if rc == 0:
            self.connected = True
            print("Connected to MQTT broker", file=sys.stderr)
        else:
            print(f"Failed to connect to MQTT broker: {rc}", file=sys.stderr)

    def _on_disconnect(self, client, userdata, rc):
        self.connected = False
        if rc != 0:
            print("Disconnected from MQTT broker", file=sys.stderr)

    def connect(self):
        """Connect to MQTT broker"""
        try:
            self.client.connect(self.broker, self.port, 60)
            self.client.loop_start()
            for _ in range(50):
                if self.connected:
                    return True
                time.sleep(0.1)
            return False
        except Exception as e:
            print(f"MQTT connection error: {e}", file=sys.stderr)
            return False

    def disconnect(self):
        """Disconnect from MQTT broker"""
        self.client.loop_stop()
        self.client.disconnect()

    def publish_discovery(
        self,
        probe_num: int,
        mac_address: str,
        unit: str = "F",
        probe_name: Optional[str] = None,
    ):
        """Publish Home Assistant MQTT discovery config for a probe"""
        device_id = mac_address.replace(":", "").lower()
        probe_id = f"{self.device_name}_probe{probe_num}"
        display_name = probe_name or f"ThermoPro Probe {probe_num}"

        device_info = {
            "identifiers": [device_id],
            "name": f"ThermoPro {self.device_name.upper()}",
            "model": "TP25W",
            "manufacturer": "ThermoPro",
        }

        temp_config = {
            "name": display_name,
            "unique_id": f"{device_id}_probe{probe_num}_temp",
            "state_topic": f"{self.discovery_prefix}/sensor/{probe_id}/state",
            "unit_of_measurement": f"°{unit}",
            "device_class": "temperature",
            "value_template": "{{ value_json.temperature }}",
            "device": device_info,
        }

        config_topic = f"{self.discovery_prefix}/sensor/{probe_id}/config"
        self.client.publish(config_topic, json.dumps(temp_config), retain=True)

    def publish_battery_discovery(self, mac_address: str):
        """Publish Home Assistant MQTT discovery config for battery"""
        device_id = mac_address.replace(":", "").lower()
        sensor_id = f"{self.device_name}_battery"

        device_info = {
            "identifiers": [device_id],
            "name": f"ThermoPro {self.device_name.upper()}",
            "model": "TP25W",
            "manufacturer": "ThermoPro",
        }

        battery_config = {
            "name": f"ThermoPro Battery",
            "unique_id": f"{device_id}_battery",
            "state_topic": f"{self.discovery_prefix}/sensor/{sensor_id}/state",
            "unit_of_measurement": "%",
            "device_class": "battery",
            "value_template": "{{ value_json.battery }}",
            "device": device_info,
        }

        config_topic = f"{self.discovery_prefix}/sensor/{sensor_id}/config"
        self.client.publish(config_topic, json.dumps(battery_config), retain=True)

    def publish_state(self, probe_num: int, temperature: float, connected: bool = True):
        """Publish temperature state for a probe"""
        probe_id = f"{self.device_name}_probe{probe_num}"
        state_topic = f"{self.discovery_prefix}/sensor/{probe_id}/state"

        if not connected:
            self.client.publish(state_topic, json.dumps({"temperature": None}))
        else:
            state = {
                "temperature": round(temperature, 1),
            }
            self.client.publish(state_topic, json.dumps(state))

    def publish_battery_state(self, battery_percent: int):
        """Publish battery state"""
        sensor_id = f"{self.device_name}_battery"
        state_topic = f"{self.discovery_prefix}/sensor/{sensor_id}/state"

        state = {
            "battery": battery_percent,
        }
        self.client.publish(state_topic, json.dumps(state))


async def cmd_mqtt(cfg: RunConfig) -> int:
    """Monitor temperatures and publish to MQTT for Home Assistant with automatic reconnection"""
    if not MQTT_AVAILABLE:
        print(
            "Error: paho-mqtt library not installed. Run: pip install paho-mqtt",
            file=sys.stderr,
        )
        return 1

    if not cfg.broker:
        print(
            "Error: MQTT broker not specified. Use --broker or set MQTT_BROKER environment variable",
            file=sys.stderr,
        )
        return 1

    mqtt_pub = MQTTPublisher(
        cfg.broker, cfg.port, cfg.username, cfg.password, cfg.device_name
    )
    if not mqtt_pub.connect():
        print("Error: Failed to connect to MQTT broker", file=sys.stderr)
        return 1

    gen = connection_loop(cfg.address)
    try:
        print(
            f"Publishing to MQTT broker {cfg.broker}:{cfg.port} (Ctrl+C to stop)...\n",
            file=sys.stderr,
        )

        async for client in gen:
            log.debug("Publishing Home Assistant discovery configs...")
            for probe_num in range(1, MAX_PROBES + 1):
                name = cfg.probe_names[probe_num - 1]
                mqtt_pub.publish_discovery(
                    probe_num, cfg.address, cfg.unit, probe_name=name
                )
            mqtt_pub.publish_battery_discovery(cfg.address)
            print("Connected to thermometer successfully!", file=sys.stderr)

            while True:
                if await client.wait_for_update(timeout=15.0):
                    temps = client.state.get_temperatures(cfg.unit)
                    for i, temp in enumerate(temps[:MAX_PROBES]):
                        probe_num = i + 1
                        connected = is_valid_temp(temp)
                        mqtt_pub.publish_state(probe_num, temp, connected)
                        log.debug(
                            "Published Probe %d: %s",
                            probe_num,
                            f"{temp:.1f}°{cfg.unit}" if connected else "disconnected",
                        )

                    mqtt_pub.publish_battery_state(client.state.battery)
                    log.debug("Published Battery: %d%%", client.state.battery)

                    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                    print(
                        f"[{timestamp}] Published update - Battery:{client.state.battery}%",
                        file=sys.stderr,
                    )
                else:
                    log.warning("No update from device, reconnecting...")
                    break

                await asyncio.sleep(cfg.interval)

    except (KeyboardInterrupt, asyncio.CancelledError):
        pass
    finally:
        await gen.aclose()
        mqtt_pub.disconnect()

    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="ThermoPro Bluetooth Thermometer CLI",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )

    parser.add_argument("--debug", action="store_true", help="Enable debug output")

    subparsers = parser.add_subparsers(dest="command", help="Command to execute")

    # Scan
    p = subparsers.add_parser("scan", help="Scan for ThermoPro devices")
    p.set_defaults(func=cmd_scan)

    # Connect
    p = subparsers.add_parser("connect", help="Test connection to device")
    p.add_argument("--addr", help="Bluetooth address (auto-scans if not provided)")
    p.add_argument(
        "--probe-names",
        help="Comma-separated probe names (e.g. 'Brisket,Ambient,,Ribs')",
    )
    p.set_defaults(func=cmd_connect)

    # Temps
    p = subparsers.add_parser("temps", help="Get current temperatures")
    p.add_argument("--addr", help="Bluetooth address (auto-scans if not provided)")
    p.add_argument("--json", action="store_true", help="Output as JSON")
    p.add_argument("--unit", choices=["C", "F"], help="Temperature unit")
    p.add_argument(
        "--probe-names",
        help="Comma-separated probe names (e.g. 'Brisket,Ambient,,Ribs')",
    )
    p.set_defaults(func=cmd_temps)

    # Monitor
    p = subparsers.add_parser("monitor", help="Monitor temperatures continuously")
    p.add_argument("--addr", help="Bluetooth address (auto-scans if not provided)")
    p.add_argument("--interval", type=int, default=1, help="Update interval in seconds")
    p.add_argument("--json", action="store_true", help="Output as JSON")
    p.add_argument("--unit", choices=["C", "F"], help="Temperature unit")
    p.add_argument(
        "--probe-names",
        help="Comma-separated probe names (e.g. 'Brisket,Ambient,,Ribs')",
    )
    p.set_defaults(func=cmd_monitor)

    # MQTT
    p = subparsers.add_parser(
        "mqtt", help="Publish temperatures to MQTT for Home Assistant"
    )
    p.add_argument("--addr", required=True, help="Bluetooth address")
    p.add_argument(
        "--interval",
        type=int,
        default=5,
        help="Update interval in seconds (default: 5)",
    )
    p.add_argument("--unit", choices=["C", "F"], help="Temperature unit")
    p.add_argument("--broker", help="MQTT broker address (or set MQTT_BROKER env var)")
    p.add_argument(
        "--port", type=int, default=1883, help="MQTT broker port (default: 1883)"
    )
    p.add_argument("--username", help="MQTT username (or set MQTT_USERNAME env var)")
    p.add_argument("--password", help="MQTT password (or set MQTT_PASSWORD env var)")
    p.add_argument(
        "--device-name",
        default="thermopro",
        help="Device name for MQTT topics (default: thermopro)",
    )
    p.add_argument(
        "--probe-names",
        help="Comma-separated probe names (e.g. 'Brisket,Ambient,,Ribs')",
    )
    p.set_defaults(func=cmd_mqtt)

    return parser


def main():
    parser = build_parser()
    args = parser.parse_args()

    if not args.command:
        parser.print_help()
        return 1

    logging.basicConfig(
        level=logging.DEBUG if args.debug else logging.WARNING,
        format="[%(levelname)s] %(message)s",
        stream=sys.stderr,
    )

    cfg = RunConfig.from_args(args)

    def _sigint_handler(sig, frame):
        print("\nStopping...", file=sys.stderr)
        signal.signal(signal.SIGINT, signal.SIG_DFL)
        raise KeyboardInterrupt

    signal.signal(signal.SIGINT, _sigint_handler)

    try:
        return asyncio.run(args.func(cfg))
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    sys.exit(main())
