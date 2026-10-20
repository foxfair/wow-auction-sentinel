# WoW Auction Sentinel

A lightweight, automated 24/7 background monitor and analytics dashboard for tracking World of Warcraft Auction House item prices, supply shifts, and market anomalies. Designed to run permanently as a headless daemon on SteamOS (Steam Deck / Steam Machine) or any standard Linux distribution.

---

## Features

- **24/7 Headless Daemon (`monitor.py`):**
  - **Memory-Efficient Streaming:** Parses multi-megabyte Battle.net auction dumps via `ijson` with a minimal resident RAM footprint (<40 MB).
  - **Conditional HTTP 304 Caching:** Tracks `Last-Modified` and `ETag` headers directly in SQLite to bypass unchanged payloads and conserve bandwidth.
  - **Auction Duration Bucketing:** Breaks down listing volumes into `SHORT` (<30m), `MEDIUM` (30m-2h), `LONG` (2h-12h), and `VERY_LONG` (12h-48h) to filter out expiring listing noise.
  - **Statistical Anomaly Detection:**
    - Trailing 24-point Price Simple Moving Average (SMA) identifying >= 15% discount opportunities.
    - Volume Z-Score (>= +2.0 sigma) with absolute unit buffers to detect supply dumps and bot influxes.
  - **Rich Discord Alerts:** Automated webhook notifications for price dips, supply spikes, and market dumps with deduplication cooldowns.

- **FastAPI Web Dashboard (`app.py`):**
  - Dual synchronized Chart.js graphs (Spot Buyout vs. 24h SMA trend; Stacked Area Duration distribution).
  - Real-time market metrics: Unit Price, Rolling Baseline, Available Units, Volume Z-Score, and Signal Status.
  - Interactive time-range toggles (`24h`, `3 Days`, `7 Days`, `All`).

---

## Directory Structure

    wow-auction-sentinel/
    ├── .gitignore
    ├── requirements.txt
    ├── README.md
    ├── monitor.py               # Background ingestion & alerting daemon
    ├── app.py                   # FastAPI API & embedded Chart.js web interface
    ├── systemd/
    │   ├── wow-monitor.service   # Systemd user unit for daemon
    │   └── wow-dashboard.service # Systemd user unit for web dashboard
    └── market_data.db           # SQLite database (auto-generated at runtime)

---

## Prerequisites

- Python 3.10+
- A Battle.net Developer Client ID & Secret (obtainable from https://develop.battle.net/access/clients).
- (Optional) A Discord Webhook URL for automated notifications.

---

## Installation & Setup

### 1. Clone & Set Up Virtual Environment

    cd ~/wow-auction-sentinel
    python3 -m venv venv
    source venv/bin/activate
    pip install -r requirements.txt

### 2. Configure Environment Variables

Set your Battle.net API credentials and Discord webhook URL in your environment:

    export BLIZZARD_CLIENT_ID="your_client_id_here"
    export BLIZZARD_CLIENT_SECRET="your_client_secret_here"
    export DISCORD_WEBHOOK_URL="https://discord.com/api/webhooks/your/webhook/url"

---

## Configuration (`monitor.py`)

Adjust the configuration block in `monitor.py` to match your target server and watched reagents:

    REGION = "us"                       # 'us', 'eu', 'kr', 'tw'
    NAMESPACE = "dynamic-classic-us"    # 'dynamic-classic-us' or 'dynamic-classic1x-us'
    CONNECTED_REALM_ID = 4372           # Your server's connectedRealmId
    POLL_INTERVAL_SECONDS = 900         # 15 minutes

    WATCHLIST = {
        13468: {
            "name": "Black Lotus",
            "min_surge_units": 4        # Minimum volume delta above mean to trigger alert
        },
        13463: {
            "name": "Dreamfoil",
            "min_surge_units": 100
        },
        14344: {
            "name": "Large Brilliant Shard",
            "min_surge_units": 15
        }
    }

---

## Running Manually

### Start the Ingestion Daemon:

    python3 monitor.py

### Start the Web Dashboard:

    uvicorn app:app --host 0.0.0.0 --port 8080

Open `http://localhost:8080` (or `http://<YOUR_IP>:8080` over your LAN) to access the live dashboard.

---

## Deploying 24/7 on SteamOS / Linux (`systemd`)

On SteamOS and modern Linux distributions, you can run both services continuously in the user session without touching root filesystems.

### 1. Install Service Files

Copy the provided service templates into your user systemd directory:

    mkdir -p ~/.config/systemd/user
    cp systemd/wow-monitor.service ~/.config/systemd/user/
    cp systemd/wow-dashboard.service ~/.config/systemd/user/

Note: Update the credentials (`BLIZZARD_CLIENT_ID`, `BLIZZARD_CLIENT_SECRET`, `DISCORD_WEBHOOK_URL`) inside `~/.config/systemd/user/wow-monitor.service`.

### 2. Enable User Lingering (Persistent Across Logout)

Ensure your user session survives logouts and reboots:

    loginctl enable-linger $USER

### 3. Enable and Start Services

    systemctl --user daemon-reload
    systemctl --user enable --now wow-monitor.service
    systemctl --user enable --now wow-dashboard.service

### 4. Check Status and Logs

    # Monitor daemon logs
    journalctl --user -u wow-monitor.service -f

    # Dashboard logs
    journalctl --user -u wow-dashboard.service -f

---

## License

MIT
