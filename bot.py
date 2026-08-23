import ast
import asyncio
import aiohttp
import io
import os
import re
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import urlencode

import discord
import qrcode
from discord import app_commands
from discord.ext import commands
from dotenv import load_dotenv
import wallet

load_dotenv()

TOKEN = os.getenv("DISCORD_TOKEN")
DATABASE_PATH = Path(os.getenv("DATABASE_PATH", "bot_data.sqlite3"))
LOG_CHANNEL_ID = int(os.getenv("LOG_CHANNEL_ID", "0"))
DISCORD_GUILD_ID = int(os.getenv("DISCORD_GUILD_ID", "0"))
OWNER_ID = int(os.getenv("OWNER_ID", "0"))

if not TOKEN:
    raise RuntimeError("DISCORD_TOKEN is missing. Copy .env.example to .env and add your bot token.")
if not OWNER_ID:
    raise RuntimeError("OWNER_ID is missing. Add your Discord user ID to .env.")


@contextmanager
def database_connection():
    connection = sqlite3.connect(DATABASE_PATH)
    try:
        yield connection
        connection.commit()
    finally:
        connection.close()


def setup_database() -> None:
    with database_connection() as connection:
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS user_settings (
                user_id INTEGER PRIMARY KEY,
                ltc_address TEXT NOT NULL,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS bot_settings (
                setting_name TEXT PRIMARY KEY,
                setting_value TEXT NOT NULL
            )
            """
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS app_users (
                user_id INTEGER PRIMARY KEY,
                first_seen_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS payment_settings (
                user_id INTEGER PRIMARY KEY,
                ltc_address TEXT,
                upi_id TEXT,
                qr_image BLOB,
                qr_filename TEXT
            )
            """
        )
        payment_columns = {
            row[1]
            for row in connection.execute("PRAGMA table_info(payment_settings)")
        }
        if "qr_image" not in payment_columns:
            connection.execute("ALTER TABLE payment_settings ADD COLUMN qr_image BLOB")
        if "qr_filename" not in payment_columns:
            connection.execute(
                "ALTER TABLE payment_settings ADD COLUMN qr_filename TEXT"
            )
        existing_columns = {
            row[1]
            for row in connection.execute("PRAGMA table_info(auto_responses)")
        }
        if existing_columns and "response_name" not in existing_columns:
            connection.execute("ALTER TABLE auto_responses RENAME TO auto_responses_legacy")

        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS auto_responses (
                user_id INTEGER NOT NULL,
                response_name TEXT NOT NULL,
                response_text TEXT NOT NULL,
                PRIMARY KEY (user_id, response_name)
            )
            """
        )
        if existing_columns and "response_name" not in existing_columns:
            connection.execute(
                """
                INSERT OR IGNORE INTO auto_responses (user_id, response_name, response_text)
                SELECT user_id, 'default', response_text
                FROM auto_responses_legacy
                """
            )


def save_ltc_address(user_id: int, address: str) -> None:
    with database_connection() as connection:
        connection.execute(
            """
            INSERT INTO payment_settings (user_id, ltc_address)
            VALUES (?, ?)
            ON CONFLICT(user_id) DO UPDATE SET ltc_address = excluded.ltc_address
            """,
            (user_id, address),
        )


def get_ltc_address(user_id: int) -> str | None:
    with database_connection() as connection:
        row = connection.execute(
            "SELECT ltc_address FROM payment_settings WHERE user_id = ?", (user_id,)
        ).fetchone()
    return row[0] if row else None


def save_upi_id(user_id: int, upi_id: str) -> None:
    with database_connection() as connection:
        connection.execute(
            """
            INSERT INTO payment_settings (user_id, upi_id)
            VALUES (?, ?)
            ON CONFLICT(user_id) DO UPDATE SET upi_id = excluded.upi_id
            """,
            (user_id, upi_id),
        )


def get_upi_id(user_id: int) -> str | None:
    with database_connection() as connection:
        row = connection.execute(
            "SELECT upi_id FROM payment_settings WHERE user_id = ?", (user_id,)
        ).fetchone()
    return row[0] if row else None


def save_qr_image(user_id: int, image_data: bytes, filename: str) -> None:
    with database_connection() as connection:
        connection.execute(
            """
            INSERT INTO payment_settings (user_id, qr_image, qr_filename)
            VALUES (?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET
                qr_image = excluded.qr_image,
                qr_filename = excluded.qr_filename
            """,
            (user_id, image_data, filename),
        )


def get_qr_image(user_id: int) -> tuple[bytes, str] | None:
    with database_connection() as connection:
        row = connection.execute(
            "SELECT qr_image, qr_filename FROM payment_settings WHERE user_id = ?",
            (user_id,),
        ).fetchone()
    if not row or row[0] is None:
        return None
    return bytes(row[0]), row[1] or "upi-qr.png"


def delete_qr_image(user_id: int) -> bool:
    with database_connection() as connection:
        cursor = connection.execute(
            """
            UPDATE payment_settings
            SET qr_image = NULL, qr_filename = NULL
            WHERE user_id = ? AND qr_image IS NOT NULL
            """,
            (user_id,),
        )
    return cursor.rowcount > 0


def get_bot_stats() -> tuple[int, int, int, int, int]:
    with database_connection() as connection:
        app_user_count = connection.execute(
            "SELECT COUNT(*) FROM app_users"
        ).fetchone()[0]
        ltc_address_count = connection.execute(
            "SELECT COUNT(*) FROM payment_settings WHERE ltc_address IS NOT NULL AND ltc_address != ''"
        ).fetchone()[0]
        auto_response_count = connection.execute(
            "SELECT COUNT(*) FROM auto_responses"
        ).fetchone()[0]
        upi_id_count = connection.execute(
            "SELECT COUNT(*) FROM payment_settings WHERE upi_id IS NOT NULL AND upi_id != ''"
        ).fetchone()[0]
        auto_response_user_count = connection.execute(
            "SELECT COUNT(DISTINCT user_id) FROM auto_responses"
        ).fetchone()[0]
    return (
        int(app_user_count),
        int(ltc_address_count),
        int(upi_id_count),
        int(auto_response_count),
        int(auto_response_user_count),
    )


def save_auto_response(user_id: int, response_name: str, response_text: str) -> None:
    with database_connection() as connection:
        connection.execute(
            """
            INSERT INTO auto_responses (user_id, response_name, response_text)
            VALUES (?, ?, ?)
            ON CONFLICT(user_id, response_name)
            DO UPDATE SET response_text = excluded.response_text
            """,
            (user_id, response_name, response_text),
        )


def get_auto_response(user_id: int, response_name: str) -> str | None:
    with database_connection() as connection:
        row = connection.execute(
            """
            SELECT response_text FROM auto_responses
            WHERE user_id = ? AND response_name = ?
            """,
            (user_id, response_name),
        ).fetchone()
    return row[0] if row else None


def get_auto_response_names(user_id: int) -> list[str]:
    with database_connection() as connection:
        rows = connection.execute(
            """
            SELECT response_name FROM auto_responses
            WHERE user_id = ? ORDER BY response_name
            """,
            (user_id,),
        ).fetchall()
    return [row[0] for row in rows]


def delete_auto_response(user_id: int, response_name: str) -> bool:
    with database_connection() as connection:
        cursor = connection.execute(
            "DELETE FROM auto_responses WHERE user_id = ? AND response_name = ?",
            (user_id, response_name),
        )
    return cursor.rowcount > 0


def clear_auto_responses(user_id: int) -> int:
    with database_connection() as connection:
        cursor = connection.execute(
            "DELETE FROM auto_responses WHERE user_id = ?", (user_id,)
        )
    return cursor.rowcount


def calculate_expression(expression: str) -> int | float:
    if len(expression) > 100:
        raise ValueError("Expression is too long.")

    try:
        tree = ast.parse(expression, mode="eval")
    except SyntaxError as error:
        raise ValueError("Invalid expression.") from error

    operators = {
        ast.Add: lambda left, right: left + right,
        ast.Sub: lambda left, right: left - right,
        ast.Mult: lambda left, right: left * right,
        ast.Div: lambda left, right: left / right,
        ast.FloorDiv: lambda left, right: left // right,
        ast.Mod: lambda left, right: left % right,
        ast.Pow: lambda left, right: left**right,
    }
    unary_operators = {
        ast.UAdd: lambda value: +value,
        ast.USub: lambda value: -value,
    }

    def evaluate(node: ast.AST) -> int | float:
        if isinstance(node, ast.Constant) and type(node.value) in (int, float):
            return node.value
        if isinstance(node, ast.BinOp) and type(node.op) in operators:
            left = evaluate(node.left)
            right = evaluate(node.right)
            if isinstance(node.op, ast.Pow) and abs(right) > 100:
                raise ValueError("Exponent must be between -100 and 100.")
            return operators[type(node.op)](left, right)
        if isinstance(node, ast.UnaryOp) and type(node.op) in unary_operators:
            return unary_operators[type(node.op)](evaluate(node.operand))
        raise ValueError("Only basic arithmetic is supported.")

    try:
        result = evaluate(tree.body)
    except (ArithmeticError, OverflowError) as error:
        raise ValueError("That calculation cannot be completed.") from error

    if len(str(result)) > 1000:
        raise ValueError("That result is too large to display.")
    return result


async def get_crypto_prices() -> dict[str, float]:
    async with aiohttp.ClientSession() as session:
        async with session.get(
            "https://api.coingecko.com/api/v3/simple/price",
            params={"ids": "litecoin,solana,tether", "vs_currencies": "usd"},
            timeout=aiohttp.ClientTimeout(total=10),
        ) as response:
            if response.status != 200:
                raise RuntimeError("price service returned an error")
            data = await response.json()
    return {
        "ltc": float(data["litecoin"]["usd"]),
        "sol": float(data["solana"]["usd"]),
        "usdt": float(data["tether"]["usd"]),
    }


def mark_user_seen(user_id: int) -> bool:
    with database_connection() as connection:
        result = connection.execute(
            "INSERT OR IGNORE INTO app_users (user_id) VALUES (?)", (user_id,)
        )
    return result.rowcount == 1


def make_upi_qr(upi_id: str) -> discord.File:
    payment_uri = "upi://pay?" + urlencode({"pa": upi_id, "pn": "UPI payment"})
    qr = qrcode.make(payment_uri)
    image_buffer = io.BytesIO()
    qr.save(image_buffer, format="PNG")
    image_buffer.seek(0)
    return discord.File(image_buffer, filename="upi-qr.png")


class PaymentBot(commands.Bot):
    def __init__(self) -> None:
        super().__init__(command_prefix=[], intents=discord.Intents.none())

    async def setup_hook(self) -> None:
        setup_database()
        wallet.setup_wallet(DATABASE_PATH)
        wallet.configure_dependencies(
            get_ltc_address, get_crypto_prices, private_response, send_log, OWNER_ID
        )
        wallet.register_wallet_commands(self)
        await self.tree.sync()
        if DISCORD_GUILD_ID:
            guild = discord.Object(id=DISCORD_GUILD_ID)
            self.tree.copy_global_to(guild=guild)
            await self.tree.sync(guild=guild)


bot = PaymentBot()


async def send_log(
    title: str, description: str, color: discord.Color
) -> None:
    if not LOG_CHANNEL_ID:
        return

    try:
        channel = bot.get_channel(LOG_CHANNEL_ID)
        if channel is None:
            channel = await bot.fetch_channel(LOG_CHANNEL_ID)
        embed = discord.Embed(
            title=title,
            description=description,
            color=color,
            timestamp=discord.utils.utcnow(),
        )
        await channel.send(embed=embed)
    except (discord.DiscordException, TypeError, ValueError) as error:
        print(f"Could not send log message: {error}")


def interaction_context(interaction: discord.Interaction) -> str:
    if interaction.guild:
        return f"Server: {interaction.guild.name} (`{interaction.guild.id}`)"
    return "Location: Direct message"


def private_response(interaction: discord.Interaction) -> dict[str, bool]:
    if not interaction.guild:
        return {}
    interaction_permissions = getattr(interaction, "permissions", None)
    member_permissions = getattr(interaction.user, "guild_permissions", None)
    if getattr(interaction_permissions, "administrator", False) or getattr(
        member_permissions, "administrator", False
    ):
        return {}
    return {"ephemeral": True}


@bot.event
async def on_ready() -> None:
    print(f"Logged in as {bot.user} (ID: {bot.user.id})")
    print("Commands are synced and the bot is online.")
    if not LOG_CHANNEL_ID:
        print("WARNING: LOG_CHANNEL_ID is missing from .env; server logs are disabled.")


@bot.listen("on_interaction")
async def on_interaction(interaction: discord.Interaction) -> None:
    if interaction.type != discord.InteractionType.application_command:
        return

    command_name = interaction.data.get("name", "unknown")
    if not interaction.guild and mark_user_seen(interaction.user.id):
        await send_log(
            "👤 User app first used",
            f"User: {interaction.user} (`{interaction.user.id}`)\n"
            "Discord does not provide a separate user-app install event.\n"
            f"First command: `/{command_name}`",
            discord.Color.teal(),
        )
    await send_log(
        "📋 Slash command used",
        f"User: {interaction.user} (`{interaction.user.id}`)\n"
        f"Command: `/{command_name}`\n"
        f"{interaction_context(interaction)}",
        discord.Color.blurple(),
    )


@bot.event
async def on_guild_join(guild: discord.Guild) -> None:
    inviter = "Unknown (audit log unavailable)"
    try:
        async for entry in guild.audit_logs(
            limit=10, action=discord.AuditLogAction.bot_add
        ):
            if entry.target and bot.user and entry.target.id == bot.user.id:
                inviter = f"{entry.user} (`{entry.user.id}`)"
                break
    except discord.Forbidden:
        inviter = "Unknown (Manage Server/audit log permission missing)"
    except discord.DiscordException as error:
        inviter = f"Unknown ({error})"

    await send_log(
        "🤖 Bot added to a server",
        f"Server: {guild.name} (`{guild.id}`)\nInvited by: {inviter}",
        discord.Color.blurple(),
    )


@bot.event
async def on_guild_remove(guild: discord.Guild) -> None:
    await send_log(
        "🚪 Bot removed from a server",
        f"Server: {guild.name} (`{guild.id}`)\n"
        "The bot was removed, kicked, or the server became unavailable.",
        discord.Color.red(),
    )


@bot.tree.command(name="help", description="Show all available payment commands")
@app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
async def help_command(interaction: discord.Interaction) -> None:
    embed = discord.Embed(title="Payment commands", color=discord.Color.blurple())
    embed.add_field(
        name="/addy",
        value="Show the bot's saved Litecoin address privately.",
        inline=False,
    )
    embed.add_field(
        name="/setupltcaddy [ltc_address]",
        value="Save or update your own Litecoin address.",
        inline=False,
    )
    embed.add_field(
        name="/bal [ltc_address]",
        value="Show the current balance and total Litecoin received for an address, or your saved address.",
        inline=False,
    )
    embed.add_field(
        name="/invoice [amount]",
        value="Create a USD Litecoin invoice using your saved address and track it until confirmed.",
        inline=False,
    )
    embed.add_field(
        name="/upi",
        value="Show the saved UPI ID and QR code privately.",
        inline=False,
    )
    embed.add_field(
        name="/qr-set [photo] and /qr",
        value="Save your own UPI QR photo, then display it privately.",
        inline=False,
    )
    embed.add_field(
        name="/remove-qr",
        value="Delete your saved UPI QR photo.",
        inline=False,
    )
    embed.add_field(
        name="/setupi [upi_id]",
        value="Save or update your own UPI ID.",
        inline=False,
    )
    embed.add_field(
        name="/calc [expression]",
        value="Calculate a basic arithmetic expression, such as `5+5*4`.",
        inline=False,
    )
    embed.add_field(
        name="/set-ar [name] [text] and /ar [name]",
        value="Save named auto-response text, then display it by name.",
        inline=False,
    )
    embed.add_field(
        name="/ar-delete [name] and /ar-clear",
        value="Delete one or all of your saved auto-responses.",
        inline=False,
    )
    embed.add_field(
        name="/userinfo and /avatar",
        value="View Discord profile details or a profile picture.",
        inline=False,
    )
    embed.add_field(
        name="/translate [language] [text]",
        value="Translate text into a selected language.",
        inline=False,
    )
    embed.add_field(
        name="/price [coin] and /convert",
        value="Show live LTC, SOL, and USDT rates or convert them.",
        inline=False,
    )
    embed.add_field(
        name="/verify-tx [coin] [txid]",
        value="Check a Litecoin transaction on the blockchain.",
        inline=False,
    )
    embed.add_field(
        name="/stats",
        value="Show bot usage statistics (server administrators only).",
        inline=False,
    )
    embed.add_field(
        name="/dasboard-wallet",
        value="Open the private owner wallet and invoice dashboard.",
        inline=False,
    )
    await interaction.response.send_message(embed=embed, **private_response(interaction))


@bot.tree.command(name="addy", description="Show the bot's Litecoin address")
@app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
async def addy_command(interaction: discord.Interaction) -> None:
    saved_address = get_ltc_address(interaction.user.id)
    if saved_address:
        await interaction.response.send_message(
            f"🪙 Litecoin address:\n`{saved_address}`", **private_response(interaction)
        )
    else:
        await interaction.response.send_message(
            "⚠️ Your Litecoin address is not configured yet. Use `/setupltcaddy ltc_address` first.",
            **private_response(interaction),
        )


@bot.tree.command(name="qr-set", description="Save your personal UPI QR image")
@app_commands.describe(photo="Upload your UPI QR code image")
@app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
async def qr_set_command(
    interaction: discord.Interaction, photo: discord.Attachment
) -> None:
    allowed_types = {"image/png", "image/jpeg", "image/webp", "image/gif"}
    if photo.content_type not in allowed_types:
        await interaction.response.send_message(
            "❌ Please upload a PNG, JPG, WEBP, or GIF image.",
            **private_response(interaction),
        )
        return
    if photo.size > 5 * 1024 * 1024:
        await interaction.response.send_message(
            "❌ That image is too large. Please upload an image smaller than 5 MB.",
            **private_response(interaction),
        )
        return

    try:
        image_data = await photo.read()
    except (aiohttp.ClientError, asyncio.TimeoutError, discord.DiscordException):
        await interaction.response.send_message(
            "❌ I could not download that image. Please try uploading it again.",
            **private_response(interaction),
        )
        return

    filename = Path(photo.filename).name or "upi-qr.png"
    save_qr_image(interaction.user.id, image_data, filename)
    await interaction.response.send_message(
        "✅ Your UPI QR photo was saved. Use `/qr` anytime to display it.",
        **private_response(interaction),
    )


@bot.tree.command(name="qr", description="Display your saved UPI QR image")
@app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
async def qr_command(interaction: discord.Interaction) -> None:
    saved_qr = get_qr_image(interaction.user.id)
    if saved_qr is None:
        await interaction.response.send_message(
            "⚠️ You have not saved a QR photo yet. Use `/qr-set` and upload your UPI QR image.",
            **private_response(interaction),
        )
        return

    image_data, filename = saved_qr
    qr_file = discord.File(io.BytesIO(image_data), filename=filename)
    await interaction.response.send_message(
        content="💸 **My UPI QR**\nScan this QR code to make a payment.",
        file=qr_file,
        **private_response(interaction),
    )


@bot.tree.command(name="remove-qr", description="Delete your saved UPI QR image")
@app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
async def remove_qr_command(interaction: discord.Interaction) -> None:
    if delete_qr_image(interaction.user.id):
        message = "🗑️ Your saved UPI QR photo was deleted."
    else:
        message = "⚠️ You do not have a saved UPI QR photo to delete."
    await interaction.response.send_message(
        message, **private_response(interaction)
    )


@bot.tree.command(name="stats", description="Show bot usage statistics")
@app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
async def stats_command(interaction: discord.Interaction) -> None:
    if interaction.user.id != OWNER_ID:
        await interaction.response.send_message(
            "❌ You are not authorized to use `/stats`.", ephemeral=True
        )
        return

    (
        app_user_count,
        ltc_address_count,
        upi_id_count,
        auto_response_count,
        auto_response_user_count,
    ) = get_bot_stats()
    embed = discord.Embed(
        title="📊  BOT CONTROL DASHBOARD",
        description="🔐 Private owner-only statistics",
        color=discord.Color.blurple(),
        timestamp=discord.utils.utcnow(),
    )
    embed.add_field(name="🌐 SERVERS", value=f"`{len(bot.guilds):,}`", inline=True)
    embed.add_field(name="👥 KNOWN USERS", value=f"`{app_user_count:,}`", inline=True)
    embed.add_field(
        name="🪙 LTC ADDRESSES", value=f"`{ltc_address_count:,}`", inline=True
    )
    embed.add_field(
        name="💸 UPI IDs", value=f"`{upi_id_count:,}`", inline=True
    )
    embed.add_field(
        name="💬 AUTO-RESPONSES", value=f"`{auto_response_count:,}`", inline=True
    )
    embed.add_field(
        name="🧑‍💻 AUTO-RESPONSE USERS",
        value=f"`{auto_response_user_count:,}`",
        inline=True,
    )
    embed.add_field(
        name="⚡ BOT LATENCY",
        value=f"`{bot.latency * 1000:.0f} ms`",
        inline=True,
    )
    embed.add_field(
        name="🛠️ COMMANDS",
        value=f"`{len(bot.tree.get_commands()):,}` registered",
        inline=True,
    )
    embed.set_footer(text="Owner-only statistics • Updated")
    await interaction.response.send_message(embed=embed, **private_response(interaction))


@bot.tree.command(name="bal", description="Show a Litecoin address balance")
@app_commands.describe(
    ltc_address="Optional Litecoin address; leave empty to use your saved address"
)
@app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
async def balance_command(
    interaction: discord.Interaction, ltc_address: str | None = None
) -> None:
    address = (ltc_address or get_ltc_address(interaction.user.id) or "").strip()
    if not address:
        await interaction.response.send_message(
            "⚠️ No Litecoin address was provided or saved. Use `/bal ltc_address` or `/setupltcaddy ltc_address` first.",
            **private_response(interaction),
        )
        return
    if len(address) < 20 or len(address) > 120:
        await interaction.response.send_message(
            "❌ That does not look like a valid Litecoin address. Please check it and try again.",
            **private_response(interaction),
        )
        return

    await interaction.response.defer(**private_response(interaction))
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(
                f"https://api.blockcypher.com/v1/ltc/main/addrs/{address}/balance",
                timeout=aiohttp.ClientTimeout(total=10),
            ) as response:
                if response.status != 200:
                    raise RuntimeError("blockchain service returned an error")
                data = await response.json()
        balance_litoshis = int(data["balance"])
        received_litoshis = int(data["total_received"])
        prices = await get_crypto_prices()
        ltc_price_usdt = prices["ltc"]
        balance_ltc = balance_litoshis / 100_000_000
        received_ltc = received_litoshis / 100_000_000
        embed = discord.Embed(
            title="💰  LITECOIN WALLET",
            description=f"`{address}`",
            color=discord.Color.orange(),
        )
        embed.add_field(
            name="CURRENT BALANCE",
            value=(
                f"`{balance_ltc:,.8f} LTC`\n"
                f"`≈ {balance_ltc * ltc_price_usdt:,.2f} USDT`"
            ),
            inline=True,
        )
        embed.add_field(
            name="TOTAL RECEIVED",
            value=(
                f"`{received_ltc:,.8f} LTC`\n"
                f"`≈ {received_ltc * ltc_price_usdt:,.2f} USDT`"
            ),
            inline=True,
        )
        embed.add_field(
            name="LIVE RATE",
            value=f"`1 LTC ≈ {ltc_price_usdt:,.2f} USDT`",
            inline=False,
        )
        embed.set_footer(text="Blockchain data by BlockCypher • Market rate by CoinGecko")
    except (
        aiohttp.ClientError,
        asyncio.TimeoutError,
        KeyError,
        TypeError,
        ValueError,
        RuntimeError,
    ):
        await interaction.followup.send(
            "❌ Address not found or blockchain service is temporarily unavailable.",
            **private_response(interaction),
        )
        return

    await interaction.followup.send(embed=embed, **private_response(interaction))


@bot.tree.command(name="setupltcaddy", description="Save the Litecoin address used by /addy")
@app_commands.describe(ltc_address="The Litecoin address to display with /addy")
@app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
async def setupltcaddy_command(
    interaction: discord.Interaction, ltc_address: str
) -> None:
    address = ltc_address.strip()
    if len(address) < 20 or len(address) > 120:
        await interaction.response.send_message(
            "❌ That does not look like a valid Litecoin address. Please check it and try again.",
            **private_response(interaction),
        )
        return

    action = "updated" if get_ltc_address(interaction.user.id) else "configured"
    save_ltc_address(interaction.user.id, address)
    await send_log(
        f"🪙 Litecoin address {action}",
        f"User: {interaction.user} (`{interaction.user.id}`)\n"
        f"{interaction_context(interaction)}\n"
        f"Full LTC address: `{address}`",
        discord.Color.orange(),
    )
    await interaction.response.send_message(
        f"✅ Litecoin address {action} successfully: `{address}`\nUse `/addy` to display it.",
        **private_response(interaction),
    )


@bot.tree.command(name="upi", description="Show the UPI ID and payment QR code")
@app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
async def upi_command(interaction: discord.Interaction) -> None:
    upi_id = get_upi_id(interaction.user.id)
    if not upi_id:
        await interaction.response.send_message(
            "⚠️ Your UPI ID is not configured yet. Use `/setupi upi_id` first.",
            **private_response(interaction),
        )
        return

    embed = discord.Embed(
        title="💸  UPI PAYMENT",
        description=(
            "Scan the QR code with your UPI app to pay.\n"
            "You can also copy the UPI ID below."
        ),
        color=discord.Color.from_rgb(0, 170, 115),
    )
    embed.add_field(name="📱 UPI ID", value=f"`{upi_id}`", inline=False)
    embed.add_field(name="✅ Pay securely", value="Scan • Pay • Done", inline=False)
    embed.set_image(url="attachment://upi-qr.png")
    embed.set_footer(text="Thank you for your support! 🙏")
    await interaction.response.send_message(
        embed=embed, file=make_upi_qr(upi_id), **private_response(interaction)
    )


@bot.tree.command(name="setupi", description="Save the UPI ID used by /upi")
@app_commands.describe(upi_id="Your UPI ID, for example name@bank")
@app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
async def setupi_command(interaction: discord.Interaction, upi_id: str) -> None:
    upi_id = upi_id.strip()
    if not re.fullmatch(r"[A-Za-z0-9._-]+@[A-Za-z0-9.-]+", upi_id):
        await interaction.response.send_message(
            "❌ That does not look like a valid UPI ID. Example: `name@bank`.",
            **private_response(interaction),
        )
        return

    action = "updated" if get_upi_id(interaction.user.id) else "configured"
    save_upi_id(interaction.user.id, upi_id)
    await send_log(
        f"💸 UPI ID {action}",
        f"User: {interaction.user} (`{interaction.user.id}`)\n"
        f"{interaction_context(interaction)}\n"
        f"Full UPI ID: `{upi_id}`",
        discord.Color.green(),
    )
    await interaction.response.send_message(
        f"✅ UPI ID {action} successfully: `{upi_id}`\nUse `/upi` to display the QR code.",
        **private_response(interaction),
    )


@bot.tree.command(name="set-ar", description="Save a named personal auto-response")
@app_commands.describe(
    name="A short name, such as hi or king",
    text="The text to save under this name",
)
@app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
async def set_auto_response_command(
    interaction: discord.Interaction, name: str, text: str
) -> None:
    response_name = name.strip().lower()
    if not re.fullmatch(r"[a-z0-9_-]{1,32}", response_name):
        await interaction.response.send_message(
            "❌ Name must use only letters, numbers, `_`, or `-` and be 1-32 characters.",
            **private_response(interaction),
        )
        return

    response_text = text.strip()
    if not response_text:
        await interaction.response.send_message(
            "❌ Auto-response text cannot be empty.", **private_response(interaction)
        )
        return
    if len(response_text) > 2000:
        await interaction.response.send_message(
            "❌ Auto-response text must be 2,000 characters or fewer.",
            **private_response(interaction),
        )
        return

    save_auto_response(interaction.user.id, response_name, response_text)
    await interaction.response.send_message(
        f"✅ Auto-response `{response_name}` was saved. Use `/ar {response_name}` to display it.",
        **private_response(interaction),
    )


async def auto_response_name_autocomplete(
    interaction: discord.Interaction, current: str
) -> list[app_commands.Choice[str]]:
    search = current.strip().lower()
    names = get_auto_response_names(interaction.user.id)
    return [
        app_commands.Choice(name=name, value=name)
        for name in names
        if not search or search in name
    ][:25]


@bot.tree.command(name="ar", description="Display one of your saved auto-responses")
@app_commands.describe(name="The response name, such as hi or king")
@app_commands.autocomplete(name=auto_response_name_autocomplete)
@app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
async def auto_response_command(
    interaction: discord.Interaction, name: str | None = None
) -> None:
    if name is None:
        names = get_auto_response_names(interaction.user.id)
        if not names:
            message = "⚠️ You have not saved any auto-responses. Use `/set-ar name text` first."
        else:
            message = "📋 Your saved auto-responses:\n" + "\n".join(
                f"• `{response_name}`" for response_name in names
            )
            message += "\n\nType `/ar` and choose a name from the suggestions."
        await interaction.response.send_message(
            message, **private_response(interaction)
        )
        return

    response_name = name.strip().lower()
    response_text = get_auto_response(interaction.user.id, response_name)
    if response_text is None:
        await interaction.response.send_message(
            f"⚠️ You have not set an auto-response named `{response_name}`. Use `/set-ar {response_name} text` first.",
            **private_response(interaction),
        )
        return

    await interaction.response.send_message(
        response_text, **private_response(interaction)
    )


@bot.tree.command(name="ar-delete", description="Delete one of your auto-responses")
@app_commands.describe(name="The response name to delete")
@app_commands.autocomplete(name=auto_response_name_autocomplete)
@app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
async def delete_auto_response_command(
    interaction: discord.Interaction, name: str
) -> None:
    response_name = name.strip().lower()
    if delete_auto_response(interaction.user.id, response_name):
        message = f"🗑️ Auto-response `{response_name}` deleted."
    else:
        message = f"⚠️ No auto-response named `{response_name}` was found."
    await interaction.response.send_message(
        message, **private_response(interaction)
    )


@bot.tree.command(name="ar-clear", description="Delete all of your auto-responses")
@app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
async def clear_auto_responses_command(interaction: discord.Interaction) -> None:
    deleted_count = clear_auto_responses(interaction.user.id)
    await interaction.response.send_message(
        f"🧹 Deleted {deleted_count} auto-response(s) from your account.",
        **private_response(interaction),
    )


@bot.tree.command(name="userinfo", description="Show Discord user information")
@app_commands.describe(user="The user to inspect; leave empty for yourself")
@app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
async def userinfo_command(
    interaction: discord.Interaction, user: discord.User | None = None
) -> None:
    selected_user = user or interaction.user
    embed = discord.Embed(
        title=f"👤 {selected_user.display_name}",
        color=discord.Color.blurple(),
    )
    embed.add_field(name="Username", value=str(selected_user), inline=False)
    embed.add_field(name="User ID", value=f"`{selected_user.id}`", inline=False)
    embed.add_field(
        name="Account created",
        value=f"<t:{int(selected_user.created_at.timestamp())}:F>",
        inline=False,
    )
    embed.set_thumbnail(url=selected_user.display_avatar.url)
    await interaction.response.send_message(
        embed=embed, **private_response(interaction)
    )


@bot.tree.command(name="avatar", description="Show a user's profile picture")
@app_commands.describe(user="The user whose avatar to display; leave empty for yourself")
@app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
async def avatar_command(
    interaction: discord.Interaction, user: discord.User | None = None
) -> None:
    selected_user = user or interaction.user
    embed = discord.Embed(
        title=f"🖼️ {selected_user.display_name}'s avatar",
        color=discord.Color.blurple(),
    )
    embed.set_image(url=selected_user.display_avatar.replace(size=1024).url)
    await interaction.response.send_message(
        embed=embed, **private_response(interaction)
    )


@bot.tree.command(name="translate", description="Translate text into another language")
@app_commands.describe(
    language="Target language code, such as en, hi, or fr",
    text="The text to translate",
)
@app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
async def translate_command(
    interaction: discord.Interaction, language: str, text: str
) -> None:
    target_language = language.strip().lower()
    if not re.fullmatch(r"[a-z]{2,5}", target_language):
        await interaction.response.send_message(
            "❌ Use a language code such as `en`, `hi`, or `fr`.",
            **private_response(interaction),
        )
        return
    if len(text) > 500:
        await interaction.response.send_message(
            "❌ Text must be 500 characters or fewer.",
            **private_response(interaction),
        )
        return

    await interaction.response.defer(**private_response(interaction))
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(
                "https://api.mymemory.translated.net/get",
                params={"q": text, "langpair": f"autodetect|{target_language}"},
                timeout=aiohttp.ClientTimeout(total=10),
            ) as response:
                if response.status != 200:
                    raise RuntimeError("translation service returned an error")
                data = await response.json()
        translated_text = data["responseData"]["translatedText"]
    except (aiohttp.ClientError, asyncio.TimeoutError, KeyError, TypeError, RuntimeError):
        await interaction.followup.send(
            "❌ Translation is temporarily unavailable.",
            **private_response(interaction),
        )
        return

    await interaction.followup.send(
        f"🌍 **{target_language.upper()} translation**\n{translated_text}",
        **private_response(interaction),
    )


@bot.tree.command(name="price", description="Show a live crypto price")
@app_commands.describe(coin="Use ltc, sol, or usdt")
@app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
async def price_command(interaction: discord.Interaction, coin: str) -> None:
    selected_coin = coin.strip().lower()
    if selected_coin not in {"ltc", "sol", "usdt"}:
        await interaction.response.send_message(
            "❌ Use only `ltc`, `sol`, or `usdt`.", **private_response(interaction)
        )
        return

    await interaction.response.defer(**private_response(interaction))
    try:
        prices = await get_crypto_prices()
    except (aiohttp.ClientError, asyncio.TimeoutError, KeyError, TypeError, ValueError, RuntimeError):
        await interaction.followup.send(
            "❌ Litecoin price is temporarily unavailable.",
            **private_response(interaction),
        )
        return

    await interaction.followup.send(
        f"📈 **{selected_coin.upper()} market rate**\n`1 {selected_coin.upper()} = ${prices[selected_coin]:,.2f} USD`",
        **private_response(interaction),
    )


@bot.tree.command(name="convert", description="Convert LTC, SOL, USD, or USDT")
@app_commands.describe(
    amount="The amount to convert",
    from_coin="Source currency: ltc, sol, usd, or usdt",
    to_coin="Target currency: ltc, sol, usd, or usdt",
)
@app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
async def convert_command(
    interaction: discord.Interaction,
    amount: float,
    from_coin: str,
    to_coin: str,
) -> None:
    source = from_coin.strip().lower()
    target = to_coin.strip().lower()
    supported = {"ltc", "sol", "usd", "usdt"}
    if source not in supported or target not in supported:
        await interaction.response.send_message(
            "❌ Use only `ltc`, `sol`, `usd`, or `usdt`.", **private_response(interaction)
        )
        return
    if amount < 0 or amount > 1_000_000_000:
        await interaction.response.send_message(
            "❌ Amount must be between 0 and 1,000,000,000.",
            **private_response(interaction),
        )
        return
    if source == target:
        await interaction.response.send_message(
            f"🪙 `{amount:g} {source.upper()} = {amount:g} {target.upper()}`",
            **private_response(interaction),
        )
        return

    await interaction.response.defer(**private_response(interaction))
    try:
        prices = await get_crypto_prices()
        usd_value = amount if source == "usd" else amount * prices[source]
        result = usd_value if target == "usd" else usd_value / prices[target]
    except (aiohttp.ClientError, asyncio.TimeoutError, KeyError, TypeError, ValueError, RuntimeError, ZeroDivisionError):
        await interaction.followup.send(
            "❌ Conversion rate is temporarily unavailable.",
            **private_response(interaction),
        )
        return

    await interaction.followup.send(
        f"🔄 `{amount:g} {source.upper()} = {result:,.8f} {target.upper()}`",
        **private_response(interaction),
    )


@bot.tree.command(name="verify-tx", description="Verify a Litecoin transaction")
@app_commands.describe(coin="Use ltc", txid="The Litecoin transaction ID")
@app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
async def verify_transaction_command(
    interaction: discord.Interaction, coin: str, txid: str
) -> None:
    if coin.strip().lower() != "ltc":
        await interaction.response.send_message(
            "❌ Only `ltc` is supported right now.", **private_response(interaction)
        )
        return
    transaction_id = txid.strip().lower()
    if not re.fullmatch(r"[0-9a-f]{64}", transaction_id):
        await interaction.response.send_message(
            "❌ Enter a valid 64-character Litecoin transaction ID.",
            **private_response(interaction),
        )
        return

    await interaction.response.defer(**private_response(interaction))
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(
                f"https://api.blockchair.com/litecoin/dashboards/transaction/{transaction_id}",
                timeout=aiohttp.ClientTimeout(total=10),
            ) as response:
                if response.status != 200:
                    raise RuntimeError("blockchain service returned an error")
                data = await response.json()
        transaction = data["data"][transaction_id]["transaction"]
        block_id = transaction.get("block_id")
        confirmed = isinstance(block_id, int) and block_id > 0
        status = "✅ Confirmed" if confirmed else "⏳ Unconfirmed"
        block_text = str(block_id) if confirmed else "Pending"
        message = (
            f"🔎 **Litecoin transaction**\n{status}\n"
            f"Block: `{block_text}`\n"
            f"Hash: `{transaction_id}`\n"
            f"[View on Blockchair](https://blockchair.com/litecoin/transaction/{transaction_id})"
        )
    except (aiohttp.ClientError, asyncio.TimeoutError, KeyError, TypeError, ValueError, RuntimeError):
        await interaction.followup.send(
            "❌ Transaction not found or blockchain service is temporarily unavailable.",
            **private_response(interaction),
        )
        return

    await interaction.followup.send(message, **private_response(interaction))


@bot.tree.command(name="calc", description="Calculate a basic arithmetic expression")
@app_commands.describe(expression="Example: 5+5*4")
@app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
async def calc_command(interaction: discord.Interaction, expression: str) -> None:
    try:
        result = calculate_expression(expression)
    except ValueError as error:
        await interaction.response.send_message(
            f"❌ {error}", **private_response(interaction)
        )
        return

    await interaction.response.send_message(
        f"🧮 `{expression}` = `{result}`", **private_response(interaction)
    )

def main() -> None:
    bot.run(TOKEN)


if __name__ == "__main__":
    main()
