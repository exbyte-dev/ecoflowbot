# EcoFlow Discord Bot

A Discord bot that monitors an EcoFlow power station in real time via the official EcoFlow Open API and notifies you when charging starts or stops — useful anywhere with unreliable grid power.

Originally built to deal with frequent load-shedding and grid instability, where knowing the exact moment power is restored (and the battery starts charging) or cut out again matters for planning.

## Features

- **Automatic notifications** — sends a rich embed to your channel (and optionally a DM) the moment grid power is restored or lost
- **Real-time status** — `/status` shows battery %, input/output watts, AC input voltage, estimated charge/discharge time, temperature, and output switch states
- **Remote control** — toggle AC output, USB/DC ports, and the 12 V car port directly from Discord
- **Resilient connection** — paho-mqtt handles automatic MQTT reconnection; the bot resumes monitoring after network blips

## Slash Commands

| Command | Description |
|---|---|
| `/status` | Full device status embed |
| `/ac on\|off` | Turn the AC output inverter on or off |
| `/usb on\|off` | Turn USB / 5 V–12 V DC ports on or off |
| `/dc on\|off` | Turn the 12 V car / cigarette-lighter port on or off |

## How It Works

```
Discord ◄──────────────────────────────────── bot.py
                                                  │
                 EcoFlow REST API                 │  on_ready
          ┌──── GET /iot-open/sign/certification ─┤
          │     (HMAC-SHA256 signed)               │
          ▼                                        │
     MQTT credentials                             │
          │                                        │
          ▼                                        ▼
  mqtt-e.ecoflow.com:8883               EcoFlowMonitor (thread)
  /open/{account}/{sn}/quota  ──push──► state cache + transition detection
  /open/{account}/{sn}/set    ◄─pub──── set_ac_output / set_usb_output / …
```

1. On startup, the bot calls the EcoFlow REST API (signed with your Access Key + Secret Key) to get short-lived MQTT credentials.
2. It subscribes to the device's telemetry topic (`/open/{account}/{sn}/quota`) and caches all incoming fields.
3. State transitions (not-charging → charging, and vice versa) trigger Discord embeds in the configured channel.
4. Slash commands read from the same live cache for `/status`, and publish commands to the `/set` topic for toggles.

## Requirements

- Python 3.11+
- An [EcoFlow developer account](https://developer.ecoflow.com) with an Access Key and Secret Key
- A Discord bot token
- An EcoFlow power station connected to the internet

## Setup

### 1. Clone the repository

```bash
git clone https://github.com/your-username/ecoflowbot.git
cd ecoflowbot
```

### 2. Create a virtual environment and install dependencies

```bash
python3 -m venv venv
source venv/bin/activate        # Windows: venv\Scripts\activate
pip install -r requirements.txt
```

### 3. Get EcoFlow API credentials

1. Register at [developer.ecoflow.com](https://developer.ecoflow.com)
2. Wait for developer access approval (typically a few days)
3. Go to **Access Management** and generate an **Access Key** and **Secret Key**
4. Find your device **Serial Number** in the EcoFlow app under Settings → Device Info → SN

### 4. Create a Discord bot

1. Go to [discord.com/developers/applications](https://discord.com/developers/applications) → **New Application**
2. Under **Bot**, click **Reset Token** and copy the token
3. Under **OAuth2 → URL Generator**, select scopes: `bot` + `applications.commands`
4. Bot permissions: `Send Messages`, `Embed Links`, `View Channels`
5. Open the generated URL to add the bot to your server
6. Enable **Developer Mode** in Discord (Settings → Advanced), then right-click your target channel → **Copy Channel ID**

### 5. Configure environment variables

```bash
cp .env.example .env
```

Edit `.env` with your credentials:

```env
ECOFLOW_ACCESS_KEY=your_access_key
ECOFLOW_SECRET_KEY=your_secret_key
DEVICE_SN=R331XXXXXXXXXXXX

DISCORD_TOKEN=your_bot_token
DISCORD_CHANNEL_ID=123456789012345678

# Optional: also DM this user when an event occurs
DISCORD_DM_USER_ID=0
```

### 6. Run the bot

```bash
python bot.py
```

You should see the bot come online in Discord and post a "Connected to EcoFlow data stream" message.

## Configuration Reference

All configuration is via environment variables (`.env` file).

| Variable | Required | Default | Description |
|---|---|---|---|
| `ECOFLOW_ACCESS_KEY` | Yes | — | EcoFlow developer Access Key |
| `ECOFLOW_SECRET_KEY` | Yes | — | EcoFlow developer Secret Key |
| `DEVICE_SN` | Yes | — | Device serial number |
| `DISCORD_TOKEN` | Yes | — | Discord bot token |
| `DISCORD_CHANNEL_ID` | Yes | — | Channel ID for notifications |
| `DISCORD_DM_USER_ID` | No | `0` | User ID for DM notifications (0 = disabled) |
| `ECOFLOW_API_HOST` | No | `https://api.ecoflow.com` | Use `https://api-e.ecoflow.com` for Europe |
| `CHARGING_WATTS_THRESHOLD` | No | `10` | Min input watts to count as charging |
| `AC_OUT_VOLTAGE` | No | `230` | AC output voltage sent with `/ac` command |
| `AC_OUT_FREQ` | No | `1` | AC output frequency: `1` = 50 Hz, `2` = 60 Hz |
| `AC_XBOOST` | No | `true` | Enable X-Boost with `/ac on` |

## Running as a Service (Linux)

The included `setup.sh` script handles everything: creating the venv, installing dependencies, writing the systemd unit file, enabling the service on boot, and (re)starting it.

**First-time install or update:**

```bash
sudo ./setup.sh
```

Re-run the same command after pulling changes to update dependencies and restart the bot.

**Manual service control:**

```bash
sudo systemctl status ecoflowbot     # check status
sudo systemctl restart ecoflowbot    # restart
sudo systemctl stop ecoflowbot       # stop
journalctl -u ecoflowbot -f          # follow logs
```

## Project Structure

```
ecoflowbot/
├── bot.py              Discord bot — slash commands, notification embeds, lifecycle
├── config.py           Environment variable loading and validation
├── ecoflow/
│   ├── auth.py         HMAC-SHA256 REST auth → MQTT credential retrieval
│   └── monitor.py      MQTT client — telemetry cache, state transitions, command publishing
├── setup.sh            Install / update systemd service (run with sudo)
├── .env.example        Configuration template (copy to .env)
├── requirements.txt    Python dependencies
└── LICENSE             MIT
```

## Supported Devices

Any EcoFlow power station accessible via the [EcoFlow Open API](https://developer.ecoflow.com) should work. Tested field names are from the Delta 2 / Delta 2 Max series. If your device reports different field names, the bot will still receive all telemetry — check the logs for the raw field names and open an issue or PR to add support.

## Contributing

Contributions are welcome. Please:

1. Fork the repository and create a feature branch
2. Keep changes focused — one concern per PR
3. Make sure `python -m py_compile bot.py config.py ecoflow/auth.py ecoflow/monitor.py` passes
4. Open a pull request with a clear description of what changed and why

## License

MIT — see [LICENSE](LICENSE).
