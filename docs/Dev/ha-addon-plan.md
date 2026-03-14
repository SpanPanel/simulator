# Plan: Home Assistant Add-on for SPAN Panel Simulator

## Context

The simulator runs as a standalone process that mimics a real SPAN panel — publishing Homie v5 MQTT topics, serving an HTTP bootstrap API, and advertising via mDNS. The separate `span-panel` HA integration discovers these services and creates HA entities from them, exactly as it would with real hardware.

The goal is to package the simulator as an **HA add-on** (Docker container) so that HA users with the `span-panel` integration can easily spin up a simulated panel within their existing environment.

## Phase 0: Fix Simulator mDNS TXT Records

Align the `_ebus._tcp.local.` TXT records and SRV port with real panel behavior. Add `httpPort` for non-standard port discovery.

## Phase 1: Make span-panel-api HTTP Port Configurable

Add optional `port` parameter to all HTTP functions in `span-panel-api` and thread it through to the `span-panel` integration via `CONF_HTTP_PORT` in config entries.

## Phase 2: Build HA Add-on

Package the simulator as a Docker container with HA add-on metadata, port mappings, and a `run.sh` entry point that reads `/data/options.json`.

## Phase 3: Testing & Verification

Verify mDNS records, HTTP port flexibility, add-on startup, and integration discovery end-to-end.
