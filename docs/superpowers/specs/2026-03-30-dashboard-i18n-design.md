# Dashboard Internationalization (i18n) Design

## Overview

Add internationalization support to the simulator dashboard so all
user-visible strings (labels, buttons, tabs, tutorial text) render in the
language appropriate to the deployment context. Standalone installations
use the host system locale; Home Assistant add-on installations use HA's
configured language.

Supported languages match the existing translation files: en, nl, de, fr,
es, pt-BR.

## Locale Resolution

A single locale is determined once at dashboard startup and held for the
lifetime of the process.

**Resolution order:**

1. **HA add-on mode** (`SUPERVISOR_TOKEN` present): GET
   `http://supervisor/core/api/config` with the supervisor token, read
   the `language` field (e.g. `"nl"`).
2. **Standalone mode**: `locale.getlocale()` -> parse the language code
   (e.g. `en_US.UTF-8` -> `"en"`).
3. **Fallback**: `"en"`.

The resolved locale is validated against available translation files. If
the locale has no matching YAML file, fall back to `"en"`.

The locale string is stored on `DashboardContext`, which already flows
into every route handler.

## Translator Class

A `Translator` class loads all YAML files from
`span_panel_simulator/translations/` at startup. Each file's `dashboard:`
section is flattened into dot-notation keys:

```yaml
dashboard:
  controls:
    grid_online: Grid Online
```

Becomes: `{"controls.grid_online": "Grid Online"}`

### Interface

- `t(key: str) -> str` -- look up key in active locale, fall back to
  `en` if missing, return the raw key string as last resort.
- `to_json() -> str` -- serialize the active locale's dashboard
  dictionary as JSON for the JavaScript bridge.

The translator is created once during `create_dashboard_app()` and
registered as a Jinja2 global.

## Translation File Structure

The existing `span_panel_simulator/translations/*.yaml` files are
extended with a `dashboard:` section alongside the existing
`configuration:` section:

```yaml
configuration:
  # ... existing HA add-on config strings unchanged ...

dashboard:
  title: SPAN Panel Simulator Dashboard
  theme:
    label: Theme
    system: System
    light: Light
    dark: Dark
  tabs:
    getting_started: Getting started
    clone: Clone
    model: Model
    purge: Purge
    export: Export
  getting_started:
    title: Getting started
    step_1: "Click a simulator configuration..."
    # ... full tutorial text
  controls:
    grid_online: Grid Online
    grid_offline: Grid Offline
    islandable: Islandable
    not_islandable: Not Islandable
    runtime: Runtime
    modeling: Modeling
    date: Date
    time_of_day: Time of Day
    speed: Speed
  chart:
    live_power_flows: Live Power Flows
    grid: Grid
    solar: Solar
    battery: Battery
  panel_config:
    serial: "Serial:"
    tabs: "Tabs:"
    main_breaker: "Main Breaker (A):"
    # ... remaining config labels
  sim_config:
    interval: "Interval (s)"
    noise: Noise
    save_reload: Save & Reload
    update: Update
  panels:
    title: Panels
    import: Import
    overwrite: Overwrite
    cancel: Cancel
```

All 6 language files get the same `dashboard:` key structure. English is
the source of truth; other languages are translated to match.

## Template Integration

### Server-rendered HTML (Jinja2)

Every hardcoded English string is replaced with a `{{ t('key') }}` call:

```html
<!-- before -->
<button>Grid Online</button>

<!-- after -->
<button>{{ t('controls.grid_online') }}</button>
```

### Inline JavaScript bridge

In `base.html`, the locale and full dictionary are injected once:

```html
<script>
  window.i18nLocale = "{{ locale }}";
  window.i18n = {{ t_json | safe }};
</script>
```

JS code references strings via `window.i18n['controls.grid_online']`.

### Date and number formatting

Hardcoded month arrays and manual number formatting are replaced with
`Intl` APIs using the locale:

```js
new Intl.DateTimeFormat(window.i18nLocale, { month: 'short' }).format(date)
```

## Error Handling

- `t(key)` never throws. Fallback chain: active locale -> `en` -> raw
  key string.
- Raw keys appearing in the UI make missing translations obvious during
  development without breaking rendering.

## Testing

- **Translator unit tests**: loading, key lookup, fallback chain,
  `to_json()` output.
- **Locale resolution unit tests**: mock `SUPERVISOR_TOKEN` for HA mode,
  mock `locale.getlocale()` for standalone, verify fallback to `"en"` for
  unsupported locales.
- **Translation key parity test**: load all YAML files and assert every
  non-English file has the same set of `dashboard:` keys as `en.yaml`.
  Catches missing translations at CI time.

## Dependencies

No new dependencies. PyYAML is already in the project; `json` and
`locale` are stdlib.
