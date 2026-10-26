# Settings reference

Every user-adjustable setting, its YAML key, its environment variable and its value in the Docker configuration (`config/docker.yaml` over `config/home.yaml` and the printer model). `tests/test_docker_release.py` derives the inventory from the settings schema and fails when a setting lacks a row here or an `.env.example` line, or when a documented name is not a real setting.

* **YAML**: put keys in a file and select it with `PRINT_BUDDY_CONFIG`. In Docker, mount the file and point `PRINT_BUDDY_CONFIG` at its container path.
* **Environment**: `PRINT_BUDDY__SECTION__KEY=value` overrides any file. Booleans accept `true/false`, `1/0`, `yes/no`, `on/off`. Integer settings need integer spelling (`12`, not `12.0`). Rows marked *file only* are lists or tables and can only come from YAML or legacy TOML; the one list exception is `delivery.methods`, which also accepts a comma- or space-separated environment value (`PRINT_BUDDY__DELIVERY__METHODS=discord,bambu_handoff,printer`, #270).
* **Precedence**, later wins: vendor and printer model defaults (*model*), `config/home.yaml`, the selected config file, environment. Settings are read once at start, so restart the service after a change.
* A typo or invalid value stops startup and names the setting. YAML holds no secrets; see [Secrets](#secrets).

Schema, validation limits and TOML migration: [CONFIGURATION-YAML.md](CONFIGURATION-YAML.md). Standalone bot settings: [STANDALONE.md](STANDALONE.md). Printer vendor profiles: [PRINTER-PROFILES.md](PRINTER-PROFILES.md).

| YAML key | Environment variable | Docker value | What it does |
|---|---|---|---|
| `layout.single_copy_edge_mm` | `PRINT_BUDDY__LAYOUT__SINGLE_COPY_EDGE_MM` | `1.0` *model* | Bed-edge clearance for one copy |
| `layout.multi_copy_edge_mm` | `PRINT_BUDDY__LAYOUT__MULTI_COPY_EDGE_MM` | `3.0` *model* | Bed-edge clearance for several copies |
| `layout.merge_edge_margin_mm` | `PRINT_BUDDY__LAYOUT__MERGE_EDGE_MARGIN_MM` | `8.0` *model* | Edge margin when merging plates |
| `layout.merge_spacing_mm` | `PRINT_BUDDY__LAYOUT__MERGE_SPACING_MM` | `10.0` *model* | Gap between merged plates' parts |
| `layout.max_merged_hours` | `PRINT_BUDDY__LAYOUT__MAX_MERGED_HOURS` | `12` | Merged plates longer than this stay separate |
| `prime_tower.size_mm` | `PRINT_BUDDY__PRIME_TOWER__SIZE_MM` | `45.0` *model* | Prime tower size |
| `prime_tower.part_clearance_mm` | `PRINT_BUDDY__PRIME_TOWER__PART_CLEARANCE_MM` | `8.0` *model* | Prime tower to part clearance |
| `prime_tower.edge_mm` | `PRINT_BUDDY__PRIME_TOWER__EDGE_MM` | `10.0` *model* | Prime tower to bed edge clearance |
| `prime_tower.merge_slack_mm` | `PRINT_BUDDY__PRIME_TOWER__MERGE_SLACK_MM` | `5.0` *model* | Extra room kept for the tower when merging |
| `printer.firmware` | `PRINT_BUDDY__PRINTER__FIRMWARE` | `ZMod Klipper` *model* | Firmware label shown in job summaries |
| `printer.start_macro` | `PRINT_BUDDY__PRINTER__START_MACRO` | `START_PRINT` *model* | Klipper start macro for stock Klipper models |
| `printer.end_macro` | `PRINT_BUDDY__PRINTER__END_MACRO` | `END_PRINT` *model* | Klipper end macro for stock Klipper models |
| `printer.name` | `PRINT_BUDDY__PRINTER__NAME` | `Flashforge AD5X` *model* | Printer name shown in job summaries |
| `printer.bed_mm` | file only | `[220.0, 220.0]` *model* | Usable print area `[x, y]` in mm |
| `printer.max_print_height_mm` | `PRINT_BUDDY__PRINTER__MAX_PRINT_HEIGHT_MM` | `220.0` *model* | Tallest allowed print |
| `printer.nozzle_diameter_mm` | `PRINT_BUDDY__PRINTER__NOZZLE_DIAMETER_MM` | `0.4` *model* | Installed nozzle; slicing refuses any other |
| `printer.ifs_slots` | `PRINT_BUDDY__PRINTER__IFS_SLOTS` | `4` *model* | Filament slots; 1 for a single extruder |
| `printer.model` | `PRINT_BUDDY__PRINTER__MODEL` | `flashforge.ad5x` | Printer brand and model: `flashforge.ad5x`, `sovol.sv06`, `klipper.generic`, `octoprint.marlin`, `bambulab.a1`, `bambulab.p1s` (Bambu Lab models are offline only). Sets every model default marked *model* |
| `printer.vendor` | `PRINT_BUDDY__PRINTER__VENDOR` | `flashforge` *model* | Vendor part of the model key, set by the model; `vendor` plus a bare printer name is also accepted |
| `printer.slicer` | `PRINT_BUDDY__PRINTER__SLICER` | `orca` *model* | Slicer: `orca` everywhere; Bambu Lab printers default to `bambu_studio` and may pick `orca` |
| `slicer.flash_studio_path` | `PRINT_BUDDY__SLICER__FLASH_STUDIO_PATH` | empty | Optional path to a Flash Studio executable the user already installed. Empty keeps OrcaSlicer. A set but missing or non-executable path is an error, not a fallback. Never downloaded or bundled. |
| `printer.nozzle_type` | `PRINT_BUDDY__PRINTER__NOZZLE_TYPE` | unset | Optional nozzle material, limited to the model's supported types |
| `orca.templates` | `PRINT_BUDDY__ORCA__TEMPLATES` | the model's folder *model* | Folder with the Orca machine, process and filament JSON |
| `orca.command` | file only | `["/usr/local/bin/orca-slicer"]` | How to start the OrcaSlicer CLI; `PRINT_BUDDY_ORCA_CHANNEL` selects stable (default) or alpha; both binaries exist only when `WITH_ORCA=true` |
| `delivery.methods` | `PRINT_BUDDY__DELIVERY__METHODS` | `["printer"]` *model* | Post-slice options offered, from the vendor profile (for example `printer`); comma- or space-separated in the environment, an empty value keeps the file value |
| `bambu.command` | file only | `["/usr/bin/bambu-studio"]` | Bambu Studio CLI: the default slicer for Bambu Lab printers, Orca's one-shot fallback elsewhere; exists only in a `WITH_BAMBU=true` image |
| `bambu.lan_host` | `PRINT_BUDDY__BAMBU__LAN_HOST` | `""` | Bambu Lab printer LAN address for read-only status (#187); must be set together with `lan_serial`; empty disables LAN status |
| `bambu.lan_serial` | `PRINT_BUDDY__BAMBU__LAN_SERIAL` | `""` | The Bambu Lab printer's serial number (stored upper case); used in the MQTT topics, not as a credential |
| `bambu.lan_access_code_env` | `PRINT_BUDDY__BAMBU__LAN_ACCESS_CODE_ENV` | `BAMBU_LAN_PASSWORD` | Name of the environment variable holding the printer's LAN access code; the code itself is never a setting |
| `flashforge_stock.host` | `PRINT_BUDDY__FLASHFORGE_STOCK__HOST` | `""` | Stock Flashforge printer LAN address; empty leaves the printer offline. The serial and check code stay in `AD5X_SERIAL` and `AD5X_CHECK_CODE`, never in a setting |
| `paths.hermes_home` | `PRINT_BUDDY__PATHS__HERMES_HOME` | `/data/hermes` | State folder (`state/`, `logs/`); the name is historical, Hermes is not required |
| `paths.archive` | `PRINT_BUDDY__PATHS__ARCHIVE` | `/data/archive` | Job archive |
| `paths.records_vault` | `PRINT_BUDDY__PATHS__RECORDS_VAULT` | `/data/records` | Obsidian print records |
| `paths.cache` | `PRINT_BUDDY__PATHS__CACHE` | `/data/cache` | Regenerable cache |
| `paths.hermes_bin` | `PRINT_BUDDY__PATHS__HERMES_BIN` | `/usr/local/bin/hermes` | Hermes CLI; Hermes installs only, not in the image |
| `paths.ffmpeg` | `PRINT_BUDDY__PATHS__FFMPEG` | `ffmpeg` | ffmpeg binary or name on `PATH` |
| `paths.hermes_agent` | `PRINT_BUDDY__PATHS__HERMES_AGENT` | `/data/hermes/hermes-agent` | Hermes agent checkout (Hermes installs only) |
| `paths.hermes_restart_helper` | `PRINT_BUDDY__PATHS__HERMES_RESTART_HELPER` | `/data/hermes/restart_hermes.py` | Hermes restart helper (Hermes installs only) |
| `services.moonraker_url` | `PRINT_BUDDY__SERVICES__MOONRAKER_URL` | empty | Klipper printers: Moonraker HTTP URL, e.g. `http://printer.example:7125`. Required by the listener |
| `services.moonraker_ws_url` | `PRINT_BUDDY__SERVICES__MOONRAKER_WS_URL` | empty | Moonraker websocket; derived from `moonraker_url` when empty |
| `services.camera_snapshot_url` | `PRINT_BUDDY__SERVICES__CAMERA_SNAPSHOT_URL` | empty | Camera snapshot URL; derived from the Moonraker host when empty |
| `services.spoolman_url` | `PRINT_BUDDY__SERVICES__SPOOLMAN_URL` | empty | Spoolman URL. The Docker config and Compose default to `http://127.0.0.1:8000`, the bundled Spoolman API in the same container. Set another URL, plus `PRINT_BUDDY_BUNDLED_SPOOLMAN=false`, to use an existing external Spoolman instead |
| `services.openviking_url` | `PRINT_BUDDY__SERVICES__OPENVIKING_URL` | empty | Optional OpenViking memory store |
| `services.vision_chat_url` | `PRINT_BUDDY__SERVICES__VISION_CHAT_URL` | empty | OpenAI-compatible endpoint for camera checks and roasts; empty disables both |
| `services.stock_api_port` | `PRINT_BUDDY__SERVICES__STOCK_API_PORT` | `8898` | Flashforge stock API port for IFS presence |
| `services.octoprint_url` | `PRINT_BUDDY__SERVICES__OCTOPRINT_URL` | empty | OctoPrint printers (`octoprint.marlin`): OctoPrint URL. API key: `OCTOPRINT_API_KEY` secret |
| `discord.chat_id` | `PRINT_BUDDY__DISCORD__CHAT_ID` | empty | Discord channel ID |
| `discord.approval_user_id` | `PRINT_BUDDY__DISCORD__APPROVAL_USER_ID` | empty | Discord user ID allowed to approve |
| `discord.upload_max_bytes` | `PRINT_BUDDY__DISCORD__UPLOAD_MAX_BYTES` | `9500000` | Largest upload per file: video is shrunk to it, and a sliced file or Bambu package over it is reported with its host path instead of offered for sending (see [PRINTER-PROFILES.md](PRINTER-PROFILES.md)). Keep it under your server's Discord cap (10 MB unboosted) |
| `discord.post_print_quiet_s` | `PRINT_BUDDY__DISCORD__POST_PRINT_QUIET_S` | `7200` | Quiet time before a chat session resets |
| `discord.daily_reset_hour` | `PRINT_BUDDY__DISCORD__DAILY_RESET_HOUR` | `4` | Hour of the daily session reset |
| `discord.timezone` | `PRINT_BUDDY__DISCORD__TIMEZONE` | `America/New_York` | Display and reset time zone (IANA name) |
| `discord.bot_name` | `PRINT_BUDDY__DISCORD__BOT_NAME` | `Print Buddy` | Name the bot uses for itself |
| `discord.question_timeout_s` | `PRINT_BUDDY__DISCORD__QUESTION_TIMEOUT_S` | `1800` | How long a Discord question waits for a button |
| `discord.safety_confirm_timeout_s` | `PRINT_BUDDY__DISCORD__SAFETY_CONFIRM_TIMEOUT_S` | `600` | How long a safety confirmation waits |
| `discord.upload_wait_s` | `PRINT_BUDDY__DISCORD__UPLOAD_WAIT_S` | `3600` | How long an upload-wait freeze stays active |
| `discord.cold_pull_outcome_s` | `PRINT_BUDDY__DISCORD__COLD_PULL_OUTCOME_S` | `1800` | How long a cold-pull outcome prompt stays open |
| `discord.token_env` | `PRINT_BUDDY__DISCORD__TOKEN_ENV` | `DISCORD_BOT_TOKEN` | Name of the environment variable holding the bot token |
| `discord.allowed_user_ids` | `PRINT_BUDDY__DISCORD__ALLOWED_USER_IDS` | empty | Discord user IDs allowed to use the standalone bot, comma or space separated |
| `telegram.token_env` | `PRINT_BUDDY__TELEGRAM__TOKEN_ENV` | `TELEGRAM_BOT_TOKEN` | Name of the environment variable holding the Telegram bot token |
| `telegram.chat_id` | `PRINT_BUDDY__TELEGRAM__CHAT_ID` | empty | Telegram chat (group) ID; group and channel IDs are negative |
| `telegram.allowed_user_ids` | `PRINT_BUDDY__TELEGRAM__ALLOWED_USER_IDS` | empty | Telegram user IDs allowed to use the standalone bot, comma or space separated |
| `telegram.approval_user_id` | `PRINT_BUDDY__TELEGRAM__APPROVAL_USER_ID` | empty | Telegram user ID allowed to approve |
| `telegram.upload_max_bytes` | `PRINT_BUDDY__TELEGRAM__UPLOAD_MAX_BYTES` | `50000000` | Largest file the Telegram bot uploads (the Bot API cloud limit) |
| `matrix.homeserver_url` | `PRINT_BUDDY__MATRIX__HOMESERVER_URL` | empty | Matrix homeserver base URL, for example `https://matrix.org` or a self-hosted server |
| `matrix.access_token_env` | `PRINT_BUDDY__MATRIX__ACCESS_TOKEN_ENV` | `MATRIX_ACCESS_TOKEN` | Name of the environment variable holding the Matrix access token |
| `matrix.room_id` | `PRINT_BUDDY__MATRIX__ROOM_ID` | empty | Matrix room ID of the print room (starts with `!`); the room must be unencrypted |
| `matrix.allowed_user_ids` | `PRINT_BUDDY__MATRIX__ALLOWED_USER_IDS` | empty | Matrix user IDs allowed to use the standalone bot, full `@user:server` form, comma or space separated |
| `matrix.approval_user_id` | `PRINT_BUDDY__MATRIX__APPROVAL_USER_ID` | empty | Matrix user ID allowed to approve |
| `matrix.bot_user_id` | `PRINT_BUDDY__MATRIX__BOT_USER_ID` | empty | The bot's own Matrix user ID; empty asks the homeserver (whoami) at startup |
| `matrix.upload_max_bytes` | `PRINT_BUDDY__MATRIX__UPLOAD_MAX_BYTES` | `50000000` | Largest file the Matrix bot uploads; lowered to the homeserver's media limit when the server reports a smaller one |
| `bot.mode` | `PRINT_BUDDY__BOT__MODE` | `hermes`; `standalone` in the image | `hermes` or `standalone`; the image and Compose set `standalone` |
| `bot.platform` | `PRINT_BUDDY__BOT__PLATFORM` | `discord` | Chat platform for the standalone bot (#164): `discord`, `telegram` or `matrix` |
| `bot.health_port` | `PRINT_BUDDY__BOT__HEALTH_PORT` | `0` | Serve `GET /healthz` on this port; `0` turns the HTTP check off |
| `bot.health_stale_s` | `PRINT_BUDDY__BOT__HEALTH_STALE_S` | `120` | Bot health fails when its heartbeat is older than this |
| `bot.sync_commands` | `PRINT_BUDDY__BOT__SYNC_COMMANDS` | `true` | Register the slash commands with Discord at startup |
| `llm.chat_url` | `PRINT_BUDDY__LLM__CHAT_URL` | empty | Optional OpenAI-compatible chat URL; empty: unmatched text gets the command list |
| `llm.model` | `PRINT_BUDDY__LLM__MODEL` | empty | LLM model name, required when `chat_url` is set |
| `llm.api_key_env` | `PRINT_BUDDY__LLM__API_KEY_ENV` | `LLM_API_KEY` | Name of the environment variable holding the LLM API key |
| `llm.timeout_s` | `PRINT_BUDDY__LLM__TIMEOUT_S` | `20` | LLM request timeout |
| `listener.tick_s` | `PRINT_BUDDY__LISTENER__TICK_S` | `600` | Listener housekeeping interval |
| `listener.ifs_presence_poll_s` | `PRINT_BUDDY__LISTENER__IFS_PRESENCE_POLL_S` | `15` | IFS spool presence poll |
| `listener.spool_nag_s` | `PRINT_BUDDY__LISTENER__SPOOL_NAG_S` | `600` | Reminder interval for an unanswered spool question |
| `listener.start_claim_ttl_s` | `PRINT_BUDDY__LISTENER__START_CLAIM_TTL_S` | `1800` | How long a start button stays claimed |
| `vision.model` | `PRINT_BUDDY__VISION__MODEL` | `gemini-latest` | Model for camera checks |
| `vision.roast_model` | `PRINT_BUDDY__VISION__ROAST_MODEL` | `luna-latest` | Model for the print-start roast |
| `visual_watch.sensitivity` | `PRINT_BUDDY__VISUAL_WATCH__SENSITIVITY` | `3` | Camera watchdog sensitivity, 1 (quietest) to 5 (most alerts); also sets the periodic check interval and stall threshold, see the [project reference](PROJECT-REFERENCE.md#camera-stall-sensitivity) |
| `visual_watch.enabled` | `PRINT_BUDDY__VISUAL_WATCH__ENABLED` | `true` | Camera monitoring switch; the watchdog is alert-only and never controls the printer |
| `timelapse.archive_host` | `PRINT_BUDDY__TIMELAPSE__ARCHIVE_HOST` | empty | SSH host for the timelapse archive; empty disables it |
| `timelapse.archive_dir` | `PRINT_BUDDY__TIMELAPSE__ARCHIVE_DIR` | empty | Archive folder on that host |
| `timelapse.ssh_key` | `PRINT_BUDDY__TIMELAPSE__SSH_KEY` | empty | Path of the SSH key in the container (mount it read-only) |
| `timelapse.render_wait_s` | `PRINT_BUDDY__TIMELAPSE__RENDER_WAIT_S` | `900` | Wait for the printer to render the timelapse |
| `timelapse.render_poll_s` | `PRINT_BUDDY__TIMELAPSE__RENDER_POLL_S` | `10` | Poll interval while waiting |
| `timelapse.max_bytes` | `PRINT_BUDDY__TIMELAPSE__MAX_BYTES` | `209715200` | Largest timelapse kept |
| `timelapse.local_retention_days` | `PRINT_BUDDY__TIMELAPSE__LOCAL_RETENTION_DAYS` | `1` | Local copy retention |
| `timelapse.plan_clip_retention_days` | `PRINT_BUDDY__TIMELAPSE__PLAN_CLIP_RETENTION_DAYS` | `14` | Plan clip retention |
| `logging.level` | `PRINT_BUDDY__LOGGING__LEVEL` | `INFO` | `DEBUG`, `INFO`, `WARNING` or `ERROR` |
| `logging.format` | `PRINT_BUDDY__LOGGING__FORMAT` | `text` | `text` (one readable line per event) or `json` (one JSON object per line) |
| `logging.file` | `PRINT_BUDDY__LOGGING__FILE` | empty | Also write a rotating log file (5 MiB, 3 backups) at this path, for example under `/data/hermes/logs`; empty logs to stderr only |
| `maintenance.quick_hours` | `PRINT_BUDDY__MAINTENANCE__QUICK_HOURS` | `25` | Quick maintenance due after these print hours |
| `maintenance.full_hours` | `PRINT_BUDDY__MAINTENANCE__FULL_HOURS` | `75` | Full maintenance due after these print hours |
| `maintenance.full_days` | `PRINT_BUDDY__MAINTENANCE__FULL_DAYS` | `30` | Full maintenance due after these days |
| `nozzle_temp_offsets.<type>` | `PRINT_BUDDY__NOZZLE_TEMP_OFFSETS__<TYPE>` | `0.0` | Temperature offset, −10 to +15 °C, for `brass`, `hardened_steel`, `stainless_steel`, `ruby`, `tungsten_carbide`, `diamond` |
| `material_temps.<MATERIAL>` | file only | see `config/home.yaml` | `{nozzle, bed}` fallback temperatures for PLA, PETG, ABS, ASA, TPU, PA, PC; Spoolman filament values win |
| `cold_pull.<profile>` | file only | from the vendor profile *model* | Hot and cold cold-pull temperatures; profile names are yours |

## Compose variables

These configure the Compose deployment, not Print Buddy itself. Set them in `.env`.

| Variable | Default | What it does |
|---|---|---|
| `PRINT_BUDDY_IMAGE` | none, required | Image the combined container runs: a locally built tag or a tested immutable digest. No published image exists yet |
| `PRINT_BUDDY_DATA` | `./data` | Host folder holding the `hermes`, `archive`, `records` and `cache` volumes |
| `PRINT_BUDDY_SPOOLMAN_DATA` | `./data/spoolman` | Host folder for the bundled Spoolman database, config, logs and backups |
| `PRINT_BUDDY_CONFIG` | `/app/config/docker.yaml` | Settings file Print Buddy reads (YAML, or legacy `.toml`) |
| `TZ` | `UTC` | Container timezone (IANA name), used by the bundled Spoolman; Print Buddy itself uses `discord.timezone` |

## Runtime environment

| Variable | Default | What it does |
|---|---|---|
| `PRINT_BUDDY_ORCA_CHANNEL` | `stable` | Selects the bundled stable or alpha OrcaSlicer at startup; invalid values stop the container |
| `PRINT_BUDDY_BUNDLED_SPOOLMAN` | `true` | `true` runs the bundled Spoolman API; `false` uses `services.spoolman_url` only |
| `SPOOLMAN_HOST` | `127.0.0.1` | Bundled Spoolman bind address; widen it only together with an approved port mapping, see docs/DOCKER.md |

`WITH_ORCA=false` (default) omits both Orca channels; slicing fails closed without either binary.

## Build arguments

Image build inputs (`docker build --build-arg NAME=value`), not runtime settings. `PRINT_BUDDY_ORCA_CHANNEL` is a runtime environment variable (default `stable`, accepts `alpha`) and requires an image built with `WITH_ORCA=true`.

| Argument | Default | What it does |
|---|---|---|
| `APP_CHANNEL` | `development` | Application provenance label, `stable` or `development`; it does not change the source checkout |
| `WITH_ORCA` | `false` | `true` installs both pinned official prebuilt OrcaSlicer releases (stable 2.4.2 and alpha 2.5.0-alpha, each SHA256 verified in `docker/orca-prebuilt.sh`); OrcaSlicer is never built from source; `false` omits it and slicing fails closed |
| `SPOOLMAN_REF` | `bed3f14ee77a3a571d31b14215d54b0b88a7519c` | Pinned Spoolman v0.27.0 source commit; only its Python API backend is installed, never the web UI |
| `ORCA_BUILD_JOBS` | `auto` | Bambu Studio source build only: parallel compile jobs, `auto` (memory-aware, below the CPU count) or a whole number from 1 to 64 |
| `ORCA_BUILD_MEM_PER_JOB_MIB` | `2560` | Bambu Studio source build only: memory budgeted per job by `auto`, at least 256 |
| `ORCA_CCACHE_MAXSIZE` | `5G` | Bambu Studio source build only: size cap of the compiler cache inside the build cache |
| `WITH_BAMBU` | `false` | `true` builds Bambu Studio from pinned source into `/usr/bin/bambu-studio`; `false` omits it and Bambu Lab slicing fails closed |
| `BAMBU_REF` | `926a7192574bcb9b3a732e1ec59a46d79cb45466` | Pinned Bambu Studio 2.8.2 source commit |
| `BAMBU_BUILD_PROFILE` | `release-static-fhs` | Names the Bambu Studio build options; rename it to reset the build trees |

## Secrets

Secrets come from the environment only (Docker: `.env`, never committed or shared), never from YAML or the image.

| Variable | Used for |
|---|---|
| `DISCORD_BOT_TOKEN` | Standalone bot token. The variable name is `discord.token_env` |
| `TELEGRAM_BOT_TOKEN` | Telegram standalone bot token. The variable name is `telegram.token_env` |
| `MATRIX_ACCESS_TOKEN` | Matrix standalone bot access token. The variable name is `matrix.access_token_env` |
| `LLM_API_KEY` | Optional key for `llm.chat_url`. The variable name is `llm.api_key_env` |
| `OCTOPRINT_API_KEY` | `octoprint.marlin` printers: OctoPrint application key |
| `BAMBU_LAN_PASSWORD` | Bambu Lab printers: the LAN access code for read-only status. The variable name is `bambu.lan_access_code_env` |
| `MOONRAKER_API_KEY` | Moonraker API key, only when Moonraker requires one |
| `CLIPROXY_API_KEY` | Key for `services.vision_chat_url` |
| `OPENVIKING_API_KEY` | Key for `services.openviking_url` |
| `AD5X_SERIAL` | Stock Flashforge printers: the printer serial number for the stock LAN API. Never a setting; set it together with `flashforge_stock.host` |
| `AD5X_CHECK_CODE` | Stock Flashforge printers: the printer check code for the stock LAN API. Never a setting |

The MakerWorld account token is not an environment variable. It is the file `~/.config/bambu/token` (Docker: `/data/.config/bambu/token`), needed only for MakerWorld links; see [MakerWorld account token](PROJECT-REFERENCE.md#makerworld-account-token).

## Legacy environment names

The names below predate `PRINT_BUDDY__SECTION__KEY` and still work; new installs should use the long form. The full map is `LEGACY_ENV` in `print_buddy/settings.py`, for example `MOONRAKER_URL` → `services.moonraker_url`, `SPOOLMAN_URL` → `services.spoolman_url`, `PRINT_DISCORD_CHAT_ID` → `discord.chat_id`.
