# Installation Guide

Lightning-Node (Raspberry Pi + AS3935 + Bluesky)

Tested on **Raspberry Pi OS Lite (64-bit), Debian 13 "Trixie"** on a Raspberry Pi 3.
The examples assume the username `pi` and the project folder `~/Lightning-Node`
(the systemd service and scripts expect those paths).

---

## 1. Hardware and wiring

- Raspberry Pi (Zero / 3 / 4 / 5)
- AS3935 lightning sensor module (I²C version)
- Jumper wires (keep them short)

| AS3935 pin | Raspberry Pi pin |
|------------|------------------|
| VIN        | 3.3V (pin 1)     |
| GND        | Ground (pin 6)   |
| SDA        | Pin 3 (GPIO 2)   |
| SCL        | Pin 5 (GPIO 3)   |
| IRQ        | Pin 11 (GPIO 17) |

## 2. Enable I²C

```bash
sudo raspi-config nonint do_i2c 0
sudo reboot
```

Check that the sensor is visible. Use `-a`: the sensor sits at address `0x03`,
which `i2cdetect` skips by default.

```bash
sudo apt install -y i2c-tools
i2cdetect -y -a 1      # expect "03" in the top row
```

If reads fail with `OSError: [Errno 121] Remote I/O error` even though `03`
shows up, check the wiring and slow the I²C bus down:

```bash
echo "dtparam=i2c_arm_baudrate=10000" | sudo tee -a /boot/firmware/config.txt
sudo reboot
```

## 3. Get the code

```bash
sudo apt update
sudo apt install -y git
git clone https://github.com/vcampos2017/lightning-node.git ~/Lightning-Node
```

## 4. Install dependencies

GPIO and matplotlib come from the OS packages; the rest goes into a virtual
environment (current Raspberry Pi OS blocks system-wide `pip install`).

```bash
cd ~/Lightning-Node
sudo apt install -y python3-venv python3-rpi-lgpio python3-matplotlib python3-smbus2
python3 -m venv --system-site-packages .venv
source .venv/bin/activate
pip install -r requirements.txt
```

> Use `python3-rpi-lgpio`, not `python3-rpi.gpio`. Both provide `import RPi.GPIO`,
> but only `rpi-lgpio` works properly with current kernels. Installing one removes the other.

The stock `RPi-AS3935` library no longer needs patching. `lightning_bluesky.py`
includes the fix (explicit block-read length + I²C retries).

## 5. Bluesky credentials

Create `~/.bluesky_credentials.ini` (this exact name; it is read by the script):

```ini
[bluesky]
handle = yourname.bsky.social
app_password = xxxx-xxxx-xxxx-xxxx

[app]
dry_run = true
```

Generate the app password in Bluesky under **Settings → Privacy and security → App passwords**.
Leave `dry_run = true` until you've watched it run. In dry run, the node logs
`[DRY RUN] Would post: ...` instead of posting.

```bash
chmod 600 ~/.bluesky_credentials.ini
```

## 6. Runtime settings (NOAA gate, etc.)

Lightning posts are only published when the NOAA/NWS API shows storm
conditions near you. **With the default `NOAA_REQUIRED=true`, nothing posts
until these are set.** Copy the example and fill it in:

```bash
cp ~/Lightning-Node/lightning.env.example ~/.bluesky_env
nano ~/.bluesky_env
chmod 600 ~/.bluesky_env
```

The systemd service and `start_lightning.sh` both load `~/.bluesky_env`.
Don't commit it; it contains your location.

## 7. Test run

```bash
cd ~/Lightning-Node
source .venv/bin/activate
set -a; source ~/.bluesky_env; set +a
python lightning_bluesky.py
```

At startup it prints a `NOAA gate:` line, or a warning if the NOAA settings are missing.
Press Ctrl+C to stop.

## 8. Run at boot (systemd)

```bash
sudo cp ops/ifttt/lightning-bluesky.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now lightning-bluesky.service
journalctl -u lightning-bluesky -f
```

Optional IFTTT start/stop/failure notifications: see `ops/ifttt/` and
`ops/production-services.md`. Do **not** run the legacy `as3935_irq_daemon.py`
service alongside it; both would read and clear the same sensor interrupt.

## 9. Output files

| File | Description |
|------|-------------|
| `lightning_bluesky.log` | Application log |
| `lightning_telemetry.jsonl` | One JSON line per strike (used by Chatty Node) |
| `posting_state.json` | Rate-limit memory, so restarts don't cause repeat posts |
| `storm_summary.png` | Chart posted at the end of a storm (needs matplotlib) |
