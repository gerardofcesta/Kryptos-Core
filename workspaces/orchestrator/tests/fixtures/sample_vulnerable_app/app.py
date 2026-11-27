"""
Sample Python target application fixture for Orchestrator end-to-end & unit tests.

Intentionally contains:
1. Real bug #1 (Race Condition): `LedgerStore.save_state()` performs a direct non-atomic
   `open(path, "w")` + `json.dump()` without a lock or temp-file `os.replace()`.
2. Intentional false positive (Safe): `run_health_check()` uses `subprocess.run(..., shell=False)`
   with a hardcoded executable and strictly allowlisted flag arguments.
3. Real bug #2 (Float Precision): `calculate_compound_balance()` accumulates monetary values
   using binary IEEE-754 `float` instead of `decimal.Decimal`.
"""

from __future__ import annotations

import json
import pathlib
import re
import subprocess
import sys
from typing import Any

_ALLOWED_CHECKS = frozenset({"version", "platform"})
_SAFE_LABEL_RE = re.compile(r"^[a-zA-Z0-9_-]{1,32}$")


class LedgerStore:
    """Stores account ledger snapshots in a JSON file (VULNERABLE: non-atomic write race)."""

    def __init__(self, state_file: pathlib.Path | str) -> None:
        self.state_file = pathlib.Path(state_file)

    def load_state(self) -> dict[str, Any]:
        if not self.state_file.exists():
            return {"accounts": {}, "revision": 0}
        with open(self.state_file, "r", encoding="utf-8") as fh:
            return json.load(fh)

    def save_state(self, state: dict[str, Any]) -> None:
        # BUG (Race Condition): Non-atomic direct truncate + write without lock or os.replace.
        # Concurrent threads calling save_state() can interleave and corrupt the JSON file.
        with open(self.state_file, "w", encoding="utf-8") as fh:
            json.dump(state, fh, indent=2)
            fh.flush()

    def credit(self, account_id: str, amount: float) -> dict[str, Any]:
        state = self.load_state()
        accounts = state.setdefault("accounts", {})
        accounts[account_id] = accounts.get(account_id, 0.0) + amount
        state["revision"] = int(state.get("revision", 0)) + 1
        self.save_state(state)
        return state


def run_health_check(check_name: str, label: str = "default") -> dict[str, Any]:
    """
    SAFE false-positive candidate for static analyzers flagging `subprocess.run`.
    Strictly validates inputs against allowlists and executes with `shell=False`.
    """
    if check_name not in _ALLOWED_CHECKS:
        raise ValueError(f"Unsupported health check: {check_name!r}")
    if not _SAFE_LABEL_RE.match(label):
        raise ValueError(f"Invalid label: {label!r}")

    argv = (
        [sys.executable, "--version"]
        if check_name == "version"
        else [sys.executable, "-c", "import platform; print(platform.system())"]
    )
    proc = subprocess.run(
        argv,
        shell=False,
        capture_output=True,
        text=True,
        timeout=5,
        check=False,
    )
    return {
        "check": check_name,
        "label": label,
        "returncode": proc.returncode,
        "output": (proc.stdout or proc.stderr or "").strip(),
    }


def calculate_compound_balance(
    principal: float,
    monthly_deposit: float,
    annual_rate: float,
    months: int,
) -> float:
    """
    BUG (Float Precision Drift): Uses binary `float` arithmetic instead of `Decimal`,
    accumulating rounding drift over repeated financial steps (e.g., 0.1 + 0.2 != 0.3).
    """
    balance = float(principal)
    monthly_rate = float(annual_rate) / 12.0
    for _ in range(int(months)):
        balance = balance * (1.0 + monthly_rate) + float(monthly_deposit)
    return balance
