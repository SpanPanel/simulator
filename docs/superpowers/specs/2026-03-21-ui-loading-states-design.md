# UI Loading States Design

## Problem

The dashboard has no loading indicators or interaction blocking during backend
calls. Users can submit forms multiple times, have no feedback that an action is
in progress, and see no wait cursor during operations.

## Approach

**CSS-only HTMX classes + fetch wrapper** — leverage HTMX's built-in
`.htmx-request` class for all HTMX-driven forms, add a thin `busyFetch()`
wrapper for manual `fetch()` call sites, and unify both under the same visual
treatment.

### Goals

- Wait cursor and dimming on any element making a backend call
- Prevent double-submission via `hx-disabled-elt` and `pointer-events: none`
- No new dependencies — CSS + ~8 lines of JS
- Polling/background fetches are excluded (only user-initiated actions)

## Design

### 1. CSS Layer (`dashboard.css`)

Add rules for `.htmx-request` (auto-added by HTMX during requests) and `.busy`
(added manually by `busyFetch()`):

```css
.htmx-request,
.busy {
  cursor: wait;
  pointer-events: none;
  opacity: 0.7;
}

.htmx-request button,
.htmx-request .btn,
.busy button,
.busy .btn {
  cursor: wait;
}

.htmx-indicator {
  margin-left: 0.5em;
}
```

This gives every HTMX form automatic visual feedback with zero template changes.

### 2. `hx-disabled-elt` Attributes

Add `hx-disabled-elt="find button"` (or `hx-disabled-elt="this"` for
single-button elements) to all user-initiated HTMX forms and buttons. This
adds the native `disabled` attribute during requests, blocking keyboard
submission (Enter key) in addition to the CSS pointer-events block.

**Forms (use `hx-disabled-elt="find button"`):**

| Template                        | Element                              |
|---------------------------------|--------------------------------------|
| `entity_edit.html`              | main entity form (hx-put)            |
| `profile_editor.html`           | profile form (hx-put)                |
| `battery_profile_editor.html`   | battery profile form (hx-put)        |
| `evse_schedule.html`            | EVSE schedule form (hx-put)          |
| `panel_config.html`             | panel config form (hx-put)           |
| `sim_config.html`               | sim params form (hx-put)             |
| `entity_list.html`              | add entity form (hx-post)            |
| `entity_list.html`              | unmapped form (hx-post)              |
| `clone_panel.html`              | clone form (hx-post)                 |
| `running_panels.html`           | import form (hx-post)                |

**Single-element triggers (use `hx-disabled-elt="this"`):**

| Template                        | Element                              |
|---------------------------------|--------------------------------------|
| `entity_edit.html`              | Cancel button (hx-get)               |
| `entity_row.html`               | edit button (hx-get)                 |
| `entity_row.html`               | delete button (hx-delete)            |
| `entity_row.html`               | toggle-replay button (hx-post)       |
| `entity_row.html`               | restore-recorder button (hx-post)    |
| `profile_editor.html`           | preset button (hx-post)              |
| `battery_profile_editor.html`   | preset button (hx-post)              |
| `battery_profile_editor.html`   | charge mode select (hx-put)          |
| `evse_schedule.html`            | preset button (hx-post)              |
| `sim_config.html`               | save-reload button (hx-post)         |
| `panels_list_rows.html`         | lifecycle buttons (restart/stop/etc.) |
| `panel_source.html`             | sync button (hx-post)                |

### 3. `busyFetch()` Wrapper (`base.html`)

A global function added to the existing `<script>` block in `base.html`:

```javascript
function busyFetch(trigger, url, options) {
  trigger.classList.add('busy');
  return fetch(url, options).finally(function() {
    trigger.classList.remove('busy');
  });
}
```

**Call sites to convert** (user-initiated only):

| Template                | Call                          | Trigger element              |
|-------------------------|-------------------------------|------------------------------|
| `runtime_controls.html` | `set-sim-time`                | time slider container        |
| `runtime_controls.html` | `set-acceleration`            | acceleration slider container|
| `runtime_controls.html` | `set-grid-state`              | grid state toggle            |
| `runtime_controls.html` | `set-grid-islandable`         | islandable toggle            |
| `runtime_controls.html` | `entities/.../relay`          | relay status dot             |
| `panel_config.html`     | `geocode?q=...`               | location search input        |
| `panel_config.html`     | `fetch-weather?...`           | weather fetch trigger        |
| `modeling_view.html`    | `modeling-data?horizon=...`   | modeling container           |
| `modeling_view.html`    | `save-reload` + `modeling-data` | eBus Energy button         |
| `pv_profile.html`       | PV curve data fetch           | PV profile container         |

**Excluded** (background/polling):

- `power-summary` — 3s polling loop
- `panels-list` — 5s HTMX polling
- `check-dirty` — internal dirty-check
- `discovered-panels` — auto-discovery on page load

## Files Changed

| File | Change |
|------|--------|
| `dashboard.css` | Add `.htmx-request`, `.busy`, `.htmx-indicator` rules |
| `base.html` | Add `busyFetch()` function |
| `entity_edit.html` | Add `hx-disabled-elt` to form and cancel button |
| `entity_row.html` | Add `hx-disabled-elt` to edit/delete/toggle/restore buttons |
| `entity_list.html` | Add `hx-disabled-elt` to add-entity and unmapped forms |
| `profile_editor.html` | Add `hx-disabled-elt` to form and preset button |
| `battery_profile_editor.html` | Add `hx-disabled-elt` to form, preset, charge mode |
| `evse_schedule.html` | Add `hx-disabled-elt` to form and preset button |
| `panel_config.html` | Add `hx-disabled-elt` to form; convert geocode/weather to busyFetch |
| `sim_config.html` | Add `hx-disabled-elt` to form and save-reload button |
| `clone_panel.html` | Add `hx-disabled-elt` to form |
| `running_panels.html` | Add `hx-disabled-elt` to import form |
| `panels_list_rows.html` | Add `hx-disabled-elt` to lifecycle buttons |
| `panel_source.html` | Add `hx-disabled-elt` to sync button |
| `runtime_controls.html` | Convert 5 fetch calls to busyFetch |
| `modeling_view.html` | Convert 2 fetch calls to busyFetch |
| `pv_profile.html` | Convert 1 fetch call to busyFetch |
