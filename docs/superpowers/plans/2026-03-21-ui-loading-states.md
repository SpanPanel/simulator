# UI Loading States Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add wait cursors, dimming, and double-submit prevention to all user-initiated backend calls in the dashboard.

**Architecture:** CSS rules for `.htmx-request` and `.busy` classes provide visual feedback (opacity + wait cursor + pointer-events block). HTMX's `hx-disabled-elt` attribute adds native `disabled` to form controls during requests. A global `busyFetch()` wrapper applies the same `.busy` treatment to manual `fetch()` calls.

**Tech Stack:** HTMX (existing), vanilla CSS, vanilla JS

**Spec:** `docs/superpowers/specs/2026-03-21-ui-loading-states-design.md`

---

### File Structure

| File | Responsibility | Change type |
|------|---------------|-------------|
| `src/span_panel_simulator/dashboard/static/dashboard.css` | Loading state visual styles | Modify (append) |
| `src/span_panel_simulator/dashboard/templates/base.html` | `busyFetch()` global function | Modify |
| `src/span_panel_simulator/dashboard/templates/partials/entity_edit.html` | Entity form disabled-elt | Modify |
| `src/span_panel_simulator/dashboard/templates/partials/entity_row.html` | Row action buttons disabled-elt | Modify |
| `src/span_panel_simulator/dashboard/templates/partials/entity_list.html` | Add entity forms disabled-elt | Modify |
| `src/span_panel_simulator/dashboard/templates/partials/profile_editor.html` | Profile form + preset disabled-elt | Modify |
| `src/span_panel_simulator/dashboard/templates/partials/battery_profile_editor.html` | Battery form + preset + charge mode disabled-elt | Modify |
| `src/span_panel_simulator/dashboard/templates/partials/evse_schedule.html` | EVSE form + preset disabled-elt | Modify |
| `src/span_panel_simulator/dashboard/templates/partials/panel_config.html` | Config form disabled-elt + weather busyFetch | Modify |
| `src/span_panel_simulator/dashboard/templates/partials/sim_config.html` | Sim form + save-reload disabled-elt | Modify |
| `src/span_panel_simulator/dashboard/templates/partials/clone_panel.html` | Clone form disabled-elt | Modify |
| `src/span_panel_simulator/dashboard/templates/partials/running_panels.html` | Import form disabled-elt | Modify |
| `src/span_panel_simulator/dashboard/templates/partials/panels_list_rows.html` | Lifecycle + clone buttons disabled-elt/busyFetch | Modify |
| `src/span_panel_simulator/dashboard/templates/partials/panel_source.html` | Sync button disabled-elt | Modify |
| `src/span_panel_simulator/dashboard/templates/partials/runtime_controls.html` | Slider/toggle busyFetch conversions | Modify |
| `src/span_panel_simulator/dashboard/templates/partials/modeling_view.html` | Horizon change busyFetch | Modify |
| `src/span_panel_simulator/dashboard/templates/partials/pv_profile.html` | PV curve busyFetch | Modify |

---

### Task 1: CSS loading state rules + busyFetch function

**Files:**
- Modify: `src/span_panel_simulator/dashboard/static/dashboard.css:1328` (append after last line)
- Modify: `src/span_panel_simulator/dashboard/templates/base.html:42-43` (add function before closing `</script>`)

- [ ] **Step 1: Add CSS rules to dashboard.css**

Append at the end of `dashboard.css`:

```css
/* Loading / busy state — applied automatically by HTMX (.htmx-request)
   or manually via busyFetch() (.busy) */
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

- [ ] **Step 2: Add busyFetch function to base.html**

In `base.html`, add this function inside the existing `<script>` block, between the `toggleRandomParams` function (ending line 42) and the theme switcher IIFE (starting line 44):

```javascript
    // Busy-state wrapper for manual fetch() calls (non-HTMX).
    // Adds .busy class to trigger element during request.
    function busyFetch(trigger, url, options) {
      trigger.classList.add('busy');
      return fetch(url, options).finally(function() {
        trigger.classList.remove('busy');
      });
    }
```

- [ ] **Step 3: Verify manually**

Open the dashboard in a browser. Confirm:
- No visual regressions (nothing should look different yet — no requests in flight at idle)
- `busyFetch` is defined: open browser console, type `busyFetch` — should show the function

- [ ] **Step 4: Commit**

```bash
git add src/span_panel_simulator/dashboard/static/dashboard.css \
        src/span_panel_simulator/dashboard/templates/base.html
git commit -m "feat: add CSS loading states and busyFetch wrapper"
```

---

### Task 2: hx-disabled-elt on entity forms and row actions

**Files:**
- Modify: `src/span_panel_simulator/dashboard/templates/partials/entity_edit.html:5,162`
- Modify: `src/span_panel_simulator/dashboard/templates/partials/entity_row.html:12,19,29,33`
- Modify: `src/span_panel_simulator/dashboard/templates/partials/entity_list.html:11,46`

- [ ] **Step 1: entity_edit.html — form and cancel button**

On line 5, add `hx-disabled-elt="find button"` to the `<form>`:

```html
  <form hx-put="entities/{{ e.id }}" hx-target="#entity-list-section" hx-swap="innerHTML"
        hx-disabled-elt="find button"
        onchange="window._formDirty=true;this.querySelector('#_dirty').value='1'"
```

On line 162, add `hx-disabled-elt="this"` to the Cancel button:

```html
      <button type="button" class="btn btn-secondary"
              hx-get="entities" hx-target="#entity-list-section" hx-swap="innerHTML"
              hx-disabled-elt="this">Cancel</button>
```

- [ ] **Step 2: entity_row.html — toggle-replay, restore-recorder, edit, delete**

On line 11 (the `<span>` start), add `hx-disabled-elt="this"` to the toggle-replay span (note: `disabled` has no HTML effect on `<span>` — CSS `.htmx-request` provides the actual blocking):

```html
  <span class="badge badge-data-source {{ 'badge-override' if entity.user_modified else 'badge-replay' }}"
        hx-post="entities/{{ entity.id }}/toggle-replay"
        hx-target="#entity-list-section"
        hx-swap="innerHTML"
        hx-disabled-elt="this"
```

On line 18 (the `<span>` start), add `hx-disabled-elt="this"` to the restore-recorder span (same CSS-only note):

```html
  <span class="badge badge-data-source badge-restore"
        hx-post="entities/{{ entity.id }}/restore-recorder"
        hx-target="#entity-list-section"
        hx-swap="innerHTML"
        hx-disabled-elt="this"
```

On line 29, add `hx-disabled-elt="this"` to the Edit button:

```html
  <button class="btn btn-sm"
          hx-get="entities/{{ entity.id }}/edit"
          hx-target="#entity-list-section"
          hx-swap="innerHTML"
          hx-disabled-elt="this">Edit</button>
```

On line 33, add `hx-disabled-elt="this"` to the Del button:

```html
  <button class="btn btn-sm btn-danger"
          hx-delete="entities/{{ entity.id }}"
          hx-target="#entity-list-section"
          hx-swap="innerHTML"
          hx-confirm="Delete {{ entity.name }}?"
          hx-disabled-elt="this">Del</button>
```

- [ ] **Step 3: entity_list.html — add-entity form and unmapped form**

On line 11, add `hx-disabled-elt="find button"` to the add-entity form:

```html
      <form id="add-entity-form" hx-post="entities" hx-target="#entity-list-section" hx-swap="innerHTML"
            hx-disabled-elt="find button">
```

On line 46, add `hx-disabled-elt="find button"` to the unmapped form:

```html
  <form id="unmapped-form" hx-post="entities/from-tabs" hx-target="#entity-list-section" hx-swap="innerHTML"
        hx-disabled-elt="find button">
```

- [ ] **Step 4: Verify manually**

Open the dashboard, edit an entity, click Save. Verify the Save and Cancel buttons become disabled and the form dims during the request.

- [ ] **Step 5: Commit**

```bash
git add src/span_panel_simulator/dashboard/templates/partials/entity_edit.html \
        src/span_panel_simulator/dashboard/templates/partials/entity_row.html \
        src/span_panel_simulator/dashboard/templates/partials/entity_list.html
git commit -m "feat: add hx-disabled-elt to entity forms and row actions"
```

---

### Task 3: hx-disabled-elt on profile, battery, and EVSE editors

**Files:**
- Modify: `src/span_panel_simulator/dashboard/templates/partials/profile_editor.html:15-16,35`
- Modify: `src/span_panel_simulator/dashboard/templates/partials/battery_profile_editor.html:13,39,65`
- Modify: `src/span_panel_simulator/dashboard/templates/partials/evse_schedule.html:11-12,32`

- [ ] **Step 1: profile_editor.html — preset button and profile form**

On line 15, add `hx-disabled-elt="this"` to the Apply button:

```html
    <button type="button" class="btn btn-sm"
            hx-post="entities/{{ entity.id }}/profile/preset"
            hx-target="#profile-{{ entity.id }}"
            hx-swap="innerHTML"
            hx-include="#preset-form-{{ entity.id }}"
            hx-disabled-elt="this">Apply</button>
```

On line 35, add `hx-disabled-elt="find button"` to the profile form:

```html
  <form hx-put="entities/{{ entity.id }}/profile" hx-target="#profile-{{ entity.id }}" hx-swap="innerHTML"
        hx-disabled-elt="find button">
```

- [ ] **Step 2: battery_profile_editor.html — charge mode labels, preset labels, profile form**

Note: `hx-disabled-elt="this"` on `<label>` elements has no native `disabled` effect per the HTML spec. The CSS `.htmx-request` layer provides the actual interaction blocking for these elements.

On line 13, add `hx-disabled-elt="this"` to each charge mode `<label>`:

```html
      <label class="charge-mode-option{% if battery_charge_mode == mode %} active{% endif %}"
             hx-put="entities/{{ entity.id }}/battery-charge-mode"
             hx-target="#battery-profile-{{ entity.id }}"
             hx-swap="innerHTML"
             hx-vals='{"charge_mode": "{{ mode }}"}'
             hx-disabled-elt="this">
```

On line 39, add `hx-disabled-elt="this"` to each preset `<label>`:

```html
      <label class="charge-mode-option{% if battery_active_preset == key %} active{% endif %}"
             hx-post="entities/{{ entity.id }}/battery-profile/preset"
             hx-target="#battery-profile-{{ entity.id }}"
             hx-swap="innerHTML"
             hx-vals='{"preset": "{{ key }}"}'
             hx-disabled-elt="this">
```

On line 65, add `hx-disabled-elt="find button"` to the battery profile form:

```html
  <form hx-put="entities/{{ entity.id }}/battery-profile"
        hx-target="#battery-profile-{{ entity.id }}" hx-swap="innerHTML"
        hx-disabled-elt="find button">
```

- [ ] **Step 3: evse_schedule.html — preset button and schedule form**

On line 11, add `hx-disabled-elt="this"` to the Apply button:

```html
    <button type="button" class="btn btn-sm"
            hx-post="entities/{{ e.id }}/evse-schedule/preset"
            hx-target="#evse-schedule-{{ e.id }}" hx-swap="outerHTML"
            hx-include="#evse-preset-form-{{ e.id }}"
            hx-disabled-elt="this">Apply</button>
```

On line 32, add `hx-disabled-elt="find button"` to the EVSE form:

```html
  <form hx-put="entities/{{ e.id }}/evse-schedule"
        hx-target="#evse-schedule-{{ e.id }}" hx-swap="outerHTML"
        hx-disabled-elt="find button">
```

- [ ] **Step 4: Verify manually**

Open an entity with a profile editor. Click Apply on a preset — verify button disables and area dims. Click Save on the profile form — verify buttons disable.

- [ ] **Step 5: Commit**

```bash
git add src/span_panel_simulator/dashboard/templates/partials/profile_editor.html \
        src/span_panel_simulator/dashboard/templates/partials/battery_profile_editor.html \
        src/span_panel_simulator/dashboard/templates/partials/evse_schedule.html
git commit -m "feat: add hx-disabled-elt to profile, battery, and EVSE editors"
```

---

### Task 4: hx-disabled-elt on config, sim, clone, import, lifecycle, and sync

**Files:**
- Modify: `src/span_panel_simulator/dashboard/templates/partials/panel_config.html:3`
- Modify: `src/span_panel_simulator/dashboard/templates/partials/sim_config.html:7-8,14`
- Modify: `src/span_panel_simulator/dashboard/templates/partials/clone_panel.html:18`
- Modify: `src/span_panel_simulator/dashboard/templates/partials/running_panels.html:5-6`
- Modify: `src/span_panel_simulator/dashboard/templates/partials/panels_list_rows.html:29,35,43,49`
- Modify: `src/span_panel_simulator/dashboard/templates/partials/panel_source.html:14`

- [ ] **Step 1: panel_config.html — config form**

On line 3, add `hx-disabled-elt="find button"`:

```html
  <form hx-put="panel-config" hx-target="#panel-config-section" hx-swap="innerHTML"
        hx-disabled-elt="find button">
```

- [ ] **Step 2: sim_config.html — save-reload button and sim-params form**

On line 7, add `hx-disabled-elt="this"` to the Save & Reload button:

```html
      <button class="btn btn-sm btn-primary"
              hx-post="save-reload" hx-target="#flash-area" hx-swap="innerHTML"
              hx-disabled-elt="this">
```

On line 14, add `hx-disabled-elt="find button"` to the sim-params form:

```html
  <form hx-put="sim-params" hx-target="#sim-config-section" hx-swap="innerHTML"
        hx-disabled-elt="find button">
```

- [ ] **Step 3: clone_panel.html — clone form**

On line 18, add `hx-disabled-elt="find button"`:

```html
  <form hx-post="clone-from-panel"
        hx-target="#clone-panel-section"
        hx-swap="outerHTML"
        hx-indicator="#clone-spinner"
        hx-disabled-elt="find button">
```

- [ ] **Step 4: running_panels.html — import form**

On line 5, add `hx-disabled-elt="find label"` (the trigger is a `<label>` styled as a button, not a `<button>`):

```html
    <form method="post" enctype="multipart/form-data"
          hx-post="import" hx-target="#main-content" hx-encoding="multipart/form-data"
          hx-disabled-elt="find label"
          style="display:inline; margin-left:auto">
```

- [ ] **Step 5: panels_list_rows.html — lifecycle buttons**

Add `hx-disabled-elt="this"` to each lifecycle button.

Restart button (line 29):
```html
  <button class="btn btn-sm btn-secondary"
          onclick="event.stopPropagation()"
          hx-post="restart-panel"
          hx-vals='{"filename": "{{ p.filename }}"}'
          hx-target="#flash-area" hx-swap="innerHTML"
          hx-disabled-elt="this"
          title="Restart engine">Restart</button>
```

Stop button (line 35):
```html
  <button class="btn btn-sm btn-danger"
          onclick="event.stopPropagation()"
          hx-post="stop-panel"
          hx-vals='{"filename": "{{ p.filename }}"}'
          hx-target="#flash-area" hx-swap="innerHTML"
          hx-disabled-elt="this"
          title="Stop engine">Stop</button>
```

Start button (line 42):
```html
  <button class="btn btn-sm btn-primary"
          onclick="event.stopPropagation()"
          hx-post="start-panel"
          hx-vals='{"filename": "{{ p.filename }}"}'
          hx-target="#flash-area" hx-swap="innerHTML"
          hx-disabled-elt="this"
          title="Start engine">Start</button>
```

Del button (line 49):
```html
  <button class="btn btn-sm btn-danger"
          onclick="event.stopPropagation()"
          hx-post="delete-config"
          hx-vals='{"filename": "{{ p.filename }}"}'
          hx-target="#flash-area" hx-swap="innerHTML"
          hx-confirm="Delete {{ p.filename }}? This cannot be undone."
          hx-disabled-elt="this"
          title="Delete config file">Del</button>
```

- [ ] **Step 6: panel_source.html — sync button**

On line 14, add `hx-disabled-elt="this"`:

```html
  <button class="btn btn-sm btn-primary"
          hx-post="sync-panel-source"
          hx-target="#panel-source-section"
          hx-swap="outerHTML"
          hx-disabled-elt="this">
```

- [ ] **Step 7: Verify manually**

Test: Click Save & Reload in sim config — button disables. Click Clone — form disables and spinner shows. Click Restart/Stop/Start on a panel — button disables. Click Del on a config — button disables after confirm dialog.

- [ ] **Step 8: Commit**

```bash
git add src/span_panel_simulator/dashboard/templates/partials/panel_config.html \
        src/span_panel_simulator/dashboard/templates/partials/sim_config.html \
        src/span_panel_simulator/dashboard/templates/partials/clone_panel.html \
        src/span_panel_simulator/dashboard/templates/partials/running_panels.html \
        src/span_panel_simulator/dashboard/templates/partials/panels_list_rows.html \
        src/span_panel_simulator/dashboard/templates/partials/panel_source.html
git commit -m "feat: add hx-disabled-elt to config, sim, clone, import, lifecycle, sync"
```

---

### Task 5: Convert runtime_controls.html fetch calls to busyFetch

**Files:**
- Modify: `src/span_panel_simulator/dashboard/templates/partials/runtime_controls.html:234,260,279,296,320,527-572`

- [ ] **Step 1: set-sim-time (line 234)**

The `sendSimDateTime()` function fires on slider `end` event. The slider container already has an id. Replace `fetch(` with `busyFetch(` using the time slider container as trigger:

```javascript
  function sendSimDateTime() {
    var md = window.getSimDate();
    var mins = Math.round(Number(timeSlider.noUiSlider.get()));
    var hh = ('0' + Math.floor(mins / 60)).slice(-2);
    var mm = ('0' + (mins % 60)).slice(-2);
    var year = md.year || currentSimYear;
    var iso = year + '-' + ('0' + md.month).slice(-2) + '-' + ('0' + md.day).slice(-2) + 'T' + hh + ':' + mm + ':00';
    busyFetch(timeSlider, 'set-sim-time', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({time: iso})
    });
  }
```

- [ ] **Step 2: set-acceleration (line 260)**

Replace `fetch(` with `busyFetch(accelSlider,`:

```javascript
  accelSlider.noUiSlider.on('end', function(values) {
    var idx = Math.round(Number(values[0]));
    var accel = ACCEL_STOPS[idx];
    busyFetch(accelSlider, 'set-acceleration', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({acceleration: accel})
    });
  });
```

- [ ] **Step 3: set-grid-state (line 279)**

Replace `fetch(` with `busyFetch(gridBtn,`. **Critical:** the existing code at lines 284-291 overwrites `gridBtn.className` with a full string, which would strip the `.busy` class added by `busyFetch()`. Convert the immediate visual feedback from `className =` to `classList` manipulation so `.busy` is preserved:

```javascript
  gridBtn.addEventListener('click', function() {
    var isOnline = gridBtn.classList.contains('grid-online');
    busyFetch(gridBtn, 'set-grid-state', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({online: !isOnline})
    });
    // Immediate visual feedback (use classList to preserve .busy)
    if (isOnline) {
      gridBtn.classList.remove('grid-online');
      gridBtn.classList.add('grid-offline');
      gridBtn.textContent = 'Grid Offline';
    } else {
      gridBtn.classList.remove('grid-offline');
      gridBtn.classList.add('grid-online');
      gridBtn.textContent = 'Grid Online';
    }
  });
```

- [ ] **Step 4: set-grid-islandable (line 296)**

Same `className` overwrite issue at lines 301-308. Replace `fetch(` with `busyFetch(islandableBtn,` and convert visual feedback to `classList`:

```javascript
  islandableBtn.addEventListener('click', function() {
    var isIslandable = islandableBtn.classList.contains('islandable-on');
    busyFetch(islandableBtn, 'set-grid-islandable', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({islandable: !isIslandable})
    });
    // Immediate visual feedback (use classList to preserve .busy)
    if (isIslandable) {
      islandableBtn.classList.remove('islandable-on');
      islandableBtn.classList.add('islandable-off');
      islandableBtn.textContent = 'Not Islandable';
    } else {
      islandableBtn.classList.remove('islandable-off');
      islandableBtn.classList.add('islandable-on');
      islandableBtn.textContent = 'Islandable';
    }
  });
```

- [ ] **Step 5: relay toggle (line 320)**

Replace `fetch(` with `busyFetch(dot,`:

```javascript
    busyFetch(dot, 'entities/' + encodeURIComponent(cid) + '/relay', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({relay_state: newState})
    });
```

- [ ] **Step 6: Convert pollPower() sync code to classList (lines 527-572)**

The `pollPower()` function runs every 3 seconds and syncs grid/islandable buttons and relay dots using `className =` assignment. This would strip `.busy` if a busyFetch is in flight when a poll arrives. Convert all three sync blocks to `classList` for consistency:

Grid button sync (lines 526-532):
```javascript
        if (data.grid_online !== undefined) {
          if (data.grid_online) {
            gridBtn.classList.remove('grid-offline');
            gridBtn.classList.add('grid-online');
            gridBtn.textContent = 'Grid Online';
          } else {
            gridBtn.classList.remove('grid-online');
            gridBtn.classList.add('grid-offline');
            gridBtn.textContent = 'Grid Offline';
          }
        }
```

Islandable button sync (lines 536-542):
```javascript
          if (data.is_islandable) {
            islandableBtn.classList.remove('islandable-off');
            islandableBtn.classList.add('islandable-on');
            islandableBtn.textContent = 'Islandable';
          } else {
            islandableBtn.classList.remove('islandable-on');
            islandableBtn.classList.add('islandable-off');
            islandableBtn.textContent = 'Not Islandable';
          }
```

Relay dots sync (lines 562-573) — this one is trickier because the full `className` is built dynamically. Use a helper approach that preserves `.busy`:
```javascript
        dots.forEach(function(dot) {
          var cid = dot.dataset.cid;
          dot.classList.remove('status-off', 'status-shed', 'status-open');
          if (data.all_off) {
            dot.classList.add('status-off');
          } else if (shedIds.indexOf(cid) >= 0) {
            dot.classList.add('status-shed');
          } else if (userOpenIds.indexOf(cid) >= 0) {
            dot.classList.add('status-open');
          }
        });
```

- [ ] **Step 7: Verify manually**

Drag the time slider — slider dims briefly during POST. Click grid toggle — button dims briefly. Click a relay dot — dot dims briefly. All should clear immediately on response. Wait for a 3s poll cycle — verify the busy state is not prematurely stripped.

- [ ] **Step 8: Commit**

```bash
git add src/span_panel_simulator/dashboard/templates/partials/runtime_controls.html
git commit -m "feat: convert runtime control fetch calls to busyFetch"
```

---

### Task 6: Convert remaining fetch calls to busyFetch

**Files:**
- Modify: `src/span_panel_simulator/dashboard/templates/partials/panel_config.html:95-97`
- Modify: `src/span_panel_simulator/dashboard/templates/partials/modeling_view.html:142`
- Modify: `src/span_panel_simulator/dashboard/templates/partials/pv_profile.html:89`
- Modify: `src/span_panel_simulator/dashboard/templates/partials/panels_list_rows.html:19`

- [ ] **Step 1: panel_config.html — weather fetch (user-click path only)**

In the `fetchWeather` function (line 95), replace `fetch(` with `busyFetch(weatherStatus,`. The `weatherStatus` element already exists and is the natural feedback target. Only the user-click invocation at line 123 triggers this — the page-load invocation at line 139 also calls `fetchWeather` but `weatherStatus` will just dim briefly on load, which is acceptable since it already shows "Fetching historical weather data..." text.

```javascript
  function fetchWeather(lat, lon) {
    weatherStatus.textContent = 'Fetching historical weather data...';
    busyFetch(weatherStatus, 'fetch-weather?lat=' + lat + '&lon=' + lon)
      .then(function(r) {
```

- [ ] **Step 2: modeling_view.html — horizon change**

In the `fetchModelingData` function (line 142), replace `fetch(` with `busyFetch(horizonSelect,`. The spec says "modeling container" but `horizonSelect` is a better trigger — it dims the dropdown during fetch, which is the element the user just interacted with, and prevents repeated horizon changes:

```javascript
    busyFetch(horizonSelect, 'modeling-data?horizon=' + horizon)
      .then(function(r) { return r.json(); })
```

This dims the horizon select dropdown during fetch, preventing repeated changes.

- [ ] **Step 3: pv_profile.html — PV curve fetch**

In the `fetchAndRender` function (line 89), replace `fetch(url)` with `busyFetch(sliderEl, url)` to dim the month slider during fetch:

```javascript
    busyFetch(sliderEl, url)
      .then(function(r) { return r.json(); })
```

- [ ] **Step 4: panels_list_rows.html — Clone button**

The Clone button at line 19 uses an inline `htmx.ajax()` call. Replace the inline onclick to use `busyFetch` with a direct POST instead:

```html
  <button class="btn btn-sm btn-secondary"
          onclick="event.stopPropagation();var btn=this;var n=prompt('Clone as:','{{ p.filename.replace('default_', '') }}');if(n){btn.classList.add('busy');htmx.ajax('POST','clone',{target:'#flash-area',swap:'innerHTML',values:{filename:n,source_file:'{{ p.filename }}'}}).finally(function(){btn.classList.remove('busy');});};"
          title="Clone this config">
    Clone
  </button>
```

Since `htmx.ajax()` returns a promise, we can use the same `.busy` class pattern directly without `busyFetch`.

- [ ] **Step 5: Verify manually**

- Select a location in panel config — weather fetch shows busy on the status text
- Change horizon in modeling view — dropdown dims during fetch
- Switch months in PV profile — slider dims briefly
- Click Clone — Clone button dims until complete

- [ ] **Step 6: Commit**

```bash
git add src/span_panel_simulator/dashboard/templates/partials/panel_config.html \
        src/span_panel_simulator/dashboard/templates/partials/modeling_view.html \
        src/span_panel_simulator/dashboard/templates/partials/pv_profile.html \
        src/span_panel_simulator/dashboard/templates/partials/panels_list_rows.html
git commit -m "feat: convert weather, modeling, PV, and clone fetch calls to busyFetch"
```
