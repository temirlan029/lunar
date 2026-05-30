import os
import sqlite3
import threading
import asyncio
import signal
from datetime import UTC, datetime, time, timezone, timedelta
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import discord
from discord import app_commands
from discord.ext import commands, tasks


BASE_DIR = Path(__file__).resolve().parent


def load_dotenv_file(path: str = ".env") -> None:
    env_path = BASE_DIR / path
    if not env_path.exists():
        return

    with env_path.open("r", encoding="utf-8") as file:
        for raw_line in file:
            line = raw_line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue

            key, value = line.split("=", 1)
            key = key.strip()
            value = value.strip().strip('"').strip("'")
            if key and key not in os.environ:
                os.environ[key] = value


load_dotenv_file()

TOKEN = os.environ.get("DISCORD_TOKEN")
db_path_value = os.environ.get("VOICE_DB_PATH", "voice_stats.db")
DB_PATH = str((BASE_DIR / db_path_value).resolve()) if not os.path.isabs(db_path_value) else db_path_value
MOSCOW_TZ = timezone(timedelta(hours=3))
REPORT_TIMES = (time(hour=0, minute=0), time(hour=12, minute=0))
TARGET_GUILD_ID = int(os.environ["DISCORD_GUILD_ID"]) if os.environ.get("DISCORD_GUILD_ID") else None
TARGET_GUILD_OBJECT = discord.Object(id=TARGET_GUILD_ID) if TARGET_GUILD_ID is not None else None
PORT = int(os.environ.get("PORT", "0"))
COMMAND_KWARGS = {"guild": TARGET_GUILD_OBJECT} if TARGET_GUILD_OBJECT is not None else {}
ADMIN_ROLE_ID = 1508160990062317668
health_server: HTTPServer | None = None
LOG_PATH = BASE_DIR / "bot.log"

intents = discord.Intents.default()
intents.voice_states = True
intents.members = True

bot = commands.Bot(command_prefix=commands.when_mentioned, intents=intents)
resolved_guild_id: int | None = TARGET_GUILD_ID


def utc_now() -> datetime:
    return datetime.now(UTC)


def log(message: str) -> None:
    timestamp = datetime.now(MOSCOW_TZ).strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{timestamp}] {message}"
    print(line)
    with LOG_PATH.open("a", encoding="utf-8") as file:
        file.write(line + "\n")


def init_db() -> None:
    # Таблица статистики хранит общее время пользователя на одном сервере.
    Path(DB_PATH).parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS voice_time (
                user_id INTEGER PRIMARY KEY,
                total_time REAL NOT NULL DEFAULT 0
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS active_sessions (
                user_id INTEGER PRIMARY KEY,
                started_at TEXT NOT NULL,
                last_seen_at TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS guild_settings (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                report_channel_id INTEGER
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS sent_reports (
                report_key TEXT PRIMARY KEY
            )
            """
        )


def parse_dt(value: str) -> datetime:
    return datetime.fromisoformat(value)


def store_total_time(user_id: int, seconds: float) -> None:
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            """
            INSERT INTO voice_time (user_id, total_time)
            VALUES (?, ?)
            ON CONFLICT(user_id)
            DO UPDATE SET total_time = total_time + excluded.total_time
            """,
            (user_id, seconds),
        )


def get_report_channel_id() -> int | None:
    with sqlite3.connect(DB_PATH) as conn:
        row = conn.execute("SELECT report_channel_id FROM guild_settings WHERE id = 1").fetchone()
    return row[0] if row and row[0] else None


def set_report_channel_id(channel_id: int) -> None:
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            """
            INSERT INTO guild_settings (id, report_channel_id)
            VALUES (1, ?)
            ON CONFLICT(id)
            DO UPDATE SET report_channel_id = excluded.report_channel_id
            """,
            (channel_id,),
        )


def was_report_sent(report_key: str) -> bool:
    with sqlite3.connect(DB_PATH) as conn:
        row = conn.execute("SELECT 1 FROM sent_reports WHERE report_key = ?", (report_key,)).fetchone()
    return row is not None


def mark_report_sent(report_key: str) -> None:
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("INSERT OR IGNORE INTO sent_reports (report_key) VALUES (?)", (report_key,))


def format_duration(seconds: float) -> str:
    seconds = int(seconds)
    hours, remainder = divmod(seconds, 3600)
    minutes, seconds = divmod(remainder, 60)

    if hours:
        return f"{hours} ч. {minutes} мин."
    if minutes:
        return f"{minutes} мин. {seconds} сек."
    return f"{seconds} сек."


def build_active_sessions_embed(guild: discord.Guild) -> discord.Embed:
    now = utc_now()
    lines: list[str] = []

    with sqlite3.connect(DB_PATH) as conn:
        rows = conn.execute("SELECT user_id, started_at FROM active_sessions ORDER BY started_at ASC").fetchall()

    for user_id, started_at_raw in rows:
        started_at = parse_dt(started_at_raw)
        member = guild.get_member(user_id)
        name = member.mention if member else f"<@{user_id}>"
        lines.append(f"• {name} — `{format_duration((now - started_at).total_seconds())}`")

    return discord.Embed(
        title="Активные голосовые сессии",
        description="\n".join(lines) if lines else "Сейчас никто не находится в голосовых каналах.",
        color=discord.Color.green(),
        timestamp=datetime.now(MOSCOW_TZ),
    )


def build_dashboard_embed() -> discord.Embed:
    return discord.Embed(
        title="Проверка статистики",
        description="Нажмите кнопку ниже, чтобы открыть меню проверки статистики.",
        color=discord.Color.green(),
    )


def is_target_guild(guild_id: int) -> bool:
    return resolved_guild_id is None or guild_id == resolved_guild_id


def has_voice_admin_role(member: discord.Member) -> bool:
    return any(role.id == ADMIN_ROLE_ID for role in member.roles)


def resolve_target_guild() -> discord.Guild | None:
    if resolved_guild_id is not None:
        guild = bot.get_guild(resolved_guild_id)
        if guild is not None:
            return guild
    if len(bot.guilds) == 1:
        return bot.guilds[0]
    return bot.guilds[0] if bot.guilds else None


def get_total_time_with_active_sessions() -> dict[int, float]:
    totals: dict[int, float] = {}

    with sqlite3.connect(DB_PATH) as conn:
        rows = conn.execute("SELECT user_id, total_time FROM voice_time").fetchall()

    for user_id, total_time in rows:
        totals[user_id] = float(total_time)

    now = utc_now()
    with sqlite3.connect(DB_PATH) as conn:
        active_rows = conn.execute("SELECT user_id, started_at FROM active_sessions").fetchall()

    for user_id, started_at_raw in active_rows:
        totals[user_id] = totals.get(user_id, 0) + (now - parse_dt(started_at_raw)).total_seconds()

    return totals


def get_user_total_time(user_id: int) -> float:
    total = 0.0
    with sqlite3.connect(DB_PATH) as conn:
        row = conn.execute("SELECT total_time FROM voice_time WHERE user_id = ?", (user_id,)).fetchone()
        if row is not None:
            total = float(row[0])

    with sqlite3.connect(DB_PATH) as conn:
        row = conn.execute("SELECT started_at FROM active_sessions WHERE user_id = ?", (user_id,)).fetchone()

    if row is not None:
        total += (utc_now() - parse_dt(row[0])).total_seconds()

    return total


def start_session(user_id: int, started_at: datetime | None = None) -> None:
    started_at = started_at or utc_now()

    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            """
            INSERT INTO active_sessions (user_id, started_at, last_seen_at)
            VALUES (?, ?, ?)
            ON CONFLICT(user_id)
            DO UPDATE SET started_at = excluded.started_at, last_seen_at = excluded.last_seen_at
            """,
            (user_id, started_at.isoformat(), started_at.isoformat()),
        )


def finish_session(user_id: int, finished_at: datetime | None = None) -> None:
    finished_at = finished_at or utc_now()

    with sqlite3.connect(DB_PATH) as conn:
        row = conn.execute(
            "SELECT started_at FROM active_sessions WHERE user_id = ?",
            (user_id,),
        ).fetchone()

        if row is None:
            return

        started_at = parse_dt(row[0])
        elapsed = max(0.0, (finished_at - started_at).total_seconds())
        if elapsed > 0:
            conn.execute("INSERT OR IGNORE INTO voice_time (user_id, total_time) VALUES (?, 0)", (user_id,))
            conn.execute(
                "UPDATE voice_time SET total_time = total_time + ? WHERE user_id = ?",
                (elapsed, user_id),
            )
        conn.execute("DELETE FROM active_sessions WHERE user_id = ?", (user_id,))


def reset_user_stats(user_id: int) -> None:
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("DELETE FROM active_sessions WHERE user_id = ?", (user_id,))
        conn.execute("DELETE FROM voice_time WHERE user_id = ?", (user_id,))


def refresh_active_sessions() -> None:
    now_iso = utc_now().isoformat()
    with sqlite3.connect(DB_PATH) as conn:
        rows = conn.execute("SELECT user_id FROM active_sessions").fetchall()
        if not rows:
            return
        conn.executemany(
            "UPDATE active_sessions SET last_seen_at = ? WHERE user_id = ?",
            [(now_iso, user_id) for (user_id,) in rows],
        )


def bootstrap_active_sessions(guild: discord.Guild) -> None:
    current_voice_users = {member.id for channel in guild.voice_channels for member in channel.members if not member.bot}

    with sqlite3.connect(DB_PATH) as conn:
        rows = conn.execute("SELECT user_id, started_at, last_seen_at FROM active_sessions").fetchall()

    seen_user_ids: set[int] = set()

    for user_id, started_at_raw, last_seen_raw in rows:
        seen_user_ids.add(user_id)

        if user_id in current_voice_users:
            continue

        finish_session(user_id, parse_dt(last_seen_raw))

    now = utc_now()
    for user_id in current_voice_users - seen_user_ids:
        start_session(user_id, now)


async def build_stats_embeds(guild: discord.Guild, *, limit: int | None = None) -> list[discord.Embed]:
    totals = get_total_time_with_active_sessions()
    sorted_totals = sorted(totals.items(), key=lambda item: item[1], reverse=True)

    if limit is not None:
        sorted_totals = sorted_totals[:limit]

    if not sorted_totals:
        embed = discord.Embed(
            title="Статистика голосовых каналов",
            description="Пока нет накопленной статистики.",
            color=discord.Color.blurple(),
        )
        return [embed]

    lines: list[str] = []
    for index, (user_id, total_time) in enumerate(sorted_totals, start=1):
        member = guild.get_member(user_id)
        name = member.mention if member else f"<@{user_id}>"
        lines.append(f"**{index}.** {name} — `{format_duration(total_time)}`")

    embeds: list[discord.Embed] = []
    chunk_size = 20
    total_pages = (len(lines) - 1) // chunk_size + 1
    for page, start in enumerate(range(0, len(lines), chunk_size), start=1):
        chunk = lines[start : start + chunk_size]
        title = "Топ-10 по времени в голосовых каналах" if limit else "Отчет по всем участникам"
        embed = discord.Embed(
            title=title,
            description="\n".join(chunk),
            color=discord.Color.blurple(),
            timestamp=datetime.now(MOSCOW_TZ),
        )
        embed.set_footer(text=f"Страница {page}/{total_pages} • Москва")
        embeds.append(embed)

    return embeds


async def build_voice_stats_embed(guild: discord.Guild) -> discord.Embed:
    return (await build_stats_embeds(guild, limit=10))[0]


def build_member_stats_embed(member: discord.Member) -> discord.Embed:
    total_time = get_user_total_time(member.id)
    return discord.Embed(
        title="Моя статистика",
        description=f"{member.mention}\nВремя в голосовых каналах: `{format_duration(total_time)}`",
        color=discord.Color.blurple(),
        timestamp=datetime.now(MOSCOW_TZ),
    )


dashboard_delete_tasks: dict[int, asyncio.Task] = {}
ephemeral_cleanup_tasks: dict[int, asyncio.Task] = {}


def cancel_dashboard_delete(message_id: int) -> None:
    task = dashboard_delete_tasks.pop(message_id, None)
    if task is not None:
        task.cancel()


def schedule_dashboard_delete(message: discord.Message, delay: int = 60) -> None:
    cancel_dashboard_delete(message.id)

    async def _delete_later() -> None:
        try:
            await asyncio.sleep(delay)
            await message.delete()
        except (asyncio.CancelledError, discord.NotFound, discord.Forbidden, discord.HTTPException):
            return
        finally:
            dashboard_delete_tasks.pop(message.id, None)

    dashboard_delete_tasks[message.id] = asyncio.create_task(_delete_later())


def schedule_ephemeral_cleanup(interaction: discord.Interaction, delay: int = 60) -> None:
    if interaction.message is not None:
        key = interaction.message.id
    else:
        key = interaction.id

    task = ephemeral_cleanup_tasks.pop(key, None)
    if task is not None:
        task.cancel()

    async def _cleanup_later() -> None:
        try:
            await asyncio.sleep(delay)
            try:
                await interaction.delete_original_response()
                return
            except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                try:
                    await interaction.edit_original_response(content="Панель устарела.", embed=None, view=None)
                except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                    return
        except asyncio.CancelledError:
            return
        finally:
            ephemeral_cleanup_tasks.pop(key, None)

    ephemeral_cleanup_tasks[key] = asyncio.create_task(_cleanup_later())


class VoicePanelOpenView(discord.ui.View):
    def __init__(self) -> None:
        super().__init__(timeout=None)

    @discord.ui.button(label="Проверить", style=discord.ButtonStyle.success, custom_id="lunar:voice_panel:open")
    async def open_button(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ) -> None:
        if interaction.guild is None or not is_target_guild(interaction.guild.id):
            await interaction.response.send_message("Эта панель доступна только на целевом сервере.", ephemeral=True)
            return

        await interaction.response.send_message(
            embed=build_dashboard_embed(),
            view=VoiceDashboardView(show_reset=isinstance(interaction.user, discord.Member) and has_voice_admin_role(interaction.user)),
            ephemeral=True,
        )
        schedule_ephemeral_cleanup(interaction)


class VoiceDashboardView(discord.ui.View):
    def __init__(self, *, show_reset: bool = False) -> None:
        super().__init__(timeout=None)
        if not show_reset:
            for item in list(self.children):
                if isinstance(item, discord.ui.Button) and item.custom_id == "lunar:voice:reset":
                    self.remove_item(item)

    async def _update(self, interaction: discord.Interaction, *, embed: discord.Embed, view: discord.ui.View | None = None) -> None:
        await interaction.response.edit_message(embed=embed, content=None, view=view or self)
        schedule_ephemeral_cleanup(interaction)

    @discord.ui.button(label="Моя статистика", style=discord.ButtonStyle.primary, custom_id="lunar:voice:my_stats")
    async def my_stats_button(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ) -> None:
        if interaction.guild is None or not is_target_guild(interaction.guild.id):
            await interaction.response.send_message("Эта панель доступна только на целевом сервере.", ephemeral=True)
            return

        member = interaction.user if isinstance(interaction.user, discord.Member) else None
        if member is None:
            await interaction.response.send_message("Не удалось определить участника.", ephemeral=True)
            return

        await self._update(interaction, embed=build_member_stats_embed(member))

    @discord.ui.button(label="Участник", style=discord.ButtonStyle.primary, custom_id="lunar:voice:member_stats")
    async def member_stats_button(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ) -> None:
        if interaction.guild is None or not is_target_guild(interaction.guild.id):
            await interaction.response.send_message("Эта панель доступна только на целевом сервере.", ephemeral=True)
            return

        if not isinstance(interaction.user, discord.Member) or not has_voice_admin_role(interaction.user):
            await interaction.response.send_message("Эта кнопка доступна только для админ-роля.", ephemeral=True)
            return

        await interaction.response.edit_message(
            content="Выберите участника для просмотра статистики.",
            embed=None,
            view=VoiceMemberStatsSelectView(),
        )
        schedule_ephemeral_cleanup(interaction)

    @discord.ui.button(label="Топ 10", style=discord.ButtonStyle.primary, custom_id="lunar:voice:top10")
    async def voice_stats_button(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ) -> None:
        if interaction.guild is None or not is_target_guild(interaction.guild.id):
            await interaction.response.send_message("Эта панель доступна только на целевом сервере.", ephemeral=True)
            return

        await self._update(interaction, embed=await build_voice_stats_embed(interaction.guild))

    @discord.ui.button(label="Активные голосовые каналы", style=discord.ButtonStyle.primary, custom_id="lunar:voice:active")
    async def voice_active_button(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ) -> None:
        if interaction.guild is None or not is_target_guild(interaction.guild.id):
            await interaction.response.send_message("Эта панель доступна только на целевом сервере.", ephemeral=True)
            return

        await self._update(interaction, embed=build_active_sessions_embed(interaction.guild))

    @discord.ui.button(label="Назад", style=discord.ButtonStyle.secondary, custom_id="lunar:voice:back")
    async def back_button(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ) -> None:
        if interaction.guild is None or not is_target_guild(interaction.guild.id):
            await interaction.response.send_message("Эта панель доступна только на целевом сервере.", ephemeral=True)
            return

        await self._update(interaction, embed=build_dashboard_embed())

    @discord.ui.button(label="Сбросить", style=discord.ButtonStyle.danger, custom_id="lunar:voice:reset")
    async def reset_button(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ) -> None:
        if interaction.guild is None or not is_target_guild(interaction.guild.id):
            await interaction.response.send_message("Эта панель доступна только на целевом сервере.", ephemeral=True)
            return

        if not isinstance(interaction.user, discord.Member) or not has_voice_admin_role(interaction.user):
            await interaction.response.send_message("Эта кнопка доступна только для админ-роля.", ephemeral=True)
            return

        await interaction.response.edit_message(content="Выберите участника для сброса статистики.", embed=None, view=VoiceResetSelectView())
        schedule_ephemeral_cleanup(interaction)


class VoiceResetSelect(discord.ui.UserSelect):
    def __init__(self) -> None:
        super().__init__(placeholder="Выберите участника", min_values=1, max_values=1)

    async def callback(self, interaction: discord.Interaction) -> None:
        if interaction.guild is None or not is_target_guild(interaction.guild.id):
            await interaction.response.send_message("Эта панель доступна только на целевом сервере.", ephemeral=True)
            return

        if not isinstance(interaction.user, discord.Member) or not has_voice_admin_role(interaction.user):
            await interaction.response.send_message("Эта кнопка доступна только для админ-роля.", ephemeral=True)
            return

        selected = self.values[0]
        member = interaction.guild.get_member(selected.id)
        if member is None:
            try:
                member = await interaction.guild.fetch_member(selected.id)
            except discord.HTTPException:
                await interaction.response.send_message("Не удалось найти участника на сервере.", ephemeral=True)
                return

        if member.bot:
            await interaction.response.send_message("Нельзя сбрасывать статистику бота.", ephemeral=True)
            return

        reset_user_stats(member.id)
        embed = discord.Embed(
            title="Статистика сброшена",
            description=f"Статистика пользователя {member.mention} сброшена.",
            color=discord.Color.red(),
            timestamp=datetime.now(MOSCOW_TZ),
        )
        await interaction.response.edit_message(embed=embed, content=None, view=VoiceDashboardView(show_reset=True))
        schedule_ephemeral_cleanup(interaction)


class VoiceMemberStatsSelect(discord.ui.UserSelect):
    def __init__(self) -> None:
        super().__init__(placeholder="Выберите участника", min_values=1, max_values=1)

    async def callback(self, interaction: discord.Interaction) -> None:
        if interaction.guild is None or not is_target_guild(interaction.guild.id):
            await interaction.response.send_message("Эта панель доступна только на целевом сервере.", ephemeral=True)
            return

        if not isinstance(interaction.user, discord.Member) or not has_voice_admin_role(interaction.user):
            await interaction.response.send_message("Эта кнопка доступна только для админ-роля.", ephemeral=True)
            return

        selected = self.values[0]
        member = interaction.guild.get_member(selected.id)
        if member is None:
            try:
                member = await interaction.guild.fetch_member(selected.id)
            except discord.HTTPException:
                await interaction.response.send_message("Не удалось найти участника на сервере.", ephemeral=True)
                return

        embed = discord.Embed(
            title="Статистика участника",
            description=f"{member.mention}\nВремя в голосовых каналах: `{format_duration(get_user_total_time(member.id))}`",
            color=discord.Color.blurple(),
            timestamp=datetime.now(MOSCOW_TZ),
        )
        await interaction.response.edit_message(embed=embed, content=None, view=VoiceDashboardView(show_reset=True))
        schedule_ephemeral_cleanup(interaction)


class VoiceResetSelectView(discord.ui.View):
    def __init__(self) -> None:
        super().__init__(timeout=None)
        self.add_item(VoiceResetSelect())

    @discord.ui.button(label="Назад", style=discord.ButtonStyle.secondary, custom_id="lunar:voice:reset_back")
    async def back_button(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ) -> None:
        if interaction.guild is None or not is_target_guild(interaction.guild.id):
            await interaction.response.send_message("Эта панель доступна только на целевом сервере.", ephemeral=True)
            return

        if not isinstance(interaction.user, discord.Member) or not has_voice_admin_role(interaction.user):
            await interaction.response.send_message("Эта кнопка доступна только для админ-роля.", ephemeral=True)
            return

        await interaction.response.edit_message(embed=build_dashboard_embed(), content=None, view=VoiceDashboardView(show_reset=True))
        schedule_ephemeral_cleanup(interaction)


class VoiceMemberStatsSelectView(discord.ui.View):
    def __init__(self) -> None:
        super().__init__(timeout=None)
        self.add_item(VoiceMemberStatsSelect())

    @discord.ui.button(label="Назад", style=discord.ButtonStyle.secondary, custom_id="lunar:voice:member_stats_back")
    async def back_button(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ) -> None:
        if interaction.guild is None or not is_target_guild(interaction.guild.id):
            await interaction.response.send_message("Эта панель доступна только на целевом сервере.", ephemeral=True)
            return

        if not isinstance(interaction.user, discord.Member) or not has_voice_admin_role(interaction.user):
            await interaction.response.send_message("Эта кнопка доступна только для админ-роля.", ephemeral=True)
            return

        await interaction.response.edit_message(embed=build_dashboard_embed(), content=None, view=VoiceDashboardView(show_reset=True))
        schedule_ephemeral_cleanup(interaction)


bot.add_view(VoicePanelOpenView())
bot.add_view(VoiceDashboardView())


def start_health_server() -> None:
    global health_server
    if PORT <= 0:
        return

    class HealthHandler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            if self.path not in ("/", "/health", "/healthz"):
                self.send_response(404)
                self.end_headers()
                return

            body = b"ok"
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format: str, *args: object) -> None:
            return

    health_server = HTTPServer(("0.0.0.0", PORT), HealthHandler)
    thread = threading.Thread(target=health_server.serve_forever, daemon=True)
    thread.start()


async def graceful_shutdown() -> None:
    if active_heartbeat.is_running():
        active_heartbeat.stop()
    if scheduled_reports.is_running():
        scheduled_reports.stop()
    if health_server is not None:
        health_server.shutdown()
    if not bot.is_closed():
        await bot.close()


@bot.event
async def on_ready() -> None:
    init_db()

    guild = resolve_target_guild()
    if guild is None:
        print("Бот запущен, но целевой сервер еще не найден.")
        return

    global resolved_guild_id
    resolved_guild_id = guild.id
    bootstrap_active_sessions(guild)

    if not active_heartbeat.is_running():
        active_heartbeat.start()

    if not scheduled_reports.is_running():
        scheduled_reports.start()

    if TARGET_GUILD_OBJECT is not None:
        synced = await bot.tree.sync(guild=TARGET_GUILD_OBJECT)
        log(f"Синхронизировано серверных команд: {len(synced)}")
    else:
        synced = await bot.tree.sync()
        log(f"Синхронизировано глобальных команд: {len(synced)}")

    log(f"Бот запущен как {bot.user} для сервера {guild.name} ({guild.id})")


@bot.event
async def on_voice_state_update(
    member: discord.Member,
    before: discord.VoiceState,
    after: discord.VoiceState,
) -> None:
    if member.bot or not is_target_guild(member.guild.id):
        return

    before_channel = before.channel
    after_channel = after.channel

    if before_channel == after_channel:
        return

    # Переход между голосовыми каналами внутри одного сервера не завершает сессию.
    if before_channel is not None and after_channel is not None:
        return

    # Пользователь вошел в голосовой канал.
    if before_channel is None and after_channel is not None:
        start_session(member.id)
        return

    # Пользователь вышел из голосового канала.
    if before_channel is not None and after_channel is None:
        finish_session(member.id)


@bot.tree.command(name="voice_stats", description="Показать топ-10 участников по времени в голосовых каналах", **COMMAND_KWARGS)
async def voice_stats(interaction: discord.Interaction) -> None:
    if interaction.guild is None or not is_target_guild(interaction.guild.id):
        await interaction.response.send_message("Эта команда доступна только на целевом сервере.", ephemeral=True)
        return

    embeds = await build_stats_embeds(interaction.guild, limit=10)
    await interaction.response.send_message(embeds=embeds)


@bot.tree.command(name="voice_active", description="Показать активные голосовые сессии сейчас", **COMMAND_KWARGS)
async def voice_active(interaction: discord.Interaction) -> None:
    if interaction.guild is None or not is_target_guild(interaction.guild.id):
        await interaction.response.send_message("Эта команда доступна только на целевом сервере.", ephemeral=True)
        return

    embed = build_active_sessions_embed(interaction.guild)
    await interaction.response.send_message(embed=embed, ephemeral=True)


@bot.tree.command(name="report", description="Отправить текущий отчет по голосовым каналам", **COMMAND_KWARGS)
async def report(interaction: discord.Interaction) -> None:
    if interaction.guild is None or not is_target_guild(interaction.guild.id):
        await interaction.response.send_message("Эта команда доступна только на целевом сервере.", ephemeral=True)
        return

    embeds = await build_stats_embeds(interaction.guild)
    await interaction.response.send_message(embeds=embeds[:10], ephemeral=True)


@bot.tree.command(name="help", description="Показать список команд и их назначение", **COMMAND_KWARGS)
async def help_command(interaction: discord.Interaction) -> None:
    if interaction.guild is None or not is_target_guild(interaction.guild.id):
        await interaction.response.send_message("Эта команда доступна только на целевом сервере.", ephemeral=True)
        return

    embed = discord.Embed(
        title="Помощь по командам Lunar",
        description=(
            "**/voice_panel** — отправляет панель с кнопками голосовой статистики.\n"
            "**Участник** — открывает выбор участника для просмотра его статистики.\n"
            "**/reset** — сбрасывает статистику выбранного участника (только для админ-роля).\n"
            "**/report** — отправляет текущий полный отчет.\n"
            "**/stats_user** — показывает статистику выбранного участника.\n"
            "**/set_report_channel** — задает канал для автоотчетов.\n"
        ),
        color=discord.Color.orange(),
    )
    embed.add_field(
        name="Как работает подсчет",
        value=(
            "Бот считает время, которое участник реально провел в голосовых каналах.\n"
            "Переход между voice-каналами внутри сервера не сбрасывает сессию.\n"
            "Активные сессии хранятся в SQLite и не держатся только в памяти."
        ),
        inline=False,
    )
    await interaction.response.send_message(embed=embed, ephemeral=True)


@bot.tree.command(name="voice_panel", description="Отправить панель с кнопками для проверки статистики", **COMMAND_KWARGS)
async def voice_panel(interaction: discord.Interaction) -> None:
    if interaction.guild is None or not is_target_guild(interaction.guild.id):
        await interaction.response.send_message("Эта команда доступна только на целевом сервере.", ephemeral=True)
        return

    if not isinstance(interaction.user, discord.Member) or not has_voice_admin_role(interaction.user):
        await interaction.response.send_message("Эта команда доступна только для админ-роля.", ephemeral=True)
        return

    await interaction.response.send_message(
        embed=discord.Embed(
            title="Проверка",
            description="Нажмите кнопку ниже, чтобы открыть панель голосовой статистики.",
            color=discord.Color.green(),
        ),
        view=VoicePanelOpenView(),
    )


@bot.tree.command(name="stats_user", description="Показать статистику конкретного участника", **COMMAND_KWARGS)
async def stats_user(
    interaction: discord.Interaction,
    member: discord.Member,
) -> None:
    if interaction.guild is None or not is_target_guild(interaction.guild.id):
        await interaction.response.send_message("Эта команда доступна только на целевом сервере.", ephemeral=True)
        return

    total_time = get_user_total_time(member.id)
    embed = discord.Embed(
        title="Статистика участника",
        description=f"{member.mention}\nВремя в голосовых каналах: `{format_duration(total_time)}`",
        color=discord.Color.blurple(),
        timestamp=datetime.now(MOSCOW_TZ),
    )
    await interaction.response.send_message(embed=embed, ephemeral=True)


@bot.tree.command(name="reset", description="Сбросить статистику конкретного участника", **COMMAND_KWARGS)
async def reset(
    interaction: discord.Interaction,
    member: discord.Member,
) -> None:
    if interaction.guild is None or not is_target_guild(interaction.guild.id):
        await interaction.response.send_message("Эта команда доступна только на целевом сервере.", ephemeral=True)
        return

    if not isinstance(interaction.user, discord.Member) or not has_voice_admin_role(interaction.user):
        await interaction.response.send_message("Эта команда доступна только для админ-роля.", ephemeral=True)
        return

    if member.bot:
        await interaction.response.send_message("Нельзя сбрасывать статистику бота.", ephemeral=True)
        return

    reset_user_stats(member.id)
    await interaction.response.send_message(
        f"Статистика пользователя {member.mention} сброшена.",
        ephemeral=True,
    )


@bot.tree.command(name="set_report_channel", description="Выбрать канал для отчетов каждые 12 часов", **COMMAND_KWARGS)
@app_commands.default_permissions(manage_guild=True)
async def set_report_channel(
    interaction: discord.Interaction,
    channel: discord.TextChannel,
) -> None:
    if interaction.guild is None or not is_target_guild(interaction.guild.id):
        await interaction.response.send_message("Эта команда доступна только на целевом сервере.", ephemeral=True)
        return

    set_report_channel_id(channel.id)
    await interaction.response.send_message(
        f"Канал для отчетов установлен: {channel.mention}",
        ephemeral=True,
    )


@tasks.loop(minutes=1)
async def active_heartbeat() -> None:
    refresh_active_sessions()


@tasks.loop(minutes=1)
async def scheduled_reports() -> None:
    now = datetime.now(MOSCOW_TZ)
    current_time = time(hour=now.hour, minute=now.minute)

    if current_time not in REPORT_TIMES or resolved_guild_id is None:
        return

    report_key = now.strftime("%Y-%m-%d-%H-%M")
    if was_report_sent(report_key):
        return

    guild = bot.get_guild(resolved_guild_id)
    if guild is None:
        return

    channel_id = get_report_channel_id()
    if channel_id is None:
        return

    channel = guild.get_channel(channel_id)
    if not isinstance(channel, discord.TextChannel):
        try:
            fetched = await guild.fetch_channel(channel_id)
        except discord.HTTPException:
            return
        if not isinstance(fetched, discord.TextChannel):
            return
        channel = fetched

    embeds = await build_stats_embeds(guild)
    await channel.send(
        content=f"Автоматический отчет за {now.strftime('%d.%m.%Y %H:%M')} МСК",
        embeds=embeds[:10],
    )
    mark_report_sent(report_key)


@active_heartbeat.before_loop
async def before_active_heartbeat() -> None:
    await bot.wait_until_ready()


@scheduled_reports.before_loop
async def before_scheduled_reports() -> None:
    await bot.wait_until_ready()


async def main() -> None:
    init_db()
    start_health_server()
    log("Запуск бота")

    if not TOKEN:
        raise RuntimeError("Переменная окружения DISCORD_TOKEN не установлена.")

    loop = asyncio.get_running_loop()

    def request_shutdown() -> None:
        loop.create_task(graceful_shutdown())

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, request_shutdown)
        except NotImplementedError:
            signal.signal(sig, lambda *_: request_shutdown())

    try:
        await bot.start(TOKEN)
    except Exception as exc:
        log(f"Ошибка бота: {exc!r}")
        raise


if __name__ == "__main__":
    asyncio.run(main())
