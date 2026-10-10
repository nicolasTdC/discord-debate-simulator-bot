# Setup and configuration

## 1. Create the Discord application and bot (phone-friendly)

1. On Android, open Chrome and visit <https://discord.com/developers/applications>. Sign in. If controls are hidden, use Chrome's ⋮ menu → **Desktop site**, then rotate the phone sideways.
2. Tap **New Application**, give it a name, accept the terms, and tap **Create**.
3. Open **Bot** in the application's menu. The application may already have a bot; otherwise tap **Add Bot** and confirm.
4. Under **Privileged Gateway Intents**, enable **Message Content Intent** and save. Server Members and Presence intents are not required.
5. Turn **Public Bot** off. Keep **Requires OAuth2 Code Grant** off. Use a server you own/manage for your bot.
6. Under **Token**, tap **Reset Token** and confirm if necessary, then **Copy**. This is `DISCORD_BOT_TOKEN`. Never use your personal Discord account token. Treat the bot token like a password; reset it if exposed.
7. Open **OAuth2 → URL Generator**. Select scopes **bot** and **applications.commands**. Select bot permissions **View Channels**, **Send Messages**, and **Read Message History**. Do not grant Administrator.
8. Copy the generated URL, open it in Chrome, choose your server, and authorize. You need permission to manage that server. If the developer portal restricts a private app's OAuth URL, use its **Application ID** from **General Information** in this URL (replace APPLICATION_ID):

   `https://discord.com/oauth2/authorize?client_id=APPLICATION_ID&scope=bot%20applications.commands&permissions=68608`

9. In Discord, set channel permissions so the bot can view, read history, and send in the configured channel. Restrict its role's access to other channels if desired. The code also enforces the configured guild/channel regardless of permissions.

## 2. Copy numeric IDs on Android

1. In the Discord Android app, open your profile (**You**), then the gear (**Settings**) → **Advanced** → enable **Developer Mode**. Labels can differ slightly by app release; search settings for “Developer Mode” if needed.
2. Open your server, tap its name or long-press its icon, open the server menu, and tap **Copy Server ID**. Set `DISCORD_GUILD_ID` to that number.
3. Long-press the text channel in the channel list and tap **Copy Channel ID**. Set `DISCORD_CHANNEL_ID` to that number. Use a normal server text channel, not a DM, forum, voice channel, or thread.
4. Find a message from each authorized participant, tap their avatar to open their profile, tap **⋮**, then **Copy User ID**. Repeat for everyone you want to authorize. You can also long-press their member-list entry to find the copy action.
5. Set `ALLOWED_USER_IDS` to one or more numbers separated by commas, without usernames. Confirm you selected the correct people; names can change and are not identifiers.
6. Copy the ID of the person allowed to control the bot and set `OWNER_USER_ID`. The owner may be one of the authorized participants or a different user. Only this ID can use controls, and only in the configured channel.

This repository does not guess IDs from the supplied usernames.

## 3. Create the Gemini API key

1. In Chrome, visit <https://aistudio.google.com/apikey> and sign in to Google.
2. Tap **Create API key**, select/create an eligible Google Cloud project as prompted, and copy the key securely.
3. Set `GEMINI_API_KEY`. Check project quota, supported region, and billing requirements in AI Studio. Google Search grounding may have separate pricing/limits. Review these before running continuously.
4. `GEMINI_MODEL` selects the model. Blank or unset uses `gemini-flash-latest`, the lightweight Flash alias used in the current [official SDK examples](https://github.com/googleapis/python-genai#readme). An alias can change underneath you; pin a supported concrete model in your environment if reproducibility matters.

The bot requests `types.Tool(google_search=types.GoogleSearch())` on each generation so the model can search when current claims require it. Unsupported models, search failures, quota errors, and timeouts trigger one attempt without tools, with explicit instructions to disclose lack of verification and avoid unverified current claims. Only HTTPS URLs supplied in grounding metadata are appended, at most three. A response without sources does **not** establish that its factual claims were verified. Model factual accuracy is not guaranteed; inspect consequential claims yourself.

Official references: [models](https://ai.google.dev/gemini-api/docs/models), [search grounding](https://ai.google.dev/gemini-api/docs/google-search), [Python SDK](https://github.com/googleapis/python-genai). Live grounding/model availability must be checked using your project.

## 4. Set configuration and run locally

Run these commands in a terminal on a computer or cloud machine with Python 3.12 installed. On a phone, use the hosting provider's browser terminal or SSH app; these are not commands for a Discord chat.

```sh
git clone https://github.com/nicolasTdC/discord-ai-chat-bot.git
cd discord-ai-chat-bot
python3.12 -m venv .venv
. .venv/bin/activate
python -m pip install -r requirements.txt
cp .env.example .env
```

On Windows PowerShell, use `py -3.12 -m venv .venv`, `.venv\Scripts\Activate.ps1`, and `Copy-Item .env.example .env` instead of the corresponding commands above.

Open `.env` in your editor and fill all blank values. Do not overwrite an existing `.env` containing your settings. Keep it private. Hosting providers can instead inject these variables directly; injected values take precedence over `.env`.

| Variable | Value |
| --- | --- |
| `DISCORD_BOT_TOKEN` | Bot token from developer portal; secret |
| `DISCORD_GUILD_ID` | Numeric server ID |
| `DISCORD_CHANNEL_ID` | Numeric text-channel ID |
| `ALLOWED_USER_IDS` | One or more positive numeric IDs, comma-separated; duplicates are deduplicated |
| `OWNER_USER_ID` | Numeric control owner ID |
| `GEMINI_API_KEY` | AI Studio API key; secret |
| `GEMINI_MODEL` | Default `gemini-flash-latest`; override as needed |
| `SCAN_INTERVAL_SECONDS` | Default `30`; initial/fallback seconds, clamped to min/max |
| `MIN_SCAN_INTERVAL_SECONDS` | Default `10`; positive lower heartbeat bound |
| `MAX_SCAN_INTERVAL_SECONDS` | Default `120`; upper bound, at least the minimum |
| `MAX_RESPONSES_PER_SCAN` | Default `1`; positive integer, maximum response attempts per heartbeat |
| `SKIP_LOW_CONTENT_MESSAGES` | Default `true`; locally skip laughter, common keyboard mashing, and brief acknowledgments |
| `BOT_ENABLED` | `true` or `false`; used when valid saved enabled state is absent |

Start with:

```sh
python bot.py
```

Keep the terminal/process running. Stop with Ctrl+C. Logging goes to stdout. Missing/invalid required configuration fails startup explicitly. Never paste tokens into chat, logs, commits, or screenshots.

### First live check

1. Confirm the log says Discord connected and the slash commands appear in the configured server/channel. If missing, check invite scopes and command-sync errors.
2. Existing channel messages should receive no replies on the first fresh startup. Send a new text message as one of the allowed users; wait one heartbeat (initially 30 seconds, with 10–120 second bounds by default), plus generation time.
3. Confirm a reply arrives. Messages from another user, another bot, or another channel should not produce a reply.
4. As the owner, run `/bot_pause`, send another message, and confirm no response after a heartbeat. Run `/bot_status` to check state. Run `/bot_resume`: messages sent during the pause should stay unanswered. Send a fresh message and check the next heartbeat.
5. Check useful factual responses for grounding sources. Check logs for Gemini failures; absent sources are not proof of grounding success.

No live API calls are part of the offline tests. You must supply real secrets and IDs for this live check.


For a private custom prompt, set `SYSTEM_PROMPT_PATH` to an external UTF-8 file or ignored `system_prompt.local.txt`. See the [README](../README.md#customize-the-prompt).
