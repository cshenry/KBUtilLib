"""kbutillib.interfaces.cli.verab — shim re-exporting from kbutillib.cli.verab.

The authoritative implementation is in ``kbutillib.cli.verab`` so that
tests can monkeypatch ``kbutillib.cli.verab._get_toolkit`` correctly.
This module is NOT imported by interfaces.cli.__init__ at module load time
(to avoid circular imports); use ``kbutillib.cli.verab`` directly instead.
"""

from __future__ import annotations

from kbutillib.cli.verab import (  # noqa: F401
    _get_toolkit,
    verab_cmd,
)

__all__ = ["verab_cmd", "_get_toolkit"]
