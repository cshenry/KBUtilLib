#!/usr/bin/env python3
"""Thin launcher for the KBDL sweeper (library: kbutillib.kbdl_index.sweep).

Post-deploy, this can replace ~/kbdl_sweep/sweep.py; CLI flags are identical
(--types, --dry-run, --max, --no-mirror, --loop SECONDS).
"""
from kbutillib.kbdl_index.sweep import main

if __name__ == '__main__':
    main()
