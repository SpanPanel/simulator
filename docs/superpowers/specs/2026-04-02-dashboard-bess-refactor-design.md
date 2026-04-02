# Dashboard BESS Refactor — Design Spec

**Date:** 2026-04-02
**Status:** Draft
**Scope:** Migrate dashboard battery management from circuit-template-based to panel-level BESS config
**Depends on:** BESS circuit removal refactor (complete)

## Problem

The engine now reads BESS config from a top-level `bess` YAML section, but the
dashboard still reads/writes `battery_behavior` on circuit templates. This is a
complete data flow mismatch — dashboard edits have no effect on the running
simulation.

## Approach

Present BESS as a dedicated panel-level card in the dashboard (Option A from
mockup review). Battery is not an entity — it's a system-level feature of the
panel sitting on the upstream lugs as GFE. The dashboard shows a "Battery (GFE)"
card between Panel Config and the Entity list, with inline stats and edit/schedule
controls.

## Changes

### 1. ConfigStore — BESS as Panel-Level Config

**Remove:**
- `EntityView.battery_behavior` field
- `_detect_entity_type()` check for `battery_behavior.enabled`
- `"battery"` from entity type defaults in `defaults.py`
- Battery entity from `get_entities()` output

**Add:**
- `get_bess_config() -> dict` — returns `self._state.get("bess", {})`
- `update_bess_config(data: dict)` — writes to `self._state["bess"]` with field
  name mapping (`max_charge_power` from form → `max_charge_w` in config)

**Rewrite to use `self._state["bess"]` instead of circuit template navigation:**
- `get_battery_charge_mode()` — no entity_id param, reads `self._state["bess"]`
- `update_battery_charge_mode(mode)` — no entity_id param, writes `self._state["bess"]`
- `get_battery_profile()` — no entity_id param
- `update_battery_profile(hour_modes)` — no entity_id param
- `get_active_days()` — battery branch reads `self._state["bess"]`
- `update_active_days(days)` — battery branch writes `self._state["bess"]`
- `apply_battery_preset(preset_name)` — no entity_id param

### 2. Routes and Templates

**New partial:** `partials/bess_card.html` — dedicated battery card between panel
config and entity list. Shows nameplate, reserve, charge/discharge power, charge
mode, SOC from engine state. "Edit Settings" and "Schedule" buttons.

**New routes (panel-level, no entity ID):**
- `GET /bess/edit` — returns BESS settings edit form
- `PUT /bess` — saves BESS settings (nameplate, reserve, power limits, charge mode)
- `PUT /bess/schedule` — saves the 24-hour charge/discharge schedule
- `POST /bess/schedule/preset` — applies a schedule preset
- `PUT /bess/active-days` — saves active days

All new routes call `_persist_config()` after writing (fixing existing bug where
battery profile updates did not persist to YAML).

**Remove old entity-based battery routes:**
- `PUT /entities/{id}/battery-charge-mode`
- `PUT /entities/{id}/battery-profile`
- `POST /entities/{id}/battery-profile/preset`

**Form field mapping:** HTML forms use user-facing names (`max_charge_power`,
`max_discharge_power`). Route handlers translate to engine field names
(`max_charge_w`, `max_discharge_w`) when writing to config.

**Entity list cleanup:**
- Remove `"battery"` from addable entity types dropdown
- Remove battery row handling from entity list template
- Entity count reflects only circuits + PV + EVSE

**Battery profile editor:** Existing `battery_profile_editor.html` adapted to
work without entity ID. Schedule grid, charge mode radio buttons, and active days
checkboxes remain functionally identical.

### 3. Energy Projection and Cleanup

**Energy projection** in `config_store.py`: Read battery specs from
`self._state["bess"]` directly using new field names (`max_charge_w`,
`max_discharge_w`). Battery is not iterated as an entity.

**Remove from templates:**
- `entity_edit.html`: Remove the `{% if e.battery_behavior %}` fieldset block

## Out of Scope

- EVSE two-tab allocation (separate follow-on)
- Charge mode enum changes (dashboard continues to offer `self-consumption`,
  `custom`, `backup-only` — engine already accepts these)
