# Dashboard BESS Refactor Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Migrate the dashboard from managing BESS as a circuit entity to a dedicated panel-level card backed by the top-level `bess` YAML section.

**Architecture:** BESS becomes a dedicated card between panel config and the entity list. ConfigStore reads/writes `self._state["bess"]` directly. All entity-based battery methods lose their `entity_id` parameter. Battery is no longer an entity type — it cannot be added, deleted, or listed alongside circuits.

**Tech Stack:** Python 3.14, aiohttp, Jinja2, HTMX, pytest

---

### Task 1: Remove battery from entity types and defaults

**Files:**
- Modify: `src/span_panel_simulator/dashboard/defaults.py:106-129`
- Modify: `src/span_panel_simulator/dashboard/routes.py:77-79`
- Modify: `src/span_panel_simulator/dashboard/config_store.py:54-63,296-299`

- [ ] **Step 1: Remove `"battery"` from `ENTITY_TYPES` and `_SINGLETON_TYPES`**

In `src/span_panel_simulator/dashboard/routes.py`, change line 77:

```python
ENTITY_TYPES = ["circuit", "pv", "evse"]
```

And line 79:

```python
_SINGLETON_TYPES = {"pv"}
```

- [ ] **Step 2: Remove battery defaults from `defaults.py`**

In `src/span_panel_simulator/dashboard/defaults.py`, remove the entire `"battery"` block (lines 106-129):

```python
    "battery": {
        "template": {
            ...
        },
        "circuit": {},
    },
```

Also remove `"battery"` from `default_name_for_type` (line 139).

- [ ] **Step 3: Remove `battery_behavior` from `_detect_entity_type`**

In `src/span_panel_simulator/dashboard/config_store.py`, simplify `_detect_entity_type` (lines 54-63):

```python
def _detect_entity_type(template: dict[str, Any]) -> str:
    """Infer entity type from template fields."""
    device_type = template.get("device_type", "")
    if device_type == "pv":
        return "pv"
    if device_type == "evse":
        return "evse"
    return "circuit"
```

- [ ] **Step 4: Remove battery from `list_entities` sort order**

In `src/span_panel_simulator/dashboard/config_store.py` line 297, update the sort order:

```python
        _type_order = {"pv": 0, "evse": 1, "circuit": 2}
```

- [ ] **Step 5: Remove battery exclusion from `get_unmapped_tabs`**

In `config_store.py` around line 434, the method excludes battery entities from tab counting. Since battery is no longer an entity type, simplify:

```python
    def get_unmapped_tabs(self) -> list[int]:
        """Return tab numbers not assigned to any circuit, sorted ascending."""
        total_tabs = self._state.get("panel_config", {}).get("total_tabs", 32)
        used: set[int] = set()
        for circ in self._circuits():
            used.update(circ.get("tabs", []))
        return sorted(t for t in range(1, total_tabs + 1) if t not in used)
```

- [ ] **Step 6: Run type checker and tests**

Run: `mypy src/span_panel_simulator/dashboard/ && pytest tests/ -q`
Expected: May have errors from route handlers still referencing battery entity methods — that's OK, fixed in later tasks.

- [ ] **Step 7: Commit**

```
git add src/span_panel_simulator/dashboard/defaults.py src/span_panel_simulator/dashboard/routes.py src/span_panel_simulator/dashboard/config_store.py
git commit -m "Remove battery from entity types, defaults, and detection"
```

---

### Task 2: Rewrite ConfigStore battery methods to use top-level `bess`

**Files:**
- Modify: `src/span_panel_simulator/dashboard/config_store.py`

- [ ] **Step 1: Remove `battery_behavior` from `EntityView` and `_merge_entity`**

In `EntityView` dataclass (line 46), remove:
```python
    battery_behavior: dict[str, Any] | None = None
```

In `_merge_entity` (line 287), remove:
```python
            battery_behavior=template.get("battery_behavior"),
```

- [ ] **Step 2: Remove battery keys handling from `update_entity`**

In `update_entity` (lines 372-382), remove the `battery_keys` block:
```python
        battery_keys = (
            "nameplate_capacity_kwh",
            "backup_reserve_pct",
            "max_charge_power",
            "max_discharge_power",
        )
        if any(k in data for k in battery_keys):
            bb: dict[str, Any] = template.setdefault("battery_behavior", {})
            for k in battery_keys:
                if k in data:
                    bb[k] = float(data[k])
```

Also remove the battery check from tabs handling (line 327):
```python
        if "tabs" in data and _detect_entity_type(template) != "battery":
```
Changes to:
```python
        if "tabs" in data:
```

- [ ] **Step 3: Add `get_bess_config` and `update_bess_config` methods**

Add after the panel config section (around line 140):

```python
    # -- BESS config --

    def get_bess_config(self) -> dict[str, Any]:
        """Return the top-level BESS configuration, or empty dict if absent."""
        bess = self._state.get("bess")
        return dict(bess) if isinstance(bess, dict) else {}

    def has_bess(self) -> bool:
        """Whether a BESS is configured and enabled."""
        bess = self._state.get("bess")
        return isinstance(bess, dict) and bool(bess.get("enabled"))

    def update_bess_config(self, data: dict[str, Any]) -> None:
        """Update top-level BESS settings from form data.

        Translates form field names to YAML field names:
        ``max_charge_power`` → ``max_charge_w``,
        ``max_discharge_power`` → ``max_discharge_w``.
        """
        bess = self._state.setdefault("bess", {"enabled": True})
        field_map = {
            "nameplate_capacity_kwh": "nameplate_capacity_kwh",
            "backup_reserve_pct": "backup_reserve_pct",
            "max_charge_power": "max_charge_w",
            "max_discharge_power": "max_discharge_w",
        }
        for form_key, yaml_key in field_map.items():
            if form_key in data:
                bess[yaml_key] = float(data[form_key])
        self._dirty = True
```

- [ ] **Step 4: Rewrite battery charge mode methods**

Replace existing methods (lines 645-667):

```python
    # -- Battery charge mode --

    def get_battery_charge_mode(self) -> str:
        """Return the BESS charge mode (default ``"self-consumption"``)."""
        bess = self.get_bess_config()
        return str(bess.get("charge_mode", "self-consumption"))

    def update_battery_charge_mode(self, mode: str) -> None:
        """Set the BESS charge mode."""
        valid_modes = ("self-consumption", "custom", "backup-only")
        if mode not in valid_modes:
            raise ValueError(f"Invalid charge mode: {mode!r}")
        bess = self._state.setdefault("bess", {"enabled": True})
        bess["charge_mode"] = mode
        self._dirty = True
```

- [ ] **Step 5: Rewrite battery profile methods**

Replace existing methods (lines 671-713):

```python
    # -- Battery profile --

    def get_battery_profile(self) -> dict[int, str]:
        """Return the 24-hour BESS schedule as hour → mode mapping."""
        bess = self.get_bess_config()
        charge_hours = set(bess.get("charge_hours", []))
        discharge_hours = set(bess.get("discharge_hours", []))

        profile: dict[int, str] = {}
        for h in range(24):
            if h in charge_hours:
                profile[h] = "charge"
            elif h in discharge_hours:
                profile[h] = "discharge"
            else:
                profile[h] = "idle"
        return profile

    def update_battery_profile(self, hour_modes: dict[int, str]) -> None:
        """Write per-hour charge/discharge/idle schedule into BESS config."""
        bess = self._state.setdefault("bess", {"enabled": True})
        bess["charge_hours"] = sorted(h for h, m in hour_modes.items() if m == "charge")
        bess["discharge_hours"] = sorted(h for h, m in hour_modes.items() if m == "discharge")
        self._dirty = True

    def apply_battery_preset(self, preset_name: str) -> dict[int, str]:
        """Apply a named battery preset and return the schedule."""
        hour_modes = get_battery_preset(preset_name)
        self.update_battery_profile(hour_modes)
        self._dirty = True
        return hour_modes
```

- [ ] **Step 6: Rewrite battery branch in `get_active_days` / `update_active_days`**

In `get_active_days` (lines 498-511), replace the battery branch:

```python
    def get_bess_active_days(self) -> list[int]:
        """Return active weekdays for BESS (empty = all days)."""
        bess = self.get_bess_config()
        days: list[int] = bess.get("active_days", [])
        return [d for d in days if isinstance(d, int) and 0 <= d <= 6]

    def update_bess_active_days(self, days: list[int]) -> None:
        """Write active weekdays into BESS config."""
        bess = self._state.setdefault("bess", {"enabled": True})
        clean = sorted(set(d for d in days if 0 <= d <= 6))
        if clean and len(clean) < 7:
            bess["active_days"] = clean
        else:
            bess.pop("active_days", None)
        self._dirty = True
```

The existing `get_active_days(entity_id)` and `update_active_days(entity_id, days)` methods keep their entity-based signatures but remove the battery branch — they now only handle circuits/EVSE via `time_of_day_profile`.

- [ ] **Step 7: Update energy projection to read from `bess`**

In the energy projection method (around line 844), replace the `entity.entity_type == "battery"` branch:

```python
        # Battery from top-level bess config (not an entity)
        bess = self.get_bess_config()
        if bess.get("enabled"):
            charge_p = abs(float(bess.get("max_charge_w") or 3500))
            discharge_p = abs(float(bess.get("max_discharge_w") or 3500))
            charge_hrs: list[int] = bess.get("charge_hours") or []
            discharge_hrs: list[int] = bess.get("discharge_hours") or []
            battery_specs.append((charge_p, discharge_p, charge_hrs, discharge_hrs))
```

Move this outside the entity loop (before or after) since it reads from panel config, not entities.

- [ ] **Step 8: Run type checker**

Run: `mypy src/span_panel_simulator/dashboard/config_store.py`
Expected: PASS (routes may still fail — fixed in Task 3)

- [ ] **Step 9: Commit**

```
git add src/span_panel_simulator/dashboard/config_store.py
git commit -m "Rewrite ConfigStore battery methods to use top-level bess config"
```

---

### Task 3: Create BESS card template and update routes

**Files:**
- Create: `src/span_panel_simulator/dashboard/templates/partials/bess_card.html`
- Modify: `src/span_panel_simulator/dashboard/templates/dashboard.html`
- Modify: `src/span_panel_simulator/dashboard/templates/partials/entity_edit.html`
- Modify: `src/span_panel_simulator/dashboard/templates/partials/battery_profile_editor.html`
- Modify: `src/span_panel_simulator/dashboard/routes.py`

- [ ] **Step 1: Create `bess_card.html` partial**

Create `src/span_panel_simulator/dashboard/templates/partials/bess_card.html`:

```html
{% if bess_config.enabled is defined and bess_config.enabled %}
<div class="card bess-card" id="bess-card-section">
  <div class="card-header">
    <h2>Battery (GFE) <span class="badge badge-battery">UPSTREAM LUGS</span></h2>
  </div>

  {% if bess_editing %}
  <form hx-put="bess" hx-target="#bess-card-section" hx-swap="innerHTML" hx-disabled-elt="find button">
    <div class="form-grid" style="padding: 0.75rem 1rem;">
      <label>Nameplate Capacity (kWh):
        <input type="number" name="nameplate_capacity_kwh"
               value="{{ bess_config.nameplate_capacity_kwh | default(13.5) }}"
               min="0.1" max="500" step="0.1">
      </label>
      <label>Backup Reserve (%):
        <input type="number" name="backup_reserve_pct"
               value="{{ bess_config.backup_reserve_pct | default(20) }}"
               min="5" max="80" step="5">
      </label>
      <label>Charge Power (W):
        <input type="number" name="max_charge_power"
               value="{{ bess_config.max_charge_w | default(3500) }}"
               min="0" max="20000" step="100">
      </label>
      <label>Discharge Power (W):
        <input type="number" name="max_discharge_power"
               value="{{ bess_config.max_discharge_w | default(3500) }}"
               min="0" max="20000" step="100">
      </label>
    </div>
    <div class="form-actions" style="padding: 0.5rem 1rem;">
      <button type="submit" class="btn btn-sm btn-primary">Save</button>
      <button type="button" class="btn btn-sm"
              hx-get="bess" hx-target="#bess-card-section" hx-swap="innerHTML">Cancel</button>
    </div>
  </form>
  {% else %}
  <div style="padding: 0.75rem 1rem;">
    <div class="bess-summary">
      <span><strong>{{ bess_config.nameplate_capacity_kwh | default(13.5) }}</strong> kWh</span>
      <span>Reserve: <strong>{{ bess_config.backup_reserve_pct | default(20) }}%</strong></span>
      <span>Charge: <strong>{{ bess_config.max_charge_w | default(3500) }}W</strong></span>
      <span>Discharge: <strong>{{ bess_config.max_discharge_w | default(3500) }}W</strong></span>
      <span>Mode: <strong>{{ bess_config.charge_mode | default('self-consumption') }}</strong></span>
    </div>
    {% if not readonly %}
    <div style="margin-top: 0.5rem;">
      <button class="btn btn-sm"
              hx-get="bess/edit" hx-target="#bess-card-section" hx-swap="innerHTML">Edit Settings</button>
      <button class="btn btn-sm"
              hx-get="bess/schedule" hx-target="#bess-card-section" hx-swap="innerHTML">Schedule</button>
    </div>
    {% endif %}
  </div>
  {% endif %}
</div>
{% endif %}
```

- [ ] **Step 2: Include BESS card in dashboard layout**

In `src/span_panel_simulator/dashboard/templates/dashboard.html`, add after the sim-config section (line 34) and before the entity list (line 36):

```html
  <section id="bess-card-section" hx-target="this" hx-swap="innerHTML">
    {% include "partials/bess_card.html" %}
  </section>
```

- [ ] **Step 3: Remove battery fieldset from entity_edit.html**

In `src/span_panel_simulator/dashboard/templates/partials/entity_edit.html`, remove lines 135-162 (the `{% if e.battery_behavior %}` block).

- [ ] **Step 4: Update battery_profile_editor.html for panel-level routes**

In `src/span_panel_simulator/dashboard/templates/partials/battery_profile_editor.html`, replace all entity-based route references:

- `hx-put="entities/{{ entity.id }}/battery-charge-mode"` → `hx-put="bess/charge-mode"`
- `hx-target="#battery-profile-{{ entity.id }}"` → `hx-target="#bess-card-section"`
- `hx-post="entities/{{ entity.id }}/battery-profile/preset"` → `hx-post="bess/schedule/preset"`
- `hx-put="entities/{{ entity.id }}/active-days"` → `hx-put="bess/active-days"`
- `hx-put="entities/{{ entity.id }}/battery-profile"` → `hx-put="bess/schedule"`
- `id="charge-mode-{{ entity.id }}"` → `id="bess-charge-mode"`
- `id="days-{{ entity.id }}"` → `id="bess-days"`

Remove all `{{ entity.id }}` references — the template no longer needs an entity context. Keep `battery_profile`, `battery_charge_mode`, `battery_preset_labels`, `battery_active_preset`, and `active_days` context variables.

- [ ] **Step 5: Add BESS context to `_dashboard_context`**

In `routes.py`, in `_dashboard_context` (line 165), add BESS config to the context:

```python
        "bess_config": store.get_bess_config(),
```

- [ ] **Step 6: Add new BESS route handlers and register routes**

Replace the old entity-based battery routes with panel-level ones. In `routes.py`:

```python
def _bess_card_context(request: web.Request, editing: bool = False, schedule: bool = False) -> dict[str, Any]:
    """Build the BESS card template context."""
    store = _store(request)
    ctx: dict[str, Any] = {
        "bess_config": store.get_bess_config(),
        "bess_editing": editing,
        "readonly": _is_readonly(_ctx(request)),
    }
    if schedule:
        battery_profile = store.get_battery_profile()
        ctx["bess_schedule"] = True
        ctx["battery_profile"] = battery_profile
        ctx["battery_preset_labels"] = _presets(request).battery_labels
        ctx["battery_charge_mode"] = store.get_battery_charge_mode()
        ctx["battery_active_preset"] = match_battery_preset(battery_profile)
        ctx["active_days"] = store.get_bess_active_days()
    return ctx


async def handle_get_bess(request: web.Request) -> web.Response:
    """GET /bess — return BESS card in display mode."""
    return _render("partials/bess_card.html", request, _bess_card_context(request))


async def handle_get_bess_edit(request: web.Request) -> web.Response:
    """GET /bess/edit — return BESS card in edit mode."""
    return _render("partials/bess_card.html", request, _bess_card_context(request, editing=True))


async def handle_put_bess(request: web.Request) -> web.Response:
    """PUT /bess — save BESS settings."""
    data = await request.post()
    _store(request).update_bess_config(dict(data))
    _persist_config(request)
    return _render("partials/bess_card.html", request, _bess_card_context(request))


async def handle_get_bess_schedule(request: web.Request) -> web.Response:
    """GET /bess/schedule — return BESS card with schedule editor."""
    return _render("partials/bess_card.html", request, _bess_card_context(request, schedule=True))


async def handle_put_bess_schedule(request: web.Request) -> web.Response:
    """PUT /bess/schedule — save BESS charge/discharge schedule."""
    data = await request.post()
    hour_modes: dict[int, str] = {}
    for h in range(24):
        key = f"hour_{h}"
        mode = str(data.get(key, "idle"))
        hour_modes[h] = mode if mode in ("charge", "discharge", "idle") else "idle"
    store = _store(request)
    store.update_battery_profile(hour_modes)
    active = _parse_active_days(data)
    if active is not None:
        store.update_bess_active_days(active)
    _persist_config(request)
    return _render("partials/bess_card.html", request, _bess_card_context(request, schedule=True))


async def handle_post_bess_schedule_preset(request: web.Request) -> web.Response:
    """POST /bess/schedule/preset — apply a schedule preset."""
    data = await request.post()
    preset_name = str(data.get("preset", "custom"))
    _store(request).apply_battery_preset(preset_name)
    _persist_config(request)
    return _render("partials/bess_card.html", request, _bess_card_context(request, schedule=True))


async def handle_put_bess_charge_mode(request: web.Request) -> web.Response:
    """PUT /bess/charge-mode — change BESS charge mode."""
    data = await request.post()
    mode = str(data.get("charge_mode", "custom"))
    _store(request).update_battery_charge_mode(mode)
    _persist_config(request)
    return _render("partials/bess_card.html", request, _bess_card_context(request, schedule=True))


async def handle_put_bess_active_days(request: web.Request) -> web.Response:
    """PUT /bess/active-days — update BESS active days."""
    data = await request.post()
    active = _parse_active_days(data)
    if active is not None:
        _store(request).update_bess_active_days(active)
    _persist_config(request)
    return _render("partials/bess_card.html", request, _bess_card_context(request, schedule=True))
```

- [ ] **Step 7: Register new routes and remove old ones**

In the route registration function (around line 506-510), replace:

```python
    # Battery profile
    app.router.add_get("/entities/{id}/battery-profile", handle_get_battery_profile)
    app.router.add_put("/entities/{id}/battery-profile", handle_put_battery_profile)
    app.router.add_post("/entities/{id}/battery-profile/preset", handle_apply_battery_preset)
    app.router.add_put("/entities/{id}/battery-charge-mode", handle_put_battery_charge_mode)
```

With:

```python
    # BESS (panel-level)
    app.router.add_get("/bess", handle_get_bess)
    app.router.add_get("/bess/edit", handle_get_bess_edit)
    app.router.add_put("/bess", handle_put_bess)
    app.router.add_get("/bess/schedule", handle_get_bess_schedule)
    app.router.add_put("/bess/schedule", handle_put_bess_schedule)
    app.router.add_post("/bess/schedule/preset", handle_post_bess_schedule_preset)
    app.router.add_put("/bess/charge-mode", handle_put_bess_charge_mode)
    app.router.add_put("/bess/active-days", handle_put_bess_active_days)
```

Remove the old handler functions: `handle_get_battery_profile`, `handle_put_battery_profile`, `handle_apply_battery_preset`, `handle_put_battery_charge_mode`.

Also remove `_battery_profile_context` (lines 257-269) and the battery-specific section from `_entity_list_context` (lines 218-223).

- [ ] **Step 8: Run type checker and tests**

Run: `mypy src/span_panel_simulator/dashboard/ && pytest tests/ -q`
Expected: PASS

- [ ] **Step 9: Commit**

```
git add src/span_panel_simulator/dashboard/
git commit -m "Add BESS card, panel-level routes, remove entity-based battery handling"
```

---

### Task 4: Update bess_card.html to support schedule view

**Files:**
- Modify: `src/span_panel_simulator/dashboard/templates/partials/bess_card.html`

- [ ] **Step 1: Add schedule view to BESS card**

The BESS card has three states: display, edit (settings), and schedule. Add the schedule state after the edit block and before the display block in `bess_card.html`:

```html
  {% elif bess_schedule is defined and bess_schedule %}
  <div style="padding: 0.75rem 1rem;">
    {% include "partials/battery_profile_editor.html" %}
    <div style="margin-top: 0.5rem;">
      <button class="btn btn-sm"
              hx-get="bess" hx-target="#bess-card-section" hx-swap="innerHTML">Back</button>
    </div>
  </div>
```

Insert this between `{% if bess_editing %}...{% else %}` — making it `{% elif bess_schedule %}`.

- [ ] **Step 2: Run manually to verify**

Start the simulator and verify:
- BESS card appears between sim config and entity list
- "Edit Settings" opens the settings form
- "Schedule" opens the schedule editor
- Save persists to YAML
- Charge mode radio buttons work
- Schedule grid works

- [ ] **Step 3: Commit**

```
git add src/span_panel_simulator/dashboard/templates/partials/bess_card.html
git commit -m "Add schedule view to BESS card"
```

---

### Task 5: Final verification and cleanup

- [ ] **Step 1: Run full type check**

Run: `mypy src/`
Expected: PASS

- [ ] **Step 2: Run full test suite**

Run: `pytest tests/ -v`
Expected: All tests pass

- [ ] **Step 3: Search for orphaned battery_behavior references**

Run: `grep -rn "battery_behavior" src/ tests/ configs/`

Expected: No matches in non-dashboard code. Dashboard should have zero remaining references.

- [ ] **Step 4: Verify entity list no longer shows battery**

Start the simulator and confirm:
- Entity list count reflects only circuits + PV + EVSE
- "Add Entity" dropdown does not include "Battery"
- BESS card is the only place to manage battery settings

- [ ] **Step 5: Commit any cleanup**

```
git add -A
git commit -m "Final cleanup: remove orphaned dashboard battery_behavior references"
```
