# 🌩️ Lightning Node (Raspberry Pi + AS3935 + Bluesky)

Lightning detection project for Raspberry Pi using the **AS3935** sensor.
Logs every strike as JSON telemetry, posts storm alerts to Bluesky (cross-checked
against NOAA/NWS storm data), and attaches a storm summary chart when a storm ends.

---

## ⚡ Features

- Real-time lightning strike detection using the AS3935 (I²C)
- Distance reported in **kilometers and miles**
- Structured JSON telemetry to `lightning_telemetry.jsonl` (one line per strike)
- Automatic posting to **Bluesky** using `atproto`, with:
  - a NOAA/NWS "storm plausible?" gate so sensor false alarms don't get posted
  - one "storm detected" post per storm, follow-ups at most every 3 hours, and a
    "conditions have quieted" post after 2 quiet hours
  - rate limits that survive restarts (`posting_state.json`)
  - **dry-run mode by default**: logs what it *would* post
- Optional PNG chart of storm activity (`storm_summary.png`), uploaded with the storm summary
- Optional enrichment from other nodes: temperature/humidity from greenhouse-node,
  rainfall from rain-node (skipped automatically if they're offline)
- CEU-style PASS node identity:
  - Node ID (e.g., `PASS-LN-01`)
  - Region label (e.g., `Greater Harmony Hills`)
  - Channel label (e.g., `Atmospheric Telemetry`)

---

## 🧱 Hardware

- Raspberry Pi (Zero / 3 / 4 / 5)
- AS3935 lightning sensor module (I²C version, address `0x03`)
- Jumper wires
- Optional: E-ink display HAT for a local PASS dashboard

---

## 🔧 Setup

See **[INSTALL.md](INSTALL.md)** for the full setup on current Raspberry Pi OS
(I²C, virtual environment, credentials, NOAA settings, systemd service).

Quick version:

```bash
git clone https://github.com/vcampos2017/lightning-node.git ~/Lightning-Node
cd ~/Lightning-Node
sudo apt install -y python3-venv python3-rpi-lgpio python3-matplotlib python3-smbus2 i2c-tools
python3 -m venv --system-site-packages .venv
source .venv/bin/activate
pip install -r requirements.txt
```

---

## 📶 WiFi keeper

`wifi_keeper.py` keeps the node on the best saved WiFi that really reaches the
internet (the same keeper Chatty uses). Every minute it checks the internet;
after two misses it tries the other saved networks, strongest first, and if
none works it reconnects and, every few rounds, switches WiFi off and on. It
also moves off a weak signal when a saved network is much stronger, leaves WiFi
alone on a working cable, and turns WiFi power saving off. As a last resort,
after 30 minutes offline with the home router not answering either, it restarts
the Pi (at most 3 times a day; never when only the internet is down). The
installer also turns on the Pi's hardware watchdog, so a frozen Pi restarts
itself after about 15 seconds. It works with
NetworkManager (Bookworm/Trixie) and older wpa_supplicant setups, and never
reads or prints passwords.

```bash
bash install_wifi_keeper.sh                 # once: installs node-wifi.service
sudo python3 wifi_keeper.py --status        # saved networks and signals
journalctl -u node-wifi -f                  # what it's doing
```

Settings (all optional) go in `/etc/node-wifi.env`; see the top of `wifi_keeper.py`.
Tests: `python3 tests/test_wifi_keeper.py`.

---

## 🔒 Privacy & Sensitive Data Policy

This repository intentionally excludes private or sensitive information.

Do **not** commit:
- Street addresses or precise physical locations
- Exact latitude/longitude for private locations
- API keys, tokens, or credentials
- Local `.env` files or runtime state containing private data

All location-specific or sensitive values must be supplied at runtime
(e.g., via environment variables or local, git-ignored config files).
See `lightning.env.example`.

Example:

```python
lat = float(os.environ["NOAA_LAT"])
lon = float(os.environ["NOAA_LON"])
```
