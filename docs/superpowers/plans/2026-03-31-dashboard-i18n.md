# Dashboard Internationalization Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Internationalize the simulator dashboard so all user-visible strings render in the host system's language (standalone) or HA's configured language (add-on mode).

**Architecture:** A `Translator` class loads YAML translation files at startup and exposes a `t(key)` function registered as a Jinja2 global. Templates call `{{ t('key') }}` for server-rendered strings. Inline JS receives the full dictionary as `window.i18n` and uses `Intl` APIs for date/number formatting. Locale is resolved once at startup from the HA supervisor API or host `locale.getlocale()`.

**Tech Stack:** Python stdlib (`locale`, `json`), PyYAML (existing dep), aiohttp/Jinja2 (existing), JS `Intl` APIs.

**Spec:** `docs/superpowers/specs/2026-03-30-dashboard-i18n-design.md`

---

## File Structure

| Action | File | Responsibility |
|--------|------|----------------|
| Create | `src/span_panel_simulator/dashboard/translator.py` | Translator class: load YAMLs, flatten keys, `t()`, `to_json()`, locale resolution |
| Create | `tests/test_translator.py` | Unit tests for Translator, locale resolution, fallback, key parity |
| Modify | `src/span_panel_simulator/dashboard/context.py` | Add `locale: str` field to `DashboardContext` |
| Modify | `src/span_panel_simulator/dashboard/__init__.py` | Instantiate Translator, register Jinja2 globals |
| Modify | `src/span_panel_simulator/dashboard/keys.py` | Add `APP_KEY_TRANSLATOR` app key |
| Modify | `src/span_panel_simulator/app.py` | Resolve locale and pass to DashboardContext |
| Modify | `span_panel_simulator/translations/en.yaml` | Add `dashboard:` section with all UI strings |
| Modify | `span_panel_simulator/translations/nl.yaml` | Add `dashboard:` section (Dutch) |
| Modify | `span_panel_simulator/translations/de.yaml` | Add `dashboard:` section (German) |
| Modify | `span_panel_simulator/translations/fr.yaml` | Add `dashboard:` section (French) |
| Modify | `span_panel_simulator/translations/es.yaml` | Add `dashboard:` section (Spanish) |
| Modify | `span_panel_simulator/translations/pt-BR.yaml` | Add `dashboard:` section (Portuguese) |
| Modify | `src/span_panel_simulator/dashboard/templates/base.html` | Inject i18n JS bridge, translate theme strings |
| Modify | `src/span_panel_simulator/dashboard/templates/dashboard.html` | Translate getting-started text |
| Modify | `src/span_panel_simulator/dashboard/templates/partials/runtime_controls.html` | Translate labels, buttons, chart legends; replace month arrays with Intl |
| Modify | `src/span_panel_simulator/dashboard/templates/partials/panel_config.html` | Translate form labels and JS messages |
| Modify | `src/span_panel_simulator/dashboard/templates/partials/sim_config.html` | Translate form labels and buttons |
| Modify | `src/span_panel_simulator/dashboard/templates/partials/entity_list.html` | Translate headings, buttons, hints |
| Modify | `src/span_panel_simulator/dashboard/templates/partials/entity_row.html` | Translate badges, tooltips, buttons |
| Modify | `src/span_panel_simulator/dashboard/templates/partials/entity_edit.html` | Translate all form labels |
| Modify | `src/span_panel_simulator/dashboard/templates/partials/clone_panel.html` | Translate labels, hints, buttons |
| Modify | `src/span_panel_simulator/dashboard/templates/partials/clone_confirm.html` | Translate dialog text |
| Modify | `src/span_panel_simulator/dashboard/templates/partials/running_panels.html` | Translate headings, buttons |
| Modify | `src/span_panel_simulator/dashboard/templates/partials/panels_list_rows.html` | Translate badges, buttons, tooltips, JS messages |
| Modify | `src/span_panel_simulator/dashboard/templates/partials/profile_editor.html` | Translate labels, buttons |
| Modify | `src/span_panel_simulator/dashboard/templates/partials/pv_profile.html` | Translate labels, replace month arrays with Intl |
| Modify | `src/span_panel_simulator/dashboard/templates/partials/battery_profile_editor.html` | Translate mode labels, hints, buttons |
| Modify | `src/span_panel_simulator/dashboard/templates/partials/evse_schedule.html` | Translate labels, buttons |
| Modify | `src/span_panel_simulator/dashboard/templates/partials/panel_source.html` | Translate headings, status text |
| Modify | `src/span_panel_simulator/dashboard/templates/partials/modeling_view.html` | Translate all labels, dialogs, chart text, JS messages |

---

## Task 1: Translator Class — Core

**Files:**
- Create: `src/span_panel_simulator/dashboard/translator.py`
- Create: `tests/test_translator.py`

- [ ] **Step 1: Write test for YAML loading and key flattening**

```python
# tests/test_translator.py
"""Tests for the dashboard Translator."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from span_panel_simulator.dashboard.translator import Translator


@pytest.fixture()
def translations_dir(tmp_path: Path) -> Path:
    """Create a temporary translations directory with test YAML files."""
    en = {
        "configuration": {"tick_interval": {"name": "Tick interval"}},
        "dashboard": {
            "title": "Dashboard Title",
            "controls": {"grid_online": "Grid Online", "speed": "Speed"},
        },
    }
    nl = {
        "configuration": {"tick_interval": {"name": "Tick-interval"}},
        "dashboard": {
            "title": "Dashboard Titel",
            "controls": {"grid_online": "Grid Aan", "speed": "Snelheid"},
        },
    }
    (tmp_path / "en.yaml").write_text(yaml.dump(en, allow_unicode=True))
    (tmp_path / "nl.yaml").write_text(yaml.dump(nl, allow_unicode=True))
    return tmp_path


class TestTranslatorLoading:
    def test_loads_english(self, translations_dir: Path) -> None:
        t = Translator(translations_dir, "en")
        assert t("title") == "Dashboard Title"

    def test_loads_nested_key(self, translations_dir: Path) -> None:
        t = Translator(translations_dir, "en")
        assert t("controls.grid_online") == "Grid Online"

    def test_loads_requested_locale(self, translations_dir: Path) -> None:
        t = Translator(translations_dir, "nl")
        assert t("title") == "Dashboard Titel"
        assert t("controls.grid_online") == "Grid Aan"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_translator.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'span_panel_simulator.dashboard.translator'`

- [ ] **Step 3: Implement Translator class**

```python
# src/span_panel_simulator/dashboard/translator.py
"""Internationalization support for the dashboard.

Loads YAML translation files and provides a ``t(key)`` function for
looking up translated strings by dot-notation key.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import yaml


def _flatten(data: dict[str, Any], prefix: str = "") -> dict[str, str]:
    """Flatten a nested dictionary into dot-notation keys."""
    result: dict[str, str] = {}
    for key, value in data.items():
        full_key = f"{prefix}{key}" if prefix else key
        if isinstance(value, dict):
            result.update(_flatten(value, f"{full_key}."))
        else:
            result[full_key] = str(value)
    return result


class Translator:
    """Provides translated strings for the dashboard UI.

    Loads all ``*.yaml`` files from the translations directory at init.
    Each file's ``dashboard:`` section is flattened into dot-notation keys.
    """

    def __init__(self, translations_dir: Path, locale: str) -> None:
        self._locale = locale
        self._strings: dict[str, dict[str, str]] = {}  # locale -> flat dict

        for path in translations_dir.glob("*.yaml"):
            lang = path.stem  # e.g. "en", "nl", "pt-BR"
            raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
            dashboard = raw.get("dashboard", {})
            if dashboard:
                self._strings[lang] = _flatten(dashboard)

    @property
    def locale(self) -> str:
        """The active locale code."""
        return self._locale

    def __call__(self, key: str) -> str:
        """Look up a translated string.

        Fallback chain: active locale -> ``en`` -> raw key.
        """
        active = self._strings.get(self._locale, {})
        value = active.get(key)
        if value is not None:
            return value
        # Fall back to English.
        en = self._strings.get("en", {})
        value = en.get(key)
        if value is not None:
            return value
        # Last resort: return the key itself.
        return key

    def to_json(self) -> str:
        """Serialize the active locale's dashboard strings as JSON.

        Falls back to English for any keys missing in the active locale.
        """
        en = self._strings.get("en", {})
        active = self._strings.get(self._locale, {})
        merged = {**en, **active}
        return json.dumps(merged, ensure_ascii=False)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_translator.py::TestTranslatorLoading -v`
Expected: 3 PASSED

- [ ] **Step 5: Commit**

```bash
git add src/span_panel_simulator/dashboard/translator.py tests/test_translator.py
git commit -m "Add Translator class with YAML loading and dot-key lookup"
```

---

## Task 2: Translator — Fallback and JSON Bridge

**Files:**
- Modify: `tests/test_translator.py`
- (No new source changes — testing existing behavior)

- [ ] **Step 1: Write tests for fallback chain and to_json**

Append to `tests/test_translator.py`:

```python
class TestTranslatorFallback:
    def test_falls_back_to_english_for_missing_key(self, translations_dir: Path) -> None:
        # Add a partial locale missing some keys
        partial = {
            "dashboard": {"title": "Titulo"},
        }
        (translations_dir / "es.yaml").write_text(yaml.dump(partial, allow_unicode=True))
        t = Translator(translations_dir, "es")
        assert t("title") == "Titulo"
        assert t("controls.grid_online") == "Grid Online"  # falls back to en

    def test_returns_raw_key_when_missing_everywhere(self, translations_dir: Path) -> None:
        t = Translator(translations_dir, "en")
        assert t("nonexistent.key") == "nonexistent.key"

    def test_unsupported_locale_falls_back_to_english(self, translations_dir: Path) -> None:
        t = Translator(translations_dir, "ja")
        assert t("title") == "Dashboard Title"

    def test_empty_translations_dir(self, tmp_path: Path) -> None:
        t = Translator(tmp_path, "en")
        assert t("anything") == "anything"


class TestTranslatorJson:
    def test_to_json_contains_all_keys(self, translations_dir: Path) -> None:
        t = Translator(translations_dir, "en")
        data = json.loads(t.to_json())
        assert data["title"] == "Dashboard Title"
        assert data["controls.grid_online"] == "Grid Online"
        assert data["controls.speed"] == "Speed"

    def test_to_json_merges_active_over_english(self, translations_dir: Path) -> None:
        t = Translator(translations_dir, "nl")
        data = json.loads(t.to_json())
        assert data["title"] == "Dashboard Titel"
        assert data["controls.grid_online"] == "Grid Aan"

    def test_to_json_includes_english_fallbacks(self, translations_dir: Path) -> None:
        partial = {"dashboard": {"title": "Titre"}}
        (translations_dir / "fr.yaml").write_text(yaml.dump(partial, allow_unicode=True))
        t = Translator(translations_dir, "fr")
        data = json.loads(t.to_json())
        assert data["title"] == "Titre"
        assert data["controls.grid_online"] == "Grid Online"  # en fallback
```

Add `import json` to the top of the test file.

- [ ] **Step 2: Run tests to verify they pass**

Run: `python -m pytest tests/test_translator.py -v`
Expected: All 10 tests PASS (implementation already handles these cases)

- [ ] **Step 3: Commit**

```bash
git add tests/test_translator.py
git commit -m "Add fallback and JSON bridge tests for Translator"
```

---

## Task 3: Locale Resolution

**Files:**
- Modify: `src/span_panel_simulator/dashboard/translator.py`
- Modify: `tests/test_translator.py`

- [ ] **Step 1: Write tests for locale resolution**

Append to `tests/test_translator.py`:

```python
from unittest.mock import AsyncMock, patch

from span_panel_simulator.dashboard.translator import resolve_locale


class TestResolveLocale:
    async def test_supervisor_mode_fetches_language(self) -> None:
        mock_resp = AsyncMock()
        mock_resp.status = 200
        mock_resp.json = AsyncMock(return_value={"language": "nl"})
        mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
        mock_resp.__aexit__ = AsyncMock(return_value=False)

        mock_session = AsyncMock()
        mock_session.get = AsyncMock(return_value=mock_resp)
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=False)

        available = {"en", "nl", "de"}
        with patch.dict("os.environ", {"SUPERVISOR_TOKEN": "test-token"}):
            with patch("aiohttp.ClientSession", return_value=mock_session):
                result = await resolve_locale(available)
        assert result == "nl"

    async def test_supervisor_unsupported_language_falls_back(self) -> None:
        mock_resp = AsyncMock()
        mock_resp.status = 200
        mock_resp.json = AsyncMock(return_value={"language": "ja"})
        mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
        mock_resp.__aexit__ = AsyncMock(return_value=False)

        mock_session = AsyncMock()
        mock_session.get = AsyncMock(return_value=mock_resp)
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=False)

        available = {"en", "nl"}
        with patch.dict("os.environ", {"SUPERVISOR_TOKEN": "test-token"}):
            with patch("aiohttp.ClientSession", return_value=mock_session):
                result = await resolve_locale(available)
        assert result == "en"

    async def test_standalone_uses_system_locale(self) -> None:
        available = {"en", "de", "fr"}
        with patch.dict("os.environ", {}, clear=True):
            with patch("locale.getlocale", return_value=("de_DE", "UTF-8")):
                result = await resolve_locale(available)
        assert result == "de"

    async def test_standalone_none_locale_falls_back(self) -> None:
        available = {"en", "nl"}
        with patch.dict("os.environ", {}, clear=True):
            with patch("locale.getlocale", return_value=(None, None)):
                result = await resolve_locale(available)
        assert result == "en"

    async def test_standalone_unsupported_locale_falls_back(self) -> None:
        available = {"en", "nl"}
        with patch.dict("os.environ", {}, clear=True):
            with patch("locale.getlocale", return_value=("ja_JP", "UTF-8")):
                result = await resolve_locale(available)
        assert result == "en"

    async def test_supervisor_api_error_falls_back_to_system(self) -> None:
        mock_resp = AsyncMock()
        mock_resp.status = 500
        mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
        mock_resp.__aexit__ = AsyncMock(return_value=False)

        mock_session = AsyncMock()
        mock_session.get = AsyncMock(return_value=mock_resp)
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=False)

        available = {"en", "fr"}
        with patch.dict("os.environ", {"SUPERVISOR_TOKEN": "test-token"}):
            with patch("aiohttp.ClientSession", return_value=mock_session):
                with patch("locale.getlocale", return_value=("fr_FR", "UTF-8")):
                    result = await resolve_locale(available)
        assert result == "fr"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_translator.py::TestResolveLocale -v`
Expected: FAIL — `ImportError: cannot import name 'resolve_locale'`

- [ ] **Step 3: Implement resolve_locale**

Add to `src/span_panel_simulator/dashboard/translator.py` (at the top, add imports; at the bottom, add the function):

```python
import locale as locale_mod
import logging
import os

import aiohttp

_LOGGER = logging.getLogger(__name__)

_SUPERVISOR_CONFIG_URL = "http://supervisor/core/api/config"


async def resolve_locale(available_locales: set[str]) -> str:
    """Determine the dashboard locale.

    Resolution order:
    1. HA Supervisor API language (add-on mode)
    2. Host system locale (standalone mode)
    3. Fallback to ``"en"``
    """
    lang = await _locale_from_supervisor()
    if lang and lang in available_locales:
        _LOGGER.info("Locale from HA Supervisor: %s", lang)
        return lang

    lang = _locale_from_system()
    if lang and lang in available_locales:
        _LOGGER.info("Locale from system: %s", lang)
        return lang

    _LOGGER.info("Locale fallback: en")
    return "en"


async def _locale_from_supervisor() -> str | None:
    """Fetch language from the HA Supervisor config API."""
    token = os.environ.get("SUPERVISOR_TOKEN")
    if not token:
        return None

    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(
                _SUPERVISOR_CONFIG_URL,
                headers={"Authorization": f"Bearer {token}"},
            ) as resp:
                if resp.status != 200:
                    _LOGGER.warning("Supervisor config API returned %s", resp.status)
                    return None
                data = await resp.json()
                return data.get("language")
    except Exception:
        _LOGGER.warning("Failed to fetch locale from Supervisor", exc_info=True)
        return None


def _locale_from_system() -> str | None:
    """Extract language code from the host system locale."""
    raw, _ = locale_mod.getlocale()
    if not raw:
        return None
    # "en_US" -> "en", "pt_BR" -> "pt-BR"
    parts = raw.split("_")
    if len(parts) >= 2:
        region = parts[1].split(".")[0]  # strip encoding like ".UTF-8"
        # Check for regional variants first (e.g. pt-BR)
        regional = f"{parts[0]}-{region}"
        return regional if regional != f"{parts[0]}-{parts[0].upper()}" else parts[0]
    return parts[0]
```

Note on `_locale_from_system`: for most locales like `en_US`, `de_DE`, `fr_FR`, the region is just the uppercased language — we return just the language code (`en`, `de`, `fr`). For `pt_BR`, the language and region differ, so we return `pt-BR` to match the translation filename.

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_translator.py::TestResolveLocale -v`
Expected: 6 PASSED

- [ ] **Step 5: Commit**

```bash
git add src/span_panel_simulator/dashboard/translator.py tests/test_translator.py
git commit -m "Add locale resolution from HA supervisor and system locale"
```

---

## Task 4: Translation Key Parity Test

**Files:**
- Modify: `tests/test_translator.py`

- [ ] **Step 1: Write key parity test against real translation files**

Append to `tests/test_translator.py`:

```python
class TestTranslationKeyParity:
    """Validate that all non-English YAML files have the same dashboard keys as en.yaml."""

    @staticmethod
    def _real_translations_dir() -> Path:
        """Path to the actual translations directory."""
        return Path(__file__).resolve().parent.parent / "span_panel_simulator" / "translations"

    def test_all_languages_have_same_dashboard_keys(self) -> None:
        translations_dir = self._real_translations_dir()
        en_path = translations_dir / "en.yaml"
        if not en_path.exists():
            pytest.skip("translations/en.yaml not found")

        en_raw = yaml.safe_load(en_path.read_text(encoding="utf-8")) or {}
        en_dashboard = en_raw.get("dashboard")
        if not en_dashboard:
            pytest.skip("No dashboard section in en.yaml yet")

        en_keys = set(_flatten(en_dashboard).keys())

        for path in sorted(translations_dir.glob("*.yaml")):
            if path.stem == "en":
                continue
            raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
            dashboard = raw.get("dashboard", {})
            lang_keys = set(_flatten(dashboard).keys())
            missing = en_keys - lang_keys
            extra = lang_keys - en_keys
            assert not missing, f"{path.name} missing keys: {missing}"
            assert not extra, f"{path.name} has extra keys: {extra}"
```

Add import at top of test file:

```python
from span_panel_simulator.dashboard.translator import _flatten
```

- [ ] **Step 2: Run test — it should skip for now (no dashboard section yet)**

Run: `python -m pytest tests/test_translator.py::TestTranslationKeyParity -v`
Expected: SKIPPED — "No dashboard section in en.yaml yet"

- [ ] **Step 3: Commit**

```bash
git add tests/test_translator.py
git commit -m "Add translation key parity test for CI validation"
```

---

## Task 5: Wire Translator into Dashboard App

**Files:**
- Modify: `src/span_panel_simulator/dashboard/keys.py`
- Modify: `src/span_panel_simulator/dashboard/context.py`
- Modify: `src/span_panel_simulator/dashboard/__init__.py`

- [ ] **Step 1: Add APP_KEY_TRANSLATOR to keys.py**

Add to `src/span_panel_simulator/dashboard/keys.py`:

```python
from span_panel_simulator.dashboard.translator import Translator

APP_KEY_TRANSLATOR = web.AppKey("translator", Translator)
```

- [ ] **Step 2: Add locale field to DashboardContext**

In `src/span_panel_simulator/dashboard/context.py`, add `locale` as the last field:

```python
    panel_browser: Any = None  # PanelBrowser | None — mDNS discovery for standalone mode
    locale: str = "en"
```

- [ ] **Step 3: Wire Translator into create_dashboard_app**

In `src/span_panel_simulator/dashboard/__init__.py`, add imports:

```python
from span_panel_simulator.dashboard.keys import (
    APP_KEY_DASHBOARD_CONTEXT,
    APP_KEY_PENDING_CLONES,
    APP_KEY_PRESET_REGISTRY,
    APP_KEY_RATE_CACHE,
    APP_KEY_STORE,
    APP_KEY_TRANSLATOR,
)
from span_panel_simulator.dashboard.translator import Translator
```

After line 54 (`APP_KEY_RATE_CACHE` assignment), add:

```python
    translations_dir = Path(__file__).resolve().parent.parent.parent.parent / (
        "span_panel_simulator" / "translations"
    )
    translator = Translator(translations_dir, context.locale)
    app[APP_KEY_TRANSLATOR] = translator
```

After line 61 (`env.globals["static_url"] = "static"`), add:

```python
    env.globals["t"] = translator
    env.globals["locale"] = translator.locale
    env.globals["t_json"] = translator.to_json()
```

- [ ] **Step 4: Verify the app still starts**

Run: `python -m pytest tests/ -x -q --timeout=30`
Expected: All existing tests pass (locale defaults to "en", translator loads but templates don't use it yet)

- [ ] **Step 5: Commit**

```bash
git add src/span_panel_simulator/dashboard/keys.py \
        src/span_panel_simulator/dashboard/context.py \
        src/span_panel_simulator/dashboard/__init__.py
git commit -m "Wire Translator into dashboard app with Jinja2 globals"
```

---

## Task 6: Resolve Locale at App Startup

**Files:**
- Modify: `src/span_panel_simulator/app.py` (the section that builds `DashboardContext`)

- [ ] **Step 1: Find where DashboardContext is constructed in app.py**

Search `app.py` for `DashboardContext(` — this is where locale resolution will be called.

- [ ] **Step 2: Add locale resolution call**

Add import at top of `app.py`:

```python
from span_panel_simulator.dashboard.translator import resolve_locale
```

Before the `DashboardContext(...)` construction, resolve the locale:

```python
        translations_dir = (
            Path(__file__).resolve().parent.parent / "span_panel_simulator" / "translations"
        )
        available_locales = {
            p.stem for p in translations_dir.glob("*.yaml")
        }
        locale = await resolve_locale(available_locales)
```

Then pass `locale=locale` to the `DashboardContext(...)` constructor.

- [ ] **Step 3: Fix translations_dir in __init__.py to use a consistent path**

The translations directory path needs to resolve correctly in both installed (wheel) and development modes. Instead of hard-coding the path in both `app.py` and `__init__.py`, have the `Translator` find its own translations directory.

Update `src/span_panel_simulator/dashboard/translator.py` — add a module-level constant:

```python
TRANSLATIONS_DIR = Path(__file__).resolve().parent.parent.parent.parent / (
    "span_panel_simulator" / "translations"
)
```

Then use `TRANSLATIONS_DIR` in both `app.py` (for `resolve_locale`) and `__init__.py` (for `Translator()`). Alternatively, pass the dir into both from `app.py` via `DashboardContext`.

The cleanest approach: add `translations_dir: Path` as a field on `DashboardContext` alongside `locale`, and use it in `__init__.py` when constructing the Translator. The `app.py` already knows the right path.

- [ ] **Step 4: Verify the app still starts with locale resolution**

Run: `python -m pytest tests/ -x -q --timeout=30`
Expected: All tests pass. In standalone mode without `SUPERVISOR_TOKEN`, locale resolves from system or falls back to "en".

- [ ] **Step 5: Commit**

```bash
git add src/span_panel_simulator/app.py \
        src/span_panel_simulator/dashboard/context.py \
        src/span_panel_simulator/dashboard/__init__.py \
        src/span_panel_simulator/dashboard/translator.py
git commit -m "Resolve locale at startup from HA supervisor or system locale"
```

---

## Task 7: English Translation File — Dashboard Strings

**Files:**
- Modify: `span_panel_simulator/translations/en.yaml`

- [ ] **Step 1: Add the complete dashboard section to en.yaml**

This is the source of truth. Every user-visible string from every template gets a key here. Append to `span_panel_simulator/translations/en.yaml` after the existing `configuration:` section:

```yaml
dashboard:
  title: SPAN Panel Simulator Dashboard

  theme:
    label: Theme
    system: System
    light: Light
    dark: Dark

  getting_started:
    title: Getting started
    step_click: >-
      Click a simulator configuration to view it. Templates are read-only.
      A running simulator appears as a discovered panel in the SpanPanel
      integration (default configs excluded).
    step_clone: >-
      Clone creates an editable copy from a template or from your real
      panel — cloning your panel preserves recorder history per circuit.
    step_model: >-
      Model opens the what-if view; add battery, PV, or circuits and
      compare before/after. Edits mark equipment as SYN; click the badge
      to revert to REC.
    step_purge: >-
      Purge removes recorder history written by the simulated panel's
      sensors if you added the simulated panel to Home Assistant's
      integration.

  tabs:
    getting_started: Getting started
    clone: Clone
    model: Model
    purge: Purge
    export: Export

  controls:
    title: Runtime Controls
    date: Date
    time_of_day: Time of Day
    speed: Speed
    grid_online: Grid Online
    grid_offline: Grid Offline
    islandable: Islandable
    not_islandable: Not Islandable
    runtime: Runtime
    modeling: Modeling
    soc: "SOC "
    circuits_shed: "{count} circuit(s) shed"

  chart:
    live_power_flows: Live Power Flows
    grid: Grid
    solar: Solar
    battery: Battery
    watts: Watts
    watts_suffix: " W"

  panel_config:
    title: Panel Config
    serial: "Serial:"
    tabs: "Tabs:"
    main_breaker: "Main Breaker (A):"
    soc_shed: "SOC Shed (%):"
    soc_shed_hint: Battery SOC below which SOC_THRESHOLD circuits are shed
    location: "Location:"
    search_placeholder: Search city or address...
    lat: "Lat:"
    lon: "Lon:"
    update: Update
    no_results: No results
    fetching_weather: Fetching historical weather data...
    deterministic_weather: Using deterministic weather model

  sim_config:
    title: Simulation Config
    export: Export
    save_reload: Save & Reload
    interval: "Interval (s):"
    noise: "Noise:"
    update: Update

  panels:
    title: Panels
    config: config
    configs: configs
    import_btn: Import
    overwrite: Overwrite
    cancel: Cancel
    already_exists: already exists.
    no_configs: No config files found. Import or clone a panel to get started.
    clone_hint: Clone a template above to create your own editable configuration.
    clone_as: "Clone as:"
    clone_failed: "Clone failed: "
    unsaved_warning: You have unsaved changes that will be lost. Switch anyway?
    switch_failed: "Failed to switch panel: "

  panel_row:
    bootstrap_port: Bootstrap HTTP port
    template: template
    template_hint: Clone this template to create an editable config
    viewing: viewing
    editing: editing
    clone: Clone
    clone_hint: Clone this config
    model: Model
    model_hint: Open modeling view
    restart: Restart
    restart_hint: Restart engine
    stop: Stop
    stop_hint: Stop engine
    start: Start
    start_hint: Start engine
    delete: Del
    delete_hint: Delete config file
    purge: Purge
    purge_hint: Remove HA recorder history for this profile

  clone_panel:
    title: Clone from Panel
    hint_ha: >-
      Clones circuit configuration from a SPAN panel registered with this
      Home Assistant instance, including usage profiles from the recorder.
    hint_standalone: Scrapes circuit configuration from a real SPAN panel via its eBus.
    panel_label: Panel
    scanning: "Scanning\u2026"
    select_panel: "\u2014 select a panel \u2014"
    no_panels: No panels found
    discovery_unavailable: Discovery unavailable
    panel_ip: Panel IP / hostname
    ip_placeholder: 192.168.1.100
    passphrase: Passphrase
    required: required
    clone: Clone

  clone_confirm:
    title: Clone from Panel
    exists_prefix: "Config file "
    exists_suffix: " already exists. Choose how to proceed:"
    overwrite: "Overwrite "
    save_as_new: "Save as new name:"
    continue_btn: Continue
    cancel: Cancel

  entities:
    title: "Entities ({count})"
    add_entity: "+ Add Entity"
    clone_hint: Clone a template to create an editable configuration.
    unmapped_tabs: "Unmapped Tabs ({count})"
    add_from_tabs: Add Circuit from Selected Tabs
    nothing_selected: "Nothing selected \u2014 all enabled"
    select_tabs: Select 1 or 2 tabs
    single_tab_hint: Add as 120V single-pole, or select a second tab for 240V
    valid_pair: Valid 240V double-pole pair
    invalid_pair: "Invalid pair: must be same parity, exactly 2 apart"

  entity_row:
    overlay_hint: Overlay on modeling charts
    toggle_relay: Toggle relay
    override_hint: "Overridden \u2014 click to resume replay"
    replay_hint: "Replaying recorded data \u2014 click for synthetic"
    syn: SYN
    rec: REC
    rec_lost_hint: "Recorder link lost \u2014 click to restore"
    rec_lost: "REC?"
    tabs_prefix: "tabs: "
    watts_suffix: W
    edit: Edit
    delete_confirm: "Delete "
    delete: Del

  entity_edit:
    editing: "Editing: "
    name: "Name:"
    tabs: "Tabs (comma-separated):"
    priority: "Priority:"
    relay_behavior: "Relay Behavior:"
    breaker: "Breaker (A):"
    breaker_placeholder: auto
    pv_section: PV System
    pv_nameplate: "Nameplate Rating (W):"
    pv_efficiency: "Efficiency:"
    pv_inverter_type: "Inverter Type:"
    pv_grid_tied: Grid-Tied
    pv_hybrid: Hybrid
    evse_section: EVSE Charger
    evse_charge_power: "Charge Power (W):"
    evse_max_power: "Max Power (W):"
    profile_section: Energy Profile
    typical_power: "Typical Power (W):"
    min_power: "Min Power (W):"
    max_power: "Max Power (W):"
    hvac_type: "HVAC Type:"
    hvac_none: None
    hvac_central: Central AC / Gas Furnace
    hvac_heat_pump: Heat Pump
    hvac_heat_pump_aux: Heat Pump + Aux Strips
    cycling_section: Cycling Pattern
    on_duration: "On Duration (s): "
    off_duration: "Off Duration (s): "
    smart_section: Smart Behavior
    responds_to_grid: "Responds to Grid: "
    max_power_reduction: "Max Power Reduction: "
    battery_section: Battery Behavior
    battery_nameplate: "Nameplate Capacity (kWh):"
    battery_reserve: "Backup Reserve (%):"
    battery_reserve_hint: "Normal discharge stops here; grid outages can draw deeper"
    battery_charge_power: "Charge Power (W):"
    battery_discharge_power: "Discharge Power (W):"
    save: Save
    cancel: Cancel

  profile_editor:
    title: 24-Hour Profile
    select_preset: "-- Select Preset --"
    from: "from "
    to: "to "
    apply: Apply
    active_days: Active Days
    save_profile: Save Profile

  pv_profile:
    title: Solar Production Profile
    weather_degradation: Monthly Weather Degradation
    no_weather: >-
      No historical weather data available. Set a location and weather
      data will be fetched automatically.
    peak: "Peak: "
    weather_label: " W | Weather: "
    lat_label: "% | Lat: "
    lon_label: ", Lon: "
    error_loading: Error loading curve data
    production_label: "Production (W)"

  battery_schedule:
    title: Battery Schedule
    charge_mode: Charge Mode
    self_consumption: Self-Consumption
    self_consumption_hint: >-
      Discharge to offset grid import, charge from solar excess — always active
    time_of_use: Time-of-Use
    time_of_use_hint: Charge and discharge on a manual hourly schedule
    backup_only: Backup Only
    backup_only_hint: Holds battery at full charge, discharges only during grid outages
    self_consumption_detail: >-
      Battery automatically discharges to reduce grid import and charges
      from surplus solar. No schedule needed.
    backup_only_detail: Battery stays fully charged and only discharges during grid outages.
    time_of_use_detail: Set charge and discharge hours in the schedule below.
    discharge_preset: Discharge Preset
    active_days: Active Days
    idle: Idle
    charge: Chg
    discharge: Dis
    save_schedule: Save Schedule

  evse_schedule:
    title: Charging Schedule
    select_preset: "-- Select Preset --"
    apply: Apply
    active_days: Active Days
    start: "Start:"
    duration: "Duration:"
    duration_unit: h
    apply_schedule: Apply

  panel_source:
    title: Source Panel
    cloned_from: "Cloned from "
    last_synced: "\u2014 last synced "
    utc: " UTC"
    update_ebus: Update eBus Energy

  modeling:
    title: "Modeling \u2014 "
    back_to_runtime: Back to Runtime
    horizon: Horizon
    last_month: Last Month
    last_3_months: Last 3 Months
    last_6_months: Last 6 Months
    last_year: Last Year
    visible_range: Visible Range
    loading: Loading modeling data...
    billing_data: Billing Data (Opower)
    change: Change
    select_account: Select Electric Account
    current_rate: Current Rate
    data_source_hint: Data source attribution
    no_rate: No rate plan selected
    configure: Configure
    refresh: Refresh
    proposed_rate: Proposed Rate
    using_current: Using current rate for comparison
    set_proposed: Set Proposed Rate
    clear: Clear
    openei_title: OpenEI Rate Plan
    api_settings: API Settings
    api_url: API URL
    api_url_placeholder: "https://api.openei.org/utility_rates"
    api_key: API Key
    api_key_placeholder: Enter your OpenEI API key
    save: Save
    get_api_key: Get a free API key
    select_rate: Select Rate Plan
    utility: Utility
    loading_utilities: Loading utilities...
    rate_plan: Rate Plan
    select_utility_first: Select a utility first
    use_this_rate: Use This Rate
    rate_source_title: Rate Data Source
    close: Close
    before: Before
    before_subtitle: "(Grid Power \u2014 recorder baseline)"
    after: After
    after_subtitle: "(Grid Power \u2014 current config)"
    energy_kwh: Energy (kWh)
    cost: Cost
    difference: Difference
    savings: Savings
    full_horizon: Full Horizon
    error_loading: "Error loading data: "
    bought_suffix: " (bought), "
    exported_suffix: " exp)"
    cost_billed: " (billed)"
    months_prefix: " of "
    months_suffix: " months)"
    import_suffix: " imp, "
    export_net: " exp \u2014 Net: "
    elec_label: " \u2014 ELEC "
    select_utility: Select a utility...
    loading_plans: Loading plans...
    select_rate_plan: Select a rate plan...
    error_loading_utilities: Error loading utilities
    error_loading_plans: Error loading plans
    network_error: "Network error: "
    provider: "Provider:"
    license: "License:"
    urdb_label: "URDB Label:"
    retrieved: "Retrieved:"
    view_on_openei: View on OpenEI
    engine_reload_timeout: Engine reload timed out
    no_running_sim: No running simulation
    cancel: Cancel
```

- [ ] **Step 2: Verify YAML is valid**

Run: `python -c "import yaml; yaml.safe_load(open('span_panel_simulator/translations/en.yaml'))" && echo "valid"`
Expected: `valid`

- [ ] **Step 3: Run key parity test — it should now run but skip non-English (they have no dashboard section yet)**

Run: `python -m pytest tests/test_translator.py::TestTranslationKeyParity -v`
Expected: Either PASS (non-English files have no dashboard section so their key set is empty, assertion fails) or indicates what needs to happen next.

- [ ] **Step 4: Commit**

```bash
git add span_panel_simulator/translations/en.yaml
git commit -m "Add complete English dashboard translation strings"
```

---

## Task 8: Non-English Translation Files

**Files:**
- Modify: `span_panel_simulator/translations/nl.yaml`
- Modify: `span_panel_simulator/translations/de.yaml`
- Modify: `span_panel_simulator/translations/fr.yaml`
- Modify: `span_panel_simulator/translations/es.yaml`
- Modify: `span_panel_simulator/translations/pt-BR.yaml`

- [ ] **Step 1: Add dashboard sections to all 5 non-English files**

Each file gets a `dashboard:` section with the exact same key structure as `en.yaml`, with values translated into the target language. The full translations for each language should be appended after the existing `configuration:` section.

Translate all keys accurately for each language. Use professional-grade translations — these are UI strings, not marketing copy, so prefer clear and concise phrasing.

- [ ] **Step 2: Validate all YAML files**

Run: `for f in span_panel_simulator/translations/*.yaml; do python -c "import yaml; yaml.safe_load(open('$f'))" && echo "$f: valid"; done`
Expected: All 6 files report valid.

- [ ] **Step 3: Run key parity test**

Run: `python -m pytest tests/test_translator.py::TestTranslationKeyParity -v`
Expected: PASS — all languages have the same dashboard keys as English.

- [ ] **Step 4: Commit**

```bash
git add span_panel_simulator/translations/
git commit -m "Add dashboard translations for nl, de, fr, es, pt-BR"
```

---

## Task 9: Template — base.html and dashboard.html

**Files:**
- Modify: `src/span_panel_simulator/dashboard/templates/base.html`
- Modify: `src/span_panel_simulator/dashboard/templates/dashboard.html`

- [ ] **Step 1: Update base.html**

Replace hardcoded strings with `{{ t('key') }}` calls. Add the i18n JS bridge in a `<script>` tag before other scripts:

- `<title>` → `{{ t('title') }}`
- `<h1>` → `{{ t('title') }}`
- Theme select `title` → `{{ t('theme.label') }}`
- Option "System" → `{{ t('theme.system') }}`
- Option "Light" → `{{ t('theme.light') }}`
- Option "Dark" → `{{ t('theme.dark') }}`

Add before the closing `</head>` or as the first `<script>` in the body:

```html
<script>
  window.i18nLocale = "{{ locale }}";
  window.i18n = {{ t_json | safe }};
</script>
```

- [ ] **Step 2: Update dashboard.html**

Replace getting-started strings:
- "Getting started" heading → `{{ t('getting_started.title') }}`
- Each instruction paragraph → `{{ t('getting_started.step_click') }}`, `{{ t('getting_started.step_clone') }}`, etc.

- [ ] **Step 3: Verify the dashboard still renders**

Run: `python -m pytest tests/ -x -q --timeout=30`
Expected: All tests pass.

- [ ] **Step 4: Commit**

```bash
git add src/span_panel_simulator/dashboard/templates/base.html \
        src/span_panel_simulator/dashboard/templates/dashboard.html
git commit -m "Translate base.html and dashboard.html to use i18n"
```

---

## Task 10: Template — runtime_controls.html

**Files:**
- Modify: `src/span_panel_simulator/dashboard/templates/partials/runtime_controls.html`

- [ ] **Step 1: Replace HTML strings**

- "Runtime Controls" → `{{ t('controls.title') }}`
- "Date" → `{{ t('controls.date') }}`
- "Time of Day" → `{{ t('controls.time_of_day') }}`
- "Speed" → `{{ t('controls.speed') }}`
- "Grid Online" / "Grid Offline" buttons → `{{ t('controls.grid_online') }}` / `{{ t('controls.grid_offline') }}`
- "Islandable" / "Not Islandable" → use `t()` calls
- "Runtime" / "Modeling" → use `t()` calls
- "Live Power Flows" → `{{ t('chart.live_power_flows') }}`
- Legend items "Grid", "Solar", "Battery" → `{{ t('chart.grid') }}`, etc.

- [ ] **Step 2: Replace JavaScript strings**

- Replace `MONTH_NAMES` array with `Intl.DateTimeFormat`:

```javascript
function monthShort(monthIndex) {
  return new Intl.DateTimeFormat(window.i18nLocale, { month: 'short' })
    .format(new Date(2024, monthIndex));
}
```

- Replace hardcoded button text toggles in JS (e.g., `btn.textContent = 'Grid Offline'`) with `window.i18n['controls.grid_offline']`
- Replace tooltip `" W"` suffix with `window.i18n['chart.watts_suffix']`
- Replace SOC status text construction with i18n lookups

- [ ] **Step 3: Verify rendering**

Run: `python -m pytest tests/ -x -q --timeout=30`
Expected: All tests pass.

- [ ] **Step 4: Commit**

```bash
git add src/span_panel_simulator/dashboard/templates/partials/runtime_controls.html
git commit -m "Translate runtime_controls.html to use i18n"
```

---

## Task 11: Template — panel_config.html and sim_config.html

**Files:**
- Modify: `src/span_panel_simulator/dashboard/templates/partials/panel_config.html`
- Modify: `src/span_panel_simulator/dashboard/templates/partials/sim_config.html`

- [ ] **Step 1: Replace all hardcoded strings in panel_config.html**

- "Panel Config" → `{{ t('panel_config.title') }}`
- All form labels (Serial, Tabs, Main Breaker, SOC Shed, Location, Lat, Lon) → `t()` calls
- Placeholder text → `t()` calls
- "Update" button → `{{ t('panel_config.update') }}`
- JS messages ("No results", "Fetching historical weather data...") → `window.i18n[...]`

- [ ] **Step 2: Replace all hardcoded strings in sim_config.html**

- "Simulation Config" → `{{ t('sim_config.title') }}`
- "Export", "Save & Reload" → `t()` calls
- "Interval (s):", "Noise:" → `t()` calls
- "Update" → `{{ t('sim_config.update') }}`

- [ ] **Step 3: Verify and commit**

Run: `python -m pytest tests/ -x -q --timeout=30`

```bash
git add src/span_panel_simulator/dashboard/templates/partials/panel_config.html \
        src/span_panel_simulator/dashboard/templates/partials/sim_config.html
git commit -m "Translate panel_config and sim_config templates to use i18n"
```

---

## Task 12: Template — Entity Templates

**Files:**
- Modify: `src/span_panel_simulator/dashboard/templates/partials/entity_list.html`
- Modify: `src/span_panel_simulator/dashboard/templates/partials/entity_row.html`
- Modify: `src/span_panel_simulator/dashboard/templates/partials/entity_edit.html`

- [ ] **Step 1: Replace strings in entity_list.html**

- "Entities (" heading → use `{{ t('entities.title').format(count=entities|length) }}` or split into prefix/suffix
- "+ Add Entity" → `{{ t('entities.add_entity') }}`
- Hint text → `{{ t('entities.clone_hint') }}`
- "Unmapped Tabs" → similar pattern
- JS hint strings → `window.i18n[...]`

- [ ] **Step 2: Replace strings in entity_row.html**

- Title attributes → `{{ t('entity_row.overlay_hint') }}`, etc.
- Badge text (SYN/REC) → `{{ t('entity_row.syn') }}`, etc.
- "Edit", "Del" buttons → `t()` calls

- [ ] **Step 3: Replace strings in entity_edit.html**

- All form labels (Name, Tabs, Priority, Relay Behavior, Breaker, etc.) → `t()` calls
- Fieldset legends (PV System, EVSE Charger, Energy Profile, etc.) → `t()` calls
- Select options (Grid-Tied, Hybrid, HVAC types) → `t()` calls
- Save/Cancel buttons → `t()` calls

- [ ] **Step 4: Verify and commit**

Run: `python -m pytest tests/ -x -q --timeout=30`

```bash
git add src/span_panel_simulator/dashboard/templates/partials/entity_list.html \
        src/span_panel_simulator/dashboard/templates/partials/entity_row.html \
        src/span_panel_simulator/dashboard/templates/partials/entity_edit.html
git commit -m "Translate entity templates to use i18n"
```

---

## Task 13: Template — Clone and Panel Templates

**Files:**
- Modify: `src/span_panel_simulator/dashboard/templates/partials/clone_panel.html`
- Modify: `src/span_panel_simulator/dashboard/templates/partials/clone_confirm.html`
- Modify: `src/span_panel_simulator/dashboard/templates/partials/running_panels.html`
- Modify: `src/span_panel_simulator/dashboard/templates/partials/panels_list_rows.html`
- Modify: `src/span_panel_simulator/dashboard/templates/partials/panel_source.html`

- [ ] **Step 1: Replace strings in clone_panel.html**

- "Clone from Panel" → `{{ t('clone_panel.title') }}`
- Hint text (HA / standalone) → `t()` calls
- Form labels, placeholders, button text → `t()` calls
- JS option text → `window.i18n[...]`

- [ ] **Step 2: Replace strings in clone_confirm.html**

- Dialog text and button labels → `t()` calls

- [ ] **Step 3: Replace strings in running_panels.html**

- "Panels" heading → `{{ t('panels.title') }}`
- "Import", "Overwrite", "Cancel" → `t()` calls
- "already exists." → `{{ t('panels.already_exists') }}`

- [ ] **Step 4: Replace strings in panels_list_rows.html**

- Badge text ("template", "viewing", "editing") → `t()` calls
- Button text and title attributes → `t()` calls
- JS messages (clone prompt, error messages, unsaved warning) → `window.i18n[...]`

- [ ] **Step 5: Replace strings in panel_source.html**

- "Source Panel" → `{{ t('panel_source.title') }}`
- "Cloned from", "last synced", "UTC" → `t()` calls
- "Update eBus Energy" → `{{ t('panel_source.update_ebus') }}`

- [ ] **Step 6: Verify and commit**

Run: `python -m pytest tests/ -x -q --timeout=30`

```bash
git add src/span_panel_simulator/dashboard/templates/partials/clone_panel.html \
        src/span_panel_simulator/dashboard/templates/partials/clone_confirm.html \
        src/span_panel_simulator/dashboard/templates/partials/running_panels.html \
        src/span_panel_simulator/dashboard/templates/partials/panels_list_rows.html \
        src/span_panel_simulator/dashboard/templates/partials/panel_source.html
git commit -m "Translate clone and panel management templates to use i18n"
```

---

## Task 14: Template — Profile and Schedule Templates

**Files:**
- Modify: `src/span_panel_simulator/dashboard/templates/partials/profile_editor.html`
- Modify: `src/span_panel_simulator/dashboard/templates/partials/pv_profile.html`
- Modify: `src/span_panel_simulator/dashboard/templates/partials/battery_profile_editor.html`
- Modify: `src/span_panel_simulator/dashboard/templates/partials/evse_schedule.html`

- [ ] **Step 1: Replace strings in profile_editor.html**

- "24-Hour Profile" → `{{ t('profile_editor.title') }}`
- "-- Select Preset --", "from", "to", "Apply" → `t()` calls
- "Active Days", "Save Profile" → `t()` calls

- [ ] **Step 2: Replace strings in pv_profile.html**

- "Solar Production Profile" → `{{ t('pv_profile.title') }}`
- "Monthly Weather Degradation" → `{{ t('pv_profile.weather_degradation') }}`
- No-weather hint text → `{{ t('pv_profile.no_weather') }}`
- Replace `MONTH_LABELS` and `MONTH_NAMES` arrays with `Intl.DateTimeFormat`:

```javascript
const MONTH_LABELS = Array.from({length: 12}, (_, i) =>
  new Intl.DateTimeFormat(window.i18nLocale, { month: 'short' }).format(new Date(2024, i))
);
const MONTH_NAMES = Array.from({length: 12}, (_, i) =>
  new Intl.DateTimeFormat(window.i18nLocale, { month: 'long' }).format(new Date(2024, i))
);
```

- Chart labels and info text → `window.i18n[...]`

- [ ] **Step 3: Replace strings in battery_profile_editor.html**

- "Battery Schedule" → `{{ t('battery_schedule.title') }}`
- "Charge Mode" → `{{ t('battery_schedule.charge_mode') }}`
- Mode labels and hints → `t()` calls
- "Discharge Preset", "Active Days" → `t()` calls
- Hour labels (Idle, Chg, Dis) → `t()` calls
- "Save Schedule" → `{{ t('battery_schedule.save_schedule') }}`

- [ ] **Step 4: Replace strings in evse_schedule.html**

- "Charging Schedule" → `{{ t('evse_schedule.title') }}`
- All labels and buttons → `t()` calls

- [ ] **Step 5: Verify and commit**

Run: `python -m pytest tests/ -x -q --timeout=30`

```bash
git add src/span_panel_simulator/dashboard/templates/partials/profile_editor.html \
        src/span_panel_simulator/dashboard/templates/partials/pv_profile.html \
        src/span_panel_simulator/dashboard/templates/partials/battery_profile_editor.html \
        src/span_panel_simulator/dashboard/templates/partials/evse_schedule.html
git commit -m "Translate profile and schedule templates to use i18n"
```

---

## Task 15: Template — modeling_view.html

**Files:**
- Modify: `src/span_panel_simulator/dashboard/templates/partials/modeling_view.html`

- [ ] **Step 1: Replace HTML strings**

This is the largest partial. Replace all hardcoded strings:
- "Modeling —" heading → `{{ t('modeling.title') }}`
- "Back to Runtime" → `{{ t('modeling.back_to_runtime') }}`
- "Horizon", select options (Last Month, etc.) → `t()` calls
- "Visible Range", "Loading modeling data..." → `t()` calls
- "Billing Data (Opower)", "Change" → `t()` calls
- "Select Electric Account" dialog → `t()` calls
- "Current Rate", "Proposed Rate" sections → `t()` calls
- "OpenEI Rate Plan" dialog → `t()` calls
- "Before" / "After" chart sections → `t()` calls
- Table headers (Energy, Cost, Difference, Savings) → `t()` calls
- Legend items (Grid, Solar, Battery) → `t()` calls

- [ ] **Step 2: Replace JavaScript strings**

- Error messages → `window.i18n['modeling.error_loading']`
- Tooltip suffixes → `window.i18n[...]`
- Y-axis title "Watts" → `window.i18n['chart.watts']`
- Cost/energy display text construction → `window.i18n[...]`
- Opower account display → `window.i18n[...]`
- Utility/rate plan select options → `window.i18n[...]`
- Attribution popup labels → `window.i18n[...]`
- Engine reload/error messages → `window.i18n[...]`

- [ ] **Step 3: Verify and commit**

Run: `python -m pytest tests/ -x -q --timeout=30`

```bash
git add src/span_panel_simulator/dashboard/templates/partials/modeling_view.html
git commit -m "Translate modeling_view.html to use i18n"
```

---

## Task 16: Full Integration Test

**Files:**
- All previously modified files

- [ ] **Step 1: Run full test suite**

Run: `python -m pytest tests/ -v --timeout=60`
Expected: All tests pass.

- [ ] **Step 2: Run type checker**

Run: `mypy src/span_panel_simulator/dashboard/translator.py`
Expected: No errors.

- [ ] **Step 3: Run linter**

Run: `ruff check src/span_panel_simulator/dashboard/translator.py tests/test_translator.py`
Expected: No errors.

- [ ] **Step 4: Run key parity test to validate all translations**

Run: `python -m pytest tests/test_translator.py::TestTranslationKeyParity -v`
Expected: PASS — all 6 language files have identical dashboard key sets.

- [ ] **Step 5: Manual smoke test**

Start the simulator and verify the dashboard loads correctly in a browser. Check that:
- All strings render (no raw keys visible)
- Theme selector works
- Charts display with correct labels
- All buttons and form labels are translated

- [ ] **Step 6: Commit any fixes**

```bash
git add -u
git commit -m "Fix any issues found during integration testing"
```
