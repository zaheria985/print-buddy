# Standalone bot (no Hermes, no LLM)

Print Buddy can talk to Discord itself. Links, dropped 3MF and STL files, color and fit buttons, slicing, the
bed-clear start button, slash commands and printer alerts all run from Python with a Discord bot token. No
Hermes and no LLM are needed. An OpenAI-compatible LLM is an optional extra for replying to free text.

It can also run on Telegram instead (#164): set `bot.platform` to `telegram` and follow
[Telegram bot setup](TELEGRAM-BOT-SETUP.md). The Telegram bot is a pure bot with no LLM at all — unmatched
text gets the command list even when `[llm]` is configured for another platform.

Or in a Matrix room: set `bot.platform` to `matrix` and follow
[Matrix bot setup](MATRIX-BOT-SETUP.md). The Matrix bot is a pure bot with no LLM too; prompts are a
numbered list answered by typing the number (Matrix has no buttons), and the room must be unencrypted.

The standalone bot runs the same handlers as the Hermes plugin, in the same order: `print_buddy.bot.router`
loads `plugins/print-feedback` through its own `register()` hook. A new command added to the plugin works in
both modes. Every safety gate is unchanged: nothing heats, moves or starts without a button press from an
allowed user, and the G-code guards and fit checks run exactly as before.

Use one bot token for one mode. If the Hermes gateway and the standalone bot share a token, both answer every
message. `python3 -m print_buddy.doctor` warns when a Hermes profile holds the standalone bot's token.

## Discord setup

1. In the [Discord developer portal](https://discord.com/developers/applications), create an application and a bot.
   Copy the bot token.
2. Under **Bot > Privileged Gateway Intents**, turn on **Message Content** (needed for links, typed replies and
   "print it").
3. Invite the bot with the `bot` and `applications.commands` scopes and these permissions in the print channel:
   View Channel, Send Messages, Send Messages in Threads, Attach Files, Embed Links, Read Message History.
4. Copy the channel ID and the Discord user IDs of the people who may print (Developer Mode, then Copy ID).

## Settings

| Key | Default | What it does |
|---|---|---|
| `bot.mode` | `hermes` | `standalone` runs `python3 -m print_buddy.bot` with no Hermes. `hermes` keeps the gateway plugin |
| `bot.platform` | `discord` | The chat platform the standalone bot talks to (#164): `discord`, `telegram` or `matrix`. A config without this key keeps `discord` |
| `telegram.token_env` | `TELEGRAM_BOT_TOKEN` | Name of the environment variable that holds the Telegram bot token |
| `telegram.chat_id` | empty | The print chat. Group and channel IDs are negative. The bot ignores every other chat (forum topics inside it work like threads) |
| `telegram.allowed_user_ids` | empty | Telegram user IDs allowed to use the bot, separated by commas or spaces |
| `telegram.approval_user_id` | empty | Always allowed as well |
| `telegram.upload_max_bytes` | `50000000` | Largest file the Telegram bot uploads (the Bot API cloud limit) |
| `matrix.homeserver_url` | empty | The Matrix homeserver base URL (matrix.org or self-hosted) |
| `matrix.access_token_env` | `MATRIX_ACCESS_TOKEN` | Name of the environment variable that holds the Matrix access token |
| `matrix.room_id` | empty | The print room's ID (starts with `!`). The room must be unencrypted; the bot ignores every other room |
| `matrix.allowed_user_ids` | empty | Matrix user IDs allowed to use the bot, full `@user:server` form, separated by commas or spaces |
| `matrix.approval_user_id` | empty | Always allowed as well |
| `matrix.bot_user_id` | empty | The bot's own Matrix user ID. Empty: asked from the homeserver at startup |
| `matrix.upload_max_bytes` | `50000000` | Largest file the Matrix bot uploads; lowered to the homeserver's media limit when it reports a smaller one |
| `bot.health_port` | `0` | Serve `GET /healthz` on this port (200 healthy, 503 not). `0` turns the HTTP check off |
| `bot.health_stale_s` | `120` | The health check fails when the heartbeat is older than this |
| `bot.sync_commands` | `true` | Register the slash commands with Discord at startup |
| `logging.level` | `INFO` | `DEBUG`, `INFO`, `WARNING` or `ERROR`. Use `DEBUG` while chasing a problem; environment: `PRINT_BUDDY__LOGGING__LEVEL` |
| `logging.format` | `text` | `text` is one readable line per event. `json` is one JSON object per line for a log shipper |
| `logging.file` | empty | Also write a rotating log file (5 MiB, 3 backups) at this path. Empty logs to stderr only |
| `discord.bot_name` | `Print Buddy` | Name used in bot replies and help; environment: `PRINT_BUDDY__DISCORD__BOT_NAME` |
| `discord.chat_id` | empty | The print channel. The bot ignores every other channel (threads inside it work) |
| `discord.allowed_user_ids` | empty | Discord user IDs allowed to use the bot, separated by commas or spaces |
| `discord.approval_user_id` | empty | Always allowed as well |
| `discord.token_env` | `DISCORD_BOT_TOKEN` | Name of the environment variable that holds the bot token |
| `llm.chat_url` | empty | Optional OpenAI-compatible chat completions URL. Empty: unmatched text gets the command list |
| `llm.model` | empty | Model name, required when `chat_url` is set |
| `llm.api_key_env` | `LLM_API_KEY` | Name of the environment variable that holds the LLM API key, if it needs one |
| `llm.timeout_s` | `20` | LLM request timeout |

Every key can come from the environment as `PRINT_BUDDY__SECTION__KEY`, for example
`PRINT_BUDDY__BOT__MODE=standalone` or `PRINT_BUDDY__DISCORD__ALLOWED_USER_IDS="111 222"`.

For example, set `PRINT_BUDDY__DISCORD__BOT_NAME="Print Bitch"` in the process environment, or
`bot_name: "Print Bitch"` inside the YAML `discord:` mapping. Docker Compose reads
the same variable from `.env`. Restart the bot after changing it. This changes its self-description,
not the Discord account username or server nickname; set those in Discord. The default stays `Print Buddy`.

The token is only ever read from the environment variable `discord.token_env` names, or from the `.env` file in
`paths.hermes_home` (the data folder; the name is historical, it does not need Hermes). Nobody allowed means
nobody can use the bot: with no allowed users, every message is ignored and `check` fails.

```yaml
# config/local.yaml
bot:
  mode: standalone
  health_port: 8080
discord:
  chat_id: "123456789012345678"
  allowed_user_ids: "111111111111111111, 222222222222222222"
paths:
  hermes_home: ~/.local/share/print-buddy   # state, logs and the .env with DISCORD_BOT_TOKEN
services:
  moonraker_url: http://192.168.1.10:7125
  spoolman_url: http://192.168.1.10:7912
```

## Run

| Command | What it does |
|---|---|
| `python3 -m print_buddy.bot` (or `... run`) | Start the bot. Refuses to start, and lists why, unless every check below passes |
| `python3 -m print_buddy.bot check` | Offline setup check: mode, token present, channel, allowed users, discord.py, handlers loaded, no Hermes module imported, state folder writable, LLM. Exit 1 on any FAIL |
| `python3 -m print_buddy.bot health` | Exit 0 only while a running bot reports a fresh heartbeat, is logged in, its process is alive and no Hermes module is loaded in it |
| `python3 -m print_buddy.hermes.moonraker_event_listener` | The printer listener, unchanged. In standalone mode it posts over the configured platform's REST API with that platform's bot token (Discord REST, the Telegram Bot API when `bot.platform` is `telegram`, or the Matrix Client-Server API when it is `matrix`; its Discord button posts degrade to plain text on Telegram and Matrix) |
| `python3 -m print_buddy.doctor` | Also reports the bot mode, the Discord login and a token shared with a Hermes profile |

The bot writes its heartbeat to `<paths.hermes_home>/state/bot_health.json` every 15 seconds. `health` and
`GET /healthz` read the same record:

```json
{"mode": "standalone", "connected": true, "user": "Print Buddy#1234", "handlers": 15,
 "hermes_modules": [], "heartbeat_at": 1792727951.3, "pid": 7, "chat_id": "123...", "allowed_users": 2,
 "llm": false, "open_prompts": 0, "started_at": 1792727900.1}
```

`hermes_modules` lists any loaded module from Hermes (`tools`, `gateway`, `hermes_state`, `hermes_cli`). It must
be empty; the check fails otherwise. That is the proof the bot started without Hermes.

### systemd

`scripts/install-standalone.sh` writes `print-buddy-bot.service` and `print-buddy-listener.service` to
`~/.config/systemd/user`, creates the data folder and a `.env` (mode 600) for the token, and enables nothing.
Follow the lines it prints.

### Container

The bot and the listener are two long-running processes from one image:

```
python3 -m print_buddy.bot run                                  # Discord bot
python3 -m print_buddy.hermes.moonraker_event_listener          # printer listener
HEALTHCHECK: python3 -m print_buddy.bot health                  # or GET :$PRINT_BUDDY__BOT__HEALTH_PORT/healthz
```

Required environment: `PRINT_BUDDY__BOT__MODE=standalone`, `DISCORD_BOT_TOKEN`, `PRINT_BUDDY__DISCORD__CHAT_ID`,
`PRINT_BUDDY__DISCORD__ALLOWED_USER_IDS`, `PRINT_BUDDY__SERVICES__MOONRAKER_URL`. Both processes must share
`paths.hermes_home` (state) and `paths.archive`; the bot also writes dropped files under
`<paths.cache>/discord-uploads`.

## What you get

* Post a MakerWorld or Printables link, or drop one `.3mf` or `.stl`: the same intake as under Hermes, with the same
  buttons. "print it" after a link, and "print the last one again", work.
* Slash commands: `/print-next`, `/print-stop`, `/print-resume`, `/print-again`, `/print-reboot`,
  `/print-coldpull`, `/print-maintenance`, `/print-colors`, `/spool`, `/print-lookup`, `/print-troubleshoot`,
  `/print-buddy` (guided setup and read-only debugging), `/print-help`. Typing the same text works too.
* Listener posts (started, complete, failed, timelapse) and alert buttons (Resume, Cancel, spool questions, IFS
  insertion, plate picker) work with the Hermes gateway stopped.
* A typed reply answers your own open question, as under Hermes ("2" for two copies, "split", "cancel"). It
  never answers another allowed user's question, even in the same channel or thread. Buttons are shared: any
  allowed user may press an open question's button, and the message then shows who chose.
* Anything else gets the command list, or with `[llm]` set, a short reply from the model. The model may name one
  command from a fixed list (read-only or button-gated: `/print-stop`, `/print-next`, `/spool list`, ...). That
  command then asks for its button like a typed one. It can never run `/spool change`, `/spool new`,
  `/spool sync`, `/print-colors` or `/print-again stop`, and it never sees links, files or the printer.

## Hermes pieces and their replacement

| Hermes piece | Standalone replacement |
|---|---|
| `pre_gateway_dispatch` hook with the plugin's handlers | `print_buddy.bot.router.Router` calls the plugin's `register()` and runs the same handlers in the same order. `receive_ack` (a receipt promising an agent reply) is skipped, since no agent follows |
| Gateway authorization (`_is_user_authorized_for_source`), delivery adapter, session key | `print_buddy.bot.runtime.Gateway`: allowed user IDs plus the print channel; replies go to the same channel or thread; one session per person per channel |
| Adapter `send` / `send_clarify` | The platform interface in `print_buddy.bot.platform` (#164): `send`, `send_clarify`, `answer_typed`, `close_prompt`, `is_allowed_id`, plus per-platform limits (`message_limit`, `upload_max_bytes`, `supports_buttons`, `max_buttons`). Discord's implementation is `print_buddy.bot.adapter.StandaloneAdapter` over discord.py. `MEDIA:/path` becomes an attachment; long text is split at 2000 characters |
| Clarify gateway (register, wait, resolve, cancelled) | `print_buddy.core.prompts`, the same API. `prompts.gateway()` picks Hermes' module under Hermes and this one in standalone mode, so the shared handlers work in both |
| Platform handler registration and the adapter's `_is_allowed_user` | The router hands the plugin's `alert_button_handlers` our discord.py client; the adapter implements `_is_allowed_user` |
| `hermes send` subprocess in the listener | `print_buddy.bot.rest.send`: Discord REST with the bot token, `MEDIA:` files as attachments. `hermes send` stays in Hermes mode |
| Hermes `state.db` messages for "print it" | `print_buddy.bot.recent_links`: the last model link per channel in `<state>/recent_model_links.json`. Under Hermes, `state.db` is still read when that file has nothing |
| Agent session resets and cleanups after a print (`state.db`, `SessionDB`) | Not needed: there is no agent conversation. Skipped in standalone mode, so no `state.db` is created |
| Tool-call hooks (`pre_tool_call`, `post_tool_call`) | Not needed: they only guard an agent's tool calls. Kept registered for Hermes |
| Hermes home, install script, systemd units, restart drop-in | `paths.hermes_home` is just the data folder; `scripts/install-standalone.sh` and `systemd/*.service.template` with no gateway coupling |

## Moving from Hermes

1. Stop the gateway for this bot: `systemctl --user disable --now hermes-gateway` (or remove the Discord token from
   that profile).
2. Keep `paths.hermes_home` pointing at the old profile to keep its state (print numbers, plans, alerts), or copy
   its `state/` folder to the new data folder.
3. Set `bot.mode: standalone`, `discord.allowed_user_ids`, and put `DISCORD_BOT_TOKEN` in the environment or the
   data folder's `.env`.
4. Run `python3 -m print_buddy.bot check`, then `scripts/install-standalone.sh` and enable both units.

Going back is the reverse: `bot.mode: hermes`, stop the standalone units, start the gateway.

## Adding a chat platform

For contributors (#164): the bot core and the plugin handlers talk to a chat service only through
`print_buddy.bot.platform` — `BotCore` is built with one platform adapter and never imports a platform
library. A new platform is a new module under `print_buddy/bot/` holding a `StandaloneAdapter`-shaped
implementation of `send` / `send_clarify` / `answer_typed` / `close_prompt` / `is_allowed_id` (prompt
bookkeeping and choice resolution are shared in `BasePlatformAdapter`), a client that turns service
events into `runtime.make_source` / `runtime.make_event` values, its name in `platform.KNOWN_PLATFORMS`
and its module in `platform.platform_module`, a settings section validated in `settings.py`, and a fake
in `tests/bot/` that drives a full intake with no network (see `tests/bot/test_platform.py`). Per-platform
limits live on the adapter, not in callers. No handler or core change should be needed.

## Not covered yet

* Agent-only work has no scripted equivalent: filament photo inventory, incident write-ups, mesh routines from chat
  and free-form troubleshooting. With an LLM configured you get a short reply, not an agent.
* Button questions asked by a running intake live in memory. After a bot restart an old question's buttons answer
  "That question already closed"; send the link again. Listener alert buttons do survive restarts (#61, #62).
