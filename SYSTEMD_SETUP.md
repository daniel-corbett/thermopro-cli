# ThermoPro MQTT Systemd Service Setup

This guide explains how to set up the ThermoPro CLI as a systemd service for continuous operation, ideal for Home Assistant integration.

## Prerequisites

1. Python 3.7+ with virtual environment
2. Bluetooth adapter and permissions
3. MQTT broker (e.g., Mosquitto on Home Assistant)

## Installation Steps

### 1. Install Dependencies

```bash
cd /home/user/src/thermobbq_jadx
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

### 2. Test the CLI

First, test that the CLI works correctly:

```bash
# Test connection
python3 thermopro_cli.py connect --addr E3:5E:A8:FA:2F:2C

# Test MQTT publishing (Ctrl+C to stop)
export MQTT_BROKER=192.168.1.100
export MQTT_USERNAME=homeassistant
export MQTT_PASSWORD=your_password
python3 thermopro_cli.py mqtt --addr E3:5E:A8:FA:2F:2C
```

### 3. Configure Environment File

Create a secure environment file for MQTT credentials:

```bash
# Option A: System-wide configuration
sudo mkdir -p /etc/thermopro
sudo cp mqtt.env.example /etc/thermopro/mqtt.env
sudo nano /etc/thermopro/mqtt.env
sudo chmod 600 /etc/thermopro/mqtt.env
sudo chown user:user /etc/thermopro/mqtt.env

# Option B: User configuration
mkdir -p ~/.config/thermopro
cp mqtt.env.example ~/.config/thermopro/mqtt.env
nano ~/.config/thermopro/mqtt.env
chmod 600 ~/.config/thermopro/mqtt.env
```

Edit the file and set your MQTT broker details:

```bash
MQTT_BROKER=192.168.1.100
MQTT_PORT=1883
MQTT_USERNAME=homeassistant
MQTT_PASSWORD=your_secure_password
```

### 4. Customize the Systemd Service File

Edit `thermopro-mqtt.service` to match your setup:

```bash
nano thermopro-mqtt.service
```

Key settings to customize:

- **User**: Change `user` to your username
- **WorkingDirectory**: Update paths to match your installation
- **ExecStart**: Update paths and device address (`--addr`)
- **EnvironmentFile**: Uncomment and set path to your mqtt.env file

Example customization:

```ini
[Service]
User=your_username
WorkingDirectory=/path/to/thermobbq_jadx
ExecStart=/path/to/venv/bin/python3 /path/to/thermopro_cli.py mqtt --addr E3:5E:A8:FA:2F:2C --interval 30
EnvironmentFile=/etc/thermopro/mqtt.env
```

### 5. Install the Systemd Service

```bash
# Copy service file to systemd directory
sudo cp thermopro-mqtt.service /etc/systemd/system/

# Reload systemd to recognize the new service
sudo systemctl daemon-reload

# Enable the service to start on boot
sudo systemctl enable thermopro-mqtt.service

# Start the service
sudo systemctl start thermopro-mqtt.service
```

### 6. Verify the Service is Running

```bash
# Check service status
sudo systemctl status thermopro-mqtt.service

# View live logs
sudo journalctl -u thermopro-mqtt.service -f

# View recent logs
sudo journalctl -u thermopro-mqtt.service -n 50
```

Expected output:

```
● thermopro-mqtt.service - ThermoPro MQTT Publisher for Home Assistant
     Loaded: loaded (/etc/systemd/system/thermopro-mqtt.service; enabled)
     Active: active (running) since ...
```

## Bluetooth Permissions

If you encounter Bluetooth permission issues, you may need to:

### Option 1: Add user to bluetooth group

```bash
sudo usermod -aG bluetooth user
# Log out and back in for group changes to take effect
```

### Option 2: Configure D-Bus permissions

Create `/etc/dbus-1/system.d/bluetooth.conf`:

```xml
<!DOCTYPE busconfig PUBLIC "-//freedesktop//DTD D-BUS Bus Configuration 1.0//EN"
 "http://www.freedesktop.org/standards/dbus/1.0/busconfig.dtd">
<busconfig>
  <policy user="user">
    <allow send_destination="org.bluez"/>
  </policy>
</busconfig>
```

Then restart D-Bus:

```bash
sudo systemctl restart dbus
```

## Managing the Service

### Start/Stop/Restart

```bash
sudo systemctl start thermopro-mqtt.service
sudo systemctl stop thermopro-mqtt.service
sudo systemctl restart thermopro-mqtt.service
```

### Enable/Disable Auto-Start

```bash
# Enable (start on boot)
sudo systemctl enable thermopro-mqtt.service

# Disable (don't start on boot)
sudo systemctl disable thermopro-mqtt.service
```

### View Logs

```bash
# Live logs
sudo journalctl -u thermopro-mqtt.service -f

# Recent logs (last 100 lines)
sudo journalctl -u thermopro-mqtt.service -n 100

# Logs since boot
sudo journalctl -u thermopro-mqtt.service -b

# Logs for specific date
sudo journalctl -u thermopro-mqtt.service --since "2025-01-01" --until "2025-01-02"
```

## Home Assistant Configuration

Once the service is running, sensors should automatically appear in Home Assistant through MQTT discovery.

### Expected Entities

The following entities will be created automatically:

- `sensor.thermopro_probe1` - Probe 1 temperature
- `sensor.thermopro_probe2` - Probe 2 temperature
- `sensor.thermopro_probe3` - Probe 3 temperature
- `sensor.thermopro_probe4` - Probe 4 temperature
- `sensor.thermopro_battery` - Battery percentage

Disconnected probes will show as "Unavailable" in Home Assistant.

### Manual MQTT Configuration (Optional)

If auto-discovery doesn't work, add to `configuration.yaml`:

```yaml
mqtt:
  sensor:
    - name: "ThermoPro Probe 1"
      state_topic: "homeassistant/sensor/thermopro_probe1/state"
      unit_of_measurement: "°C"
      device_class: "temperature"
      value_template: "{{ value_json.temperature }}"

    - name: "ThermoPro Probe 2"
      state_topic: "homeassistant/sensor/thermopro_probe2/state"
      unit_of_measurement: "°C"
      device_class: "temperature"
      value_template: "{{ value_json.temperature }}"

    - name: "ThermoPro Battery"
      state_topic: "homeassistant/sensor/thermopro_battery/state"
      unit_of_measurement: "%"
      device_class: "battery"
      value_template: "{{ value_json.battery }}"
```

## Troubleshooting

### Service won't start

1. Check service status: `sudo systemctl status thermopro-mqtt.service`
2. Check logs: `sudo journalctl -u thermopro-mqtt.service -n 50`
3. Verify Python path: `which python3` and update ExecStart
4. Verify device address is correct
5. Test manually: `source venv/bin/activate && python3 thermopro_cli.py mqtt --addr E3:5E:A8:FA:2F:2C`

### Bluetooth connection issues

1. Check device is powered on and in range
2. Test connection manually: `python3 thermopro_cli.py connect --addr E3:5E:A8:FA:2F:2C`
3. Check Bluetooth permissions (see Bluetooth Permissions section above)
4. Restart Bluetooth service: `sudo systemctl restart bluetooth`

### MQTT not connecting

1. Verify MQTT broker is running: `mosquitto_sub -h 192.168.1.100 -t '#' -v`
2. Check credentials in mqtt.env
3. Check firewall allows MQTT port (1883)
4. Test MQTT manually with username/password

### No sensors in Home Assistant

1. Check MQTT discovery is enabled in Home Assistant
2. Check MQTT integration is configured
3. Check service logs for "Published update" messages
4. Manually subscribe to topics: `mosquitto_sub -h localhost -t 'homeassistant/sensor/#' -v`
5. Restart Home Assistant after service starts

### Service keeps restarting

1. Check logs: `sudo journalctl -u thermopro-mqtt.service -n 100`
2. Common causes:
   - Wrong device address
   - Device out of range
   - Bluetooth permissions
   - MQTT broker unreachable
   - Python dependencies missing

The service has automatic retry with exponential backoff, so temporary issues should resolve automatically.

## Updating the Service

After making changes to `thermopro_cli.py` or `thermopro-mqtt.service`:

```bash
# If you changed the Python script
sudo systemctl restart thermopro-mqtt.service

# If you changed the service file
sudo cp thermopro-mqtt.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl restart thermopro-mqtt.service
```

## Uninstalling

```bash
# Stop and disable service
sudo systemctl stop thermopro-mqtt.service
sudo systemctl disable thermopro-mqtt.service

# Remove service file
sudo rm /etc/systemd/system/thermopro-mqtt.service
sudo systemctl daemon-reload

# Remove configuration (optional)
sudo rm -rf /etc/thermopro
# or
rm -rf ~/.config/thermopro
```

## Advanced Configuration

### Custom Update Interval

Change `--interval 30` in the ExecStart line to adjust how often temperatures are published (in seconds):

```ini
ExecStart=... mqtt --addr E3:5E:A8:FA:2F:2C --interval 60
```

### Fahrenheit Instead of Celsius

Add `--unit F` to the ExecStart line:

```ini
ExecStart=... mqtt --addr E3:5E:A8:FA:2F:2C --interval 30 --unit F
```

### Debug Logging

Add `--debug` flag for verbose logging:

```ini
ExecStart=... --debug mqtt --addr E3:5E:A8:FA:2F:2C
```

### Custom Device Name

Use `--device-name` to customize the MQTT topic prefix:

```ini
ExecStart=... mqtt --addr E3:5E:A8:FA:2F:2C --device-name kitchen_thermometer
```

This will create topics like `homeassistant/sensor/kitchen_thermometer_probe1/state`.

## Multiple Thermometers

To run multiple thermometers, create separate service files:

```bash
# Copy and customize for each device
sudo cp thermopro-mqtt.service /etc/systemd/system/thermopro-mqtt-kitchen.service
sudo cp thermopro-mqtt.service /etc/systemd/system/thermopro-mqtt-patio.service

# Edit each service file with different:
# - Device address (--addr)
# - Device name (--device-name)

# Enable and start each service
sudo systemctl enable thermopro-mqtt-kitchen.service
sudo systemctl start thermopro-mqtt-kitchen.service

sudo systemctl enable thermopro-mqtt-patio.service
sudo systemctl start thermopro-mqtt-patio.service
```
