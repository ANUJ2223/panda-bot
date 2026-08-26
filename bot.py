import ast
import asyncio
import aiohttp
import io
import os
import re
from pathlib import Path
from urllib.parse import urlencode

import discord
import qrcode
from discord import app_commands
from discord.ext import commands
from dotenv import load_dotenv

load_dotenv()

import database
import wallet
import tool

TOKEN = os.getenv("DISCORD_TOKEN")
LOG_CHANNEL_ID = int(os.getenv("LOG_CHANNEL_ID", "0"))
DISCORD_GUILD_ID = int(os.getenv("DISCORD_GUILD_ID", "0"))
OWNER_ID = int(os.getenv("OWNER_ID", "0"))

if not TOKEN:
    raise RuntimeError("DISCORD_TOKEN is missing. Copy .env.example to .env and add your bot token.")
if not OWNER_ID:
    raise RuntimeError("OWNER_ID is missing. Add your Discord user ID to .env.")


def setup_database() -> None:
    database.setup_database()


def save_ltc_address(user_id: int, address: str) -> None:
    database.save_ltc_address(user_id, address)


def get_ltc_address(user_id: int) -> str | None:
    return database.get_ltc_address(user_id)


def save_upi_id(user_id: int, upi_id: str) -> None:
    database.save_upi_id(user_id, upi_id)


def get_upi_id(user_id: int) -> str | None:
    return database.get_upi_id(user_id)


def save_qr_image(user_id: int, image_data: bytes, filename: str) -> None:
    database.save_qr_image(user_id, image_data, filename)


def get_qr_image(user_id: int) -> tuple[bytes, str] | None:
    return database.get_qr_image(user_id)


def save_qr2_image(user_id: int, image_data: bytes, filename: str) -> None:
    database.save_qr2_image(user_id, image_data, filename)


def get_qr2_image(user_id: int) -> tuple[bytes, str] | None:
    return database.get_qr2_image(user_id)


def delete_qr_image(user_id: int) -> bool:
    return database.delete_qr_image(user_id)


def delete_qr2_image(user_id: int) -> bool:
    return database.delete_qr2_image(user_id)


def get_bot_stats() -> tuple[int, int, int, int, int]:
    return database.get_bot_stats()


def save_auto_response(user_id: int, response_name: str, response_text: str) -> None:
    database.save_auto_response(user_id, response_name, response_text)


def get_auto_response(user_id: int, response_name: str) -> str | None:
    return database.get_auto_response(user_id, response_name)


def get_auto_response_names(user_id: int) -> list[str]:
    return database.get_auto_response_names(user_id)


def delete_auto_response(user_id: int, response_name: str) -> bool:
    return database.delete_auto_response(user_id, response_name)


def clear_auto_responses(user_id: int) -> int:
    return database.clear_auto_responses(user_id)


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
    return database.mark_user_seen(user_id)


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
        wallet.setup_wallet()
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


def user_only_response(interaction: discord.Interaction) -> dict[str, bool]:
    return {} if not interaction.guild else {"ephemeral": True}


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


async def _save_uploaded_qr(
    interaction: discord.Interaction,
    photo: discord.Attachment,
    *,
    save_to_db,
    filename_prefix: str,
    success_message: str,
) -> None:
    allowed_types = {"image/png", "image/jpeg", "image/webp", "image/gif"}
    content_type = (photo.content_type or "").lower()
    if content_type not in allowed_types:
        await interaction.response.send_message(
            "❌ Please upload a PNG, JPG, WEBP, or GIF image.",
            **user_only_response(interaction),
        )
        return
    if photo.size > 5 * 1024 * 1024:
        await interaction.response.send_message(
            "❌ That image is too large. Please upload an image smaller than 5 MB.",
            **user_only_response(interaction),
        )
        return

    try:
        image_data = await photo.read()
    except (aiohttp.ClientError, asyncio.TimeoutError, discord.DiscordException):
        await interaction.response.send_message(
            "❌ I could not download that image. Please try uploading it again.",
            **user_only_response(interaction),
        )
        return

    filename = Path(photo.filename or f"{filename_prefix}.png").name or f"{filename_prefix}.png"
    try:
        save_to_db(interaction.user.id, image_data, filename)
    except Exception as error:  # pragma: no cover - runtime protection against DB issues
        print(f"QR save failed for user {interaction.user.id}: {error}")
        await interaction.response.send_message(
            "❌ I could not save that QR image right now. Please try again in a moment.",
            **user_only_response(interaction),
        )
        return

    await interaction.response.send_message(
        success_message,
        **user_only_response(interaction),
    )


@bot.tree.command(name="set-qr", description="Save your personal UPI QR image")
@app_commands.describe(
    qr="Choose which QR slot to save",
    photo="Upload your UPI QR code image",
)
@app_commands.choices(
    qr=[
        app_commands.Choice(name="QR1", value="qr1"),
        app_commands.Choice(name="QR2", value="qr2"),
    ]
)
@app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
async def set_qr_command(
    interaction: discord.Interaction,
    qr: app_commands.Choice[str],
    photo: discord.Attachment,
) -> None:
    if qr.value == "qr2":
        await _save_uploaded_qr(
            interaction,
            photo,
            save_to_db=save_qr2_image,
            filename_prefix="upi-qr2",
            success_message="✅ Your second UPI QR photo was saved. Use `/qr2` anytime to display it.",
        )
        return

    await _save_uploaded_qr(
        interaction,
        photo,
        save_to_db=save_qr_image,
        filename_prefix="upi-qr",
        success_message="✅ Your UPI QR photo was saved. Use `/qr` anytime to display it.",
    )


@bot.tree.command(name="qr", description="Display your saved UPI QR image")
@app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
async def qr_command(interaction: discord.Interaction) -> None:
    saved_qr = get_qr_image(interaction.user.id)
    if saved_qr is None:
        await interaction.response.send_message(
            "⚠️ You have not saved a QR photo yet. Use `/set-qr`, choose QR1, and upload your UPI QR image.",
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


@bot.tree.command(name="qr2", description="Display your second saved UPI QR image")
@app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
async def qr2_command(interaction: discord.Interaction) -> None:
    saved_qr = get_qr2_image(interaction.user.id)
    if saved_qr is None:
        await interaction.response.send_message(
            "⚠️ You have not saved a second QR photo yet. Use `/set-qr`, choose QR2, and upload your UPI QR image.",
            **private_response(interaction),
        )
        return

    image_data, filename = saved_qr
    qr_file = discord.File(io.BytesIO(image_data), filename=filename)
    await interaction.response.send_message(
        content="💸 **My Second UPI QR**\nScan this QR code to make a payment.",
        file=qr_file,
        **private_response(interaction),
    )


@bot.tree.command(name="remove-qr", description="Delete one of your saved UPI QR images")
@app_commands.describe(qr="Choose which QR slot to delete")
@app_commands.choices(
    qr=[
        app_commands.Choice(name="QR1", value="qr1"),
        app_commands.Choice(name="QR2", value="qr2"),
    ]
)
@app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
async def remove_qr_command(
    interaction: discord.Interaction, qr: app_commands.Choice[str]
) -> None:
    if qr.value == "qr2":
        deleted = delete_qr2_image(interaction.user.id)
        slot_name = "second UPI QR"
    else:
        deleted = delete_qr_image(interaction.user.id)
        slot_name = "UPI QR"
    if deleted:
        message = f"🗑️ Your {slot_name} photo was deleted."
    else:
        message = f"⚠️ You do not have a saved {slot_name} photo to delete."
    await interaction.response.send_message(
        message, **user_only_response(interaction)
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
            **user_only_response(interaction),
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
        **user_only_response(interaction),
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
            **user_only_response(interaction),
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
        **user_only_response(interaction),
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
            **user_only_response(interaction),
        )
        return

    response_text = text.strip()
    if not response_text:
        await interaction.response.send_message(
            "❌ Auto-response text cannot be empty.", **user_only_response(interaction)
        )
        return
    if len(response_text) > 2000:
        await interaction.response.send_message(
            "❌ Auto-response text must be 2,000 characters or fewer.",
            **user_only_response(interaction),
        )
        return

    save_auto_response(interaction.user.id, response_name, response_text)
    await interaction.response.send_message(
        f"✅ Auto-response `{response_name}` was saved. Use `/ar {response_name}` to display it.",
        **user_only_response(interaction),
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
        message, **user_only_response(interaction)
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


@bot.tree.command(name="token-checker", description="Check a Discord account token and retrieve account info")
@app_commands.describe(
    token_input="Discord account token or EMAIL:PASSWORD:TOKEN format"
)
@app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
async def token_checker_command(interaction: discord.Interaction, token_input: str) -> None:
    """Check if a Discord account token is valid and show account details"""
    await interaction.response.defer(ephemeral=True)
    
    try:
        # Validate token
        result = await tool.check_token_format(token_input)
        
        if not result.get('valid'):
            embed = discord.Embed(
                title="❌ Invalid Token",
                description=result.get('error', 'Unknown error'),
                color=discord.Color.red()
            )
            await interaction.followup.send(embed=embed, ephemeral=True)
            return
        
        # Build embed with token info
        embed = discord.Embed(
            title="✅ Token Valid",
            description="Account Information",
            color=discord.Color.green()
        )
        
        # Basic Info
        embed.add_field(
            name="👤 Username",
            value=f"{result.get('username')}#{result.get('discriminator', '0')}",
            inline=False
        )
        embed.add_field(
            name="🆔 User ID",
            value=f"`{result.get('user_id')}`",
            inline=False
        )
        
        # Email & Verification
        email = result.get('email', 'Not set')
        verified = "✅ Yes" if result.get('verified') else "❌ No"
        embed.add_field(name="📧 Email", value=email, inline=True)
        embed.add_field(name="✔️ Verified", value=verified, inline=True)
        
        # MFA Status
        mfa = "✅ Enabled" if result.get('mfa_enabled') else "❌ Disabled"
        embed.add_field(name="🔐 MFA Enabled", value=mfa, inline=True)
        
        # Server Count
        guild_count = result.get('guild_count', 0)
        embed.add_field(name="🖥️ Servers Joined", value=str(guild_count), inline=True)
        
        # Nitro Status
        nitro_type = result.get('nitro_type', 'none')
        if nitro_type == 'none':
            nitro_status = "❌ No Nitro"
        elif nitro_type == 'nitro_classic':
            nitro_status = "⭐ Nitro Classic"
        elif nitro_type == 'nitro':
            nitro_status = "🚀 Nitro"
        else:
            nitro_status = "❓ Unknown"
        
        embed.add_field(name="💎 Nitro Status", value=nitro_status, inline=True)
        
        # Nitro Since (if applicable)
        if result.get('has_nitro') and result.get('nitro_since'):
            embed.add_field(
                name="📅 Nitro Since",
                value=result.get('nitro_since', 'Unknown'),
                inline=True
            )
        
        # Avatar
        if result.get('avatar'):
            embed.set_thumbnail(url=result.get('avatar_url'))
        
        # Guild List (if user has guilds)
        guild_list = result.get('guild_list', [])
        if guild_list and len(guild_list) <= 10:
            guild_names = "\n".join([f"• {g.get('name')}" for g in guild_list[:10]])
            embed.add_field(
                name="📋 Servers",
                value=guild_names if guild_names else "None",
                inline=False
            )
        elif guild_list:
            embed.add_field(
                name="📋 Servers (showing first 10)",
                value="\n".join([f"• {g.get('name')}" for g in guild_list[:10]]),
                inline=False
            )
        
        embed.set_footer(text="Token Checker • Keep your tokens safe and never share them!")
        await interaction.followup.send(embed=embed, ephemeral=True)
        
    except Exception as error:
        embed = discord.Embed(
            title="❌ Error",
            description=f"An error occurred: {str(error)}",
            color=discord.Color.red()
        )
        await interaction.followup.send(embed=embed, ephemeral=True)


def main() -> None:
    bot.run(TOKEN)


if __name__ == "__main__":
    main()
