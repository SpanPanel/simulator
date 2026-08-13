# Changelog

## 1.0.16 — 2026-08-11 — EVSE nodes carry the drive serial

**An EVSE's Homie node id is now its drive serial**, where it used to be a positional
slot — `evse`, `evse-2`. No panel publishes those. Firmware names the node after the
drive, the flat parser keys its snapshot on the node id verbatim, and the integration
builds the Home Assistant device identifier from that key — so against real firmware an
EVSE is already serial-identified. v1.0 names the same device `<panel>-<serial>` and
strips it back to the serial, reproducing the flat key exactly.

That equality is what carries a drive's identity, history and automations across a
firmware upgrade. The positional slot broke it: upgrading re-keyed every EVSE, so Home
Assistant built new devices and stranded the old ones with their entities Unavailable —
two devices for one drive, same serial, one live and one dead. The defect was only ever
in this simulator, but this simulator is what the upgrade path is rehearsed against, so
it made a passing rehearsal out of a migration that does not survive.

**The default serial is lower-case** (`sim-evse-<panel>`), because it is a topic level
now and not merely a property value: Homie 5 allows only `a`-`z`, `0`-`9` and `-` in a
topic-level id. A configured `evse.serial_number` outside that set is refused at build
time rather than sanitised — rewriting it would publish a node id that no longer matches
the `info/serial-number` beside it, quietly reintroducing the mismatch this release
removes.

**Upgrade note.** Existing simulator users will see their EVSE devices re-key once, on
first run after this version: the old positional-slot devices are stranded and can be
deleted from the device registry. Panels running real firmware are unaffected, having
been serial-keyed all along.

## 1.0.15 — 2026-08-06 — the flat schema release

The flat (`node-on-parent`) Homie data model, which SPAN firmware speaks today. Development
of the parent/child (v1.0) model continues separately and will take over `main`; the flat
lineage stays reachable at this tag and on the `flat` branch, which is where any further
flat-only fix belongs.

Nothing in the published surface changed since 1.0.14 — no new entities, no topic moves, no
configuration changes. Both fixes below landed after 1.0.14 was cut, which is the reason to
re-cut rather than leave that tag as the flat reference.

### Fixed

- **The Home Assistant long-lived token no longer appears in the process command line.** It
  was passed to the simulator as `--ha-token`, which put it in `argv`, readable by any
  process listing on the machine. It is now exported to the child process instead, and
  `.env` is the documented place to set it. If you ran a previous version with a token on
  the command line, rotate it.
- **`--stop` waits for the process to actually exit before returning.** It sent SIGTERM and
  returned immediately, which is not the same as being stopped: graceful shutdown clears
  retained topics and closes the broker connection before releasing the dashboard and HTTP
  ports. Anything stopping and immediately restarting on the same ports raced the old
  process and failed with "address already in use", which made `--restart` intermittently
  fail. Waits up to 10s per process, then forces.

## 1.0.14 — 2026-07-31 — vendor the flat emitter

### Fixed

- **The HA add-on image could never start.** `ebus_emitter` is a hard, unconditional import
  (`app.py` → `emitter_adapter/runtime.py`), but the Dockerfile installs only
  `pip install --no-cache-dir .`, and the package was not a declared dependency — it is not
  on PyPI and was installed editable from `EBUS_EMITTER_PATH` by `scripts/dev-setup.sh`.
  The image therefore built successfully and failed at container start with
  `ModuleNotFoundError: No module named 'ebus_emitter'`. The same applied to anyone who
  cloned this repo and ran `uv sync` without `dev-setup.sh`. Vendoring removes the external
  dependency entirely, so both paths now work.

### Changed

- **The flat emitter is vendored at `src/span_panel_simulator/flat_emitter`**, copied from
  `ebus-emitter` 0.2.1 (commit `5b84de8`) — MIT, same copyright holders. The upstream repo
  has permanently diverged onto the parent/child (v1.0) Homie data model while this
  simulator continues to publish the flat schema, so the dependency delivered no upstream
  changes while costing path configuration, stale editable metadata, and an unsolvable
  distribution problem for the add-on. See the package docstring for full provenance.

  It also closes a correctness hazard: `clone.py` seeds energy accumulators against what
  this code publishes, and while the two lived in separate repos each side could look
  locally correct while jointly inverting circuit energy — which is exactly what happened.
  Both ends now sit in one repo under one test run.

- **The emitter's test suite came with it** (`tests/flat_emitter/`, 154 tests), including
  the circuit energy reference-frame regression tests. Total suite is now 395 tests.

- **`scripts/dev-setup.sh` is now a thin `uv sync` wrapper** and `.env.example` no longer
  defines `EBUS_EMITTER_PATH`; every dependency resolves from PyPI.

- **`ebus-sdk` is pinned to `==0.1.5`** rather than the range upstream declared, so that
  vendoring is behaviour-neutral: 0.1.5 is what the emitter's lockfile resolved and what
  this code was tested against. Letting it float within `<0.2` resolves 0.1.10, which drops
  the module-level `setLevel(INFO)` on the `homie` logger that `tests/test_main_logging.py`
  guards. Raising it is a deliberate follow-up, not a side effect of moving code.

- **`[tool.ruff.lint]` now declares `ignore = ["TC001", "TC002", "TC003"]`.** The existing
  comment already described this ignore, but the key was never present — none of the
  simulator's own modules happened to trigger the rules, so the omission was invisible.
  The vendored code was authored under an identical select list plus this ignore.

- **`ChargeMode` is exported from the vendored package** and used to annotate the
  `charge_mode` derivation in `engine.py` and `emitter_adapter/runtime.py`. Both sites
  already produced only valid values; mypy could not see it while `ebus_emitter` was an
  `ignore_missing_imports` module and `BESSConfig` was therefore `Any`.

> **Version note.** This work merged as 1.0.13, the same version the circuit-energy fix
> below had already published. The add-on image tag is derived from `config.yaml`, so the
> second merge overwrote the first's image without changing the version — leaving anyone
> who had already pulled 1.0.13 on the earlier build with no update signal. Re-cut as
> 1.0.14 so Supervisor sees a change.

## 1.0.13 — 2026-07-31 — circuit energy reference frame

### Fixed

- **Clone energy seeds were read in the wrong reference frame.** `clone.py` seeded
  `initial_consumed_energy_wh` from a scraped panel's `imported-energy` and
  `initial_produced_energy_wh` from its `exported-energy`. The wire is enclosure-framed:
  `exported-energy` is energy the enclosure exported *to* a circuit (normal load
  consumption) and `imported-energy` is energy it imported *from* a circuit (backfeed).
  The two are now read the correct way round, in both the initial-translation path
  (`_translate_circuit`) and the refresh path (`update_config_from_scrape`).

  This mirrors the fix in `ebus-emitter` 0.2.1, which corrected the same inversion on the
  publish side. The two were previously wrong in a mutually cancelling way — clone read
  `imported-energy` into "consumed" and the emitter published "consumed" back out as
  `imported-energy` — so a cloned panel round-tripped its wire values faithfully while
  every value carried the wrong meaning. Correcting only one side would have broken the
  round-trip, so they move together.

- **Test fixtures encoded the same inversion.** `test_clone.py` gave a load circuit a
  rising `imported-energy` and a backfeeding solar circuit a rising `exported-energy`,
  which is the reverse of what a real panel publishes, and one fixture comment described
  positive `active-power` as "export" when on the wire it means the enclosure is importing
  from the circuit. Fixtures and the two energy-seeding test names now describe the
  enclosure frame.

### Requires

- **ebus-emitter >= 0.2.1**, which carries the matching publish-side fix. Pairing this
  release with an older emitter reinstates the inversion.

## 1.0.12 — 2026-07-30 — emitter live-schema alignment and abstraction

### Changed

- **Emitter schema alignment**: Adapter updated to work with emitter's live SPAN panel Homie 5 schema (flat node layout, accurate topology and properties).
- **Lugs IDs**: Updated to match emitter convention (`lugs-upstream`, `lugs-downstream`).
- **BESS/PV feeds**: Updated spec_generator to derive device feeds and metadata from circuit templates (stable circuit UUID linkage).
- **Simulator adapter**: Updated `spec_generator.py` and `runtime.py` to pass EVSE powers to emitter, set `clear_retained=True` on clone stop for graceful shutdown.

### Fixed

- **Dev bootstrap dependency drift**: `scripts/dev-setup.sh` installed `ebus-emitter` with a bare `uv pip install --editable`, which re-resolves the emitter's dependency constraints against PyPI and ignores its `uv.lock`. A fresh bootstrap pulled `ebus-sdk` 0.12.0 — whose `Device` constructor is incompatible with the 0.1.x API the emitter targets — and panel startup died with `AttributeError: 'NoneType' object has no attribute 'get'` in `connect_broker()`. The script now installs the emitter's locked runtime dependencies first, then the emitter itself with `--no-deps`, so the venv matches what the emitter pins.
- **Type safety**: Fixed mypy error in simulator runtime (`_first_feed_for_device_type`) where template_name could be None.
