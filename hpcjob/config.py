"""Configuration: a small TOML file, validated into a dataclass."""
from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field


@dataclass
class Config:
    host: str
    workdir: str
    scheduler: str = "slurm"
    account: str = ""
    logdir: str = "auto/logs"            # relative to workdir
    jobsdir: str = "auto/jobs"           # relative to workdir
    local_src: str = "src"
    local_jobs: str = "jobs"
    local_results: str = "results"
    sync_extensions: tuple[str, ...] = (".py", ".sh", ".json", ".toml", ".yaml", ".csv", ".mat", ".npz")
    text_extensions: tuple[str, ...] = (".py", ".sh", ".json", ".toml", ".yaml", ".csv", ".md", ".txt")
    resource: str = ""
    walltime: str = "4:00:00"
    max_walltime: str = "24:00:00"
    trial_max_walltime: str = "0:03:00"
    poll_seconds: int = 60
    connect_timeout: int = 15
    templates: dict[str, str] = field(default_factory=dict)   # scheduler command overrides
    cost_elapsed_weight: float = 1.0
    cost_requested_weight: float = 0.0
    cost_coefficient: dict[str, float] = field(default_factory=dict)
    joblog: str = "hpcjob-joblog.tsv"
    usage_dir: str = "usage"
    root: str = "."                       # directory that holds the config file

    @property
    def remote_logdir(self) -> str:
        return f"{self.workdir}/{self.logdir}"

    @property
    def remote_jobsdir(self) -> str:
        return f"{self.workdir}/{self.jobsdir}"


def load_config(path: str) -> Config:
    with open(path, "rb") as f:
        raw = tomllib.load(f)
    remote = raw.get("remote", {})
    local = raw.get("local", {})
    defaults = raw.get("defaults", {})
    cost = raw.get("cost", {})
    if "host" not in remote or "workdir" not in remote:
        raise ValueError(f"{path}: [remote] needs 'host' and 'workdir'")
    kind = remote.get("scheduler", "slurm").lower()
    if kind not in ("slurm", "uge"):
        raise ValueError(f"{path}: scheduler must be 'slurm' or 'uge', got {kind!r}")
    return Config(
        host=remote["host"],
        workdir=remote["workdir"].rstrip("/"),
        scheduler=kind,
        account=remote.get("account", ""),
        logdir=remote.get("logdir", "auto/logs").strip("/"),
        jobsdir=remote.get("jobsdir", "auto/jobs").strip("/"),
        local_src=local.get("src", "src"),
        local_jobs=local.get("jobs", "jobs"),
        local_results=local.get("results", "results"),
        sync_extensions=tuple(local.get("sync_extensions", Config.sync_extensions)),
        text_extensions=tuple(local.get("text_extensions", Config.text_extensions)),
        resource=defaults.get("resource", ""),
        walltime=defaults.get("walltime", "4:00:00"),
        max_walltime=defaults.get("max_walltime", "24:00:00"),
        trial_max_walltime=defaults.get("trial_max_walltime", "0:03:00"),
        poll_seconds=int(defaults.get("poll_seconds", 60)),
        connect_timeout=int(defaults.get("connect_timeout", 15)),
        templates=dict(raw.get("templates", {})),
        cost_elapsed_weight=float(cost.get("elapsed_weight", 1.0)),
        cost_requested_weight=float(cost.get("requested_weight", 0.0)),
        cost_coefficient={k: float(v) for k, v in cost.get("coefficient", {}).items()},
        joblog=local.get("joblog", "hpcjob-joblog.tsv"),
        usage_dir=local.get("usage_dir", "usage"),
        root=os.path.dirname(os.path.abspath(path)),
    )


def parse_walltime(text: str) -> int:
    """'HH:MM:SS' | 'MM:SS' | 'SS' | 'D-HH:MM:SS' (Slurm) -> seconds."""
    days = 0
    if "-" in text:
        d, text = text.split("-", 1)
        days = int(d)
    parts = [int(p) for p in text.split(":")]
    if len(parts) == 3:
        h, m, s = parts
    elif len(parts) == 2:
        h, (m, s) = 0, parts
    elif len(parts) == 1:
        h, m, s = 0, 0, parts[0]
    else:
        raise ValueError(f"bad walltime: {text!r}")
    return days * 86400 + h * 3600 + m * 60 + s
