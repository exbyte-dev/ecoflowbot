"""
EcoFlow Discord bot.

Monitors a power station via the EcoFlow Open API (MQTT) and sends
Discord notifications when charging starts or stops.  Slash commands
let you check status and toggle AC / USB outputs on the fly.

Usage:
    python bot.py
"""

import asyncio
import logging
import sys
from datetime import datetime, timezone

import discord

from config import Config
from ecoflow.auth import get_mqtt_credentials
from ecoflow.monitor import DeviceState, EcoFlowMonitor

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("ecoflowbot")

# ---------------------------------------------------------------------------
# Embed colours
# ---------------------------------------------------------------------------
COLOUR_CHARGING = 0x2ECC71   # green  – power restored / charging
COLOUR_STOPPED  = 0xE74C3C   # red    – power gone / idle
COLOUR_INFO     = 0x3498DB   # blue   – info / status
COLOUR_WARN     = 0xF39C12   # orange – warning / unknown


# ---------------------------------------------------------------------------
# Formatting helpers
# ---------------------------------------------------------------------------

def _fmt_watts(w: float | None) -> str:
    return f"{w:.0f} W" if w is not None else "—"

def _fmt_volts(v: float | None) -> str:
    # EcoFlow sends millivolts for some fields; normalise values > 1000 V
    if v is None:
        return "—"
    if v > 1000:
        v /= 10   # some firmware reports in tenths of a volt
    return f"{v:.0f} V"

def _fmt_pct(p: float | None) -> str:
    return f"{p:.0f}%" if p is not None else "—"

def _fmt_temp(t: float | None) -> str:
    return f"{t:.0f} °C" if t is not None else "—"

def _fmt_remain(minutes: float | None) -> str:
    if minutes is None:
        return "—"
    m = int(minutes)
    if m <= 0:
        return "—"
    if m >= 60:
        return f"{m // 60}h {m % 60:02d}m"
    return f"{m}m"

def _onoff(val: bool | None, *, true_label="ON", false_label="OFF") -> str:
    if val is None:
        return "—"
    return true_label if val else false_label

def _chg_state_label(state: int | None) -> str:
    labels = {0: "Idle", 1: "CC Charging", 2: "CV Charging", 3: "CC Discharging", 4: "Discharging"}
    return labels.get(state, "Unknown") if state is not None else "—"


# ---------------------------------------------------------------------------
# Status embed builder (shared by /status and notifications)
# ---------------------------------------------------------------------------

def build_status_embed(state: DeviceState, device_sn: str, title: str = "Status", colour: int = COLOUR_INFO) -> discord.Embed:
    embed = discord.Embed(title=title, colour=colour, timestamp=datetime.now(timezone.utc))
    embed.set_footer(text=f"EcoFlow Monitor • {device_sn}")

    if not state.has_data:
        embed.description = "No data received yet — waiting for device telemetry."
        return embed

    # ── Battery ────────────────────────────────────────────────
    batt_lines = [f"**{_fmt_pct(state.soc)}**"]
    if state.is_charging and state.chg_remain_min:
        batt_lines.append(f"Full in ~{_fmt_remain(state.chg_remain_min)}")
    elif not state.is_charging and state.dsg_remain_min:
        batt_lines.append(f"~{_fmt_remain(state.dsg_remain_min)} remaining")
    batt_lines.append(_chg_state_label(state.chg_state))
    embed.add_field(name="🔋 Battery", value="\n".join(batt_lines), inline=True)

    # ── Power flow ─────────────────────────────────────────────
    # Use summed watts when available; fall back to AC-only
    watts_in  = state.watts_in  if state.watts_in  is not None else state.ac_in_watts
    watts_out = state.watts_out if state.watts_out is not None else state.ac_out_watts
    embed.add_field(name="⚡ Input",  value=_fmt_watts(watts_in),  inline=True)
    embed.add_field(name="💡 Output", value=_fmt_watts(watts_out), inline=True)

    # ── AC input ───────────────────────────────────────────────
    ac_in_parts = [_fmt_volts(state.ac_in_voltage)]
    if state.ac_in_watts is not None:
        ac_in_parts.append(_fmt_watts(state.ac_in_watts))
    if state.ac_in_freq is not None:
        ac_in_parts.append(f"{state.ac_in_freq:.0f} Hz")
    embed.add_field(name="🔌 AC Input", value="  ·  ".join(p for p in ac_in_parts if p != "—") or "—", inline=True)

    # ── Output switches ───────────────────────────────────────
    ac_out_val = _onoff(state.ac_out_enabled)
    if state.ac_out_watts is not None:
        ac_out_val += f"  ·  {_fmt_watts(state.ac_out_watts)}"
    embed.add_field(name="🔌 AC Output", value=ac_out_val, inline=True)
    embed.add_field(name="🔌 USB / DC",  value=_onoff(state.usb_out_enabled), inline=True)

    # ── Temperature ───────────────────────────────────────────
    if state.inv_temp_c is not None:
        embed.add_field(name="🌡 Temp", value=_fmt_temp(state.inv_temp_c), inline=True)

    return embed


# ---------------------------------------------------------------------------
# Bot
# ---------------------------------------------------------------------------

class EcoFlowBot(discord.Bot):
    def __init__(self, config: Config) -> None:
        intents = discord.Intents.default()
        super().__init__(intents=intents)
        self.cfg = config
        self._monitor: EcoFlowMonitor | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._register_commands()

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def on_ready(self) -> None:
        self._loop = asyncio.get_running_loop()
        logger.info("Logged in as %s (ID: %s)", self.user, self.user.id)
        await self._start_monitor()

    async def close(self) -> None:
        if self._monitor:
            logger.info("Stopping MQTT monitor…")
            self._monitor.stop()
        await super().close()

    # ------------------------------------------------------------------
    # Monitor startup
    # ------------------------------------------------------------------

    async def _start_monitor(self) -> None:
        try:
            logger.info("Fetching EcoFlow MQTT credentials…")
            creds = await asyncio.get_running_loop().run_in_executor(
                None,
                get_mqtt_credentials,
                self.cfg.api_host,
                self.cfg.ecoflow_access_key,
                self.cfg.ecoflow_secret_key,
            )
            logger.info("MQTT broker: %s:%d", creds.host, creds.port)
        except Exception as exc:
            logger.error("Failed to get MQTT credentials: %s", exc)
            await self._send_embed(discord.Embed(
                title="Startup Error",
                description=f"Could not connect to EcoFlow API:\n```{exc}```",
                colour=0xFF0000,
            ))
            return

        self._monitor = EcoFlowMonitor(
            credentials=creds,
            device_sn=self.cfg.device_sn,
            watts_threshold=self.cfg.charging_watts_threshold,
            on_charging_start=self._on_charging_start,
            on_charging_stop=self._on_charging_stop,
            on_connect=self._on_mqtt_connect,
            on_disconnect=self._on_mqtt_disconnect,
        )
        self._monitor.start()
        logger.info("EcoFlow monitor started for %s", self.cfg.device_sn)

    # ------------------------------------------------------------------
    # MQTT event callbacks  (called from paho's background thread)
    # ------------------------------------------------------------------

    def _on_mqtt_connect(self) -> None:
        asyncio.run_coroutine_threadsafe(
            self._send_embed(discord.Embed(
                description=f"Connected to EcoFlow data stream. Monitoring `{self.cfg.device_sn}`.",
                colour=COLOUR_INFO,
            )),
            self._loop,
        )

    def _on_mqtt_disconnect(self) -> None:
        logger.warning("MQTT disconnected — paho will attempt to reconnect")

    def _on_charging_start(self, state: DeviceState) -> None:
        asyncio.run_coroutine_threadsafe(
            self._notify_charging_start(state), self._loop
        )

    def _on_charging_stop(self, state: DeviceState) -> None:
        asyncio.run_coroutine_threadsafe(
            self._notify_charging_stop(state), self._loop
        )

    # ------------------------------------------------------------------
    # Notification helpers
    # ------------------------------------------------------------------

    async def _get_targets(self) -> list[discord.abc.Messageable]:
        targets: list[discord.abc.Messageable] = []
        channel = self.get_channel(self.cfg.discord_channel_id)
        if channel:
            targets.append(channel)
        else:
            logger.error("Channel %d not found", self.cfg.discord_channel_id)

        if self.cfg.discord_dm_user_id:
            try:
                user = await self.fetch_user(self.cfg.discord_dm_user_id)
                targets.append(await user.create_dm())
            except discord.NotFound:
                logger.warning("DM user %d not found", self.cfg.discord_dm_user_id)
        return targets

    async def _send_embed(self, embed: discord.Embed) -> None:
        for target in await self._get_targets():
            try:
                await target.send(embed=embed)
            except (discord.Forbidden, discord.HTTPException) as exc:
                logger.error("Failed to send to %s: %s", target, exc)

    async def _notify_charging_start(self, state: DeviceState) -> None:
        embed = build_status_embed(
            state, self.cfg.device_sn,
            title="⚡ Power Restored — Charging Started",
            colour=COLOUR_CHARGING,
        )
        await self._send_embed(embed)

    async def _notify_charging_stop(self, state: DeviceState) -> None:
        embed = build_status_embed(
            state, self.cfg.device_sn,
            title="🔌 Power Gone — Charging Stopped",
            colour=COLOUR_STOPPED,
        )
        await self._send_embed(embed)

    # ------------------------------------------------------------------
    # Guard helpers used by slash commands
    # ------------------------------------------------------------------

    def _monitor_ready(self) -> EcoFlowMonitor | None:
        """Return the monitor if ready, or None."""
        return self._monitor if self._monitor and self._monitor.is_connected else None

    # ------------------------------------------------------------------
    # Slash commands
    # ------------------------------------------------------------------

    def _register_commands(self) -> None:

        # ── /status ────────────────────────────────────────────────────
        @self.slash_command(name="status", description="Show current power station status")
        async def cmd_status(ctx: discord.ApplicationContext) -> None:
            await ctx.defer()
            monitor = self._monitor

            if monitor is None:
                await ctx.respond("Monitor is not started yet. Try again in a moment.")
                return

            if not monitor.is_connected:
                await ctx.respond(embed=discord.Embed(
                    description="Not connected to EcoFlow — trying to reconnect.",
                    colour=COLOUR_WARN,
                ))
                return

            state = monitor.get_state()
            charging = monitor.current_charging
            colour = COLOUR_CHARGING if charging else (COLOUR_STOPPED if charging is False else COLOUR_WARN)
            embed = build_status_embed(state, self.cfg.device_sn, colour=colour)
            await ctx.respond(embed=embed)

        # ── /ac ────────────────────────────────────────────────────────
        @self.slash_command(name="ac", description="Turn AC output on or off")
        async def cmd_ac(
            ctx: discord.ApplicationContext,
            state: discord.Option(str, "on or off", choices=["on", "off"]),
        ) -> None:
            await ctx.defer()
            monitor = self._monitor_ready()

            if monitor is None:
                await ctx.respond(embed=discord.Embed(
                    description="Not connected to EcoFlow. Cannot send command.",
                    colour=COLOUR_WARN,
                ))
                return

            enabled = state == "on"
            ok = monitor.set_ac_output(
                enabled=enabled,
                voltage=self.cfg.ac_out_voltage,
                freq=self.cfg.ac_out_freq,
                xboost=self.cfg.ac_xboost,
            )

            if ok:
                label = "ON" if enabled else "OFF"
                colour = COLOUR_CHARGING if enabled else COLOUR_STOPPED
                embed = discord.Embed(
                    description=f"AC output command sent — turning **{label}**.",
                    colour=colour,
                )
                embed.set_footer(text=f"Device: {self.cfg.device_sn}")
            else:
                embed = discord.Embed(
                    description="Command failed — MQTT not connected.",
                    colour=COLOUR_WARN,
                )
            await ctx.respond(embed=embed)

        # ── /usb ───────────────────────────────────────────────────────
        @self.slash_command(name="usb", description="Turn USB / DC output on or off")
        async def cmd_usb(
            ctx: discord.ApplicationContext,
            state: discord.Option(str, "on or off", choices=["on", "off"]),
        ) -> None:
            await ctx.defer()
            monitor = self._monitor_ready()

            if monitor is None:
                await ctx.respond(embed=discord.Embed(
                    description="Not connected to EcoFlow. Cannot send command.",
                    colour=COLOUR_WARN,
                ))
                return

            enabled = state == "on"
            ok = monitor.set_usb_output(enabled)

            if ok:
                label = "ON" if enabled else "OFF"
                colour = COLOUR_CHARGING if enabled else COLOUR_STOPPED
                embed = discord.Embed(
                    description=f"USB / DC output command sent — turning **{label}**.",
                    colour=colour,
                )
                embed.set_footer(text=f"Device: {self.cfg.device_sn}")
            else:
                embed = discord.Embed(
                    description="Command failed — MQTT not connected.",
                    colour=COLOUR_WARN,
                )
            await ctx.respond(embed=embed)

        # ── /dc ────────────────────────────────────────────────────────
        @self.slash_command(name="dc", description="Turn 12 V car / cigarette-lighter port on or off")
        async def cmd_dc(
            ctx: discord.ApplicationContext,
            state: discord.Option(str, "on or off", choices=["on", "off"]),
        ) -> None:
            await ctx.defer()
            monitor = self._monitor_ready()

            if monitor is None:
                await ctx.respond(embed=discord.Embed(
                    description="Not connected to EcoFlow. Cannot send command.",
                    colour=COLOUR_WARN,
                ))
                return

            enabled = state == "on"
            ok = monitor.set_dc_car_output(enabled)

            if ok:
                label = "ON" if enabled else "OFF"
                colour = COLOUR_CHARGING if enabled else COLOUR_STOPPED
                embed = discord.Embed(
                    description=f"12 V car port command sent — turning **{label}**.",
                    colour=colour,
                )
                embed.set_footer(text=f"Device: {self.cfg.device_sn}")
            else:
                embed = discord.Embed(
                    description="Command failed — MQTT not connected.",
                    colour=COLOUR_WARN,
                )
            await ctx.respond(embed=embed)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    try:
        config = Config.from_env()
    except EnvironmentError as exc:
        logger.error("Configuration error: %s", exc)
        sys.exit(1)

    # Python 3.12+ no longer auto-creates an event loop; py-cord's __init__
    # calls asyncio.get_event_loop() before bot.run() sets one up, so we
    # create and register the loop explicitly.
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    bot = EcoFlowBot(config)

    try:
        bot.run(config.discord_token, reconnect=True)
    except discord.LoginFailure:
        logger.error("Invalid Discord token. Check your DISCORD_TOKEN.")
        sys.exit(1)
    except KeyboardInterrupt:
        logger.info("Shutting down…")


if __name__ == "__main__":
    main()
