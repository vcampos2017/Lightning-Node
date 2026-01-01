## Privacy & Sensitive Data Policy

This repository intentionally excludes private or sensitive information.

Do **not** commit:
- Street addresses or precise physical locations
- Exact latitude/longitude for private locations
- API keys, tokens, or credentials
- Local `.env` files or runtime state containing private data

All location-specific or sensitive values must be supplied at runtime
(e.g., via environment variables or local, git-ignored config files).

Example:
```python
lat = float(os.environ["NOAA_LAT"])
lon = float(os.environ["NOAA_LON"])


# 🌩️ Lightning Node (Raspberry Pi + AS3935 + Bluesky)

Lightning detection project for Raspberry Pi using the **AS3935** sensor.  
Includes bilingual alerts (English + French), JSON telemetry logs, CEU PASS node
metadata, and automatic Bluesky posting with optional storm summary charts.

---

## ⚡ Features

- Real-time lightning strike detection using the AS3935 (I²C)
- Distance reported in **kilometers and miles**
- Bilingual console and log messages (**EN / FR**)
- Automatic posting to **Bluesky** using `atproto`
- Per-strike text logging to `lightning_alerts.log`
- Structured JSON telemetry to `lightning_telemetry.jsonl`
- CEU-style PASS node identity:
  - Node ID (e.g., `PASS-LN-01`)
  - Region label (e.g., `Greater Harmony Hills`)
  - Channel label (e.g., `Atmospheric Telemetry`)
- Optional PNG chart of storm activity (`storm_summary.png`)
  - Uploaded to Bluesky with the storm summary

---

## 🧱 Hardware

- Raspberry Pi (Zero / 3 / 4 / 5)
- AS3935 lightning sensor module (I²C version)
- Jumper wires
- Optional: E-ink display HAT for a local PASS dashboard

---

## 🔧 Software Requirements

Install on the Raspberry Pi:

```bash
sudo apt update
sudo apt install python3 python3-pip python3-rpi.gpio python3-matplotlib -y
pip3 install atproto RPi-AS3935
