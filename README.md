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
