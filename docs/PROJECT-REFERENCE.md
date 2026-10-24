# Detailed project reference

[Documentation home](README.md) · [Project overview](../README.md)

This page preserves the detailed project reference. Start with the setup guides for installation; features and defaults vary by printer and configuration.

[![CI](https://github.com/catduckgnaf/print-buddy/actions/workflows/ci.yml/badge.svg)](https://github.com/catduckgnaf/print-buddy/actions/workflows/ci.yml)

[Contributing](../CONTRIBUTING.md) · [User documentation](../docs/README.md)

Print Buddy is an automated 3D printing pipeline driven from a Discord channel, for Sovol and other Klipper printers, and Flashforge printers running ZMod. Post a MakerWorld or Printables link, or a 3MF or STL file, and the bot downloads the model, merges multi plate parts, matches colors to loaded spools in Spoolman, and slices headless with OrcaSlicer. Once someone confirms the bed is clear, it sends the job to the printer through Moonraker, watches it through the camera, and posts the result.

## About

Print Buddy is almost entirely Python. Downloading, plate merging, color matching, slicing, job start, printer monitoring, spool sync, timelapses, and print records are all plain scripts with fixed rules, so they are fast and make no LLM calls.

The Discord side runs one of two ways. As a **standalone bot** (`python3 -m print_buddy.bot`), Print Buddy talks to Discord itself with a bot token: links, files, buttons, slash commands, slicing, print start and printer alerts need no Hermes and no LLM; an OpenAI-compatible model can optionally word replies to free text. See [Standalone bot](../docs/STANDALONE.md). Or the conversation runs on [Hermes Agent](https://github.com/NousResearch/hermes-agent) using Luna at medium reasoning, with the same handlers loaded as a Hermes plugin. Two optional extras also call a model: the camera watchdog asks a vision model to classify a print frame, and the "print started" post adds a one line roast. Leave `services.vision_chat_url` empty and neither runs.

Supported printers:
* **Sovol** on stock Klipper, where Print Buddy started, plus any other Klipper printer with Moonraker. Single extruder.
* **Flashforge (running ZMod)**, including IFS multi color slots and the ZMod plugin below.

Pick one with `[printer] model` (see Settings). The offline core in `print_buddy/core/` runs with no printer.

## How it works

In the `#3d-printer` channel, someone posts a link. The bot replies with a job summary and a button:

> **Mrs catduck:** https://makerworld.com/en/models/931302-holder-for-post-it-notes
>
> **Print Buddy:** Model: Post it Note Holder with Stencils. Filament: Matte Black PLA (~48g). Estimated print time: 1 hour 14 minutes. Target printer: Flashforge (ZMod).
>
> [Slice and print now (bed is clear)]
>
> **Print Buddy:** Bed heating started. Job sent to Moonraker. Finish time estimated for 4:22 PM ET. Don't touch the build plate.

The print starts only when someone clicks the button. On completion, the bot posts stats and writes the job to an Obsidian archive note.

A bed-clear approval starts one print. Starting the same G-code again needs a new confirmation (`print-now --reprint`). Only one start runs per printer at a time. If Print Buddy cannot tell whether a start reached the printer (it stopped during the request, or the printer never confirmed it), it refuses later starts on that printer until the printer reports that exact file. An operator can also check the printer and run `python3 -m print_buddy.core.model_job reconcile-launch --started` or `--not-started`; without a flag, the command only shows the pending start. Print Buddy never resends that start on its own. This launch state is kept in `<hermes_home>/state/print_buddy_launches.sqlite3`.

## Features

**Intake and slicing**
* MakerWorld and Printables links, or uploaded 3MF and STL files.
* Print time and grams from the model page in the first reply.
* Multi plate models merged onto one bed when they fit; plates that would run longer than `layout.max_merged_hours` stay separate.
* Oversized models split across plates, tracked as one plan.
* Copy counts offered only where the G-code will actually contain the copies.
* Each slice records a versioned recipe with its G-code: source file, printer model, orientation and scale, copies, supports and brim, split assignments, filament and nozzle, Orca profile files and slicer command. A slice made for one recipe is never reused for another.
* Headless OrcaSlicer with versioned machine, process, and filament templates; Bambu Studio as a fallback slicer with its network cut.
* Readable part names: generic CAD names like "Cylinder" become a shape, size, plate, and color description.

**Filament and spools**
* Colors matched to loaded IFS slots from Spoolman. PLA+ is accepted in a PLA slot.
* Filament estimates split across parts that share a spool, never multiplied.
* `/spool` lists, changes, creates, and syncs spools without moving the printer.
* A swapped spool can finish a job sliced for the old one, by explicit choice.
* `PRINT_BUDDY` Klipper macro confirms ZMod can fall back to a matching backup slot.

**Printing**
* Nothing starts without a bed clear button press.
* Multi plate plans: `/print-next` offers the exact next plate, and finishing a plate advances the plan.
* `/print-again` reprints an archived job exactly, with no re-slice. A job sliced before slice recipes were recorded is refused until it is re-sliced once.
* `/print-stop` and `/print-resume` each send at most one Moonraker command behind a confirm button.
* `/print-reboot` uses the safe ZMod reboot flow.
* Printer models: `flashforge.ad5x`, `sovol.sv06`, `klipper.generic`, `octoprint.marlin`, `bambulab.a1` (offline only, see [Bambu Lab printers](BAMBU-LAB.md)). Each job records the model it was sliced for and refuses another.

**Monitoring**
* Event driven Moonraker listener: start, pause, error, and completion posts with stats.
* Camera watchdog checks layer 2, layer 5, and periodic frames for empty bed, spaghetti, a detached part, or a stalled print. Stall sensitivity is adjustable from 1 to 5.
* Stop and pause alerts carry Resume and Cancel buttons.
* Klipper report posted when a print is cancelled.
* Timelapse per print, or one combined video for a multi plate plan, sized to fit Discord.

**Records and upkeep**
* Every finished job written to an Obsidian vault and the job archive.
* Nightly catch up against Moonraker job history so nothing is missed.
* `/print-maintenance` guided checklist and reminders by print hours and days.
* `/print-coldpull` runs an operator confirmed cold pull at configured temperatures.

## Settings

`config/home.yaml` holds the shared defaults and picks the printer. Printer-specific defaults (bed, height, nozzle, slots, margins, prime tower and macros) come from the selected model and can be overridden in YAML. Put machine-specific overrides in the gitignored `config/local.yaml`; YAML is preferred for new configuration. Existing `config/home.toml` and `config/local.toml` remain supported during migration. When both formats are present, YAML wins; TOML is used when the corresponding YAML file is absent. `PRINT_BUDDY_CONFIG=/path/to/settings.yaml` selects one YAML file, and `.toml` paths remain supported. Environment variables (`PRINT_BUDDY__SECTION__KEY` and the existing legacy names) override file settings.

Settings merge in this order: printer-model defaults, home configuration, the selected local or explicit configuration, then environment. A typo or invalid value stops startup and identifies the setting. Python 3.11 or newer is required. Changes require restarting the relevant process; settings are loaded once at process startup. See [YAML configuration and TOML migration](../docs/CONFIGURATION-YAML.md) for schema, precedence, safety limits and migration guidance.

```yaml
# config/local.yaml example
services:
  moonraker_url: "http://192.168.x.x:7125"
  spoolman_url: "http://192.168.x.x:7912"
visual_watch:
  enabled: false
  sensitivity: 2
```

### Printer models

One package per brand under `print_buddy/printers/`, one file per printer inside it: `flashforge/ad5x.py`, `sovol/sv06.py`, `klipper/generic.py`. A model file holds the printer's defaults, its adapter and the name of its Orca template folder (`print_buddy/templates/orca/<folder>`). Its user-adjustable keys are `printer.name`, `printer.firmware`, `printer.start_macro`, `printer.end_macro`, `printer.bed_mm`, `printer.max_print_height_mm`, `printer.nozzle_diameter_mm`, `printer.ifs_slots`, `layout.single_copy_edge_mm`, `layout.multi_copy_edge_mm`, `layout.merge_edge_margin_mm`, `layout.merge_spacing_mm`, `prime_tower.size_mm`, `prime_tower.part_clearance_mm`, `prime_tower.edge_mm`, `prime_tower.merge_slack_mm`, `cold_pull` profile names and values, and `orca.templates`. `config/home.yaml` picks one with `printer.model: flashforge.ad5x`; settings merge in this order, later wins: model defaults, home configuration, selected local or explicit configuration, environment.

Adding a printer is one new file in its brand package plus its templates; nothing in `core/` changes. The AD5X and ZMod code lives in `print_buddy/printers/flashforge/zmod/`, and `base.py` lists what core still shares with it.

Upgrading from `[printer] family`: replace it in `config/local.toml` with `model = "flashforge.ad5x"` (or `sovol.sv06`, `klipper.generic`). A leftover `family` stops the import and names the key.

### Camera stall sensitivity

`visual_watch.enabled` independently enables camera monitoring; it defaults to `true`, preserving the historical behavior for existing TOML users. `visual_watch.sensitivity` sets how readily the camera watchdog posts a "stalled layers" alert, from 1 (quietest) to 5 (most alerts). Default sensitivity is 3.

| Level | Frame counts as unchanged if diff below | Stall alert after | Periodic camera check | Vision "ok" cancels a stall |
|---|---|---|---|---|
| 1 | 1.5 | 8 layers | 30 min | yes |
| 2 | 2.0 | 5 layers | 20 min | yes |
| 3 | 3.0 | 3 layers | 15 min | no |
| 4 | 4.0 | 3 layers | 10 min | no |
| 5 | 5.0 | 2 layers | 7.5 min | no |

Empty bed, spaghetti, detached part, uncertain, and vision failure alerts fire at every level.

### All settings

| Section | Key | Default | What it does |
|---|---|---|---|
| `printer` | `name`, `firmware` | from the model | Shown in job summaries |
| | `nozzle_type` | from the model or unset | Optional nozzle material; use a type supported by the selected printer model |
| | `model` | `flashforge.ad5x` | Printer model: `flashforge.ad5x`, `sovol.sv06`, `klipper.generic`, `octoprint.marlin`, `bambulab.a1`. Sets every default below |
| | `start_macro`, `end_macro` | `PRINT_START`, `PRINT_END` | Start and end macros for stock Klipper models |
| | `bed_mm` | `[220, 220]` | Usable print area |
| | `max_print_height_mm` | `220` | Tallest allowed print |
| | `nozzle_diameter_mm` | `0.4` | Slicing refuses any other nozzle |
| | `ifs_slots` | `4` | Filament slots; 1 for a single extruder |
| `layout` | `single_copy_edge_mm`, `multi_copy_edge_mm` | `1`, `3` | Distance from the bed edge |
| | `merge_edge_margin_mm`, `merge_spacing_mm` | `8`, `10` | Gaps when merging plates |
| | `max_merged_hours` | `12` | Longer merged plates stay separate |
| `prime_tower` | `size_mm`, `part_clearance_mm`, `edge_mm`, `merge_slack_mm` | `45`, `8`, `10`, `5` | Prime tower size and clearances |
| `orca` | `templates` | the model's folder under `print_buddy/templates/orca` | Orca machine, process, and filament JSON |
| | `command` | Flathub OrcaSlicer | How to start the Orca CLI |
| `bambu` | `command` | Flathub Bambu Studio, no network | Fallback slicer |
| `paths` | `hermes_home` | `~/.hermes/profiles/3dprinting` | Hermes profile the bot runs in |
| | `archive`, `records_vault`, `cache` | `~/3d-prints`, `~/Documents/3D Printing Records`, `~/.hermes/cache` | Job archive, Obsidian records, cache |
| | `hermes_bin`, `hermes_agent`, `hermes_restart_helper`, `ffmpeg` | | Tool locations |
| `services` | `moonraker_url`, `moonraker_ws_url`, `camera_snapshot_url` | empty | Printer and camera; websocket is derived when empty |
| | `spoolman_url`, `openviking_url` | empty | Spoolman and optional memory store |
| | `vision_chat_url` | empty | OpenAI compatible endpoint for vision checks and roasts; empty disables both |
| | `stock_api_port` | `8898` | Flashforge stock API for IFS presence |
| `discord` | `chat_id`, `approval_user_id` | empty | Channel and approver IDs |
| | `upload_max_bytes` | `9500000` | Largest upload before video is shrunk |
| | `post_print_quiet_s` | `7200` | Quiet time before a chat session resets |
| | `daily_reset_hour`, `timezone` | `4`, `America/New_York` | Daily session reset |
| | `bot_name` | `Print Buddy` | Name the bot uses in the roast persona and the rendered profile templates (`SOUL.md`, locale strings). Set it to anything, for example your old name |
| | `question_timeout_s` | `1800` | How long a Discord question waits for a button press |
| | `safety_confirm_timeout_s` | `600` | How long a safety confirmation (stop, resume, reboot, cold-pull go-ahead) waits |
| | `upload_wait_s` | `3600` | How long an upload-wait freeze stays active |
| | `cold_pull_outcome_s` | `1800` | How long a cold-pull outcome prompt stays open |
| | `token_env` | `DISCORD_BOT_TOKEN` | Standalone bot: environment variable holding the bot token |
| | `allowed_user_ids` | empty | Standalone bot: Discord user IDs allowed to use it, comma or space separated |
| `bot` | `mode` | `hermes` | `standalone` runs `python3 -m print_buddy.bot` with no Hermes; see [Standalone bot](../docs/STANDALONE.md) |
| | `health_port`, `health_stale_s` | `0`, `120` | Standalone bot: `GET /healthz` port (0 off) and heartbeat age limit |
| | `sync_commands` | `true` | Standalone bot: register slash commands at startup |
| `llm` | `chat_url`, `model`, `api_key_env`, `timeout_s` | empty, empty, `LLM_API_KEY`, `20` | Optional OpenAI-compatible model for standalone free-text replies; never starts anything itself |
| `listener` | `tick_s` | `600` | Listener housekeeping interval |
| | `ifs_presence_poll_s` | `15` | IFS spool presence poll |
| | `spool_nag_s` | `600` | Reminder interval for an unanswered spool question |
| | `start_claim_ttl_s` | `1800` | How long a start button stays claimed |
| `vision` | `model`, `roast_model` | `gemini-latest`, `luna-latest` | Models for camera checks and roasts |
| `visual_watch` | `enabled`, `sensitivity` | `true`, `3` | Camera monitoring switch; sensitivity from 1 to 5 |
| `timelapse` | `archive_host`, `archive_dir`, `ssh_key` | empty | Remote timelapse copy; empty host disables it |
| | `render_wait_s`, `render_poll_s` | `900`, `10` | Wait for the printer to render the timelapse |
| | `max_bytes`, `local_retention_days`, `plan_clip_retention_days` | 200 MiB, `1`, `14` | Size cap and retention |
| `logging` | `level`, `format`, `file` | `INFO`, `text`, empty | Log verbosity (`DEBUG`, `INFO`, `WARNING`, `ERROR`), `text` or `json` lines, and an optional rotating log file |
| `maintenance` | `quick_hours`, `full_hours`, `full_days` | `25`, `75`, `30` | When maintenance reminders come due |
| `nozzle_temp_offsets` | supported nozzle type | `0.0` | Optional temperature compensation from −10 to +15 °C for a known nozzle type |
| `material_temps` | PLA, PETG, ABS, ASA, TPU, PA, PC (`nozzle`, `bed`) | see `config/home.yaml` | Configurable fallback temperatures in °C; explicit Spoolman filament settings take precedence |
| `cold_pull` | profile name | `pla_trial = [220, 90]`, `petg = [245, 100]` | Hot and cold temperatures in °C; profile names are configurable |

## Install and update

See [docs/INSTALL.md](../docs/INSTALL.md) to install into a Hermes profile, or [docs/STANDALONE.md](../docs/STANDALONE.md) and `scripts/install-standalone.sh` to run the standalone bot with no Hermes.

Update only from a commit that is green in CI. On the machine running Print Buddy:
```bash
scripts/home-update.sh          # fast-forward to origin/main
scripts/home-update.sh <tag>    # or a release tag
```
The script refuses to run if tracked files were edited by hand. It runs the offline tests and the doctor after the update, and resets to the previous commit if either fails. `config/local.yaml` and legacy `config/local.toml` are never touched. A listener restart command can go in `config/after-update.sh` (gitignored), which runs after a good update.

Check the setup any time with `python3 -m print_buddy.doctor`. It also checks Moonraker and Spoolman when they are set in `[services]`, and warns about Print Buddy state files stranded outside the profile `state` folder. The update and install scripts remove those strays when the profile holds the real copy (or the stray is empty); files in other profiles are only reported; `python3 -m print_buddy.state_files [--dry-run] [--remove]` does the same by hand.

## Run with Docker

Prerequisites: Docker Engine with the Docker Compose plugin, and Git. From the repository root, copy `.env.example` to `.env` and set `PRINT_BUDDY__SERVICES__MOONRAKER_URL` to your printer's Moonraker URL. Set other scalar settings with `PRINT_BUDDY__SECTION__KEY` variables in `.env`; `config/docker.yaml` supplies Docker defaults. Lists such as `orca.command` belong in a config file, not environment variables. To replace the full config, mount YAML and set `PRINT_BUDDY_CONFIG` to its container path. Existing TOML mounts remain compatible by mounting at `/app/config/docker.toml` and setting `PRINT_BUDDY_CONFIG=/app/config/docker.toml` in `.env`.

Compose stores persistent data in `./data/hermes`, `./data/archive`, `./data/records`, and `./data/cache`; Spoolman uses `./data/spoolman`. Create these directories before starting. Spoolman is published on host port `7912`; Print Buddy has no published host port and connects to Moonraker and Spoolman over the Compose network.

```bash
cp .env.example .env
mkdir -p data/hermes data/archive data/records data/cache data/spoolman
docker compose up -d
docker compose logs -f print-buddy
```

To update a checkout, pull the desired source revision and rebuild/recreate the services with `docker compose up -d --build`. Stop the stack with `docker compose down`; the bind-mounted data directories remain. See [Docker details](../docs/DOCKER.md) for the full configuration notes.

## Running tests

From the repository root, CI runs this on every push and pull request:
```bash
python3 -m pip install -r requirements-test.txt
python3 -m pytest tests
```
A second CI job installs the Flathub OrcaSlicer, slices small test models with the templates, and runs the slice guards on the real G-code. Run it locally with `PRINT_BUDDY_REAL_SLICER=1 python3 -m pytest -s tests/slicer`.

### Discord flow corpus

`tests/corpus/cases/` holds generated, redistributable 3MF projects and STL meshes run end to end through the Discord intake with fake button clicks. Nothing contacts a printer. Each case runs twice: through the Hermes plugin hook, and through the standalone bot on a fake discord.py client (`tests/corpus/fake_discord.py`), where real buttons resolve the core prompt registry. Every PR runs the corpus with fake Orca; a separate real-slicer CI job runs the same cases with Flathub Orca.

| Coverage | Cases | Behavior asserted |
| --- | --- | --- |
| Native STL intake and color selection | `stl-single-part` | Source kind and 30 × 30 × 20 mm bounds, one G-code, support off |
| Fit and operator-selected shrink | `must-shrink`, `stl-must-shrink` | 3MF and STL trigger fit prompt, scale range, one G-code |
| STL overhang and tall narrow geometry | `stl-needs-supports`, `stl-thin-footprint` | Source bounds, default support-off behavior, successful guarded slice |
| Named parts and single-plate multicolor | `parts-one-plate` | Exact part names/colors, merge mode, three tool slots, one G-code |
| Multiplate merge and split planning | `plates-multicolor`, `split-plan` | Source plate count, merged colors/tool slots, split mode and expected G-code counts |
| Supports-on geometry and baseline 3MF | `needs-supports`, `single-part` | Support-enabled profile plus support G-code, ordinary single-part intake |

All ten current models are generated locally by `tests/corpus/make_models.py`, have CC0 provenance in their manifests, and are under 5 MB. A future hand-authored regression fixture is also allowed when its redistribution license is documented and the public size limit is met; generator checks pin the built-in fixtures without requiring every public case to be generated. The corpus contains no private or license-restricted source files. Current scenario gaps include painted multicolor meshes, partial-plate selection, non-English part-name translation, rotation-only fit, archived-job reprints, and a near-5 MB timeout case. Generated STL covers shrink-to-fit but not split planning; split planning remains covered by 3MF.

To add a model that broke Discord: make `tests/corpus/cases/<name>/` with one supported model file (`.3mf` or `.stl`; `model.3mf` wins if a folder has more than one) and a `case.toml` listing the buttons pressed and what you expect. Only include models you may redistribute: your own work, CC0, CC BY, CC BY-SA, MIT, Apache (unversioned, 1.0, 1.1, or 2.0), or public domain. The license field must be an exact recognized label. Its only accepted suffix is `: generated by <path>, no third party geometry`, for generated fixtures. Other qualifiers, restrictions, and ambiguous labels are rejected. Record the exact provenance in `license = "..."`. Public models must be 5 MB or smaller. An unanswered question fails the test and prints its buttons, so copy them in. `tests/corpus/make_models.py` writes the built-in models; see any existing `case.toml` for the format.

```bash
python3 -m pytest -q tests/corpus                                # fake Orca
PRINT_BUDDY_REAL_SLICER=1 python3 -m pytest -q -s tests/corpus   # real Orca, slices under .slicer-work/
```

#### Local private corpus

Personal or non-redistributable models must stay outside the checkout. Create one folder per case under a private directory, with the model and a `case.toml` copied from a public example. The private corpus is selected only when explicitly configured:

```bash
export PRINT_BUDDY_PRIVATE_CORPUS="$HOME/.local/share/print-buddy/private-cases"
python3 -m pytest -q tests/corpus
unset PRINT_BUDDY_PRIVATE_CORPUS
```

Private cases use the same offline flow and skip only the public redistribution-license check. The configured directory must exist outside the repository and contain at least one case. Private-corpus opt-in fails when `CI=true`; never set it in CI. The private model is not added to the checkout, public diff, or CI artifacts. Real-slicer output is temporary local scratch under the ignored `.slicer-work/` directory; do not commit or upload it.

## ZMod plugin and spool matching

ZMod switches to a backup spool mid-print only when another slot holds the exact same color and material. The `print_buddy.cfg` macro checks that before a job starts:
```gcode
PRINT_BUDDY SLOT=1
```
If slot 1 and slot 3 are both black PETG, it confirms:
```
PRINT_BUDDY: black / PETG also on slot 3. ZMod can switch. This macro does not.
```
`PRINT_BUDDY COLOR=black MATERIAL=PETG` checks all four slots without assuming an active toolhead.

To install, run this on the printer console:
```gcode
ENABLE_PLUGIN name=print-buddy
```
Then add your slot assignments to `mod_data/user.cfg` in a `[gcode_macro _PRINT_BUDDY_VARS]` block and run `FIRMWARE_RESTART`. Moonraker update manager entry, in `mod_data/user.moonraker.conf`:
```ini
[update_manager print-buddy]
type: git_repo
path: /root/printer_data/config/mod_data/plugins/print-buddy
origin: https://github.com/catduckgnaf/print-buddy.git
primary_branch: main
managed_services: klipper
channel: dev
is_system_service: False
```

## Repository structure

* `config/home.yaml`: default settings. Legacy TOML remains supported during migration. See Settings.
* `print_buddy/settings.py`: loads and validates settings.
* `print_buddy/doctor.py`: checks that a machine can run Print Buddy.
* `print_buddy/core/`: offline 3MF and STL pipeline. No printer connection.
* `print_buddy/hermes/`: Moonraker listener, Discord posts, spool sync, print control, and records.
* `print_buddy/printers/`: one package per brand, one file per printer model; see `base.py` for what each one owns.
* `print_buddy/templates/orca/<model folder>/`: Orca machine, process, and filament JSON per printer.
* `plugins/print-feedback/`: the Discord command handlers and interaction guards, loaded by the Hermes gateway or by the standalone bot.
* `print_buddy/bot/`: standalone Discord bot (discord.py adapter, router, health check, optional LLM); `systemd/` holds its units.
* `hermes/`: profile templates and skills installed by `scripts/install-hermes.sh`.
* `scripts/`: `install-hermes.sh`, `install-standalone.sh`, `home-update.sh`, and `migrate-names.sh` (one-shot, see `docs/RENAME-MIGRATION.md`).
* `print_buddy.cfg`, `install.sh`, `uninstall.sh`, `update.sh`: ZMod plugin macro and lifecycle scripts.
* `tests/`: offline suite, real slicer checks, the Discord flow corpus (`tests/corpus/`), and the Klipper macro tests.
* `docs/`: install guide and AD5X integration specs.

## Links

* ZMod AD5X docs: [AD5X.md at 0017033](https://github.com/ghzserg/zmod/blob/001703371c90e228defce41bb4230b7fafd37fea/docs/en/AD5X.md)
* ZMod firmware project: [ghzserg/zmod](https://github.com/ghzserg/zmod)
* ZMod plugin guide: [wiki.zmod.link/Plugin](https://wiki.zmod.link/Plugin/)
* OrcaSlicer CLI documentation: [CLI mode](https://github.com/OrcaSlicer/OrcaSlicer/wiki/cli_mode)
* Releases: [GitHub releases](https://github.com/catduckgnaf/print-buddy/releases)

## Spool / IFS source of truth

Spoolman is authoritative for spool identity, filament material/color, and location (`IFS Slot N` versus `Shelf`). `printer_hardware.json` caches verified slot-to-Spoolman-ID bindings; it is not authoritative when Spoolman is available. Physical feeder slots are 1-based (1–4); G-code tools are zero-based (T0–T3), and mappings explicitly translate between them. G-code/job snapshots are provenance, not permission to override newer Spoolman facts. Normalize screen colors to six-digit RGB; never infer missing color from another spool or slot. Correct missing/invalid Spoolman color before binding. Screen updates are immediate when idle or persisted pending while printing; deferred/failed updates require explicit sync after printing. Moonraker active spool is usage accounting only: single-slot launch repoints it; multi-slot launch leaves it unchanged, so usage can be approximate. Selected physical slot determines identity/readiness, not the global accounting pointer.
