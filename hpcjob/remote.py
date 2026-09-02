"""Thin wrapper around ssh / scp with a dry-run mode.

Everything that touches the cluster goes through this class, so a test (or a
cautious user) can see every command before it runs.
"""
from __future__ import annotations

import shlex
import subprocess
from typing import Sequence


class RemoteError(RuntimeError):
    pass


class Remote:
    def __init__(self, host: str, connect_timeout: int = 15, dry_run: bool = False):
        self.host = host
        self.connect_timeout = connect_timeout
        self.dry_run = dry_run
        self.log: list[list[str]] = []      # every command issued (for tests)

    # ---- helpers ------------------------------------------------------------
    def _ssh_base(self) -> list[str]:
        return ["ssh", "-o", "BatchMode=yes", "-o", f"ConnectTimeout={self.connect_timeout}", self.host]

    def _exec(self, argv: Sequence[str], capture: bool, timeout: int | None) -> str:
        self.log.append(list(argv))
        if self.dry_run:
            print("[dry-run] " + " ".join(shlex.quote(a) for a in argv))
            return ""
        r = subprocess.run(list(argv), capture_output=capture, text=True, timeout=timeout)
        if r.returncode != 0:
            err = (r.stderr or "").strip() if capture else ""
            raise RemoteError(f"command failed (exit {r.returncode}): {' '.join(argv[:3])} ...\n{err}")
        return r.stdout if capture else ""

    # ---- public API ---------------------------------------------------------
    def run(self, command: str, capture: bool = True, timeout: int | None = 600) -> str:
        """Run a shell command on the cluster and return its stdout."""
        return self._exec(self._ssh_base() + [command], capture, timeout)

    def stream(self, command: str) -> None:
        """Run a long command and let its output flow to the terminal."""
        self._exec(self._ssh_base() + [command], capture=False, timeout=None)

    def upload(self, files: Sequence[str], remote_dir: str) -> None:
        if not files:
            return
        self._exec(["scp", "-o", "BatchMode=yes", "-q", *files, f"{self.host}:{remote_dir}/"],
                   capture=True, timeout=1800)

    def download(self, remote_path: str, local_dir: str) -> None:
        self._exec(["scp", "-o", "BatchMode=yes", "-r", f"{self.host}:{remote_path}", local_dir],
                   capture=True, timeout=3600)

    def normalise_line_endings(self, remote_dir: str, globs: Sequence[str]) -> None:
        """Strip CR from text files that were uploaded from Windows."""
        if not globs:
            return
        targets = " ".join(f"{remote_dir}/{g}" for g in globs)
        self.run(f"sed -i -e 's/\\r$//' {targets} 2>/dev/null; true")
