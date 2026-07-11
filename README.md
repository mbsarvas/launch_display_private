# 🚀 Rocket Launch Display System — Private

Personal deployment of the Rocket Launch Display System consisting of a dedicated fetcher Pi and one or more follower display Pis.

**Created by Matthew Sarvas**

---

## System Overview

| Script | Pi Type | Role |
|--------|---------|------|
| `launch_display_fetcher.py` | Fetcher Pi (one, dedicated) | Calls Launch Library 2 API, pushes `launches.json` to this repo, drives its own 6 displays |
| `launch_display_follower.py` | Follower Pi (one or more) | Reads `launches.json` from this repo, falls back to API if unavailable, drives its own 6 displays |

The fetcher Pi is the only device that calls the Launch Library 2 API directly. All follower Pis read cached data from this repository, meaning any number of follower Pis can run without hitting the API rate limit.

---

## Before You Start

If you use **Raspberry Pi Imager** to flash your SD card, it will prompt you to apply OS customisation settings before writing — use this to pre-configure your username, password, Wi-Fi, and SSH. A password is required for the initial setup steps in this guide — once setup is complete the scripts run hands-free on every boot.

---

## Hardware (same for all Pis)

- Raspberry Pi Zero 2 W
- 3x 16x2 I2C LCD displays (PCF8574 backpack)
- 3x 20x4 I2C LCD displays (PCF8574 backpack)
- 1x momentary push button
- Jumper wires

---

## Wiring (same for all Pis)

### I2C LCD Displays

| LCD | Size | I2C Address | Slot |
|-----|------|-------------|------|
| 16x2 #1 | 16 cols, 2 rows | 0x24 | Launch #1 |
| 16x2 #2 | 16 cols, 2 rows | 0x23 | Launch #2 |
| 16x2 #3 | 16 cols, 2 rows | 0x22 | Launch #3 |
| 20x4 #1 | 20 cols, 4 rows | 0x27 | Launch #1 |
| 20x4 #2 | 20 cols, 4 rows | 0x26 | Launch #2 |
| 20x4 #3 | 20 cols, 4 rows | 0x25 | Launch #3 |

| LCD Pin | Pi Pin | Description |
|---------|--------|-------------|
| VCC | Pin 2 | 5V Power |
| GND | Pin 6 | Ground |
| SDA | Pin 3 (GPIO2) | I2C Data |
| SCL | Pin 5 (GPIO3) | I2C Clock |

### Toggle Button

| Button Leg | Pi Pin | Description |
|------------|--------|-------------|
| Leg 1 | Pin 13 (GPIO27) | Signal |
| Leg 2 | Pin 9 (GND) | Ground |

---

## Installation — Follower Pi

### 1. Enable I2C

```bash
sudo raspi-config
# Interface Options → I2C → Enable
sudo reboot
```
> You may be prompted for your password.

### 2. Verify displays are detected

```bash
sudo apt install i2c-tools
i2cdetect -y 1
```
> You may be prompted for your password.

### 3. Download the script

```bash
cd /home/YOUR_USERNAME
mkdir launch-display-private
cd launch-display-private
wget https://raw.githubusercontent.com/mbsarvas/launch-display-private/main/launch_display_follower.py
wget https://raw.githubusercontent.com/mbsarvas/launch-display-private/main/requirements.txt
```

### 4. Set your username in the script

```bash
nano launch_display_follower.py
```

Update this line:

```python
PI_USER = "pi"   # change to your username
```

Save and exit with `Ctrl+X`, then `Y`, then `Enter`.

### 5. Install dependencies

```bash
pip install -r requirements.txt --break-system-packages
```

### 6. Test the script

```bash
python3 launch_display_follower.py
```

### 7. Autostart on boot

```bash
sudo nano /etc/systemd/system/launchfollower.service
```
> You may be prompted for your password.

```ini
[Unit]
Description=Rocket Launch Follower
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=YOUR_USERNAME
WorkingDirectory=/home/YOUR_USERNAME/launch-display-private
ExecStart=/usr/bin/python3 /home/YOUR_USERNAME/launch-display-private/launch_display_follower.py
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
```

Save and exit with `Ctrl+X`, then `Y`, then `Enter`.

```bash
sudo systemctl daemon-reload
sudo systemctl enable launchfollower
sudo systemctl start launchfollower
sudo reboot
```
> You may be prompted for your password.

---

## Installation — Fetcher Pi

### 1–5. Follow the same steps as the Follower Pi above

But download `launch_display_fetcher.py` instead:

```bash
cd /home/YOUR_USERNAME
mkdir launch-display-private
cd launch-display-private
wget https://raw.githubusercontent.com/mbsarvas/launch-display-private/main/launch_display_fetcher.py
wget https://raw.githubusercontent.com/mbsarvas/launch-display-private/main/requirements.txt
```

### 6. Generate a GitHub personal access token

1. Go to GitHub → **Settings** → **Developer settings** → **Personal access tokens** → **Tokens (classic)**
2. Click **Generate new token (classic)**
3. Name it `rocket-launch-fetcher`
4. Check the **repo** scope
5. Click **Generate token** and copy it — you will not see it again

```bash
echo "your_token_here" > /home/YOUR_USERNAME/github_token.txt
```

### 7. Set your username and GitHub details in the fetcher script

```bash
nano launch_display_fetcher.py
```

Update these lines:

```python
PI_USER          = "pi"        # your Pi username
GITHUB_USERNAME  = "mbsarvas"  # your GitHub username
```

Save and exit with `Ctrl+X`, then `Y`, then `Enter`.

### 8. Test the fetcher script

```bash
python3 launch_display_fetcher.py
```

On first run it will fetch launch data, push `launches.json` to this repository, and update the displays.

### 9. Autostart on boot

```bash
sudo nano /etc/systemd/system/rocketfetcher.service
```
> You may be prompted for your password.

```ini
[Unit]
Description=Rocket Launch Fetcher
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=YOUR_USERNAME
WorkingDirectory=/home/YOUR_USERNAME/launch-display-private
ExecStart=/usr/bin/python3 /home/YOUR_USERNAME/launch-display-private/launch_display_fetcher.py
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
```

Save and exit with `Ctrl+X`, then `Y`, then `Enter`.

```bash
sudo systemctl daemon-reload
sudo systemctl enable rocketfetcher
sudo systemctl start rocketfetcher
sudo reboot
```
> You may be prompted for your password.

---

## Allow Auto-Update Without Password

```bash
sudo visudo
```

Add these lines at the bottom:

```
YOUR_USERNAME ALL=(ALL) NOPASSWD: /bin/systemctl restart launchfollower
YOUR_USERNAME ALL=(ALL) NOPASSWD: /bin/systemctl restart rocketfetcher
```

---

## API Key (Optional — Fetcher Pi only)

```bash
echo "your_api_key_here" > /home/YOUR_USERNAME/ll2_api_key.txt
```

The fetcher script detects the key automatically and switches to 60 second refresh. Falls back to anonymous (6 minute refresh) if removed.

---

## Button Usage

| Action | Result |
|--------|--------|
| Single press | Toggles between **All Locations** and **Vandenberg SFB only** |
| Hold 3 seconds | Confirms an update install when prompted |

---

## Display Messages

| Message | Cause | Action |
|---------|-------|--------|
| `No Internet Connection` | Pi cannot reach the internet | Check Wi-Fi and router |
| `API Rate Limited` + countdown | Too many API requests | Wait for countdown — resumes automatically |
| `No Launch Data` / `Data fetch failed` | Silent fetch failure | Script retries in 30 seconds |
| `No Vandenberg Launches Found` | Vandenberg filter on, none in cache | Press button to switch to all locations |
| `Update Available` | New version found on GitHub | Hold button 3 seconds to install or short press to skip |
| `Installing Update` | Update confirmed | Do not power off — Pi restarts automatically |
| `Update Failed` | Download failed | Script continues on current version |

---

## Useful Service Commands

```bash
# Follower Pi
sudo journalctl -u launchfollower -f
sudo systemctl restart launchfollower
sudo systemctl stop launchfollower

# Fetcher Pi
sudo journalctl -u rocketfetcher -f
sudo systemctl restart rocketfetcher
sudo systemctl stop rocketfetcher
```

---

## Troubleshooting

**Displays show FAILED on startup**
- Run `i2cdetect -y 1` to confirm displays are on the I2C bus
- Verify SDA/SCL wires are on the correct Pi pins

**Fetcher not pushing to GitHub**
- Confirm token file exists: `cat ~/github_token.txt`
- Verify token has `repo` scope in GitHub Settings
- Check terminal for `[GITHUB]` messages

**Button not responding**
- Confirm wiring to Pin 13 (GPIO27) and Pin 9 (GND)
- Check lgpio: `python3 -c "import lgpio; print('OK')"`
