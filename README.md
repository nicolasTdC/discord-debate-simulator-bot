# Discord AI Chat Bot

A configurable Discord chat bot built with Python, discord.py, and Google's Gemini SDK. It follows conversations in one server channel and responds to an allowlist of participants using a customizable system prompt.

## Features

- General chat example with no fixed character or debate scenario.
- Google Search grounding with a cautious fallback when search is unavailable.
- Adaptive polling, conversation context, and configurable reply limits.
- Owner-only `/bot_pause`, `/bot_resume`, and `/bot_status` commands.
- Persistent message cursor, disabled outgoing mentions, and offline tests.

## Quick start

Requires Python 3.12 and a Discord bot token plus a Gemini API key.

```sh
git clone https://github.com/nicolasTdC/discord-ai-chat-bot.git
cd discord-ai-chat-bot
python -m venv .venv
. .venv/bin/activate
python -m pip install -r requirements.txt
cp .env.example .env
```

On Windows PowerShell, activate with `.venv\Scripts\Activate.ps1` and copy configuration with `Copy-Item .env.example .env`.

Fill in the tokens and numeric Discord IDs in `.env`, enable Message Content Intent for the bot, and invite it with View Channels, Send Messages, and Read Message History permissions. See [setup and configuration](docs/setup.md) for the complete instructions and environment variables.

```sh
python bot.py
```

## Customize the prompt

The included [system_prompt.txt](system_prompt.txt) is a general chat example. To use a personal prompt, save it outside the repository or as ignored `system_prompt.local.txt`, then set `SYSTEM_PROMPT_PATH` in your private `.env`:

```dotenv
SYSTEM_PROMPT_PATH=system_prompt.local.txt
```

Relative paths resolve from the repository root; absolute paths work too. The file is read as UTF-8 without altering its contents. Restart the bot after changing the prompt. Keep personal prompts and credentials out of commits.

## Documentation

- [Setup and configuration](docs/setup.md)
- [Operation, delivery behavior, deployment, and tests](docs/operations.md)

Only allowlisted participants in the configured channel can trigger replies. Their message text is sent to Google's API. Run one instance per channel/state file. The bot skips existing messages on a fresh start and uses at-most-once response attempts; a failed generation or send may lose a reply.

## Tests

```sh
python -m unittest discover -s tests -v
```

Tests run offline without Discord or Gemini credentials.
