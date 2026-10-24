# Print Buddy YAML Configuration

`config/home.yaml` is the checked-in default config. Override it on one installation with the gitignored `config/local.yaml`, or select a single YAML or legacy TOML file with `PRINT_BUDDY_CONFIG=/path/to/settings.yaml`. Use YAML for new configurations. Existing `config/home.toml` and `config/local.toml` remain supported during migration: `home.yaml` wins when both default files exist, and `local.yaml` wins over `local.toml`. An explicitly selected `PRINT_BUDDY_CONFIG` file replaces automatic local-file selection; it does not layer both local files.

Settings are merged key by key in this order: vendor profile defaults, printer model defaults, home configuration, selected local configuration, then environment overrides (`PRINT_BUDDY__SECTION__KEY` or supported legacy environment names). Existing explicit runtime arguments and persisted job state continue to take precedence at their call sites. Settings are loaded once per process, so restart the relevant process after changing configuration. YAML does not support secrets: keep credentials in existing environment or secret files. Runtime state remains in the existing state files, never YAML.

The checked-in `config/home.yaml`, the README's [All settings table](../README.md#all-settings), and the selected printer model's defaults form the supported settings inventory. Model IDs are `<vendor>.<printer>`: `flashforge.ad5x`, `sovol.sv06`, `klipper.generic` and `octoprint.marlin`; `printer.vendor` plus the printer part (`vendor: sovol`, `model: sv06`) is also accepted. `printer.slicer` and `delivery.methods` must be options the vendor profile offers; see [Printer profiles](PRINTER-PROFILES.md). Optional `printer.nozzle_type` values are limited to the selected model's supported set: `brass`, `hardened_steel`, `stainless_steel`, `ruby`, `tungsten_carbide` or `diamond`. `nozzle_temp_offsets` accepts those known names with values from −10 to +15 °C. `visual_watch.sensitivity` is an integer from 1 to 5; `visual_watch.enabled` is a YAML boolean and defaults to `true` in `home.yaml`, preserving the behavior of existing TOML configurations, which had no toggle and always ran camera checks.

Defaults are documented representative targets, not universal material guarantees. The selected Spoolman filament's explicit temperatures take precedence; missing targets use the matching profile below. Unknown materials fail closed. Local overrides may replace nozzle and bed values for a recognized profile.

```yaml
material_temps:
  PLA: {nozzle: 210, bed: 60}
  PETG: {nozzle: 240, bed: 85}
  ABS: {nozzle: 245, bed: 100}
  ASA: {nozzle: 260, bed: 105}
  TPU: {nozzle: 230, bed: 50}
  PA: {nozzle: 260, bed: 90}
  PC: {nozzle: 275, bed: 110}
```

The targets use representative points within the documented manufacturer/profile ranges: PLA 185–235/50–60 °C, PETG 215–270/70–90 °C, ABS 230–255/95–110 °C, ASA 220–275/90–110 °C, TPU 220–260/40–85 °C, PA 240–285/70–115 °C, and PC 270–275/100–115 °C. Brand, formulation, printer capability, and enclosure affect the correct settings. Every resolved nozzle target still passes the active nozzle type's safety limit, and the existing printer start/slice validation remains authoritative.

Example:

```yaml
printer:
  model: flashforge.ad5x
visual_watch:
  enabled: true
  sensitivity: 3
```

During migration, convert a TOML document by preserving its section names and keys as YAML mappings, then set `PRINT_BUDDY_CONFIG` to that YAML file or use `config/local.yaml`. Compare the effective values before removing the old TOML file. Do not copy secrets into either file.
