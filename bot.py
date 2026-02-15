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
import re
import sys
from datetime import datetime, timezone

import discord
from discord.ext import tasks

from config import Config
from ecoflow.auth import get_device_quota, get_mqtt_credentials
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
    # EcoFlow sends voltages in different units depending on the field:
    #   millivolts  (e.g. inv.acInVol = 253054 → 253 V) : divide by 1000
    #   tenths of V (e.g. 2300 → 230 V)                 : divide by 10
    #   actual volts (e.g. 230)                          : use as-is
    if v is None:
        return "—"
    if v > 10_000:
        v /= 1000   # millivolts
    elif v > 1_000:
        v /= 10     # tenths of a volt
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

def _battery_bar(pct: float | None, length: int = 10) -> str:
    """Build a visual progress bar for battery level."""
    if pct is None:
        return "`" + "░" * length + "`  —"
    filled = round(pct / 100 * length)
    bar = "█" * filled + "░" * (length - filled)
    return f"`{bar}`  **{pct:.0f}%**"


def _status_icon(is_charging: bool | None) -> str:
    if is_charging is True:
        return "⚡"
    if is_charging is False:
        return "💤"
    return "❓"


def build_status_embed(
    state: DeviceState,
    device_sn: str,
    title: str = "Status",
    colour: int = COLOUR_INFO,
) -> discord.Embed:
    embed = discord.Embed(colour=colour, timestamp=datetime.now(timezone.utc))
    embed.set_footer(text=f"EcoFlow • {device_sn}")

    if not state.has_data:
        embed.title = title
        embed.description = "⏳ No data received yet — waiting for device telemetry."
        return embed

    # ── Title with status icon ────────────────────────────────
    icon = _status_icon(state.is_charging)
    status_label = "Charging" if state.is_charging else ("Idle" if state.is_charging is False else "Unknown")
    embed.title = f"{icon}  {title} — {status_label}"

    # ── Battery hero section (description) ────────────────────
    desc_lines = [_battery_bar(state.soc)]
    if state.is_charging and state.chg_remain_min:
        desc_lines.append(f"🕐 Full in **~{_fmt_remain(state.chg_remain_min)}**")
    elif not state.is_charging and state.dsg_remain_min:
        desc_lines.append(f"🕐 **~{_fmt_remain(state.dsg_remain_min)}** remaining")
    if state.batt_remain_cap is not None and state.batt_full_cap is not None:
        desc_lines.append(f"📦 {state.batt_remain_cap:.0f} / {state.batt_full_cap:.0f} mAh")
    desc_lines.append(f"🔄 {_chg_state_label(state.chg_state)}")
    embed.description = "\n".join(desc_lines)

    # ── Power flow ─────────────────────────────────────────────
    watts_in  = state.watts_in  if state.watts_in  is not None else state.ac_in_watts
    watts_out = state.watts_out if state.watts_out is not None else state.ac_out_watts
    power_lines = [
        f"⬇️ In: **{_fmt_watts(watts_in)}**",
        f"⬆️ Out: **{_fmt_watts(watts_out)}**",
        f"☀️ Solar: **{_fmt_watts(state.solar_watts)}**",
    ]
    embed.add_field(name="⚡ Power Flow", value="\n".join(power_lines), inline=True)

    # ── AC ────────────────────────────────────────────────────
    ac_lines = []
    # Input
    ac_in_parts = []
    if state.ac_in_voltage is not None:
        ac_in_parts.append(_fmt_volts(state.ac_in_voltage))
    if state.ac_in_watts is not None:
        ac_in_parts.append(_fmt_watts(state.ac_in_watts))
    if state.ac_in_freq is not None:
        ac_in_parts.append(f"{state.ac_in_freq:.0f} Hz")
    ac_lines.append(f"**In:** {' · '.join(ac_in_parts) if ac_in_parts else '—'}")
    # Output
    ac_out_status = "🟢" if state.ac_out_enabled else "🔴" if state.ac_out_enabled is not None else "⚪"
    ac_out_parts = []
    if state.ac_out_voltage is not None:
        ac_out_parts.append(_fmt_volts(state.ac_out_voltage))
    if state.ac_out_watts is not None:
        ac_out_parts.append(_fmt_watts(state.ac_out_watts))
    if state.ac_out_freq is not None:
        ac_out_parts.append(f"{state.ac_out_freq:.0f} Hz")
    ac_lines.append(f"**Out:** {ac_out_status} {' · '.join(ac_out_parts) if ac_out_parts else _onoff(state.ac_out_enabled)}")
    embed.add_field(name="🔌 AC", value="\n".join(ac_lines), inline=True)

    # ── DC / USB ports ────────────────────────────────────────
    port_lines = []
    usb_status = "🟢" if state.usb_out_enabled else "🔴" if state.usb_out_enabled is not None else "⚪"
    usb_a = (state.usb1_watts or 0) + (state.usb2_watts or 0) + \
            (state.qc_usb1_watts or 0) + (state.qc_usb2_watts or 0)
    port_lines.append(f"**USB-A:** {usb_status} {f'{usb_a:.0f} W' if usb_a else ''}")
    usb_c = (state.typec1_watts or 0) + (state.typec2_watts or 0)
    if usb_c:
        port_lines.append(f"**USB-C:** {usb_c:.0f} W")
        if state.typec1_watts:
            port_lines.append(f"  └ C1: {state.typec1_watts:.0f} W")
        if state.typec2_watts:
            port_lines.append(f"  └ C2: {state.typec2_watts:.0f} W")
    else:
        port_lines.append("**USB-C:** —")
    car_status = "🟢" if state.dc_out_enabled else "🔴" if state.dc_out_enabled is not None else "⚪"
    car_w = state.car_watts
    port_lines.append(f"**12V Car:** {car_status} {f'{car_w:.0f} W' if car_w else ''}")
    embed.add_field(name="� DC Ports", value="\n".join(port_lines), inline=False)

    # ── Health & temps (combined row) ─────────────────────────
    info_lines = []
    if state.batt_soh is not None:
        info_lines.append(f"🏥 SOH: **{state.batt_soh}%**")
    if state.batt_cycles is not None:
        info_lines.append(f"🔁 **{state.batt_cycles}** cycles")
    if state.batt_temp_c is not None:
        info_lines.append(f"🌡 Batt: **{_fmt_temp(state.batt_temp_c)}**")
    if state.inv_temp_c is not None:
        info_lines.append(f"🌡 Inv: **{_fmt_temp(state.inv_temp_c)}**")
    if info_lines:
        embed.add_field(name="📊 Health & Temps", value="  ·  ".join(info_lines), inline=False)

    # ── Charge limits ─────────────────────────────────────────
    if state.max_charge_soc is not None or state.min_dsg_soc is not None:
        max_s = f"⬆️ Max {state.max_charge_soc}%" if state.max_charge_soc is not None else ""
        min_s = f"⬇️ Min {state.min_dsg_soc}%" if state.min_dsg_soc is not None else ""
        embed.add_field(name="⚙️ Charge Limits", value=f"{max_s}  {min_s}".strip(), inline=False)

    return embed


# ---------------------------------------------------------------------------
# Interactive status view (buttons)
# ---------------------------------------------------------------------------

class StatusView(discord.ui.View):
    """Persistent buttons attached to the /status embed."""

    def __init__(self, bot: "EcoFlowBot") -> None:
        super().__init__(timeout=120)  # buttons work for 2 minutes
        self.bot = bot

    async def _refresh_embed(self, interaction: discord.Interaction) -> None:
        """Fetch fresh data and update the embed in-place."""
        cfg = self.bot.cfg
        loop = asyncio.get_event_loop()
        flat = await loop.run_in_executor(
            None, get_device_quota,
            cfg.api_host, cfg.ecoflow_access_key,
            cfg.ecoflow_secret_key, cfg.device_sn,
        )
        state  = DeviceState(flat, cfg.charging_watts_threshold)
        colour = COLOUR_CHARGING if state.is_charging else (COLOUR_STOPPED if state.is_charging is False else COLOUR_WARN)
        embed  = build_status_embed(state, cfg.device_sn, colour=colour)
        await interaction.response.edit_message(embed=embed, view=self)

    @discord.ui.button(label="Refresh", emoji="🔄", style=discord.ButtonStyle.secondary)
    async def btn_refresh(self, button: discord.ui.Button, interaction: discord.Interaction) -> None:
        try:
            await self._refresh_embed(interaction)
        except Exception:
            logger.exception("Error refreshing status")
            await interaction.response.send_message(
                "❌ Failed to refresh.", ephemeral=True,
            )

    @discord.ui.button(label="AC Toggle", emoji="⚡", style=discord.ButtonStyle.primary)
    async def btn_ac_toggle(self, button: discord.ui.Button, interaction: discord.Interaction) -> None:
        try:
            monitor = await self.bot._monitor_ready()
            if monitor is None:
                await interaction.response.send_message(
                    "❌ Not connected to EcoFlow.", ephemeral=True,
                )
                return
            state = await monitor.get_state()
            new_state = not state.ac_out_enabled if state.ac_out_enabled is not None else True
            await monitor.set_ac_output(
                enabled=new_state,
                voltage=self.bot.cfg.ac_out_voltage,
                freq=self.bot.cfg.ac_out_freq,
                xboost=self.bot.cfg.ac_xboost,
            )
            label = "ON" if new_state else "OFF"
            await interaction.response.send_message(
                f"⚡ AC → **{label}**", ephemeral=True,
            )
        except Exception:
            logger.exception("Error toggling AC via button")
            await interaction.response.send_message(
                "❌ Failed to toggle AC.", ephemeral=True,
            )

    @discord.ui.button(label="USB Toggle", emoji="🔌", style=discord.ButtonStyle.primary)
    async def btn_usb_toggle(self, button: discord.ui.Button, interaction: discord.Interaction) -> None:
        try:
            monitor = await self.bot._monitor_ready()
            if monitor is None:
                await interaction.response.send_message(
                    "❌ Not connected to EcoFlow.", ephemeral=True,
                )
                return
            state = await monitor.get_state()
            new_state = not state.usb_out_enabled if state.usb_out_enabled is not None else True
            await monitor.set_usb_output(new_state)
            label = "ON" if new_state else "OFF"
            await interaction.response.send_message(
                f"🔌 USB → **{label}**", ephemeral=True,
            )
        except Exception:
            logger.exception("Error toggling USB via button")
            await interaction.response.send_message(
                "❌ Failed to toggle USB.", ephemeral=True,
            )


# ---------------------------------------------------------------------------
# Cog — all slash commands live here so py-cord can properly introspect
# option metadata from real class methods (not closures).
# ---------------------------------------------------------------------------

class EcoFlowCog(discord.Cog):
    def __init__(self, bot: "EcoFlowBot") -> None:
        self.bot = bot

    # ── Presence task (runs inside the Cog so @tasks.loop works) ──────

    @discord.Cog.listener()
    async def on_ready(self) -> None:
        self._update_presence.start()

    def cog_unload(self) -> None:
        self._update_presence.cancel()

    @tasks.loop(seconds=60)
    async def _update_presence(self) -> None:
        """Update bot status to show battery percentage."""
        try:
            if self.bot._monitor is None:
                return
            state = await self.bot._monitor.get_state()
            if state.soc is not None:
                icon = "⚡" if state.is_charging else "🔋"
                label = "Charging" if state.is_charging else "Idle"
                activity = discord.Activity(
                    type=discord.ActivityType.watching,
                    name=f"{icon} {state.soc:.0f}% | {label}",
                )
                await self.bot.change_presence(activity=activity)

                print(f"\nPresence updated:\n{icon} {state.soc:.0f}% | {label}")
            
            else:
                # get data from rest api
                flat = await self.bot.loop.run_in_executor(
                    None,
                    get_device_quota,
                    self.bot.cfg.api_host,
                    self.bot.cfg.ecoflow_access_key,
                    self.bot.cfg.ecoflow_secret_key,
                    self.bot.cfg.device_sn,
                )
                state = DeviceState(flat, self.bot.cfg.charging_watts_threshold)
                icon = "⚡" if state.is_charging else "🔋"
                label = "Charging" if state.is_charging else "Idle"
                activity = discord.Activity(
                    type=discord.ActivityType.watching,
                    name=f"{icon} {state.soc:.0f}% | {label}",
                )
                await self.bot.change_presence(activity=activity)

                print(f"\nPresence updated:\n{icon} {state.soc:.0f}% | {label}")
        except Exception:
            logger.debug("Failed to update presence", exc_info=True)
    
    @_update_presence.before_loop
    async def _before_update_presence(self) -> None:
        await self.bot.wait_until_ready()

    # ── /status ────────────────────────────────────────────────────────

    @discord.slash_command(name="status", description="Show current power station status")
    async def cmd_status(self, ctx: discord.ApplicationContext) -> None:
        # Acknowledge within the 3-second Discord window first.
        try:
            await ctx.defer(ephemeral=True)
        except discord.NotFound:
            # Interaction already expired (error 10062) — nothing we can do.
            return

        try:
            cfg = self.bot.cfg
            loop = asyncio.get_event_loop()

            # Fetch fresh data directly from the REST API (blocking call → executor).
            flat = await loop.run_in_executor(
                None,
                get_device_quota,
                cfg.api_host,
                cfg.ecoflow_access_key,
                cfg.ecoflow_secret_key,
                cfg.device_sn,
            )

            state  = DeviceState(flat, cfg.charging_watts_threshold)
            colour = COLOUR_CHARGING if state.is_charging else (COLOUR_STOPPED if state.is_charging is False else COLOUR_WARN)
            embed  = build_status_embed(state, cfg.device_sn, colour=colour)
            view   = StatusView(self.bot)
            await ctx.respond(embed=embed, view=view, ephemeral=True)

        except Exception:
            logger.exception("Unhandled error in /status")
            await ctx.respond(embed=discord.Embed(
                description="An unexpected error occurred.", colour=0xFF0000,
            ), ephemeral=True)

    # ── /ac ────────────────────────────────────────────────────────────

    @discord.slash_command(name="ac", description="Turn AC output on or off")
    async def cmd_ac(self, 
                    ctx: discord.ApplicationContext,
                    state: discord.Option(
                        str,
                        "on or off",
                        choices=["on", "off"]
                    )
                    ) -> None:
        await ctx.defer(ephemeral=True)

        try:
            monitor = await self.bot._monitor_ready()

            if monitor is None:
                await ctx.respond(embed=discord.Embed(
                    description="Not connected to EcoFlow. Cannot send command.",
                    colour=COLOUR_WARN,
                ), ephemeral=True)
                return

            enabled = state == "on"
            ok = await monitor.set_ac_output(
                enabled=enabled,
                voltage=self.bot.cfg.ac_out_voltage,
                freq=self.bot.cfg.ac_out_freq,
                xboost=self.bot.cfg.ac_xboost,
            )

            if ok:
                label  = "ON" if enabled else "OFF"
                colour = COLOUR_CHARGING if enabled else COLOUR_STOPPED
                embed  = discord.Embed(
                    description=f"AC output command sent — turning **{label}**.",
                    colour=colour,
                )
                embed.set_footer(text=f"Device: {self.bot.cfg.device_sn}")
            else:
                embed = discord.Embed(
                    description="Command failed — MQTT not connected.",
                    colour=COLOUR_WARN,
                )
            await ctx.respond(embed=embed, ephemeral=True)

        except Exception:
            logger.exception("Unhandled error in /ac")
            await ctx.respond(embed=discord.Embed(
                description="An unexpected error occurred.", colour=0xFF0000,
            ), ephemeral=True)

    # ── /usb ───────────────────────────────────────────────────────────

    @discord.slash_command(name="usb", description="Turn USB / DC output on or off")
    async def cmd_usb(self, 
                    ctx: discord.ApplicationContext,
                    state: discord.Option(
                        str,
                        "on or off",
                        choices=["on", "off"]
                    )
                    ) -> None:
        await ctx.defer(ephemeral=True)

        try:
            monitor = await self.bot._monitor_ready()

            if monitor is None:
                await ctx.respond(embed=discord.Embed(
                    description="Not connected to EcoFlow. Cannot send command.",
                    colour=COLOUR_WARN,
                ), ephemeral=True)
                return

            enabled = state == "on"
            ok = await monitor.set_usb_output(enabled)

            if ok:
                label  = "ON" if enabled else "OFF"
                colour = COLOUR_CHARGING if enabled else COLOUR_STOPPED
                embed  = discord.Embed(
                    description=f"USB / DC output command sent — turning **{label}**.",
                    colour=colour,
                )
                embed.set_footer(text=f"Device: {self.bot.cfg.device_sn}")
            else:
                embed = discord.Embed(
                    description="Command failed — MQTT not connected.",
                    colour=COLOUR_WARN,
                )
            await ctx.respond(embed=embed, ephemeral=True)

        except Exception:
            logger.exception("Unhandled error in /usb")
            await ctx.respond(embed=discord.Embed(
                description="An unexpected error occurred.", colour=0xFF0000,
            ), ephemeral=True)

    # ── /dc ────────────────────────────────────────────────────────────

    @discord.slash_command(name="dc", description="Turn 12 V car / cigarette-lighter port on or off")
    async def cmd_dc(self, 
                    ctx: discord.ApplicationContext,
                    state: discord.Option(
                        str,
                        "on or off",
                        choices=["on", "off"]
                    )
                    ) -> None:
        await ctx.defer(ephemeral=True)

        try:
            monitor = await self.bot._monitor_ready()

            if monitor is None:
                await ctx.respond(embed=discord.Embed(
                    description="Not connected to EcoFlow. Cannot send command.",
                    colour=COLOUR_WARN,
                ), ephemeral=True)
                return

            enabled = state == "on"
            ok = await monitor.set_dc_car_output(enabled)

            if ok:
                label  = "ON" if enabled else "OFF"
                colour = COLOUR_CHARGING if enabled else COLOUR_STOPPED
                embed  = discord.Embed(
                    description=f"12 V car port command sent — turning **{label}**.",
                    colour=colour,
                )
                embed.set_footer(text=f"Device: {self.bot.cfg.device_sn}")
            else:
                embed = discord.Embed(
                    description="Command failed — MQTT not connected.",
                    colour=COLOUR_WARN,
                )
            await ctx.respond(embed=embed, ephemeral=True)

        except Exception:
            logger.exception("Unhandled error in /dc")
            await ctx.respond(embed=discord.Embed(
                description="An unexpected error occurred.", colour=0xFF0000,
            ), ephemeral=True)


# ---------------------------------------------------------------------------
# Bot
# ---------------------------------------------------------------------------

class EcoFlowBot(discord.Bot):
    def __init__(self, config: Config) -> None:
        intents = discord.Intents.default()
        super().__init__(intents=intents)
        self.cfg = config
        self._monitor: EcoFlowMonitor | None = None
        self.add_cog(EcoFlowCog(self))

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def on_ready(self) -> None:
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
            creds = await self.loop.run_in_executor(
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
            self.loop,
        )

    def _on_mqtt_disconnect(self) -> None:
        logger.warning("MQTT disconnected — paho will attempt to reconnect")

    def _on_charging_start(self, state: DeviceState) -> None:
        asyncio.run_coroutine_threadsafe(
            self._notify_charging_start(state), self.loop
        )

    def _on_charging_stop(self, state: DeviceState) -> None:
        asyncio.run_coroutine_threadsafe(
            self._notify_charging_stop(state), self.loop
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

    async def _monitor_ready(self) -> EcoFlowMonitor | None:
        """Return the monitor if ready, or None."""
        monitor = self._monitor
        if monitor is None:
            return None
        monitor_connected = await monitor.is_connected
        return monitor if monitor_connected else None


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    try:
        config = Config.from_env()
    except EnvironmentError as exc:
        logger.error("Configuration error: %s", exc)
        sys.exit(1)

    # Python 3.12+ no longer implicitly creates an event loop on the main
    # thread.  discord.Bot.__init__ needs one before bot.run() sets things up,
    # so we create and register it explicitly.
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
