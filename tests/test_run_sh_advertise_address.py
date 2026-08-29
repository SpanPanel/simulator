"""Tests for the host-address detection in the add-on's run.sh.

The function under test is extracted from the shipped ``run.sh`` and executed,
rather than restated here. A copy of the pipeline would keep passing after the
original drifted, and drift is precisely the failure this guards: the previous
implementation was correct for a bridge-networked container and silently became
wrong when the add-on moved to ``host_network: true``.

``ip`` is replaced with a stub on PATH so the sample routing output is fixed and
the test says nothing about the machine it runs on.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

_RUN_SH = Path(__file__).parent.parent / "span_panel_simulator" / "run.sh"

# `host_network: true`, so this is the *host's* table: .19 is this machine and
# .1 is the upstream router. Taking the gateway is the bug being guarded.
_ROUTE_GET = "1.1.1.1 via 192.168.65.1 dev eth0 src 192.168.65.19 uid 0\n"


def _detect(
    ip_stdout: str,
    tmp_path: Path,
    *,
    ip_exit: int = 0,
    env_value: str | None = None,
) -> str:
    """Run run.sh's detection with a stubbed ``ip`` and return what it printed."""
    source = _RUN_SH.read_text()
    match = re.search(r"^detect_advertise_address\(\) \{.*?^\}", source, re.MULTILINE | re.DOTALL)
    assert match, "run.sh no longer defines detect_advertise_address()"

    stub_dir = tmp_path / "bin"
    stub_dir.mkdir()
    stub = stub_dir / "ip"
    stub.write_text(f"#!/usr/bin/env bash\ncat <<'OUT'\n{ip_stdout}OUT\nexit {ip_exit}\n")
    stub.chmod(0o755)

    # Mirrors run.sh: the same `set` flags, the function, then the assignment it
    # actually feeds -- so the env-override precedence is covered too.
    script = (
        "set -euo pipefail\n"
        f"{match.group(0)}\n"
        'ADVERTISE_ADDRESS="${ADVERTISE_ADDRESS:-$(detect_advertise_address)}"\n'
        'printf "%s" "$ADVERTISE_ADDRESS"\n'
    )
    env = {"PATH": f"{stub_dir}:/usr/bin:/bin"}
    if env_value is not None:
        env["ADVERTISE_ADDRESS"] = env_value

    result = subprocess.run(
        ["bash", "-c", script], capture_output=True, text=True, env=env, check=True
    )
    return result.stdout


def test_the_hosts_own_address_is_detected(tmp_path: Path) -> None:
    """The source address is taken, not the gateway it routes via."""
    assert _detect(_ROUTE_GET, tmp_path) == "192.168.65.19"


def test_the_gateway_is_not_mistaken_for_the_host(tmp_path: Path) -> None:
    """The original defect, stated directly.

    Named separately from the test above because this is the assertion that
    would have failed before the fix, and the one worth reading in a report.
    """
    assert _detect(_ROUTE_GET, tmp_path) != "192.168.65.1"


def test_an_on_link_route_with_no_gateway_still_yields_the_source(tmp_path: Path) -> None:
    """A destination on the local segment has no `via`, so `src` shifts position.

    Guards against a parser that counted fields instead of finding the keyword.
    """
    on_link = "10.0.0.7 dev eth0 src 10.0.0.3 uid 0\n"
    assert _detect(on_link, tmp_path) == "10.0.0.3"


def test_control_characters_are_stripped(tmp_path: Path) -> None:
    """Some container `ip` builds emit trailing non-printables."""
    noisy = "1.1.1.1 via 192.168.65.1 dev eth0 src 192.168.65.19\r uid 0\n"
    assert _detect(noisy, tmp_path) == "192.168.65.19"


def test_no_route_leaves_the_address_empty(tmp_path: Path) -> None:
    """A failing `ip` must not abort the script under `set -euo pipefail`.

    An empty address costs the leaf its IP SAN. Failing to boot costs the panel
    entirely, so the empty answer is the right one.
    """
    assert _detect("", tmp_path, ip_exit=2) == ""


def test_an_explicit_address_is_not_overridden(tmp_path: Path) -> None:
    """run-local.sh sets this, and a multi-homed host may need to choose."""
    assert _detect(_ROUTE_GET, tmp_path, env_value="10.9.9.9") == "10.9.9.9"
