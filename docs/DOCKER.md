# Docker

## Combined runtime for 2.1.2

The 2.1.2 candidate replaces separate bot, listener and Spoolman services with ONE runtime container. Published 2.1.1 remains unchanged. Pin the immutable digest from a successful publication run; candidate source is not a published image.

Tini owns orphan reaping and process-group signals. The supervisor waits for the bundled API before starting the Moonraker listener and Discord bot. Any required child exit ends the container nonzero, including an unexpected clean exit. SIGTERM stops children with a bounded grace period. Docker restart reacts to process exit, not healthcheck failures alone.

An empty `DISCORD_BOT_TOKEN` disables only the Discord consumer for staging. Never run duplicate consumers/listeners against the same state. HEALTHCHECK verifies listener heartbeat, enabled bot readiness and Spoolman `/api/v1/health`. An offline printer does not make a reconnecting listener unhealthy. Dummy credentials do not prove Discord login.

## Included Spoolman

Spoolman 0.27.0 is built from exact upstream revision `bed3f14ee77a3a571d31b14215d54b0b88a7519c` with its committed uv.lock, isolated from Print Buddy dependencies. No third-party source is patched. No Node, npm or web UI assets are built or copied. A local static placeholder satisfies upstream's static-directory check. Print Buddy spool commands and slicing lookups are the interface; `/api/v1` remains available.

Default bind is `127.0.0.1:8000` inside the container. Print Buddy defaults to `http://127.0.0.1:8000`. Compose publishes no port. Optional LAN API access requires widening `SPOOLMAN_HOST` to all interfaces plus an approved port mapping in a local override, for example host 7912 to container 8000. Never expose this unauthenticated API publicly. Neither Compose nor the DockerMan template ships this mapping. Network changes require review.

Persist `/data/spoolman` separately, including database, config, logs and backups. Upstream Alembic migrations remain intact for existing 0.26.1/0.27.0 databases. Runtime migration acceptance is required before deployment; retaining migration files alone is not evidence of a successful migration. Preserve source data and reconcile inventory at final cutover. No automatic downgrade is provided.

For an existing external instance set `PRINT_BUDDY__SERVICES__SPOOLMAN_URL=http://spoolman.example:8000`. Its data is not migrated or reset; the local database remains separate. Optionally set `PRINT_BUDDY_BUNDLED_SPOOLMAN=false` to skip the local API, requiring a nonlocal URL. Healthcheck then checks the external API. Changing a URL never moves or deletes a database.

## Camera watch (visual_watch)

While a print runs, the listener checks the printer camera at layer 2, layer 5 and then periodically. A vision model classifies each frame; spaghetti, a detached part, an empty bed, a stalled print, an uncertain frame or a failed check posts the camera photo to Discord with **Pause** and **It's fine** buttons. The watchdog is alert-only and never controls the printer: pausing, resuming or cancelling is always an operator tap (the paused post offers Resume and Cancel). A camera classification is not a guarantee of safe unattended printing.

It needs a Moonraker URL, a camera snapshot (`PRINT_BUDDY__SERVICES__CAMERA_SNAPSHOT_URL`; derived from the Moonraker host when empty) and a vision endpoint (`PRINT_BUDDY__SERVICES__VISION_CHAT_URL` plus the `CLIPROXY_API_KEY` secret in `.env`; an empty vision URL disables camera checks and roasts).

Two settings tune it, settable in the mounted YAML (`visual_watch` in `config/docker.yaml`), in TOML (`[visual_watch]` in `config/docker.toml`), or as Compose/`.env` variables — Compose and the Unraid template both carry them:

| YAML key | Environment variable | Default | What it does |
|---|---|---|---|
| `visual_watch.enabled` | `PRINT_BUDDY__VISUAL_WATCH__ENABLED` | `true` | Camera monitoring switch |
| `visual_watch.sensitivity` | `PRINT_BUDDY__VISUAL_WATCH__SENSITIVITY` | `3` | How readily the watchdog alerts, 1 (quietest) to 5 (most alerts) |

Sensitivity also sets the periodic check interval (30, 20, 15, 10 or 7.5 minutes for levels 1 to 5) and how many unchanged layers count as a stall; level 3 is the original behavior. The full per-level table is in the [project reference](PROJECT-REFERENCE.md#camera-stall-sensitivity), and every setting is listed in [SETTINGS.md](SETTINGS.md). Restart the container after a change; settings are read once at start.

## First-run configuration

1. Copy `.env.example` to a private `.env`. Set `PRINT_BUDDY_IMAGE` to a tested digest. Configure Moonraker with `PRINT_BUDDY__SERVICES__MOONRAKER_URL`; do not use a real printer for offline validation.
2. Set `PRINT_BUDDY_DATA` to a persistent directory with `hermes`, `archive`, `records`, `cache` and `spoolman` subdirectories writable by UID/GID 1000:1000; `PRINT_BUDDY_SPOOLMAN_DATA` can point the Spoolman data elsewhere, such as an existing Spoolman data folder. They map separately to `/data/hermes`, `/data/archive`, `/data/records`, `/data/cache`, `/data/spoolman`.
3. Leave the token empty for staging. At separately authorized live cutover configure token, chat ID and allowed user IDs. Stop previous consumers before enabling the replacement. Live startup never authorizes heating, movement or printing.
4. Use `PRINT_BUDDY__SECTION__KEY` scalar overrides from [SETTINGS.md](SETTINGS.md), or mount YAML/TOML and set `PRINT_BUDDY_CONFIG`. The image sets `PRINT_BUDDY_CONFIG=/app/config/docker.yaml`; a mounted file you point it at **replaces** `docker.yaml` rather than layering on it, so copy `config/docker.yaml`, edit the copy and mount that. Lists stay file-only except `delivery.methods`: `PRINT_BUDDY__DELIVERY__METHODS=discord,bambu_handoff,printer` (comma- or space-separated, values from the vendor profile) enables the Bambu LAN print opt-in without a mounted file (#270). Never bake secrets into images or mount host source over `/app`.
5. One Compose service, `combined`, uses 2 CPUs, 2 GiB RAM, 256 PIDs and 256 MiB shared memory by default. Real slicing may require more RAM. Capabilities are dropped and logs rotate.

## Unraid DockerMan

Use `docker/unraid/print-buddy.xml`, replacing the prior three templates. One entry runs all required processes; leave PostArgs empty. The template specifies UID/GID 1000:1000 and five persistent paths under `/mnt/user/appdata/print-buddy`. Set Repository to the tested immutable digest before deployment. It publishes no port; add an approved API mapping only as described under Included Spoolman. Image HEALTHCHECK covers the combined runtime.

The runtime selector `PRINT_BUDDY_ORCA_CHANNEL` (Compose, `.env.example` and the template) defaults to `stable` and accepts `alpha`; any other value stops container startup.

The template and the Compose environment also carry the feature-enabling variables (#252) — the ones that turn a feature on, as opposed to tuning settings, which stay in a mounted YAML pointed to by `PRINT_BUDDY_CONFIG`:

- **Camera watch:** `PRINT_BUDDY__SERVICES__VISION_CHAT_URL`, `PRINT_BUDDY__SERVICES__CAMERA_SNAPSHOT_URL`, `PRINT_BUDDY__VISION__MODEL` and the `CLIPROXY_API_KEY` secret; see [Camera watch](#camera-watch-visual_watch).
- **Stock Flashforge LAN:** `PRINT_BUDDY__FLASHFORGE_STOCK__HOST` with the `AD5X_SERIAL` and `AD5X_CHECK_CODE` secrets.
- **Bambu Lab LAN status:** `PRINT_BUDDY__BAMBU__LAN_HOST`, `PRINT_BUDDY__BAMBU__LAN_SERIAL` and the `BAMBU_LAN_PASSWORD` secret. The template's printer model list includes `bambulab.a1` and `bambulab.p1s` (offline only).
- **Key-protected Moonraker:** the `MOONRAKER_API_KEY` secret.
- **Discord and LLM:** `PRINT_BUDDY__DISCORD__APPROVAL_USER_ID`, `PRINT_BUDDY__DISCORD__TIMEZONE`, `PRINT_BUDDY__LLM__CHAT_URL`, `PRINT_BUDDY__LLM__MODEL` and the `LLM_API_KEY` secret.

In Compose, the settings are environment entries with their documented defaults and the secrets reach the container through the `.env` file, like `DISCORD_BOT_TOKEN`. In the template, secrets are masked fields. Leaving every one of them at its default or empty changes nothing about a running install.

This source template does not prove a Community Applications listing or an exercised Unraid installation. The host owner verifies deployment and final network mapping.

## Supported platforms and variants (#192)

Published images come in two variants on two platforms. Every combination is a multi-arch manifest, so `docker pull` selects the right architecture automatically:

| Variant | Build toggles | Tags | Contents |
|---|---|---|---|
| full | `WITH_ORCA=true`, `WITH_BAMBU=false` | `ghcr.io/catduckgnaf/print-buddy:VERSION` and `:latest` | Discord bot, Moonraker listener, bundled Spoolman API, OpenSCAD and both pinned OrcaSlicer channels (stable and alpha) |
| light | `WITH_ORCA=false`, `WITH_BAMBU=false` | `ghcr.io/catduckgnaf/print-buddy:VERSION-light` and `:latest-light` | Discord bot, Moonraker listener, bundled Spoolman API and OpenSCAD — no slicers |

Platforms are `linux/amd64` and `linux/arm64` for both variants. There is no `linux/arm/v7` image: upstream OrcaSlicer ships no 32-bit ARM Linux build, and 32-bit hosts are out of scope.

Pull the full image (the default) or the light image:

```bash
docker pull ghcr.io/catduckgnaf/print-buddy:VERSION
docker pull ghcr.io/catduckgnaf/print-buddy:VERSION-light
```

Replace `VERSION` with a published release version. The manifest scheme starts with the first release published after this support landed; the earlier single-arch amd64 tags (2.1.1, 2.1.2 and 2.1.3 era) are unchanged and are never republished or overwritten. No image is published by a source change alone — publication is the separate maintainer dispatch described under Validation and publication, and no multi-arch image is claimed published or validated on hardware until that dispatch's evidence exists.

### What light omits, and what happens when a slicer is needed

Light contains no OrcaSlicer and no Bambu Studio. Everything that does not slice still runs: the Discord bot, the Moonraker listener, the bundled Spoolman API and OpenSCAD-based CAD splitting are identical to the full image. Slicing fails closed, exactly as a `WITH_ORCA=false` image already behaves: the configured slicer executable (`/usr/local/bin/orca-slicer-stable` or `-alpha`, selected by `PRINT_BUDDY_ORCA_CHANNEL`) is absent, so a slicing attempt raises `FileNotFoundError`, produces no G-code and sends nothing to a printer. The offline acceptance harness proves this behaviour for a no-Orca image (its slice probe requires the refusal and checks that no `.gcode` file was written). Choose full on any host that must slice; choose light for a small host that only monitors, manages spools or relays an externally sliced workflow.

### How the ARM images are built

Each architecture is built on a native runner — `ubuntu-24.04` for amd64 and `ubuntu-24.04-arm` for arm64, the same native ARM runner family CI already uses for the `light-install-arm64` job. No QEMU emulation is used for the slicer-containing image. OrcaSlicer is never source-built on either architecture: `docker/orca-prebuilt.sh` downloads the pinned official upstream AppImage for the target architecture (upstream spells arm64 as `aarch64` in the asset name) and verifies its per-architecture SHA256 before extraction. The build info recorded in the image (`/orca-<channel>-build-info`) carries the real target architecture.

## Build dimensions and slicers

All OS stages use Debian 13 slim. `APP_CHANNEL=stable|development` labels provenance, not a source switch. `WITH_ORCA=true` includes Orca; default false omits it. OrcaSlicer is never built from source: `WITH_ORCA=true` installs both pinned official prebuilt releases, stable 2.4.2 and alpha 2.5.0-alpha. `PRINT_BUDDY_ORCA_CHANNEL=stable|alpha` selects wrappers; absent executables and invalid selections fail closed. `/usr/local/bin/orca-slicer` remains the stable path. Existing entrypoint preserves Xvfb for headless slicing.

The official prebuilt archives are downloaded and extracted only inside approved hosted image builds, never installed on a Hermes host. Each channel's version and source revision (per channel, shared by both architectures) and each architecture's asset name and SHA256 are pinned by `docker/orca-prebuilt.sh`, and the SHA256 is verified before extraction; any architecture other than amd64 or arm64 fails closed in that script. `ORCA_BUILD_JOBS=auto` bounds CPU/memory usage for the optional Bambu Studio source build only; explicit values accept 1 through 64. See [SETTINGS.md](SETTINGS.md#build-arguments).

`WITH_BAMBU=false` is the publication default; enabling pinned source-built Bambu Studio needs its own runtime acceptance. OpenSCAD (`openscad`) is included for CAD splitting.

## Validation and publication

The manual `.github/workflows/docker-image-validation.yml` validates ONE architecture/variant combination per dispatch: choose `architecture` (`amd64` or `arm64`, built on the matching native runner) and `variant` (`full` or `light`). It builds without pushing, runs the headless slicer smoke for the full variant, and runs `tests/docker/image_acceptance.sh` against that leg's image.

The manual `.github/workflows/docker-publish.yml` covers the whole matrix in one dispatch. Its build job runs all four legs (amd64/arm64 × full/light) on native runners; each leg builds its platform image, runs the headless slicer smoke (full legs only) and the offline runtime acceptance for that arch/variant. Only with `publish=true`, and only after that leg's acceptance succeeded, does a leg log in and push — by digest only, never by tag. A final manifest job (publication dispatches only, after every leg succeeded) combines the pushed digests with `docker buildx imagetools create` into the four tags: `:VERSION` and `:latest` (full), `:VERSION-light` and `:latest-light` (light), and writes the manifest digests to the step summary and the uploaded evidence. With `publish=false` (candidate mode) every leg still builds and must pass acceptance, and nothing is pushed and no manifest is created. Candidate inputs for the next release are `version=<next MAJOR.MINOR.PATCH>`, `publish=false`. Preserve every existing tag (2.1.1, 2.1.2, 2.1.3 era tags included); never overwrite or republish an existing tag.

Each build leg has a 350 minute timeout. Buildx uses the Docker driver, not a persistent builder container. Runtime fixtures must remain isolated, use dummy credentials and clean up owned containers. Evidence is retained 30 days. No live Discord, printer, Home Assistant or household Spoolman endpoints are permitted. Native tests prove lifecycle logic, not image startup, migration or slicing. Report actual workflow evidence and immutable identities separately.

Upgrade: stop prior consumers, preserve source state and immutable digest, reconcile at final cutover and recreate with the same five paths. Never use `compose down -v`. Rollback requires a compatible schema and matching old image; no automatic database downgrade is supplied.

## Attribution

Standalone foundations originated in zaheria985/print-buddy commit fba5727184dd8f0070e59c8f1fc088751142a003 and merged through #132. Docker contribution #139 commit 3bb0984250c20eb336d11ba1da06633950b45182 (MIT) informed context filtering and listener heartbeat. Original notices remain. Spoolman is MIT; slicers retain upstream licenses. [DOCKER-117-PROGRESS.md](DOCKER-117-PROGRESS.md) contains historical evidence, not acceptance of this candidate.
