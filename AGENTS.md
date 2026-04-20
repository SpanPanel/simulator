# Simulator — Agent Rules

Rules in this file apply to all AI coding agents working in this repository.

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
