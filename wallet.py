"""Invoice commands and Litecoin payment tracking."""

import asyncio
import io
import sqlite3
import uuid
from contextlib import closing
from pathlib import Path
from typing import Any, Callable

import aiohttp
import discord
import qrcode
from discord import app_commands


DATABASE_PATH: Path
GET_LTC_ADDRESS: Callable[[int], str | None]
GET_CRYPTO_PRICES: Callable[[], Any]
PRIVATE_RESPONSE: Callable[[discord.Interaction], dict[str, bool]]
SEND_LOG: Callable[..., Any]
OWNER_ID: int
TRACKER_TASK: asyncio.Task[None] | None = None


def setup_wallet(database_path: Path) -> None:
	global DATABASE_PATH
	DATABASE_PATH = database_path
	with closing(sqlite3.connect(DATABASE_PATH)) as connection:
		connection.execute(
			"""
			CREATE TABLE IF NOT EXISTS ltc_invoices (
				invoice_id TEXT PRIMARY KEY,
				user_id INTEGER NOT NULL,
				ltc_address TEXT NOT NULL,
				usd_amount REAL NOT NULL,
				ltc_amount REAL NOT NULL,
				required_litoshis INTEGER NOT NULL,
				baseline_received INTEGER NOT NULL,
				status TEXT NOT NULL DEFAULT 'pending',
				transaction_id TEXT,
				created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
				paid_at TEXT
			)
			"""
		)
		invoice_columns = {
			row[1]
			for row in connection.execute("PRAGMA table_info(ltc_invoices)")
		}
		if "detected_at" not in invoice_columns:
			connection.execute(
				"ALTER TABLE ltc_invoices ADD COLUMN detected_at TEXT"
			)
		if "message_channel_id" not in invoice_columns:
			connection.execute(
				"ALTER TABLE ltc_invoices ADD COLUMN message_channel_id INTEGER"
			)
		if "message_id" not in invoice_columns:
			connection.execute("ALTER TABLE ltc_invoices ADD COLUMN message_id INTEGER")
		connection.commit()


def configure_dependencies(
	get_ltc_address: Callable[[int], str | None],
	get_crypto_prices: Callable[[], Any],
	private_response: Callable[[discord.Interaction], dict[str, bool]],
	send_log: Callable[..., Any],
	owner_id: int,
) -> None:
	global GET_LTC_ADDRESS, GET_CRYPTO_PRICES, PRIVATE_RESPONSE, SEND_LOG, OWNER_ID
	GET_LTC_ADDRESS = get_ltc_address
	GET_CRYPTO_PRICES = get_crypto_prices
	PRIVATE_RESPONSE = private_response
	SEND_LOG = send_log
	OWNER_ID = owner_id


async def get_ltc_price() -> float:
	try:
		prices = await GET_CRYPTO_PRICES()
		return float(prices["ltc"])
	except (
		aiohttp.ClientError,
		asyncio.TimeoutError,
		KeyError,
		TypeError,
		ValueError,
		RuntimeError,
	):
		async with aiohttp.ClientSession() as session:
			async with session.get(
				"https://api.coinpaprika.com/v1/tickers/ltc-litecoin",
				timeout=aiohttp.ClientTimeout(total=10),
			) as response:
				if response.status != 200:
					raise RuntimeError("fallback price service returned an error")
				data = await response.json()
		return float(data["quotes"]["USD"]["price"])


def create_invoice(
	user_id: int,
	address: str,
	usd_amount: float,
	ltc_amount: float,
	baseline_received: int,
) -> str:
	invoice_id = uuid.uuid4().hex[:12].upper()
	with closing(sqlite3.connect(DATABASE_PATH)) as connection:
		connection.execute(
			"""
			INSERT INTO ltc_invoices
			(invoice_id, user_id, ltc_address, usd_amount, ltc_amount,
			 required_litoshis, baseline_received)
			VALUES (?, ?, ?, ?, ?, ?, ?)
			""",
			(
				invoice_id,
				user_id,
				address,
				usd_amount,
				ltc_amount,
				round(ltc_amount * 100_000_000),
				baseline_received,
			),
		)
		connection.commit()
	return invoice_id


def save_invoice_message(invoice_id: str, message: discord.Message) -> None:
	with closing(sqlite3.connect(DATABASE_PATH)) as connection:
		connection.execute(
			"""
			UPDATE ltc_invoices
			SET message_channel_id = ?, message_id = ?
			WHERE invoice_id = ?
			""",
			(message.channel.id, message.id, invoice_id),
		)
		connection.commit()


def get_invoice_message(invoice_id: str) -> tuple[int, int] | None:
	with closing(sqlite3.connect(DATABASE_PATH)) as connection:
		row = connection.execute(
			"SELECT message_channel_id, message_id FROM ltc_invoices WHERE invoice_id = ?",
			(invoice_id,),
		).fetchone()
	if not row or row[0] is None or row[1] is None:
		return None
	return int(row[0]), int(row[1])


async def edit_invoice_message(
	bot: discord.Client,
	invoice_id: str,
	embed: discord.Embed,
) -> bool:
	message_details = get_invoice_message(invoice_id)
	if message_details is None:
		return False
	channel_id, message_id = message_details
	try:
		channel = bot.get_channel(channel_id)
		if channel is None:
			channel = await bot.fetch_channel(channel_id)
		message = await channel.fetch_message(message_id)
		await message.edit(embed=embed, view=None)
		return True
	except (discord.DiscordException, AttributeError, TypeError):
		return False


def payment_status_embed(
	invoice_id: str,
	title: str,
	description: str,
	amount: float,
	color: discord.Color,
	transaction_id: str | None = None,
) -> discord.Embed:
	embed = discord.Embed(title=title, description=description, color=color)
	embed.add_field(name="🔖 INVOICE", value=f"`{invoice_id}`", inline=True)
	embed.add_field(name="💵 AMOUNT", value=f"`${amount:,.2f} USD`", inline=True)
	if transaction_id:
		embed.add_field(name="🔗 TRANSACTION", value=f"`{transaction_id}`", inline=False)
		embed.set_footer(text="Litecoin payment tracker")
	return embed


def get_invoice_summary() -> dict[str, float | int]:
	with closing(sqlite3.connect(DATABASE_PATH)) as connection:
		row = connection.execute(
			"""
			SELECT
				COUNT(*),
				SUM(CASE WHEN status = 'paid' THEN 1 ELSE 0 END),
				SUM(CASE WHEN status = 'pending' AND created_at >= datetime('now', '-24 hours') THEN 1 ELSE 0 END),
				SUM(CASE WHEN status = 'detected' THEN 1 ELSE 0 END),
				COALESCE(SUM(CASE WHEN status = 'paid' THEN usd_amount ELSE 0 END), 0),
				COALESCE(SUM(CASE WHEN status = 'paid' THEN ltc_amount ELSE 0 END), 0)
			FROM ltc_invoices
			"""
		).fetchone()
	return {
		"total": int(row[0]),
		"paid": int(row[1] or 0),
		"pending": int(row[2] or 0),
		"detected": int(row[3] or 0),
		"paid_usd": float(row[4]),
		"paid_ltc": float(row[5]),
	}


async def build_dashboard_embed(bot: discord.Client) -> discord.Embed:
	summary = get_invoice_summary()
	address = GET_LTC_ADDRESS(OWNER_ID)
	balance_text = "Unavailable"
	value_text = "Unavailable"
	if address:
		try:
			data = await get_address_data(address)
			ltc_balance = int(data["balance"]) / 100_000_000
			ltc_price = await get_ltc_price()
			balance_text = f"{ltc_balance:,.8f} LTC"
			value_text = f"${ltc_balance * ltc_price:,.2f}"
		except (
			aiohttp.ClientError,
			asyncio.TimeoutError,
			KeyError,
			TypeError,
			ValueError,
			RuntimeError,
			ZeroDivisionError,
		):
			pass

	embed = discord.Embed(
		title="💼  WALLET DASHBOARD",
		description="🔐 Owner-only payment overview",
		color=discord.Color.blurple(),
		timestamp=discord.utils.utcnow(),
	)
	embed.add_field(name="🧾 TOTAL INVOICES", value=f"`{summary['total']:,}`", inline=True)
	embed.add_field(name="✅ PAID", value=f"`{summary['paid']:,}`", inline=True)
	embed.add_field(name="⏳ PENDING", value=f"`{summary['pending']:,}`", inline=True)
	embed.add_field(name="💸 DETECTED", value=f"`{summary['detected']:,}`", inline=True)
	embed.add_field(name="💵 PAID VALUE", value=f"`${summary['paid_usd']:,.2f}`", inline=True)
	embed.add_field(name="🪙 PAID LTC", value=f"`{summary['paid_ltc']:,.8f}`", inline=True)
	embed.add_field(name="💰 WALLET BALANCE", value=f"`{balance_text}`", inline=True)
	embed.add_field(name="💲 BALANCE VALUE", value=f"`{value_text}`", inline=True)
	embed.add_field(name="🌐 SERVERS", value=f"`{len(bot.guilds):,}`", inline=True)
	embed.add_field(
		name="🔄 TRACKER",
		value="`Online`" if TRACKER_TASK and not TRACKER_TASK.done() else "`Offline`",
		inline=True,
	)
	embed.set_footer(text="Press Refresh for the latest data")
	return embed


async def get_address_data(address: str) -> dict[str, Any]:
	try:
		async with aiohttp.ClientSession() as session:
			async with session.get(
				f"https://api.blockcypher.com/v1/ltc/main/addrs/{address}/balance",
				timeout=aiohttp.ClientTimeout(total=10),
			) as response:
				if response.status != 200:
					raise RuntimeError("blockchain service returned an error")
				return await response.json()
	except (aiohttp.ClientError, asyncio.TimeoutError, RuntimeError):
		return await get_litecoin_space_data(address)


async def get_address_history(address: str) -> dict[str, Any]:
	try:
		async with aiohttp.ClientSession() as session:
			async with session.get(
				f"https://api.blockcypher.com/v1/ltc/main/addrs/{address}/full?limit=50",
				timeout=aiohttp.ClientTimeout(total=10),
			) as response:
				if response.status != 200:
					raise RuntimeError("blockchain history service returned an error")
				return await response.json()
	except (aiohttp.ClientError, asyncio.TimeoutError, RuntimeError):
		return await get_litecoin_space_data(address)


async def get_litecoin_space_data(address: str) -> dict[str, Any]:
	base_url = f"https://litecoinspace.org/api/address/{address}"
	async with aiohttp.ClientSession() as session:
		async with session.get(
			base_url, timeout=aiohttp.ClientTimeout(total=10)
		) as response:
			if response.status != 200:
				raise RuntimeError("Litecoin Space address service returned an error")
			address_data = await response.json()
		async with session.get(
			f"{base_url}/txs/chain?limit=50",
			timeout=aiohttp.ClientTimeout(total=10),
		) as response:
			if response.status != 200:
				raise RuntimeError("Litecoin Space transaction service returned an error")
			confirmed_transactions = await response.json()
		async with session.get(
			f"{base_url}/txs/mempool",
			timeout=aiohttp.ClientTimeout(total=10),
		) as response:
			if response.status != 200:
				raise RuntimeError("Litecoin Space mempool service returned an error")
			unconfirmed_transactions = await response.json()

	def transaction_refs(transactions: list[dict[str, Any]]) -> list[dict[str, Any]]:
		refs = []
		for transaction in transactions:
			for output in transaction.get("vout", []):
				if output.get("scriptpubkey_address") == address:
					refs.append(
						{
							"value": output["value"],
							"tx_hash": transaction["txid"],
							"tx_input_n": -1,
						}
					)
		return refs

	confirmed_refs = transaction_refs(confirmed_transactions)
	unconfirmed_refs = transaction_refs(unconfirmed_transactions)
	mempool_stats = address_data["mempool_stats"]
	return {
		"total_received": address_data["chain_stats"]["funded_txo_sum"],
		"balance": (
			address_data["chain_stats"]["funded_txo_sum"]
			- address_data["chain_stats"]["spent_txo_sum"]
		),
		"unconfirmed_balance": mempool_stats["funded_txo_sum"]
		- mempool_stats["spent_txo_sum"],
		"txrefs": confirmed_refs,
		"unconfirmed_txrefs": unconfirmed_refs,
	}


def get_pending_invoices() -> list[tuple[Any, ...]]:
	with closing(sqlite3.connect(DATABASE_PATH)) as connection:
		return connection.execute(
			"""
			     SELECT invoice_id, user_id, ltc_address, usd_amount,
				       required_litoshis, baseline_received, status
			FROM ltc_invoices
				WHERE status IN ('pending', 'detected')
			  AND created_at >= datetime('now', '-24 hours')
			"""
		).fetchall()


def mark_invoice_detected(invoice_id: str, transaction_id: str) -> int | None:
	with closing(sqlite3.connect(DATABASE_PATH)) as connection:
		row = connection.execute(
			"SELECT user_id FROM ltc_invoices WHERE invoice_id = ? AND status = 'pending'",
			(invoice_id,),
		).fetchone()
		if row is None:
			return None
		connection.execute(
			"""
			UPDATE ltc_invoices
			SET status = 'detected', transaction_id = ?, detected_at = CURRENT_TIMESTAMP
			WHERE invoice_id = ?
			""",
			(transaction_id, invoice_id),
		)
		connection.commit()
	return int(row[0])


def mark_invoice_paid(invoice_id: str, transaction_id: str) -> int | None:
	with closing(sqlite3.connect(DATABASE_PATH)) as connection:
		row = connection.execute(
			"SELECT user_id FROM ltc_invoices WHERE invoice_id = ? AND status IN ('pending', 'detected')",
			(invoice_id,),
		).fetchone()
		if row is None:
			return None
		connection.execute(
			"""
			UPDATE ltc_invoices
			SET status = 'paid', transaction_id = ?, paid_at = CURRENT_TIMESTAMP
			WHERE invoice_id = ?
			""",
			(transaction_id, invoice_id),
		)
		connection.commit()
	return int(row[0])


async def check_invoice(invoice: tuple[Any, ...]) -> tuple[str, int, str, str, float] | None:
	invoice_id, user_id, address, usd_amount, _required_litoshis, baseline_received, status = invoice
	data = await get_address_history(address)
	ltc_price = await get_ltc_price()
	received_delta = int(data["total_received"]) - int(baseline_received)
	confirmed_usd = (received_delta / 100_000_000) * ltc_price
	unconfirmed_refs = data.get("unconfirmed_txrefs", [])
	unconfirmed_received = sum(
		int(reference["value"])
		for reference in unconfirmed_refs
		if reference.get("tx_input_n") == -1
	)
	if (confirmed_usd + (unconfirmed_received / 100_000_000) * ltc_price) + 0.01 < float(usd_amount):
		return None
	if status == "pending" and unconfirmed_received:
		transaction_id = str(unconfirmed_refs[0].get("tx_hash", "unknown"))
		return str(invoice_id), int(user_id), transaction_id, "detected", float(usd_amount)
	if confirmed_usd + 0.01 < float(usd_amount):
		return None
	transactions = data.get("txrefs", [])
	transaction_id = "unknown"
	if transactions:
		transaction_id = str(transactions[0].get("tx_hash", "unknown"))
	return str(invoice_id), int(user_id), transaction_id, "paid", float(usd_amount)


async def invoice_tracker(bot: discord.Client) -> None:
	while not bot.is_closed():
		for invoice in get_pending_invoices():
			try:
				result = await check_invoice(invoice)
			except (
				aiohttp.ClientError,
				asyncio.TimeoutError,
				KeyError,
				TypeError,
				ValueError,
				RuntimeError,
				ZeroDivisionError,
			):
				continue
			if result is None:
				continue
			invoice_id, user_id, transaction_id, event, usd_amount = result
			if event == "detected":
				owner_id = mark_invoice_detected(invoice_id, transaction_id)
				if owner_id is not None:
					detected_embed = payment_status_embed(
						invoice_id,
						"💸  PAYMENT DETECTED",
						"Your Litecoin payment was detected and is waiting for blockchain confirmation.",
						usd_amount,
						discord.Color.gold(),
						transaction_id,
					)
					message_edited = await edit_invoice_message(
						bot, invoice_id, detected_embed
					)
					await SEND_LOG(
						"💸 Litecoin payment detected",
						f"Invoice: `{invoice_id}`\n"
						f"User ID: `{owner_id}`\n"
						f"Invoice amount: `${usd_amount:,.2f}`\n"
						f"Transaction: `{transaction_id}`\n"
						"Payment is unconfirmed; tracker will continue checking.",
						discord.Color.gold(),
					)
					try:
						user = await bot.fetch_user(user_id)
						if not message_edited:
							await user.send(
								f"💸 Litecoin payment detected!\nInvoice: `{invoice_id}`\n"
								f"Amount: `${usd_amount:,.2f}`\n"
								"Your payment is waiting for blockchain confirmation."
							)
					except discord.DiscordException:
						pass
				continue

			owner_id = mark_invoice_paid(invoice_id, transaction_id)
			if owner_id is None:
				continue
			confirmed_embed = payment_status_embed(
				invoice_id,
				"✅  PAYMENT CONFIRMED",
				"Your Litecoin payment has been confirmed on the blockchain.",
				usd_amount,
				discord.Color.green(),
				transaction_id,
			)
			message_edited = await edit_invoice_message(
				bot, invoice_id, confirmed_embed
			)
			await SEND_LOG(
				"✅ Litecoin invoice paid",
				f"Invoice: `{invoice_id}`\n"
				f"User ID: `{owner_id}`\n"
				f"Transaction: `{transaction_id}`",
				discord.Color.green(),
			)
			try:
				user = await bot.fetch_user(user_id)
				if not message_edited:
					await user.send(
						f"✅ Litecoin payment confirmed!\nInvoice: `{invoice_id}`\n"
						f"Transaction: `{transaction_id}`\n"
						f"[View transaction](https://live.blockcypher.com/ltc/tx/{transaction_id}/)"
					)
				await SEND_LOG(
					"📨 Invoice confirmation delivered",
					f"Invoice: `{invoice_id}`\nUser ID: `{owner_id}`\n"
					"Confirmation DM sent successfully.",
					discord.Color.teal(),
				)
			except discord.DiscordException:
				await SEND_LOG(
					"⚠️ Invoice confirmation DM failed",
					f"Invoice: `{invoice_id}`\nUser ID: `{owner_id}`\n"
					"Payment was recorded, but the confirmation DM could not be delivered.",
					discord.Color.orange(),
				)
				continue
		await asyncio.sleep(60)


class InvoiceView(discord.ui.View):
	def __init__(self, ltc_amount: float, address: str) -> None:
		super().__init__(timeout=900)
		self.ltc_amount = ltc_amount
		self.address = address

	@discord.ui.button(
		label="Copy payment details",
		emoji="📋",
		style=discord.ButtonStyle.secondary,
	)
	async def copy_details(
		self, interaction: discord.Interaction, button: discord.ui.Button
	) -> None:
		await interaction.response.send_message(
			"📋 **PAYMENT DETAILS**\n"
			f"🪙 **LTC amount:** `{self.ltc_amount:.8f}`\n"
			f"📍 **LTC address:** `{self.address}`",
			ephemeral=True,
		)


class WalletDashboardView(discord.ui.View):
	def __init__(self, bot: discord.Client) -> None:
		super().__init__(timeout=900)
		self.bot = bot

	@discord.ui.button(label="Refresh", emoji="🔄", style=discord.ButtonStyle.secondary)
	async def refresh(
		self, interaction: discord.Interaction, button: discord.ui.Button
	) -> None:
		if interaction.user.id != OWNER_ID:
			await interaction.response.send_message(
				"❌ You are not authorized to use this dashboard.", ephemeral=True
			)
			return
		embed = await build_dashboard_embed(self.bot)
		await interaction.response.edit_message(embed=embed, view=self)


def register_wallet_commands(bot: discord.Client) -> None:
	global TRACKER_TASK

	@bot.tree.command(name="invoice", description="Create a Litecoin payment invoice")
	@app_commands.describe(amount="Amount to receive in USD")
	@app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
	async def invoice_command(interaction: discord.Interaction, amount: float) -> None:
		address = GET_LTC_ADDRESS(interaction.user.id)
		if not address:
			await interaction.response.send_message(
				"⚠️ Set your Litecoin address first with `/setupltcaddy ltc_address`.",
				**PRIVATE_RESPONSE(interaction),
			)
			return
		if amount < 0.01 or amount > 1_000_000:
			await interaction.response.send_message(
				"❌ Invoice amount must be between `$0.01` and `$1,000,000`.",
				**PRIVATE_RESPONSE(interaction),
			)
			return

		await interaction.response.defer(**PRIVATE_RESPONSE(interaction))
		try:
			ltc_price = await get_ltc_price()
			ltc_amount = amount / ltc_price
			address_data = await get_address_data(address)
			baseline_received = int(address_data["total_received"])
		except (
			aiohttp.ClientError,
			asyncio.TimeoutError,
			KeyError,
			TypeError,
			ValueError,
			RuntimeError,
			ZeroDivisionError,
		):
			await interaction.followup.send(
				"❌ Could not create the invoice because a price or blockchain service is unavailable.",
				**PRIVATE_RESPONSE(interaction),
			)
			return

		invoice_id = create_invoice(
			interaction.user.id, address, amount, ltc_amount, baseline_received
		)
		location = (
			f"Server: {interaction.guild.name} (`{interaction.guild.id}`)"
			if interaction.guild
			else "Location: Direct message"
		)
		await SEND_LOG(
			"🧾 Litecoin invoice created",
			f"User: {interaction.user} (`{interaction.user.id}`)\n"
			f"{location}\nInvoice: `{invoice_id}`\n"
			f"USD amount: `${amount:,.2f}`\n"
			f"LTC amount: `{ltc_amount:.8f}`\n"
			f"LTC price: `${ltc_price:,.2f}`\n"
			f"Receive address: `{address}`",
			discord.Color.orange(),
		)
		payment_uri = f"litecoin:{address}?amount={ltc_amount:.8f}"
		qr = qrcode.make(payment_uri)
		image_buffer = io.BytesIO()
		qr.save(image_buffer, format="PNG")
		image_buffer.seek(0)
		qr_file = discord.File(image_buffer, filename="ltc-invoice.png")
		embed = discord.Embed(
			title="🧾  LITECOIN INVOICE",
			description="Send about the amount below. Small LTC price and rounding differences are accepted.",
			color=discord.Color.orange(),
		)
		embed.add_field(name="💵 AMOUNT", value=f"`${amount:,.2f} USD`", inline=True)
		embed.add_field(name="🪙 SEND", value=f"`{ltc_amount:.8f} LTC`", inline=True)
		embed.add_field(name="🔖 INVOICE", value=f"`{invoice_id}`", inline=False)
		embed.add_field(name="📍 RECEIVE AT", value=f"`{address}`", inline=False)
		embed.set_image(url="attachment://ltc-invoice.png")
		embed.set_footer(text="Waiting for a confirmed Litecoin payment")
		invoice_message = await interaction.followup.send(
			embed=embed,
			file=qr_file,
			view=InvoiceView(ltc_amount, address),
			wait=True,
			**PRIVATE_RESPONSE(interaction),
		)
		if not PRIVATE_RESPONSE(interaction).get("ephemeral"):
			save_invoice_message(invoice_id, invoice_message)

	if TRACKER_TASK is None or TRACKER_TASK.done():
		TRACKER_TASK = asyncio.create_task(invoice_tracker(bot))

	async def dashboard_command(interaction: discord.Interaction) -> None:
		if interaction.user.id != OWNER_ID:
			await interaction.response.send_message(
				"❌ You are not authorized to use the wallet dashboard.", ephemeral=True
			)
			return
		await interaction.response.defer(ephemeral=True)
		embed = await build_dashboard_embed(bot)
		await interaction.followup.send(
			embed=embed, view=WalletDashboardView(bot), ephemeral=True
		)

	bot.tree.add_command(
		app_commands.Command(
			name="dasboard-wallet",
			description="Open the owner wallet dashboard",
			callback=dashboard_command,
		)
	)
	bot.tree.add_command(
		app_commands.Command(
			name="dashboard-wallet",
			description="Open the owner wallet dashboard",
			callback=dashboard_command,
		)
	)
