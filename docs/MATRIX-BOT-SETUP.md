# Matrix bot setup

Print Buddy can run its standalone bot in a Matrix room instead of Discord
or Telegram (#164). The same handlers, prompts and safety gates serve every
platform; only the transport changes. The Matrix bot is a **pure bot: it
has no LLM**. Unmatched text gets the command list, and no `[llm]` setting
is ever consulted for Matrix — deliberate, the same no-LLM posture Telegram
proved first.

The bot talks to the homeserver with the Matrix Client-Server API by long
polling (`/sync`), so it needs no inbound port, no public URL and no
webhook. It uses only the Python standard library; there is no Matrix
package to install. It works with matrix.org or a self-hosted homeserver
(Synapse, Conduit, Dendrite, …) — anything that serves the standard
client API.

## The room must be unencrypted

End-to-end encrypted rooms are **not supported**: the bot cannot decrypt,
so it ignores `m.room.encrypted` events entirely and would never see your
messages. Create the print room **without** encryption (in Element:
room settings → Security & Privacy → Encrypted: off, chosen at creation
time; encryption cannot be turned off afterwards). Keep the room private
and invite-only instead: access is controlled by the room membership plus
Print Buddy's allowed-user list.

## 1. Create a bot account and get an access token

1. Register a dedicated account for the bot on your homeserver (for
   example `@printbuddy:matrix.org`). A dedicated account matters: the
   access token below acts as that account.
2. Sign in as the bot account in Element (a private window works), open
   **Settings → Help & About → Advanced** and click the access token to
   reveal it. It looks like `syt_...`. Keep it secret: anyone with the
   token is the bot. Signing out that session revokes it.
   (Programmatic login also works: `POST /_matrix/client/v3/login` with
   `m.login.password` returns an `access_token`.)
3. Note the bot's full user ID (`@printbuddy:matrix.org`) — you can set it
   as `matrix.bot_user_id`, or leave that empty and the bot asks the
   homeserver who it is (`whoami`) at startup.

## 2. Create the print room and invite the bot

1. Create a private, **unencrypted** room.
2. Invite the bot account to the room. The bot accepts the invitation
   itself when it next syncs, so there is nothing to click as the bot
   account.
3. **Room ID**: in Element, room settings → Advanced shows the room ID,
   for example `!abcd1234:matrix.org`. It starts with `!` — do not use the
   room alias (`#prints:...`), room IDs and aliases are different things.
4. **Your user ID**: your full `@user:server` ID, from your client
   profile. Collect the ID of every person who may print, plus the
   approver's. Matrix IDs are never numbers.

## 3. Configure Print Buddy

```yaml
# config/local.yaml
bot:
  mode: standalone
  platform: matrix
matrix:
  homeserver_url: "https://matrix.org"
  room_id: "!abcd1234:matrix.org"
  allowed_user_ids: "@you:matrix.org, @partner:example.org"
  approval_user_id: "@you:matrix.org"
services:
  moonraker_url: http://192.168.1.10:7125
```

Put the access token in the environment variable `[matrix]
access_token_env` names (`MATRIX_ACCESS_TOKEN` by default) — in the
process environment or in the `.env` file in `paths.hermes_home`, mode
600. Never put the token in a config file. Every setting can also come
from the environment, for example
`PRINT_BUDDY__MATRIX__ROOM_ID='!abcd1234:matrix.org'`.

Nobody allowed means nobody can use the bot: with no allowed users every
message is ignored and `check` fails.

## 4. Check and run

```bash
export MATRIX_ACCESS_TOKEN='syt_...'
python3 -m print_buddy.bot check   # offline: mode, token present, homeserver, room, allowed users, handlers, no Hermes
python3 -m print_buddy.bot         # start syncing
```

The bot remembers its `/sync` position in the state directory
(`matrix-next-batch`), so a restart picks up where it stopped instead of
replaying the room's history; the very first run starts from "now".

Printer event posts (started, complete, failed, timelapse) come from the
listener, unchanged: in standalone mode with `platform: matrix` it posts
into the same room through the Client-Server API instead of Discord REST.

## How Matrix differs from Discord here

- **No buttons**: Matrix has no inline keyboards, so a prompt is the
  question plus a numbered list — reply with the number (`2`) or with
  the choice text itself. Answering posts a short note recording the
  choice. The listener's Discord button posts (pause alerts, plate
  picker, insertion and spool questions) are sent as plain text that
  names the typed command or reply doing the same job.
- **One room**: the configured room is the print chat. Threads are not
  used in this version, and only that room is served.
- **Messages are plain text.** No formatted body is sent, so file names,
  model names and G-code fragments are shown exactly as written
  (Discord's `**bold**` markers appear literally).
- **Limits**: messages are split at 16000 characters (well under
  Matrix's 64 KB event cap), files up to 50 MB, lowered automatically to
  the homeserver's own media limit when the server reports a smaller
  one. Model files (.3mf/.stl) arrive as file events; previews and
  timelapses are sent back as image/video events when Matrix has a
  native type for them, everything else as a file event.
- **Edits are not messages**: editing a message never re-runs a command.
