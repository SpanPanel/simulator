# Simulator — Agent Rules

Rules in this file apply to all AI coding agents working in this repository.

## No AI Attribution in Commits

Do **not** attribute work to Copilot, Claude, or any AI agent in commit messages. Commits represent human direction and decision-making; AI assists in
implementation but does not co-author.

**Rule:**

- Never include `Co-authored-by: Copilot` or any AI co-author trailer in commit messages.
- Never mention AI tools or attribution in commit messages.
- Commits belong to the human author directing the work.

This rule takes precedence over any default tool behavior that would add AI attribution.

## Energy System Encapsulation

The `span_panel_simulator.energy` package is the **sole authority** for all energy and power-flow calculations. This boundary was deliberately established to
replace scattered inline logic and must not be eroded.

**Rules:**

- The engine (`engine.py`) provides **raw measurements** to the energy module (PV power, load power, grid status). It must never pre-compute, resolve, or
  override energy scheduling, dispatch, or balance decisions.
- `PowerInputs` carries only observable state — never derived energy decisions like BESS scheduled state.
- All BESS scheduling (charge mode logic, TOU hour resolution, islanding overrides, forced-offline behavior) lives inside `EnergySystem.tick()` and `BESSUnit`.
  The engine must not call `resolve_scheduled_state()` or read `effective_state` to feed back into inputs.
- PV curtailment, GFE throttling, SOE enforcement, and bus balancing are energy-module concerns. The engine consumes `SystemState` results — it does not
  participate in producing them.
- New energy behaviors (e.g. demand response, rate optimization) must be added inside the energy package, not grafted onto the engine.

**Test discipline:** Tests drive BESS behavior through `BESSConfig` (charge_mode, charge_hours, discharge_hours), not by injecting state into `PowerInputs`.

## Sign Frames — the panel reading is the mirror of the device reading

The panel is an **interface** to the devices around it, so its reading of a device is the mirror image of that device's reading of itself. What the device
calls "out of me", the panel calls "into me". Two frames therefore coexist on purpose, and they disagree about the same instant.

| | the device's own view | the panel's view (what we publish) |
|---|---|---|
| PV | positive = generating | **negative** while generating into the panel |
| BESS | positive = discharging | **positive** while charging, out of the panel |
| grid | positive = supplying the home | **positive** while exporting, out of the panel |
| circuit | positive = consuming | **negative** while consuming, out of the busbar |
| `site` | — not a device at the interface | positive = consuming; no mirror to take |

`site` is the exception because there is no device on the other side of it to mirror — which is why it is the one `power-flows` property that was already
correct when the other three were inverted.

**Rules:**

- **Never "reconcile" the two frames.** A panel exporting publishes `lugs-upstream/active-power` negative and `power-flows/grid` positive at the same instant.
  Both are right. Code or tests that make them agree are removing information.
- **The snapshot is device-frame; the wire layer mirrors it.** Snapshot dataclasses carry the producer-side quantity (`instant_power_w` positive = consuming,
  `active_power_w` positive = discharging). The negation belongs in the `bag_builder` resolver, next to the docstring that explains it — never by redefining
  what a snapshot field means, which would silently change every other reader.
- **The four `power-flows` values sum to zero.** They are four terms of one balance at one node, not four independent meters. `test_power_flows_sum_to_zero`
  holds this. Derive `power_flow_grid` from the physics, never by back-solving from the other three — a residual satisfies the balance by construction and
  detects nothing.
- **A new metered surface states its frame in a docstring before it is published.** These defects are silently wrong at the consumer, and the damage is
  **persisted, not displayed**. Home Assistant feeds these values into long-term statistics — the Energy dashboard, cost attribution, monthly totals. A
  renamed or removed property fails loudly; an inverted sign keeps producing plausible numbers, is recorded for weeks, and **fixing the simulator afterwards
  does not repair what the recorder already stored**. Energy registers are worse: `imported-energy` and `exported-energy` are monotonic, so a tick advancing
  both writes an import and an export that never happened, and neither can be subtracted back out.
- **This is why the simulator must match hardware rather than be internally consistent.** It is what the integration is developed and regression-tested
  against, so a frame the panel does not use gets baked into the integration and reaches the field, where it corrupts real users' statistics.

**Authority:** SPAN's published behavior, not the eBus catalog, which states the opposite for `power-flows` and is a documented, deliberate divergence. See
`spanio/SPAN-API-Client-Docs`, `docs/public/power-and-energy-conventions.md` — the tables there are normative; note that the prose sentence calling
`power-flows` a "source-centric summary" describes the un-mirrored view and contradicts the table beneath it.
