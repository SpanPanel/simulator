# Panel Selector Preview & Dirty-State Guard — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Allow users to click any panel row (including defaults) to preview its circuits in the entity list, with a dirty-state guard to prevent losing unsaved edits.

**Architecture:** Add a `_dirty` flag to `ConfigStore` that tracks in-memory mutations. Lift the default-template restriction from `handle_load_config` so any config can be viewed. Replace the "Edit" button with clickable panel rows that check dirty state before switching.

**Tech Stack:** Python (aiohttp), Jinja2 templates, HTMX, vanilla JS

**Spec:** `docs/superpowers/specs/2026-03-21-panel-selector-preview-design.md`

---

### Task 1: Add dirty-state tracking to ConfigStore

**Files:**
- Modify: `src/span_panel_simulator/dashboard/config_store.py:66-107`
- Test: `tests/test_config_store.py` (create)

- [ ] **Step 1: Write the test file with dirty-flag tests**

Create `tests/test_config_store.py`:

```python
"""Tests for ConfigStore dirty-state tracking."""

from __future__ import annotations

from pathlib import Path
from textwrap import dedent

import pytest

from span_panel_simulator.dashboard.config_store import ConfigStore

MINIMAL_YAML = dedent("""\
    panel_config:
      serial_number: "TEST-001"
      total_tabs: 16
      main_size: 200
      latitude: 37.7
      longitude: -122.4
    circuit_templates:
      lighting:
        energy_profile:
          mode: consumer
          power_range: [0.0, 500.0]
          typical_power: 80.0
          power_variation: 0.1
        relay_behavior: controllable
        priority: NEVER
    circuits:
      - id: light_1
        name: Light 1
        template: lighting
        tabs: [1]
    simulation_params:
      update_interval: 5
      time_acceleration: 1.0
      noise_factor: 0.02
      enable_realistic_behaviors: true
""")


class TestDirtyFlag:
    def test_starts_clean(self) -> None:
        store = ConfigStore()
        assert store.dirty is False

    def test_load_from_yaml_clears_dirty(self) -> None:
        store = ConfigStore()
        store.load_from_yaml(MINIMAL_YAML)
        assert store.dirty is False

    def test_load_from_file_clears_dirty(self, tmp_path: Path) -> None:
        f = tmp_path / "test.yaml"
        f.write_text(MINIMAL_YAML)
        store = ConfigStore()
        store.load_from_file(f)
        assert store.dirty is False

    def test_update_panel_config_sets_dirty(self) -> None:
        store = ConfigStore()
        store.load_from_yaml(MINIMAL_YAML)
        store.update_panel_config({"serial_number": "CHANGED"})
        assert store.dirty is True

    def test_update_simulation_params_sets_dirty(self) -> None:
        store = ConfigStore()
        store.load_from_yaml(MINIMAL_YAML)
        store.update_simulation_params({"update_interval": 10})
        assert store.dirty is True

    def test_add_entity_sets_dirty(self) -> None:
        store = ConfigStore()
        store.load_from_yaml(MINIMAL_YAML)
        store.add_entity("circuit")
        assert store.dirty is True

    def test_delete_entity_sets_dirty(self) -> None:
        store = ConfigStore()
        store.load_from_yaml(MINIMAL_YAML)
        store.delete_entity("light_1")
        assert store.dirty is True

    def test_update_entity_sets_dirty(self) -> None:
        store = ConfigStore()
        store.load_from_yaml(MINIMAL_YAML)
        store.update_entity("light_1", {"name": "New Name"})
        assert store.dirty is True

    def test_update_entity_profile_sets_dirty(self) -> None:
        store = ConfigStore()
        store.load_from_yaml(MINIMAL_YAML)
        store.update_entity_profile("light_1", {h: 0.5 for h in range(24)})
        assert store.dirty is True

    def test_save_to_file_clears_dirty(self, tmp_path: Path) -> None:
        store = ConfigStore()
        store.load_from_yaml(MINIMAL_YAML)
        store.update_panel_config({"serial_number": "CHANGED"})
        assert store.dirty is True
        out = tmp_path / "out.yaml"
        store.save_to_file(out)
        assert store.dirty is False
        assert out.exists()

    def test_load_after_dirty_clears_flag(self) -> None:
        store = ConfigStore()
        store.load_from_yaml(MINIMAL_YAML)
        store.update_panel_config({"serial_number": "CHANGED"})
        assert store.dirty is True
        store.load_from_yaml(MINIMAL_YAML)
        assert store.dirty is False
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_config_store.py -v`
Expected: Multiple FAILs — `dirty` property and `save_to_file` method don't exist yet.

- [ ] **Step 3: Implement dirty flag and save_to_file in ConfigStore**

In `src/span_panel_simulator/dashboard/config_store.py`, modify the `ConfigStore` class:

Add `_dirty` attribute to `__init__`:
```python
    def __init__(self) -> None:
        self._dirty: bool = False
        self._state: dict[str, Any] = {
            ...  # existing code
        }
```

Add the `dirty` property after `__init__`:
```python
    @property
    def dirty(self) -> bool:
        """Whether in-memory state has unsaved changes."""
        return self._dirty
```

Clear dirty in `load_from_yaml`:
```python
    def load_from_yaml(self, content: str) -> None:
        """Parse, validate, and replace state from YAML string."""
        data = yaml.safe_load(content)
        if not isinstance(data, dict):
            raise ValueError("YAML content must be a mapping")
        validate_yaml_config(data)
        self._state = data
        self._dirty = False
```

Add `save_to_file` method after `export_yaml`:
```python
    def save_to_file(self, path: Path) -> None:
        """Serialize current state to YAML and write to disk."""
        path.write_text(self.export_yaml(), encoding="utf-8")
        self._dirty = False
```

Add `self._dirty = True` as the **last line** of each mutation method:
- `update_panel_config` — after the for loops
- `update_simulation_params` — after the if blocks
- `update_entity` — at the end (after `self._mark_user_modified`)
- `add_entity` — before the `return`
- `add_entity_from_tabs` — before the `return`
- `delete_entity` — after template cleanup
- `update_entity_profile` — after `self._mark_user_modified`
- `apply_preset` — after `update_active_days` / `update_entity_profile` calls
- `update_active_days` — after `self._mark_user_modified`
- `toggle_user_modified` — before the `return` (only when actually toggled, i.e., after the `recorder_entity` check passes)
- `update_battery_profile` — after `self._mark_user_modified`
- `update_battery_charge_mode` — after `self._mark_user_modified`
- `apply_battery_preset` — before the `return`
- `update_evse_schedule` — after `self._mark_user_modified`
- `apply_evse_preset` — after `self._mark_user_modified`

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_config_store.py -v`
Expected: All PASS

- [ ] **Step 5: Run full test suite**

Run: `python -m pytest tests/ -v`
Expected: All existing tests still pass

- [ ] **Step 6: Commit**

```bash
git add tests/test_config_store.py src/span_panel_simulator/dashboard/config_store.py
git commit -m "feat: add dirty-state tracking and save_to_file to ConfigStore"
```

---

### Task 2: Add check-dirty route and update load-config / save-reload

**Files:**
- Modify: `src/span_panel_simulator/dashboard/routes.py:235-240` (setup_routes), `routes.py:911-938` (handle_load_config), `routes.py:981-1000` (handle_save_reload)
- Test: `tests/test_config_store.py` (extend)

- [ ] **Step 1: Write tests for the route changes**

Append to `tests/test_config_store.py`:

```python
class TestDirtyStateAfterMutation:
    """Tests for dirty flag transitions across mutations and loads."""

    def test_clean_store_reports_not_dirty(self) -> None:
        store = ConfigStore()
        store.load_from_yaml(MINIMAL_YAML)
        assert store.dirty is False

    def test_mutated_store_reports_dirty(self) -> None:
        store = ConfigStore()
        store.load_from_yaml(MINIMAL_YAML)
        store.update_panel_config({"serial_number": "X"})
        assert store.dirty is True


class TestSaveToFile:
    """Tests for save_to_file round-trip."""

    def test_saved_file_is_valid_yaml(self, tmp_path: Path) -> None:
        store = ConfigStore()
        store.load_from_yaml(MINIMAL_YAML)
        store.update_panel_config({"serial_number": "SAVED"})
        out = tmp_path / "saved.yaml"
        store.save_to_file(out)

        # Reload and verify
        store2 = ConfigStore()
        store2.load_from_file(out)
        assert store2.get_panel_config()["serial_number"] == "SAVED"
        assert store2.dirty is False
```

- [ ] **Step 2: Run new tests**

Run: `python -m pytest tests/test_config_store.py -v`
Expected: All PASS (these use already-implemented ConfigStore methods)

- [ ] **Step 3: Update handle_load_config to allow defaults**

In `src/span_panel_simulator/dashboard/routes.py`, remove the default rejection in `handle_load_config`:

Replace lines 917-920:
```python
    if filename.startswith("default_"):
        raise web.HTTPBadRequest(
            text="Default templates cannot be edited. Clone to create your own."
        )
```

With nothing (delete those 4 lines entirely).

- [ ] **Step 4: Add default guard to handle_save_reload**

In `handle_save_reload` (line 981), add a guard after `ctx = _ctx(request)`:

```python
    filename = ctx.config_filter or "default_config.yaml"
    if filename.startswith("default_"):
        raise web.HTTPBadRequest(
            text="Cannot save changes to a default template. Clone it first."
        )
```

Move the existing `filename = ctx.config_filter or "default_config.yaml"` line up if needed so the guard comes right after it (removing the duplicate).

- [ ] **Step 5: Refactor handle_save_reload to use store.save_to_file**

Replace the manual export + write in `handle_save_reload`:

```python
    yaml_content = store.export_yaml()
    ...
    output_path.write_text(yaml_content, encoding="utf-8")
```

With:

```python
    store.save_to_file(output_path)
```

The full `handle_save_reload` should become:

```python
async def handle_save_reload(request: web.Request) -> web.Response:
    store = _store(request)
    ctx = _ctx(request)

    filename = ctx.config_filter or "default_config.yaml"
    if filename.startswith("default_"):
        raise web.HTTPBadRequest(
            text="Cannot save changes to a default template. Clone it first."
        )

    output_path = ctx.config_dir / filename
    store.save_to_file(output_path)
    _LOGGER.info("Config saved to %s", output_path)

    ctx.start_panel(filename)

    return web.Response(
        text='<div class="flash success">Config saved and reload triggered.</div>',
        content_type="text/html",
    )
```

- [ ] **Step 6: Add the check-dirty route handler and register it**

Add the handler near the other utility handlers (after `handle_save_reload`):

```python
async def handle_check_dirty(request: web.Request) -> web.Response:
    """GET /check-dirty — return JSON dirty state for JS fetch."""
    store = _store(request)
    return web.json_response({"dirty": store.dirty})
```

In `setup_routes`, add the registration (near the other file operations):

```python
    app.router.add_get("/check-dirty", handle_check_dirty)
```

- [ ] **Step 7: Run full test suite**

Run: `python -m pytest tests/ -v`
Expected: All PASS

- [ ] **Step 8: Commit**

```bash
git add src/span_panel_simulator/dashboard/routes.py tests/test_config_store.py
git commit -m "feat: add check-dirty route, allow loading defaults, guard save-reload"
```

---

### Task 3: Make panel rows clickable with dirty-state guard

**Files:**
- Modify: `src/span_panel_simulator/dashboard/templates/partials/panels_list_rows.html`

- [ ] **Step 1: Update the panel row template**

Replace the entire content of `panels_list_rows.html` with:

```html
{% for p in panels %}
<div class="panel-row {{ 'active-panel' if p.active else '' }}"
     {% if not p.active %}
     onclick="switchPanel('{{ p.filename }}')"
     style="cursor: pointer"
     title="Click to view this config"
     {% endif %}>
  <span class="panel-status-dot {{ 'status-running' if p.running else 'status-stopped' }}"></span>
  <span class="panel-serial">{{ p.serial }}</span>
  {% if p.port %}<span class="panel-port" title="Bootstrap HTTP port">:{{ p.port }}</span>{% endif %}
  <span class="panel-filename">{{ p.filename }}</span>
  {% if p.is_default %}
  <span class="badge badge-default" title="Clone this template to create an editable config">template</span>
  {% endif %}
  {% if p.active %}
  <span class="badge badge-editing">{{ 'viewing' if p.is_default else 'editing' }}</span>
  {% endif %}
  <button class="btn btn-sm btn-secondary"
          onclick="event.stopPropagation();var n=prompt('Clone as:','{{ p.filename.replace('default_', '') }}');if(n){htmx.ajax('POST','clone',{target:'#flash-area',swap:'innerHTML',values:{filename:n,source_file:'{{ p.filename }}'}});}"
          title="Clone this config">
    Clone
  </button>
  {% if not p.is_default and p.running %}
  <button class="btn btn-sm btn-secondary"
          onclick="event.stopPropagation();window.enterModelingMode('{{ p.filename }}', '{{ p.serial }}')"
          title="Open modeling view">Model</button>
  {% endif %}
  {% if p.running %}
  <button class="btn btn-sm btn-secondary"
          onclick="event.stopPropagation()"
          hx-post="restart-panel"
          hx-vals='{"filename": "{{ p.filename }}"}'
          hx-target="#flash-area" hx-swap="innerHTML"
          title="Restart engine">Restart</button>
  <button class="btn btn-sm btn-danger"
          onclick="event.stopPropagation()"
          hx-post="stop-panel"
          hx-vals='{"filename": "{{ p.filename }}"}'
          hx-target="#flash-area" hx-swap="innerHTML"
          title="Stop engine">Stop</button>
  {% else %}
  <button class="btn btn-sm btn-primary"
          onclick="event.stopPropagation()"
          hx-post="start-panel"
          hx-vals='{"filename": "{{ p.filename }}"}'
          hx-target="#flash-area" hx-swap="innerHTML"
          title="Start engine">Start</button>
  {% if not p.is_default and not p.running %}
  <button class="btn btn-sm btn-danger"
          onclick="event.stopPropagation()"
          hx-post="delete-config"
          hx-vals='{"filename": "{{ p.filename }}"}'
          hx-target="#flash-area" hx-swap="innerHTML"
          hx-confirm="Delete {{ p.filename }}? This cannot be undone."
          title="Delete config file">Del</button>
  {% endif %}
  {% endif %}
</div>
{% endfor %}
{% if not panels %}
<p class="hint">No config files found. Import or clone a panel to get started.</p>
{% elif panels | rejectattr('is_default') | list | length == 0 %}
<p class="hint">Clone a template above to create your own editable configuration.</p>
{% endif %}

<script>
function switchPanel(filename) {
  fetch('check-dirty')
    .then(function(r) { return r.json(); })
    .then(function(data) {
      if (data.dirty) {
        if (!confirm('You have unsaved changes that will be lost. Switch anyway?')) {
          return;
        }
      }
      // load-config responds with HX-Redirect, so HTMX handles the
      // full page reload automatically — no target/swap needed.
      htmx.ajax('POST', 'load-config', {
        values: { config_file: filename }
      });
    })
    .catch(function(err) {
      var flash = document.getElementById('flash-area');
      if (flash) {
        flash.innerHTML = '<div class="flash error">Failed to switch panel: ' + err.message + '</div>';
      }
    });
}
</script>
```

Key changes from the original:
- Row div gets `onclick="switchPanel('...')"` when not active (with `cursor: pointer`)
- "Edit" button removed entirely
- Badge shows "viewing" for defaults, "editing" for user configs
- All buttons inside the row get `onclick="event.stopPropagation()"` to prevent the row click from firing when pressing a button
- `switchPanel` JS function checks `/check-dirty` before switching
- Error handling in the catch block shows a flash message

- [ ] **Step 2: Verify manually**

Start the simulator locally. In the dashboard:
1. Verify the default panel rows are clickable
2. Click a different default (e.g., `default_MAIN_32.yaml`) — entity list should update to show its circuits (read-only)
3. Click `default_MAIN_40.yaml` — should show 31 circuits
4. Click `default_MAIN_16.yaml` — should show its circuits
5. Clone a default, edit something, then try switching — confirm dialog should appear
6. Verify all existing buttons (Clone, Start, Stop, Restart, Del, Model) still work without triggering row click

- [ ] **Step 3: Run full test suite**

Run: `python -m pytest tests/ -v`
Expected: All PASS

- [ ] **Step 4: Commit**

```bash
git add src/span_panel_simulator/dashboard/templates/partials/panels_list_rows.html
git commit -m "feat: make panel rows clickable with dirty-state guard"
```

---

### Task 4: Update hover styling for clickable panel rows

**Files:**
- Modify: `src/span_panel_simulator/dashboard/static/dashboard.css:1178-1180`

- [ ] **Step 1: Modify existing hover rule to exclude active panel**

In `dashboard.css`, there is an existing hover rule at line 1178:

```css
.panel-row:hover {
  background: var(--row-hover);
}
```

Replace it with:

```css
.panel-row:not(.active-panel):hover {
  background: var(--row-hover);
  cursor: pointer;
}
```

This reuses the existing `--row-hover` variable (already defined for light mode at `#e8e8e8` and dark mode at `#2e2e2e`), but now only applies to non-active rows.

- [ ] **Step 2: Verify visually**

Start the simulator and hover over non-active panel rows. Confirm:
- Hover highlight appears on non-active rows
- Active row does NOT get hover highlight
- Works in both light and dark mode

- [ ] **Step 3: Commit**

```bash
git add src/span_panel_simulator/dashboard/static/dashboard.css
git commit -m "feat: restrict hover styling to non-active panel rows"
```
