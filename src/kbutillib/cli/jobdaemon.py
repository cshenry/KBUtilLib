"""``kbu jobdaemon`` — run the KBJobUtils watcher in the foreground."""

from __future__ import annotations

import logging
import signal
import sys
import time
from pathlib import Path
from typing import Optional

import click


@click.command(name="jobdaemon")
@click.option("--interval", default=300, type=int, help="Seconds between refresh passes.")
@click.option("--store-path", default=None, type=click.Path(), help="Override kbjobs.db path.")
@click.option("--kb-version", default="prod", type=click.Choice(["prod", "appdev", "ci"]),
              help="KBase environment (default: prod).")
@click.option("--log-level", default="INFO",
              type=click.Choice(["DEBUG", "INFO", "WARNING", "ERROR"]),
              help="Logging level (default: INFO).")
def jobdaemon_cmd(
    interval: int,
    store_path: Optional[str],
    kb_version: str,
    log_level: str,
) -> None:
    """Run the KBJobUtils watcher in the foreground until SIGINT/SIGTERM."""
    from ..kb_job_utils.utils import KBJobUtils
    from ..shared_env_utils import SharedEnvUtils

    logging.basicConfig(
        level=getattr(logging, log_level),
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    )
    log = logging.getLogger(__name__)

    db_path = Path(store_path) if store_path else None
    env = SharedEnvUtils()
    kbu = KBJobUtils(env=env, kb_version=kb_version, db_path=db_path)

    log.info("Starting jobdaemon (interval=%ds, kb_version=%s)", interval, kb_version)
    watcher = kbu.start_watcher(interval=interval, daemon=False)

    shutdown_requested = False

    def _handle_signal(signum: int, frame) -> None:
        nonlocal shutdown_requested
        if shutdown_requested:
            return
        shutdown_requested = True
        sig_name = signal.Signals(signum).name
        log.info("Received %s, shutting down...", sig_name)
        kbu.stop_watcher(timeout=10.0)

    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)

    try:
        while watcher.is_alive():
            time.sleep(1)
    except KeyboardInterrupt:
        if not shutdown_requested:
            log.info("KeyboardInterrupt, shutting down...")
            kbu.stop_watcher(timeout=10.0)

    # Log summary
    log.info(
        "Jobdaemon stopped. runs=%d errors=%d last_run=%s",
        watcher.runs,
        watcher.errors,
        watcher.last_run_at.isoformat() if watcher.last_run_at else "never",
    )
    kbu.close()
