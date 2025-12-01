INSTALLATION GUIDE

Lightning-Node (Raspberry Pi + AS3935 + Bluesky Telemetry)

This guide explains how to install, configure, and run the Lightning Node on a Raspberry Pi using the AS3935 lightning sensor and Bluesky for telemetry posting.

⸻

1. Hardware Requirements
	•	Raspberry Pi (Zero / 3 / 4 / 5)
	•	AS3935 Lightning Sensor (I2C version)
	•	Jumper wires (male-to-female)
	•	Internet connection (Wi-Fi or Ethernet)

AS3935 to Raspberry Pi Wiring (I2C + IRQ)

AS3935 Pin
Raspberry Pi Pin
VIN
3.3V
GND
Ground
SDA
Pin 3 (GPIO 2)
SCL
Pin 5 (GPIO 3)
IRQ
Pin 11 (GPIO 17)

Enable I2C:
sudo raspi-config

2. Update and Prepare the Raspberry Pi
sudo apt update
sudo apt full-upgrade -y
sudo reboot

3. Install Required Dependencies

Install Python tools and system packages:

sudo apt install python3-pip python3-dev python3-matplotlib -y

Install Python libraries from the repository’s requirements.txt:

pip3 install -r requirements.txt

4. Configure Bluesky Credentials

Create a file on the Raspberry Pi:

~/.bluesky.ini

Add the following:

[bluesky]
handle = yourname.bsky.social
app_password = xxxx-xxxx-xxxx-xxxx

Generate your app password in Bluesky under:
Settings → App Passwords

Do not commit this file. Your .gitignore already protects it.

5. Add the Lightning Script to the Repository

Your main script should be named:

lightning_bluesky.py

Make it executable:

chmod +x lightning_bluesky.py

6. Running the Lightning Node

From the project directory:

python3 lightning_bluesky.py

python3 lightning_bluesky.py

During lightning activity, the system will:
	•	Detect strikes via AS3935
	•	Log human-readable alerts to lightning_alerts.log
	•	Log structured JSON to lightning_telemetry.jsonl
	•	Post alerts automatically to Bluesky
	•	Generate storm summary PNG charts (if enabled)

7. Storm Summary Charts (Optional)

If matplotlib is installed, the node generates:

storm_summary.png

at the end of a storm.
If chart-uploading is enabled, it will also be attached to the Bluesky post.

8. Optional: Run Automatically at Boot (systemd)

Create a systemd service:

sudo nano /etc/systemd/system/lightningnode.service

Paste:

[Unit]
Description=Lightning Detection Node
After=multi-user.target

[Service]
ExecStart=/usr/bin/python3 /home/pi/Lightning-Node/lightning_bluesky.py
WorkingDirectory=/home/pi/Lightning-Node
Restart=always
User=pi

[Install]
WantedBy=multi-user.target

Enable and start:

sudo systemctl daemon-reload
sudo systemctl enable lightningnode.service
sudo systemctl start lightningnode.service

Check status:

systemctl status lightningnode.service

9. Log Files

The script generates:

File
Description
lightning_alerts.log
Timestamped human-readable strike logs
lightning_telemetry.jsonl
JSON telemetry suitable for analysis
storm_summary.png
Auto-generated storm chart (optional)

10. Testing Bluesky Posting

Run the manual test below to verify authentication:

python3 - << 'EOF'
from atproto import Client
import configparser, os

cfg = configparser.ConfigParser()
cfg.read(os.path.expanduser("~/.bluesky.ini"))
h = cfg["bluesky"]["handle"]
pw = cfg["bluesky"]["app_password"]

c = Client()
c.login(h, pw)
c.send_post(text="Test post from Lightning Node.")
EOF

A successful login will immediately post to your Bluesky feed.

⸻

Installation Complete

Your Lightning Node is now ready for operation, storm monitoring, and automated Bluesky telemetry.

For support, improvements, or additional modules (E-Ink dashboards, data analysis tools, etc.), consult the repository or open an issue.

