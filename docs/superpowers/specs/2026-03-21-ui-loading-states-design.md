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

### Error handling

Both paths automatically clear loading state on error:
- HTMX removes `.htmx-request` and re-enables `hx-disabled-elt` targets on
  completion regardless of HTTP status.
- `busyFetch()` uses `.finally()` so `.busy` is removed on both success and
  failure.

No special error-path handling is needed.

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
.busy .btn,
button.htmx-request,
button.busy {
  cursor: wait;
}

.htmx-indicator {
  margin-left: 0.5em;
}
```

Note: for standalone buttons that have `hx-*` attributes directly (not inside a
form), HTMX adds `.htmx-request` to the button itself. The `button.htmx-request`
selector handles this case — the child-button selectors only match buttons
*inside* a form with the class.

This gives every HTMX form automatic visual feedback with zero template changes.

### 2. `hx-disabled-elt` Attributes

Add `hx-disabled-elt="find button"` (or `hx-disabled-elt="this"` for
single-element triggers) to all user-initiated HTMX forms and buttons. This
adds the native `disabled` attribute during requests, blocking keyboard
submission (Enter key) in addition to the CSS pointer-events block.

Note: `hx-disabled-elt="this"` on non-form elements like `<label>` or `<span>`
has no HTML effect (only `<button>`, `<input>`, `<select>`, `<textarea>` support
`disabled`). For those elements the CSS `.htmx-request` layer provides the
actual blocking.

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

**Forms with `<label>`-as-button (use `hx-disabled-elt="find label"`):**

| Template                        | Element                              |
|---------------------------------|--------------------------------------|
| `running_panels.html`           | import form (trigger is a `<label>` styled as btn, not a `<button>`) |

**Single-element triggers (use `hx-disabled-elt="this"`):**

| Template                        | Element                              |
|---------------------------------|--------------------------------------|
| `entity_edit.html`              | Cancel button (hx-get)               |
| `entity_row.html`               | edit button (hx-get)                 |
| `entity_row.html`               | delete button (hx-delete)            |
| `entity_row.html`               | toggle-replay button (hx-post)       |
| `entity_row.html`               | restore-recorder button (hx-post)    |
| `profile_editor.html`           | preset Apply button (hx-post)        |
| `battery_profile_editor.html`   | preset Apply button (hx-post)        |
| `battery_profile_editor.html`   | charge mode labels (hx-put on `<label>` elements) |
| `evse_schedule.html`            | preset Apply button (hx-post)        |
| `sim_config.html`               | save-reload button (hx-post)         |
| `panels_list_rows.html`         | Restart button (hx-post)             |
| `panels_list_rows.html`         | Stop button (hx-post)                |
| `panels_list_rows.html`         | Start button (hx-post)               |
| `panels_list_rows.html`         | Del button (hx-post delete-config)   |
| `panel_source.html`             | sync button (hx-post)                |

**Excluded HTMX triggers:**

| Element                          | Rationale                            |
|----------------------------------|--------------------------------------|
| Day-picker checkboxes (active-days hx-put in profile_editor, battery_profile_editor, evse_schedule) | Fire-and-forget with `hx-swap="none"`, instant round-trip, no visible swap. Dimming a checkbox after click would feel broken. |
| `panels-list` (hx-get every 5s)  | Background polling                  |

**Programmatic `htmx.ajax()` calls:**

The Clone button in `panels_list_rows.html` uses inline `htmx.ajax('POST',
'clone', ...)`. Programmatic `htmx.ajax()` does not auto-add `.htmx-request`
to any element. Convert this to use `busyFetch()` with manual `.busy` class on
the Clone button.

The `switchPanel()` function uses `htmx.ajax('POST', 'load-config', ...)` which
triggers a full page reload via `HX-Redirect`. No loading state needed — the
browser's own navigation indicator covers this, and the page is replaced
immediately.

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
| `panel_config.html`     | `fetch-weather` (user-click path only) | weather status text element |
| `modeling_view.html`    | `modeling-data?horizon=...` (user-initiated horizon change) | modeling container |
| `pv_profile.html`       | PV curve data fetch           | PV profile container         |
| `panels_list_rows.html` | Clone button `htmx.ajax()` call | Clone button               |

**Excluded** (background/polling/auto-triggered):

- `power-summary` — 3s polling loop
- `panels-list` — 5s HTMX polling
- `check-dirty` — internal dirty-check, not user-visible
- `discovered-panels` — auto-discovery on page load
- `geocode?q=...` — debounced input handler; busyFetch would block typing and
  apply wait cursor mid-keystroke. The dropdown results area already provides
  implicit feedback.
- `fetch-weather` on page load — auto-triggered when coordinates already exist,
  not a user action
- `modeling_view.html` save-reload auto-refresh — triggered by `htmx:afterSwap`
  event after entity edits in modeling mode, not a direct user action. The
  entity form itself already has loading state.

### 4. Interaction with existing indicators

The clone-from-panel form in `clone_panel.html` already has `hx-indicator=
"#clone-spinner"` with a spinner element. HTMX's built-in `.htmx-indicator`
uses `opacity: 0` → `opacity: 1` transitions during requests. The new CSS
treatment (`.htmx-request` dimming + `hx-disabled-elt`) is additive — the
spinner continues to work as before, and the form also gets the dim + disabled
treatment.

### 5. Nested HTMX triggers

The `entity_edit.html` form includes sub-templates (`pv_profile.html`,
`evse_schedule.html`, `battery_profile_editor.html`, `profile_editor.html`)
that have their own `hx-*` attributes. HTMX handles these independently — each
element gets its own `.htmx-request` class during its own request. A profile
preset button firing does not affect the parent entity form and vice versa.

## Files Changed

| File | Change |
|------|--------|
| `dashboard.css` | Add `.htmx-request`, `.busy`, `button.htmx-request`, `.htmx-indicator` rules |
| `base.html` | Add `busyFetch()` function |
| `entity_edit.html` | Add `hx-disabled-elt` to form and cancel button |
| `entity_row.html` | Add `hx-disabled-elt` to edit/delete/toggle/restore buttons |
| `entity_list.html` | Add `hx-disabled-elt` to add-entity and unmapped forms |
| `profile_editor.html` | Add `hx-disabled-elt` to form and preset button |
| `battery_profile_editor.html` | Add `hx-disabled-elt` to form, preset, charge mode labels |
| `evse_schedule.html` | Add `hx-disabled-elt` to form and preset button |
| `panel_config.html` | Add `hx-disabled-elt` to form; convert weather fetch to busyFetch |
| `sim_config.html` | Add `hx-disabled-elt` to form and save-reload button |
| `clone_panel.html` | Add `hx-disabled-elt` to form (existing indicator preserved) |
| `running_panels.html` | Add `hx-disabled-elt="find label"` to import form |
| `panels_list_rows.html` | Add `hx-disabled-elt` to lifecycle buttons; convert Clone to busyFetch |
| `panel_source.html` | Add `hx-disabled-elt` to sync button |
| `runtime_controls.html` | Convert 5 fetch calls to busyFetch |
| `modeling_view.html` | Convert 1 user-initiated fetch call to busyFetch |
| `pv_profile.html` | Convert 1 fetch call to busyFetch |
