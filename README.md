# Discord payment bot

This bot provides private slash commands in DMs and servers:


Responses are ephemeral where Discord supports them, so addresses and payment details are not posted into public server channels.

In server channels, users with the Discord Administrator permission receive normal visible responses. Other server members receive ephemeral responses. In direct messages, responses are normal messages because the conversation is already private.

## Run locally

1. Install Python 3.10 or newer.
2. Create a Discord application at https://discord.com/developers/applications.
3. On the **Bot** page, reset and copy the bot token. Never share it.
4. On **Installation**, enable **User Install** and **Guild Install**. For a user install, use the `applications.commands` scope. For a server install, select scopes `bot` and `applications.commands`, then invite the bot to your server.
5. Copy `.env.example` to `.env` and fill in `DISCORD_TOKEN`, `OWNER_ID`, `LOG_CHANNEL_ID`, and optionally `DISCORD_GUILD_ID`. `OWNER_ID` must be your Discord user ID; only that account can use `/stats`. Each user sets their own payment details with `/setupi <upi_id>` and `/setupltcaddy <ltc_address>`.
6. Install dependencies and run:

```powershell
py -m pip install -r requirements.txt
py bot.py
```

Global slash commands can take up to an hour to appear after the first sync. For instant updates in one server, set `DISCORD_GUILD_ID` to that server's ID and restart the bot. Enable Discord Developer Mode, right-click the server, and choose `Copy Server ID` to get the value.

After the bot is online, each user runs `/setupi upi_id`, for example `/setupi demo@oksbi`. The setting is private to that Discord account, and that user can run `/upi` in a DM or server to receive their UPI ID and QR code.

Users can upload a personal QR image with `/qr-set photo`, then use `/qr` to display only their own saved QR code privately. Supported formats are PNG, JPG, WEBP, and GIF, up to 5 MB.

Each user runs `/setupltcaddy ltc_address` to configure their Litecoin address. They can then use `/addy` in a DM or server to view their own address privately.

Use `/bal` to check the current balance and total received for the saved address, or provide another address with `/bal ltc_address`. Litecoin address data comes from BlockCypher.

Use `/invoice amount` to create a USD Litecoin invoice using the address saved with `/setupltcaddy`. The bot creates a payment QR, stores the invoice, checks the address automatically, and sends the invoice owner a confirmation message after the required amount is confirmed on the Litecoin network. Invoices expire after 24 hours.

The owner can use `/dasboard-wallet` to view invoice totals, paid/pending/detected counts, paid USD/LTC totals, current wallet balance, wallet value, connected servers, and tracker status. The dashboard includes a refresh button and is always private to `OWNER_ID`. `/dashboard-wallet` is also accepted with the corrected spelling.

Each user can save multiple named auto-responses with `/set-ar name text` and display them with `/ar name`, for example `/set-ar hi Hello there` followed by `/ar hi`, or `/set-ar king I am the king` followed by `/ar king`. Discord slash-command names must be registered by the bot, so the response name is an option rather than a new slash-command name.

Other commands include `/ar-delete name`, `/ar-clear`, `/userinfo user`, `/avatar user`, and `/translate language text`. Translation uses an external public translation service and supports short language codes such as `en`, `hi`, and `fr`.

Crypto commands include `/price coin`, `/convert amount from_coin to_coin`, and `/verify-tx ltc txid`. Price and conversion support `ltc`, `sol`, `usd`, and `usdt`; for example, `/price sol` or `/convert 100 usd sol`. Prices come from CoinGecko, and transaction status currently supports Litecoin through Blockchair.

## Logging setup

Create a private text channel for logs, enable **Developer Mode** in Discord, right-click that channel, choose **Copy Channel ID**, and put the numeric value in `.env`:

```env
LOG_CHANNEL_ID=123456789012345678
```

The bot needs **View Channel** and **Send Messages** permission in that channel. Log messages include every slash command's user, command, server/DM context, and timestamp. UPI/LTC setup logs additionally include the full configured value. Keep the log channel private. When the bot joins a server, it tries to identify the inviter through the server audit log; Discord may report the inviter as unknown if audit-log permission is unavailable. A removal log is sent when Discord delivers the server removal event.

## Free hosting

A free service cannot promise uninterrupted 24/7 operation. For a simple always-on bot, use a free Oracle Cloud Always Free VM if your account/region has capacity, then install Python, upload this folder, create `.env`, and run the bot with `systemd` or `tmux`. Render, Railway, and similar free tiers may sleep, change policy, or require billing verification, so they are not dependable for 24/7 Discord bots. Do not use a browser keep-alive workaround.

On a Linux VM, the service command is:

```ini
[Unit]
Description=Discord payment bot
After=network-online.target

[Service]
WorkingDirectory=/home/ubuntu/anujx-panda
ExecStart=/usr/bin/python3 /home/ubuntu/anujx-panda/bot.py
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
```

Save it as `/etc/systemd/system/payment-bot.service`, then run `sudo systemctl enable --now payment-bot`.
For user-installed apps, Discord does not send install or uninstall events; the bot logs the user's first DM/app command as `User app first used` instead.
