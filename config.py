import os
from dataclasses import dataclass

from dotenv import load_dotenv

load_dotenv()


@dataclass
class Config:
    # EcoFlow developer API credentials
    # Obtain from https://developer.ecoflow.com -> Access Management
    ecoflow_access_key: str
    ecoflow_secret_key: str

    # Your device serial number (found on the device label or EcoFlow app)
    device_sn: str

    # Discord bot token (from https://discord.com/developers/applications)
    discord_token: str

    # Channel ID to send notifications to (right-click channel -> Copy ID)
    discord_channel_id: int

    # Optional: also DM this user ID when an event occurs (0 = disabled)
    discord_dm_user_id: int

    # EcoFlow API host — use "https://api-e.ecoflow.com" for Europe
    api_host: str

    # Watts threshold to consider AC charging as "started" (avoids noise near 0)
    charging_watts_threshold: float

    # AC output configuration sent with toggle commands
    # Nigeria uses 220-240 V / 50 Hz (out_freq 1 = 50 Hz, 2 = 60 Hz)
    ac_out_voltage: int
    ac_out_freq: int    # 1 = 50 Hz, 2 = 60 Hz
    ac_xboost: bool     # X-Boost allows running appliances above rated watts

    @classmethod
    def from_env(cls) -> "Config":
        missing = []

        def require(key: str) -> str:
            val = os.getenv(key, "").strip()
            if not val:
                missing.append(key)
            return val

        access_key = require("ECOFLOW_ACCESS_KEY")
        secret_key = require("ECOFLOW_SECRET_KEY")
        device_sn = require("DEVICE_SN")
        discord_token = require("DISCORD_TOKEN")
        discord_channel_id_str = require("DISCORD_CHANNEL_ID")

        if missing:
            raise EnvironmentError(
                f"Missing required environment variables: {', '.join(missing)}"
            )

        try:
            channel_id = int(discord_channel_id_str)
        except ValueError:
            raise EnvironmentError("DISCORD_CHANNEL_ID must be an integer")

        dm_user_id_str = os.getenv("DISCORD_DM_USER_ID", "0").strip()
        try:
            dm_user_id = int(dm_user_id_str)
        except ValueError:
            dm_user_id = 0

        api_host = os.getenv("ECOFLOW_API_HOST", "https://api.ecoflow.com").strip().rstrip("/")
        charging_threshold = float(os.getenv("CHARGING_WATTS_THRESHOLD", "10"))
        ac_out_voltage = int(os.getenv("AC_OUT_VOLTAGE", "230"))
        ac_out_freq = int(os.getenv("AC_OUT_FREQ", "1"))
        ac_xboost = os.getenv("AC_XBOOST", "true").strip().lower() not in {"0", "false", "no"}

        return cls(
            ecoflow_access_key=access_key,
            ecoflow_secret_key=secret_key,
            device_sn=device_sn,
            discord_token=discord_token,
            discord_channel_id=channel_id,
            discord_dm_user_id=dm_user_id,
            api_host=api_host,
            charging_watts_threshold=charging_threshold,
            ac_out_voltage=ac_out_voltage,
            ac_out_freq=ac_out_freq,
            ac_xboost=ac_xboost,
        )
