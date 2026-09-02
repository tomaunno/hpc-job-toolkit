"""Turn raw accounting output (qacct / sacct) into a table and a cost estimate.

The cost model is deliberately generic:

    cost = nodes * coefficient[resource] * (w_e * elapsed + w_r * requested) / 3600

Most centres bill elapsed node-hours (w_e = 1, w_r = 0); some bill a mix of
elapsed and requested time.  Coefficients are per resource string and live in
the config file, so nothing site-specific is hard-coded here.
"""
from __future__ import annotations

import csv
import re
from dataclasses import dataclass, asdict
from typing import Iterable

from .config import parse_walltime


@dataclass
class JobRecord:
    jobid: str
    name: str
    state: str          # normalised: COMPLETED / FAILED / CANCELLED / TIMEOUT / OTHER
    exit_code: int
    elapsed_s: int
    requested_s: int
    nodes: int
    resource: str
    start: str
    end: str

    @property
    def ok(self) -> bool:
        return self.state == "COMPLETED" and self.exit_code == 0


# ---- qacct (Grid Engine) ---------------------------------------------------
def parse_qacct(text: str) -> list[JobRecord]:
    records: list[JobRecord] = []
    for block in re.split(r"^=+\s*$", text, flags=re.M):
        kv: dict[str, str] = {}
        for line in block.splitlines():
            m = re.match(r"^(\w+)\s+(.*)$", line.strip())
            if m:
                kv[m.group(1)] = m.group(2).strip()
        if "jobnumber" not in kv:
            continue
        category = kv.get("category", "")
        h_rt = re.search(r"h_rt=(\S+)", category)
        res = re.search(r"-l ([^ ]*?)(?:,h_rt=\S+)?(?: |$)", category)
        failed = kv.get("failed", "0").split()[0]
        exit_status = int(kv.get("exit_status", "0") or 0)
        if failed not in ("0", "") :
            state = "FAILED"
        elif exit_status == 0:
            state = "COMPLETED"
        else:
            state = "FAILED"
        records.append(JobRecord(
            jobid=kv["jobnumber"], name=kv.get("jobname", ""), state=state,
            exit_code=exit_status,
            elapsed_s=int(float(kv.get("ru_wallclock", "0") or 0)),
            requested_s=parse_walltime(h_rt.group(1)) if h_rt else 0,
            nodes=1, resource=res.group(1) if res else "",
            start=kv.get("start_time", ""), end=kv.get("end_time", "")))
    return records


# ---- sacct (Slurm, -P parsable) -------------------------------------------
def parse_sacct(text: str) -> list[JobRecord]:
    lines = [ln for ln in text.splitlines() if ln.strip()]
    if not lines:
        return []
    header = lines[0].split("|")
    idx = {h: i for i, h in enumerate(header)}
    records: list[JobRecord] = []
    for ln in lines[1:]:
        f = ln.split("|")
        if len(f) < len(header):
            continue
        jobid = f[idx["JobID"]]
        if "." in jobid:                       # job steps: keep only the allocation row
            continue
        state = f[idx["State"]].split()[0]
        if state.startswith("CANCELLED"):
            state = "CANCELLED"
        elif state not in ("COMPLETED", "FAILED", "TIMEOUT"):
            state = state if state in ("RUNNING", "PENDING") else "OTHER"
        exit_code = int(f[idx["ExitCode"]].split(":")[0] or 0)
        records.append(JobRecord(
            jobid=jobid, name=f[idx["JobName"]], state=state, exit_code=exit_code,
            elapsed_s=parse_walltime(f[idx["Elapsed"]]) if f[idx["Elapsed"]] else 0,
            requested_s=parse_walltime(f[idx["Timelimit"]]) if f[idx["Timelimit"]] not in ("", "UNLIMITED") else 0,
            nodes=int(f[idx["AllocNodes"]] or 1), resource=f[idx["ReqTRES"]],
            start=f[idx["Start"]], end=f[idx["End"]]))
    return records


def parse_accounting(kind: str, text: str) -> list[JobRecord]:
    return parse_qacct(text) if kind == "uge" else parse_sacct(text)


# ---- cost + summary -----------------------------------------------------------
def job_cost(rec: JobRecord, coefficient: dict[str, float], w_elapsed: float, w_requested: float) -> float:
    coef = coefficient.get(rec.resource)
    if coef is None:                            # try the key without a count suffix, e.g. 'gpu=1' -> 'gpu'
        coef = coefficient.get(rec.resource.split("=")[0], 0.0)
    return rec.nodes * coef * (w_elapsed * rec.elapsed_s + w_requested * rec.requested_s) / 3600.0


def summarise(records: Iterable[JobRecord], coefficient: dict[str, float],
              w_elapsed: float = 1.0, w_requested: float = 0.0) -> str:
    recs = list(records)
    total_h = sum(r.elapsed_s for r in recs) / 3600
    failed = [r for r in recs if not r.ok and r.state not in ("RUNNING", "PENDING")]
    cost = sum(job_cost(r, coefficient, w_elapsed, w_requested) for r in recs)
    out = ["# Cluster usage", "",
           f"- jobs: **{len(recs)}** (failed or non-zero exit: {len(failed)})",
           f"- elapsed: **{total_h:.2f} h**",
           f"- estimated cost: **{cost:.3f}** (units of the coefficient table)", ""]
    by_res: dict[str, list[JobRecord]] = {}
    for r in recs:
        by_res.setdefault(r.resource or "(none)", []).append(r)
    out += ["| resource | jobs | elapsed h | cost |", "|---|---:|---:|---:|"]
    for res, rs in sorted(by_res.items()):
        c = sum(job_cost(r, coefficient, w_elapsed, w_requested) for r in rs)
        out.append(f"| {res} | {len(rs)} | {sum(r.elapsed_s for r in rs) / 3600:.2f} | {c:.3f} |")
    out += ["", "| job | name | state | exit | elapsed | requested | resource | end |", "|---|---|---|---:|---:|---:|---|---|"]
    for r in sorted(recs, key=lambda r: r.end):
        out.append(f"| {r.jobid} | {r.name} | {r.state} | {r.exit_code} | "
                   f"{r.elapsed_s / 3600:.2f} h | {r.requested_s / 3600:.2f} h | {r.resource} | {r.end} |")
    return "\n".join(out) + "\n"


def write_csv(records: Iterable[JobRecord], path: str) -> None:
    recs = list(records)
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(JobRecord.__dataclass_fields__))
        w.writeheader()
        for r in recs:
            w.writerow(asdict(r))
