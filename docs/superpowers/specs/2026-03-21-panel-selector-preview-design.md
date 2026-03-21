# Panel Selector Preview & Dirty-State Guard

## Problem

The dashboard entity list always shows whichever config was loaded at startup (typically `default_MAIN_16.yaml`). Users cannot preview the circuits in other default templates before cloning. The "Edit" button is hidden for defaults, and clicking on a default row does nothing to the entity list. Users must clone a template blind, then inspect what they got.

## Design

### 1. Panel Row Becomes a Selector

Clicking any panel row in the panels list loads that config into the entity list below.

**Panel row changes (`panels_list_rows.html`):**
- Remove the standalone "Edit" button from non-default rows.
- Make the row itself clickable (the filename or a new "View" affordance). On click, call a JS function that handles the switch (see dirty-state guard below).
- Add a visual indicator for which panel is currently displayed (the existing `active-panel` class and `badge-editing` badge, extended to also cover read-only viewing of defaults).
- Rename the badge: defaults being viewed show "viewing" instead of "editing".

**`handle_load_config` route change:**
- Remove the `startswith("default_")` rejection (lines 917-920 in `routes.py`).
- Allow any config file to be loaded. Read-only behavior for defaults is already enforced by `_is_readonly()` which checks `config_filter.startswith("default_")`.

**Resulting behavior:**
- Default templates: entity list renders read-only (no Add/Edit/Del buttons, no unmapped tabs form) — this already works via the `readonly` template variable.
- User configs: entity list renders fully editable — same as current "Edit" button behavior.

### 2. Dirty-State Tracking (Server-Side)

**ConfigStore changes (`config_store.py`):**
- Add a `_dirty: bool = False` instance attribute, initialized in `__init__`.
- Set `_dirty = True` in every mutation method:
  - `update_entity`
  - `add_entity` / `add_entity_from_tabs`
  - `delete_entity`
  - `update_panel_config`
  - `update_simulation_params`
  - `update_entity_profile`
  - `apply_preset`
  - `update_battery_profile` / `update_battery_charge_mode` / `apply_battery_preset`
  - `update_evse_schedule` / `apply_evse_preset`
  - `update_active_days`
  - `toggle_user_modified`
- Note: setting `_dirty = True` is idempotent. Composite methods (e.g. `apply_preset` calls `update_entity_profile`) may set it multiple times — this is harmless.
- Clear `_dirty = False` in:
  - `load_from_file`
  - `load_from_yaml`
- Add a `save_to_file(path: Path)` method that calls `export_yaml()`, writes the file, and clears `_dirty`. Refactor `handle_save_reload` in routes.py to call `store.save_to_file(path)` instead of doing export + write inline.
- Expose via `@property def dirty(self) -> bool`.

**New route (`routes.py`):**
- `GET /check-dirty` — returns `{"dirty": true/false}`. This is a deliberate departure from the HTMX-first pattern used elsewhere: the response drives a JS `confirm()` dialog, not an HTMX swap.

**Guard on `handle_save_reload`:**
- Add an explicit guard: if `config_filter` starts with `default_`, return 400. This prevents accidental overwrite of default templates when viewing them (the UI already hides Save+Reload when readonly, but a server-side guard is defense-in-depth).

**Import flow (`handle_import`):**
- `handle_import` calls `load_from_yaml` which clears `_dirty`. This is intentional — importing a file is an explicit user action that replaces the entire config. The import flow does not need a dirty guard because the user explicitly chose to import, and the replaced state becomes the new baseline.

### 3. Client-Side Switch Flow

**New JS function (`panels_list_rows.html` or a script block):**

```
switchPanel(filename):
  1. fetch('/check-dirty')
  2. if not dirty:
       POST /load-config {config_file: filename}  → HX-Redirect reloads page
  3. if dirty:
       show confirm dialog:
         "You have unsaved changes. Save and switch, Discard and switch, or Cancel?"
         - Save: POST /save-reload, then POST /load-config
         - Discard: POST /load-config directly (overwrites in-memory state)
         - Cancel: do nothing
```

The confirm dialog uses a simple `confirm()` or a small inline prompt — no modal library needed. A three-way choice can be done as two sequential confirms or a single custom dialog. Simplest approach: use `confirm("You have unsaved changes. OK to discard?")`. If they cancel, do nothing. If they confirm, switch. For saving, they can use the existing Save+Reload button before switching.

**Simplified two-option approach:**
- `confirm("You have unsaved changes that will be lost. Switch anyway?")` — OK discards, Cancel stays.
- The existing Save+Reload button remains available if they want to save first.
- This avoids a custom three-option dialog and keeps the implementation minimal.

### 4. Visual Feedback

**Active panel indication:**
- The currently viewed panel row gets the `active-panel` CSS class (already exists).
- Badge text: "editing" for user configs, "viewing" for defaults. The template uses `p.is_default` (already available) to conditionally render the badge text.
- The `config_filter` context variable already tracks which file is loaded; `_all_panels` already sets `active` based on this.

### 5. Files Changed

| File | Change |
|------|--------|
| `dashboard/config_store.py` | Add `_dirty` flag, set in mutations, clear on load/save; add `save_to_file` method |
| `dashboard/routes.py` | Remove default rejection in `handle_load_config`; add `GET /check-dirty` route; add default guard to `handle_save_reload`; refactor save-reload to use `store.save_to_file()` |
| `dashboard/templates/partials/panels_list_rows.html` | Make rows clickable, remove Edit button, add `switchPanel` JS, update badge text using `p.is_default` |

### 6. What Does NOT Change

- Entity list template (`entity_list.html`) — already respects `readonly`
- Entity row template (`entity_row.html`) — already hides Edit/Del when `readonly`
- Clone, Start, Stop, Restart buttons — unchanged
- Modeling mode — unchanged
- Unmapped tabs card — already hidden when `readonly`

## Edge Cases

- **Switching to the already-active panel:** No-op, skip the dirty check.
- **Switching while a panel is running:** Allowed. Viewing a config doesn't affect the running engine. The entity list shows the config file contents, not the live engine state.
- **Dirty state after save-reload:** Cleared. The `store.save_to_file()` call resets `_dirty`.
- **Browser refresh:** ConfigStore reloads from file via the bootstrap path, which calls `load_from_file` and clears `_dirty`. Any unsaved in-memory changes are lost — same as current behavior.
- **Clone of active config with dirty edits:** Clone copies the in-memory state (via `export_yaml`), so the clone captures unsaved edits. This is the existing behavior and is intentional — the user explicitly chose to clone what they see.
- **`switchPanel` error handling:** If `/load-config` returns an error (e.g. file deleted from disk between list render and click), the JS function should display a flash message rather than silently failing.
- **Save-reload while viewing a default:** The server-side guard on `handle_save_reload` rejects this with a 400. The UI already hides the button via the `readonly` flag, so this is defense-in-depth only.
