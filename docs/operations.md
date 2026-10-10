# Operation and deployment

## Adaptive heartbeat

The bot measures timestamp gaps between newly observed, substantive messages from authorized users. The first gap sets its target; later gaps use a moving average (35% newest gap, 65% previous average). Rapid exchanges move the next scan toward the minimum, while slower exchanges lengthen it. Laughter filtered locally, unauthorized traffic, and bot replies do not affect this timing. Several messages from one participant count too. The response cap still applies.

Before it has two messages to learn from, it uses `SCAN_INTERVAL_SECONDS`. During inactivity the delay grows to at least half the elapsed idle time, capped at `MAX_SCAN_INTERVAL_SECONDS`. Every delay, including the initial fallback, stays inside the configured bounds. Paused scans use the maximum. First-start baselines and pause/resume reset timing samples; ordinary restarts also relearn timing, retaining the saved message cursor.

Example: set fallback `30`, minimum `10`, maximum `90`. Messages spaced about 12 seconds apart bring scans near 12 seconds; a quiet channel gradually reaches 90 seconds. A new message in an idle channel is discovered at the next scheduled poll, so it can wait up to that interval. The bot still generates only during polling, without an `on_message` reply handler. API/context work adds latency before the next sleep; these bounds limit the polling sleep, not total reply time. `/bot_status` shows the current calculated delay and bounds; logs show each scheduled sleep.

## State and delivery behavior

`state.json` beside `bot.py` persists `enabled` and `last_seen_message_id`. Writes use a temporary file, fsync, and atomic replacement. Missing/malformed cursor state causes a fresh baseline at the newest message (or current-time snowflake in an empty channel), with no retrospective responses. A valid cursor is retained across restarts. A valid saved `enabled` overrides `BOT_ENABLED`.

Each heartbeat takes an upper message-ID snapshot and processes new messages in chronological order. By default it replies to at most the newest substantive message in that batch; earlier messages stay in context and are marked seen, rather than queued for replies later. Set `MAX_RESPONSES_PER_SCAN` to increase this cap. Laughter-only messages, common keyboard-row mashing, emoji-only reactions, and short acknowledgments are filtered locally when `SKIP_LOW_CONTENT_MESSAGES=true`. Substantive messages containing laughter still qualify. Gemini can also choose `[NO_REPLY]` when there is nothing useful to add; that marker is never sent to Discord. The cap counts generated responses, not Discord chunks; exceptionally long requested explanations still use safe splitting. Other users and bots advance bookkeeping but cannot trigger Gemini. Context contains up to 20 relevant text messages, including this bot's own replies, in chronological order; unauthorized messages are excluded. Old relevant messages can provide context, but are never replied to as new messages.

**At-most-once attempts, not guaranteed delivery:** read-only context fetch failures leave the cursor unchanged and retry on the next heartbeat; a cursor is persisted only after context retrieval, before generation/sending. Gemini failures, empty responses, process crashes after claiming a message, and partial multi-message send failures can lose a reply. These messages are logged and skipped, never retried automatically. This deliberately avoids duplicate replies; no JSON-only design can atomically commit both a file and a remote Discord send. Do not run multiple instances against the same channel/state file.

Paused heartbeats keep advancing the baseline. Resume atomically saves the newest channel message as the new baseline before enabling replies. Polling and controls share a lock; a pause cannot interrupt a reply already being generated, and its confirmation may wait for that reply. Generation attempts have timeouts. Connection errors are retried on later heartbeats; discord.py handles gateway reconnection and Discord rate limits. Failed state writes prevent new generation.

Only plain message text is sent to Gemini: no attachment downloads, images, or embeds. Message content for authorized participants is transmitted to Google; arrange participant consent and applicable API data settings. Mentions are disabled in outgoing replies. Responses longer than Discord's limit are split at natural boundaries when possible, including emoji-aware limits.

## Deploy to a cloud host later

Choose a host supporting an **always-on Python 3.12 background worker** and a **persistent writable disk**. Static hosting and sleeping/request-only web services are unsuitable.

1. Connect this GitHub repository and select the branch containing the bot.
2. Set runtime Python to 3.12 using the provider's runtime setting.
3. Set install/build command to `python -m pip install -r requirements.txt`.
4. Set start command to `python bot.py`, with the repository root as working directory.
5. Add all variables in the table through the provider's environment/secret settings. Store both tokens as secrets, never in the repo.
6. Mount persistent storage at the application directory so `state.json` remains alongside `bot.py`, or choose a deployment layout with that directory on persistent storage. Confirm code updates do not remove the state file. Run exactly one worker replica.
7. Allow outbound HTTPS to `generativelanguage.googleapis.com` and Discord API/Gateway access (`discord.com`, `gateway.discord.gg`, and relevant Discord infrastructure). The host must support long-lived Discord WebSockets. No inbound port is required. The bot honors `HTTPS_PROXY` (or `HTTP_PROXY`) for Discord REST and Gateway connections. A proxy-substituted token may authenticate REST but cannot necessarily be substituted inside WebSocket Identify messages: Gateway error `4004` after successful REST login requires checking token delivery. Inject the real bot token directly through the host's secure runtime-secret setting; never print it or put it in a file tracked by Git.
8. Enable the host's process restart policy. Check logs and repeat the live checks above. Do not delete state during routine deploys; losing it causes a fresh baseline, discarding pending messages.

## Validation

```sh
python -m py_compile bot.py tests/test_bot.py
python -m pip check
python -m unittest discover -s tests -v
```

The offline suite checks authorization and scope, old-message baselines, ordering, duplicate prevention/restarts, pause/resume and owner controls, context privacy, malformed/partial state, failed writes, generation/send failures, empty responses, cautious search fallback, grounding source extraction, and Discord splitting. It needs no credentials. It cannot establish real Discord permissions, valid IDs/tokens, Gemini model availability, or successful live grounding.
