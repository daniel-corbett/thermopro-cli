#!/usr/bin/env python3
"""
ThermoPro Bluetooth Thermometer CLI
Reverse-engineered protocol implementation for Linux

Usage:
    thermopro_cli.py [--debug] scan
    thermopro_cli.py [--debug] connect --addr E3:5E:A8:FA:2F:2C
    thermopro_cli.py [--debug] temps --addr E3:5E:A8:FA:2F:2C [--json] [--unit F|C]
    thermopro_cli.py [--debug] monitor --addr E3:5E:A8:FA:2F:2C [--interval 1] [--json] [--unit F|C]
    thermopro_cli.py [--debug] mqtt --addr E3:5E:A8:FA:2F:2C [--interval 5] [--unit F|C] [--broker HOST] [--port 1883]

Environment variables for MQTT:
    MQTT_BROKER   - MQTT broker address
    MQTT_PORT     - MQTT broker port (default: 1883)
    MQTT_USERNAME - MQTT username
    MQTT_PASSWORD - MQTT password
"""

import asyncio
import argparse
import json
import re
import sys
import time
import os
from datetime import datetime
from typing import Optional, List

try:
    from bleak import BleakClient, BleakScanner
except ImportError:
    print("Error: bleak library not installed. Run: pip install bleak")
    sys.exit(1)

# MQTT is optional
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


def is_valid_temp(t: float) -> bool:
    return t not in TEMP_SENTINELS


# Device state
class ThermoproState:
    def __init__(self):
        self.battery = 0
        self.device_unit = "C"  # What the device is actually reporting in
        self.display_unit = "F"  # What the user wants to see
        self.probe_count = 4
        self.temperatures = [-999.0] * 4
        self.last_update = None
        self.connected = False

    def get_display_temperatures(self) -> list:
        """Return temperatures converted to the user's requested display unit."""
        if self.device_unit == self.display_unit:
            return self.temperatures[:]
        temps = []
        for t in self.temperatures:
            if not is_valid_temp(t):
                temps.append(t)
            elif self.device_unit == "C" and self.display_unit == "F":
                temps.append(t * 9.0 / 5.0 + 32.0)
            elif self.device_unit == "F" and self.display_unit == "C":
                temps.append((t - 32.0) * 5.0 / 9.0)
            else:
                temps.append(t)
        return temps

    def to_dict(self):
        temps = self.get_display_temperatures()
        return {
            "battery": self.battery,
            "unit": self.display_unit,
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
    # Special values
    if byte1 == 0xFF and byte2 == 0xFF:
        return -999.0  # No probe connected
    if byte1 == 0xDD and byte2 == 0xDD:
        return -100.0  # Error
    if byte1 == 0xEE and byte2 == 0xEE:
        return 666.0  # Over temperature

    # Check for negative temperature
    is_negative = (byte1 & 0x80) != 0

    # Decode BCD
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
    # Using handshake from btsnoop capture that works
    return bytes.fromhex("01098a7a13b73ed68b67c2a0")


def create_timestamp_sync_command() -> bytes:
    """
    Create timestamp sync command (0x28).
    Based on p/b.java:165-169
    """
    # Unix timestamp minus epoch (Jan 1, 2020)
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


def create_set_unit_command(fahrenheit: bool = False) -> bytes:
    """
    Create command to set temperature unit.
    Based on p/b.java:116-118
    0x0F = Fahrenheit, 0x0C = Celsius
    """
    unit = 0x0F if fahrenheit else 0x0C
    cmd = [0x20, 0x01, unit]
    checksum = calculate_checksum(cmd)
    return bytes(cmd + [checksum])


class ThermoproClient:
    def __init__(self, address: str, use_polling: bool = False, debug: bool = False):
        self.address = address
        self.client: Optional[BleakClient] = None
        self.state = ThermoproState()
        self.notification_event = asyncio.Event()
        self.use_polling = use_polling  # TP25W uses polling instead of notifications
        self.is_tp25w = False  # Detected based on packet format
        self.debug = debug  # Enable debug output

    def parse_temperature_packet(self, data: bytearray):
        """
        Parse temperature data from command 0x30.
        Handles both TP920 and TP25W formats.
        """
        if len(data) < 6:
            return

        self.state.battery = data[2]
        # Device always reports temperatures in Celsius regardless of unit byte
        self.state.device_unit = "C"

        if self.debug:
            hex_dump = " ".join(f"{b:02X}" for b in data)
            print(f"[DEBUG] 0x30 raw: {hex_dump}", file=sys.stderr)
            print(f"[DEBUG] byte[3] (unit?): 0x{data[3]:02X}", file=sys.stderr)

        # Detect TP25W vs TP920 format
        # TP25W has 0x00 at offset 4, and temp data starts at offset 5
        # TP920 has probe_count at offset 4, and temp data starts at offset 5
        if data[4] == 0x00:
            # TP25W format: byte 4 is 0x00, temperatures start at byte 5
            # TP25W always reports 4 probe slots (some may be 0xFF 0xFF for disconnected)
            self.is_tp25w = True
            self.state.probe_count = 4  # TP25W always has 4 slots
            probe_offset = 5
        else:
            # TP920 format: byte 4 is probe count, temperatures start at byte 5
            self.is_tp25w = False
            self.state.probe_count = data[4]
            probe_offset = 5

        # Parse temperature data for each probe
        temps = []
        for i in range(min(self.state.probe_count, 4)):  # Max 4 probes
            offset = probe_offset + i * 2
            if offset + 1 < len(data):
                # Both TP920 and TP25W use same byte order
                t1 = data[offset]
                t2 = data[offset + 1]
                temp = decode_temperature(t1, t2)
                temps.append(temp)

        # Pad to 4 probes
        while len(temps) < 4:
            temps.append(-999.0)

        self.state.temperatures = temps[:4]
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

        # Command 0x30 (48): Live temperature update
        if cmd == 0x30:
            self.parse_temperature_packet(data)

        # Command 0x01: Device info response (after handshake)
        elif cmd == 0x01:
            if len(data) >= 4:
                model_code = data[3] if len(data) > 3 else 0
                if self.debug:
                    print(f"Device model code: 0x{model_code:02X}", file=sys.stderr)
            # Handshake complete - device will start sending temp notifications
            self.notification_event.set()

        # Command 0x41: Version response
        elif cmd == 0x41:
            if len(data) >= 3:
                version_byte = data[2]
                version = (version_byte // 16) + ((version_byte % 16) / 10.0)
                if self.debug:
                    print(f"Device version: {version}", file=sys.stderr)
            self.notification_event.set()

        # Command 0xE0: Acknowledgment
        elif cmd == 0xE0:
            # Just an ack, don't set event
            pass

        # Unknown commands - still set event to prevent hanging
        else:
            self.notification_event.set()

    async def poll_temperature(self) -> bool:
        """Poll temperature data (for TP25W)"""
        if not self.client or not self.client.is_connected:
            return False

        try:
            data = await self.client.read_gatt_char(NOTIFY_UUID)
            if self.debug:
                print(
                    f"[DEBUG] Poll received: cmd=0x{data[0]:02X} len={len(data)}",
                    file=sys.stderr,
                )
            if len(data) > 0 and data[0] == 0x30:
                # Got temperature data
                if self.debug:
                    print(f"[DEBUG] Parsing temperature data", file=sys.stderr)
                self.parse_temperature_packet(bytearray(data))
                return True
            elif len(data) > 0 and data[0] == 0xE0:
                # Command acknowledgment - ignore and return last known data
                # This happens after handshake/time sync
                if self.debug:
                    print(
                        f"[DEBUG] Got 0xE0 ack, returning {self.state.last_update is not None}",
                        file=sys.stderr,
                    )
                return self.state.last_update is not None
            else:
                # Other commands - ignore
                if self.debug:
                    print(
                        f"[DEBUG] Unknown command, returning {self.state.last_update is not None}",
                        file=sys.stderr,
                    )
                return self.state.last_update is not None
        except Exception as e:
            # BLE errors - return last known data if available
            if "ATT error" not in str(e):
                if self.debug:
                    print(f"Poll error: {e}", file=sys.stderr)
            return self.state.last_update is not None
        return False

    async def connect(self, display_unit: str = "F") -> bool:
        """Connect to the thermometer and initialize.

        The device always reports in its native unit (usually Celsius).
        We convert to the requested display_unit in software.
        """
        self.state.display_unit = display_unit
        try:
            if self.debug:
                print(f"Connecting to {self.address}...", file=sys.stderr)
            self.client = BleakClient(self.address)
            await self.client.connect()
            self.state.connected = True
            if self.debug:
                print("Connected!", file=sys.stderr)

            # Always enable notifications first
            if self.debug:
                print("Enabling notifications...", file=sys.stderr)
            await self.client.start_notify(NOTIFY_UUID, self.notification_handler)
            await asyncio.sleep(0.5)

            # Send handshake (required for both TP920 and TP25W)
            if self.debug:
                print("Sending handshake...", file=sys.stderr)
            handshake = create_handshake_command()
            await self.client.write_gatt_char(WRITE_UUID, handshake)

            # Wait for handshake response and first temperature update
            if self.debug:
                print("Waiting for temperature data...", file=sys.stderr)
            await asyncio.sleep(2.0)

            if self.debug:
                print("Initialization complete!", file=sys.stderr)
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

    async def set_unit(self, fahrenheit: bool = False):
        """Set temperature unit (Celsius or Fahrenheit)"""
        if not self.client or not self.client.is_connected:
            return False

        cmd = create_set_unit_command(fahrenheit)
        await self.client.write_gatt_char(WRITE_UUID, cmd)
        await asyncio.sleep(0.2)
        return True

    async def wait_for_update(self, timeout: float = 5.0) -> bool:
        """Wait for a temperature update via notification"""
        # Both TP920 and TP25W use notifications after proper handshake
        self.notification_event.clear()
        try:
            await asyncio.wait_for(self.notification_event.wait(), timeout)
            return True
        except asyncio.TimeoutError:
            return False


async def cmd_scan():
    """Scan for ThermoPro devices"""
    print("Scanning for Bluetooth devices...", file=sys.stderr)
    devices = await BleakScanner.discover(timeout=5.0)

    thermopro_devices = []
    for device in devices:
        name = device.name or ""
        if "thermo" in name.lower() or re.match(r"^TP\d", name):
            thermopro_devices.append(device)
            print(f"Found: {device.name} ({device.address})")

    if not thermopro_devices:
        print("\nNo ThermoPro devices found.", file=sys.stderr)
        print("Showing all devices:", file=sys.stderr)
        for device in devices:
            print(f"  {device.name or 'Unknown'} ({device.address})")

    return 0 if thermopro_devices else 1


async def cmd_connect(address: str, use_polling: bool = False, debug: bool = False):
    """Test connection to a device"""
    client = ThermoproClient(address, use_polling=use_polling, debug=debug)

    try:
        if not await client.connect(display_unit="F"):
            return 1

        if debug:
            print("\nWaiting for temperature data...", file=sys.stderr)
        if await client.wait_for_update(timeout=10.0):
            print("\nReceived temperature update!")
            print(f"Battery: {client.state.battery}%")
            print(f"Unit: {client.state.display_unit}")
            print(f"Probes: {client.state.probe_count}")
            temps = client.state.get_display_temperatures()
            for i, temp in enumerate(temps[: client.state.probe_count]):
                if is_valid_temp(temp):
                    print(f"Probe {i+1}: {temp:.1f}°{client.state.display_unit} (connected)")
                else:
                    print(f"Probe {i+1}: not connected")
            return 0
        else:
            print("Timeout waiting for temperature data", file=sys.stderr)
            return 1
    finally:
        # Always disconnect, even on Ctrl+C or errors
        await client.disconnect()


async def cmd_temps(
    address: str,
    as_json: bool = False,
    unit: Optional[str] = None,
    use_polling: bool = False,
    debug: bool = False,
):
    """Get current temperatures"""
    client = ThermoproClient(address, use_polling=use_polling, debug=debug)
    display_unit = unit.upper() if unit else "F"

    try:
        if not await client.connect(display_unit=display_unit):
            return 1

        # Wait for update
        if await client.wait_for_update(timeout=10.0):
            if as_json:
                print(json.dumps(client.state.to_dict(), indent=2))
            else:
                print(f"Battery: {client.state.battery}%")
                print(f"Unit: {client.state.display_unit}")
                temps = client.state.get_display_temperatures()
                for i, temp in enumerate(temps[: client.state.probe_count]):
                    if is_valid_temp(temp):
                        print(f"Probe {i+1}: {temp:.1f}°{client.state.display_unit}")
                    else:
                        print(f"Probe {i+1}: not connected")
            return 0
        else:
            print("Error: Timeout waiting for temperature data", file=sys.stderr)
            return 1
    finally:
        # Always disconnect, even on Ctrl+C or errors
        await client.disconnect()


async def cmd_monitor(
    address: str,
    interval: int = 1,
    as_json: bool = False,
    unit: Optional[str] = None,
    use_polling: bool = False,
    debug: bool = False,
):
    """Monitor temperatures continuously"""
    client = ThermoproClient(address, use_polling=use_polling, debug=debug)
    display_unit = unit.upper() if unit else "F"

    try:
        if not await client.connect(display_unit=display_unit):
            return 1

        if not as_json:
            print("Monitoring temperatures (Ctrl+C to stop)...\n", file=sys.stderr)

        try:
            while True:
                if await client.wait_for_update(timeout=10.0):
                    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

                    if as_json:
                        output = client.state.to_dict()
                        output["timestamp"] = timestamp
                        print(json.dumps(output))
                        sys.stdout.flush()
                    else:
                        temps_str = []
                        temps = client.state.get_display_temperatures()
                        for i, temp in enumerate(
                            temps[: client.state.probe_count]
                        ):
                            if is_valid_temp(temp):
                                temps_str.append(
                                    f"P{i+1}:{temp:.1f}°{client.state.display_unit}"
                                )
                            else:
                                temps_str.append(f"P{i+1}:---")

                        print(
                            f"[{timestamp}] Battery:{client.state.battery:3d}% | {' | '.join(temps_str)}"
                        )
                        sys.stdout.flush()
                else:
                    print("Warning: No update received", file=sys.stderr)

                await asyncio.sleep(interval)

        except KeyboardInterrupt:
            print("\nStopping...", file=sys.stderr)

        return 0
    finally:
        # Always disconnect, even on Ctrl+C or errors
        await client.disconnect()


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
            # Wait for connection
            for _ in range(50):  # 5 seconds max
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

    def publish_discovery(self, probe_num: int, mac_address: str, unit: str = "F"):
        """Publish Home Assistant MQTT discovery config for a probe"""
        device_id = mac_address.replace(":", "").lower()
        probe_id = f"{self.device_name}_probe{probe_num}"

        # Device info (shared by all sensors)
        device_info = {
            "identifiers": [device_id],
            "name": f"ThermoPro {self.device_name.upper()}",
            "model": "TP25W",
            "manufacturer": "ThermoPro",
        }

        # Temperature sensor config
        temp_config = {
            "name": f"ThermoPro Probe {probe_num}",
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


async def cmd_mqtt(
    address: str,
    interval: int = 5,
    unit: Optional[str] = None,
    debug: bool = False,
    broker: Optional[str] = None,
    port: int = 1883,
    username: Optional[str] = None,
    password: Optional[str] = None,
    device_name: str = "thermopro",
):
    """Monitor temperatures and publish to MQTT for Home Assistant with automatic reconnection"""

    if not MQTT_AVAILABLE:
        print(
            "Error: paho-mqtt library not installed. Run: pip install paho-mqtt",
            file=sys.stderr,
        )
        return 1

    # Get MQTT settings from environment variables if not provided
    broker = broker or os.environ.get("MQTT_BROKER")
    port = port or int(os.environ.get("MQTT_PORT", "1883"))
    username = username or os.environ.get("MQTT_USERNAME")
    password = password or os.environ.get("MQTT_PASSWORD")

    if not broker:
        print(
            "Error: MQTT broker not specified. Use --broker or set MQTT_BROKER environment variable",
            file=sys.stderr,
        )
        return 1

    # Connect to MQTT
    mqtt_client = MQTTPublisher(broker, port, username, password, device_name)
    if not mqtt_client.connect():
        print("Error: Failed to connect to MQTT broker", file=sys.stderr)
        return 1

    # Retry configuration
    max_retry_delay = 300  # 5 minutes max
    retry_delay = 5  # Start with 5 seconds
    consecutive_failures = 0
    last_publish_time = None

    thermo_client = None
    display_unit = unit.upper() if unit else "F"

    try:
        print(
            f"Publishing to MQTT broker {broker}:{port} (Ctrl+C to stop)...\n",
            file=sys.stderr,
        )

        while True:
            try:
                # Connect to thermometer if not connected
                if thermo_client is None or not thermo_client.state.connected:
                    if thermo_client:
                        await thermo_client.disconnect()

                    print(f"Connecting to thermometer {address}...", file=sys.stderr)
                    thermo_client = ThermoproClient(address, debug=debug)

                    if not await thermo_client.connect(display_unit=display_unit):
                        consecutive_failures += 1
                        retry_delay = min(
                            5 * (2 ** (consecutive_failures - 1)), max_retry_delay
                        )
                        print(
                            f"Connection failed. Retrying in {retry_delay} seconds... (attempt {consecutive_failures})",
                            file=sys.stderr,
                        )
                        await asyncio.sleep(retry_delay)
                        continue

                    # Publish discovery configs on (re)connection
                    if debug:
                        print(
                            "Publishing Home Assistant discovery configs...",
                            file=sys.stderr,
                        )
                    for probe_num in range(1, 5):
                        mqtt_client.publish_discovery(probe_num, address, display_unit)
                    mqtt_client.publish_battery_discovery(address)

                    # Reset retry delay on successful connection
                    consecutive_failures = 0
                    retry_delay = 5
                    print(f"Connected to thermometer successfully!", file=sys.stderr)

                # Wait for temperature update
                if await thermo_client.wait_for_update(timeout=15.0):
                    # Publish probe temperatures (converted to display unit)
                    temps = thermo_client.state.get_display_temperatures()
                    for i, temp in enumerate(temps[:4]):
                        probe_num = i + 1
                        connected = is_valid_temp(temp)
                        mqtt_client.publish_state(probe_num, temp, connected)

                        if debug:
                            status = (
                                f"{temp:.1f}°{thermo_client.state.display_unit}"
                                if connected
                                else "disconnected"
                            )
                            print(
                                f"Published Probe {probe_num}: {status}",
                                file=sys.stderr,
                            )

                    # Publish battery
                    mqtt_client.publish_battery_state(thermo_client.state.battery)
                    if debug:
                        print(
                            f"Published Battery: {thermo_client.state.battery}%",
                            file=sys.stderr,
                        )

                    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                    print(
                        f"[{timestamp}] Published update - Battery:{thermo_client.state.battery}%",
                        file=sys.stderr,
                    )
                    last_publish_time = time.time()
                    consecutive_failures = 0  # Reset on successful update
                else:
                    print("Warning: No update received from device", file=sys.stderr)
                    consecutive_failures += 1

                    # If we haven't received data in a while, try reconnecting
                    if last_publish_time and (time.time() - last_publish_time > 60):
                        print(
                            "No data received for 60 seconds, reconnecting...",
                            file=sys.stderr,
                        )
                        thermo_client.state.connected = False
                        continue

                await asyncio.sleep(interval)

            except KeyboardInterrupt:
                print("\nStopping...", file=sys.stderr)
                break
            except Exception as e:
                consecutive_failures += 1
                retry_delay = min(
                    5 * (2 ** (consecutive_failures - 1)), max_retry_delay
                )
                print(
                    f"Error: {e}. Retrying in {retry_delay} seconds...", file=sys.stderr
                )

                if thermo_client:
                    try:
                        await thermo_client.disconnect()
                    except:
                        pass
                    thermo_client = None

                await asyncio.sleep(retry_delay)

        return 0

    finally:
        # Cleanup
        if thermo_client:
            await thermo_client.disconnect()
        mqtt_client.disconnect()


def main():
    parser = argparse.ArgumentParser(
        description="ThermoPro Bluetooth Thermometer CLI",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )

    # Global debug flag
    parser.add_argument("--debug", action="store_true", help="Enable debug output")

    subparsers = parser.add_subparsers(dest="command", help="Command to execute")

    # Scan command
    subparsers.add_parser("scan", help="Scan for ThermoPro devices")

    # Connect command
    parser_connect = subparsers.add_parser("connect", help="Test connection to device")
    parser_connect.add_argument(
        "--addr", required=True, help="Bluetooth address (e.g., E3:5E:A8:FA:2F:2C)"
    )
    parser_connect.add_argument(
        "--poll", action="store_true", help="Use polling mode (for TP25W)"
    )

    # Temps command
    parser_temps = subparsers.add_parser("temps", help="Get current temperatures")
    parser_temps.add_argument("--addr", required=True, help="Bluetooth address")
    parser_temps.add_argument("--json", action="store_true", help="Output as JSON")
    parser_temps.add_argument("--unit", choices=["C", "F"], help="Temperature unit")
    parser_temps.add_argument(
        "--poll", action="store_true", help="Use polling mode (for TP25W)"
    )

    # Monitor command
    parser_monitor = subparsers.add_parser(
        "monitor", help="Monitor temperatures continuously"
    )
    parser_monitor.add_argument("--addr", required=True, help="Bluetooth address")
    parser_monitor.add_argument(
        "--interval", type=int, default=1, help="Update interval in seconds"
    )
    parser_monitor.add_argument("--json", action="store_true", help="Output as JSON")
    parser_monitor.add_argument("--unit", choices=["C", "F"], help="Temperature unit")
    parser_monitor.add_argument(
        "--poll", action="store_true", help="Use polling mode (for TP25W)"
    )

    # MQTT command
    parser_mqtt = subparsers.add_parser(
        "mqtt", help="Publish temperatures to MQTT for Home Assistant"
    )
    parser_mqtt.add_argument("--addr", required=True, help="Bluetooth address")
    parser_mqtt.add_argument(
        "--interval",
        type=int,
        default=5,
        help="Update interval in seconds (default: 5)",
    )
    parser_mqtt.add_argument("--unit", choices=["C", "F"], help="Temperature unit")
    parser_mqtt.add_argument(
        "--broker", help="MQTT broker address (or set MQTT_BROKER env var)"
    )
    parser_mqtt.add_argument(
        "--port", type=int, default=1883, help="MQTT broker port (default: 1883)"
    )
    parser_mqtt.add_argument(
        "--username", help="MQTT username (or set MQTT_USERNAME env var)"
    )
    parser_mqtt.add_argument(
        "--password", help="MQTT password (or set MQTT_PASSWORD env var)"
    )
    parser_mqtt.add_argument(
        "--device-name",
        default="thermopro",
        help="Device name for MQTT topics (default: thermopro)",
    )

    args = parser.parse_args()

    if not args.command:
        parser.print_help()
        return 1

    # Run the appropriate command
    debug = getattr(args, "debug", False)
    if args.command == "scan":
        return asyncio.run(cmd_scan())
    elif args.command == "connect":
        return asyncio.run(cmd_connect(args.addr, getattr(args, "poll", False), debug))
    elif args.command == "temps":
        return asyncio.run(
            cmd_temps(
                args.addr,
                args.json,
                getattr(args, "unit", None),
                getattr(args, "poll", False),
                debug,
            )
        )
    elif args.command == "monitor":
        return asyncio.run(
            cmd_monitor(
                args.addr,
                args.interval,
                args.json,
                getattr(args, "unit", None),
                getattr(args, "poll", False),
                debug,
            )
        )
    elif args.command == "mqtt":
        return asyncio.run(
            cmd_mqtt(
                args.addr,
                args.interval,
                getattr(args, "unit", None),
                debug,
                getattr(args, "broker", None),
                getattr(args, "port", 1883),
                getattr(args, "username", None),
                getattr(args, "password", None),
                getattr(args, "device_name", "thermopro"),
            )
        )

    return 0


if __name__ == "__main__":
    sys.exit(main())
