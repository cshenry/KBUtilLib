"""Notebook environment and name detection (split from notebook_utils.py)."""

from __future__ import annotations

import hashlib
import os
from pathlib import Path
from typing import Optional


def detect_notebook_name() -> Optional[str]:
    """Detect the name of the currently running Jupyter notebook.

    Tries multiple detection methods in order of reliability:
    1. VS Code injected variable (__vsc_ipynb_file__)
    2. JPY_SESSION_NAME environment variable (JupyterLab, newer VS Code)
    3. ipykernel __session__ variable
    4. ipynbname library (queries Jupyter server API)

    Returns:
        Notebook filename without path or .ipynb extension, or None.
    """
    # Method 1: VS Code / Cursor injected variable
    try:
        from IPython import get_ipython

        ip = get_ipython()
        if ip and "__vsc_ipynb_file__" in ip.user_ns:
            nb_path = ip.user_ns["__vsc_ipynb_file__"]
            return Path(nb_path).stem
    except (ImportError, Exception):
        pass

    # Method 2: JPY_SESSION_NAME environment variable
    jpy_session = os.environ.get("JPY_SESSION_NAME")
    if jpy_session:
        return Path(jpy_session).stem

    # Method 3: ipykernel __session__ variable
    try:
        from IPython import get_ipython

        ip = get_ipython()
        if ip:
            session = ip.user_ns.get("__session__")
            if session:
                return Path(session).stem
    except (ImportError, Exception):
        pass

    # Method 4: ipynbname library
    try:
        import ipynbname

        return ipynbname.name()
    except Exception:
        pass

    return None


def detect_notebook_environment() -> bool:
    """Return True if running inside a Jupyter notebook kernel."""
    try:
        from IPython import get_ipython

        ipython = get_ipython()
        if ipython is None:
            return False
        return hasattr(ipython, "kernel")
    except ImportError:
        return False


def get_cell_source_hash() -> Optional[str]:
    """SHA-256 of In[execution_count] if in IPython; None otherwise."""
    try:
        from IPython import get_ipython

        ip = get_ipython()
        if ip is None:
            return None
        exec_count = getattr(ip, "execution_count", None)
        if exec_count is None:
            return None
        history = ip.user_ns.get("In")
        if history and exec_count < len(history):
            src = history[exec_count]
            return hashlib.sha256(src.encode("utf-8")).hexdigest()
    except (ImportError, Exception):
        pass
    return None


def get_cell_index() -> Optional[int]:
    """Return the current IPython execution count, or None."""
    try:
        from IPython import get_ipython

        ip = get_ipython()
        if ip is not None:
            return getattr(ip, "execution_count", None)
    except (ImportError, Exception):
        pass
    return None
