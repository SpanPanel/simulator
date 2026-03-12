"""Homie v5 / eBus MQTT constants for the simulator.

These mirror the subset of constants from span-panel-api's mqtt/const.py
that the simulator actually needs, keeping this project fully independent.
"""

from __future__ import annotations

# Homie v5 topic structure
HOMIE_VERSION = 5
HOMIE_DOMAIN = "ebus"
TOPIC_PREFIX = f"{HOMIE_DOMAIN}/{HOMIE_VERSION}"

# Topic patterns (serial substituted at runtime)
STATE_TOPIC_FMT = f"{TOPIC_PREFIX}/{{serial}}/$state"
DESCRIPTION_TOPIC_FMT = f"{TOPIC_PREFIX}/{{serial}}/$description"
PROPERTY_TOPIC_FMT = f"{TOPIC_PREFIX}/{{serial}}/{{node}}/{{prop}}"
PROPERTY_SET_TOPIC_FMT = f"{TOPIC_PREFIX}/{{serial}}/{{node}}/{{prop}}/set"

# Homie device states
HOMIE_STATE_INIT = "init"
HOMIE_STATE_READY = "ready"
HOMIE_STATE_DISCONNECTED = "disconnected"

# Homie node type strings (from schema)
TYPE_CORE = "energy.ebus.device.distribution-enclosure.core"
TYPE_LUGS = "energy.ebus.device.lugs"
TYPE_CIRCUIT = "energy.ebus.device.circuit"
TYPE_BESS = "energy.ebus.device.bess"
TYPE_PV = "energy.ebus.device.pv"
TYPE_EVSE = "energy.ebus.device.evse"
TYPE_PCS = "energy.ebus.device.pcs"
TYPE_POWER_FLOWS = "energy.ebus.device.power-flows"
