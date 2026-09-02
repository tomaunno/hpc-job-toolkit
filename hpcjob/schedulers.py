"""Scheduler adapters: Slurm and (Univa / Son of) Grid Engine.

Every cluster-facing command is a template string, so a site with unusual
flags can override any of them in ``[templates]`` of the config file without
touching code.  Placeholders:

    {workdir} {jobsdir} {logdir}   remote directories
    {account_flag}                 '--account=X' / '-g X', or '' when no account
    {resource}                     resource request, passed through verbatim
    {walltime} {name} {script} {args} {jobid} {lines} {poll} {since}
"""
from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass(frozen=True)
class Scheduler:
    kind: str
    submit: str
    status: str
    cancel: str
    is_running: str          # shell test, exit 0 while the job exists
    log: str
    jobid_regex: str
    accounting: str          # command that prints raw accounting records
    account_flag: str        # format with {account}

    def submit_command(self, *, workdir: str, jobsdir: str, logdir: str, account: str,
                       resource: str, walltime: str, name: str, script: str,
                       args: list[str]) -> str:
        account_flag = self.account_flag.format(account=account) if account else ""
        resource_flag = self._resource_flag(resource)
        cmd = self.submit.format(workdir=workdir, jobsdir=jobsdir, logdir=logdir,
                                 account_flag=account_flag, resource=resource_flag,
                                 walltime=walltime, name=name, script=script,
                                 args=" ".join(args))
        return re.sub(r" {2,}", " ", cmd).strip()

    def _resource_flag(self, resource: str) -> str:
        if not resource:
            return ""
        if self.kind == "uge" and not resource.startswith("-"):
            return f"-l {resource}"           # e.g. 'node_q=1' -> '-l node_q=1'
        return resource                       # Slurm: e.g. '--gres=gpu:1 -N 1'

    def parse_jobid(self, submit_output: str) -> str | None:
        m = re.search(self.jobid_regex, submit_output)
        return m.group(1) if m else None

    def watch_command(self, *, jobid: str, logdir: str, poll: int, lines: int = 80) -> str:
        running = self.is_running.format(jobid=jobid)
        tail = self.log.format(logdir=logdir, jobid=jobid, lines=lines)
        return (f"while {running}; do sleep {poll}; done; "
                f"echo '--- job {jobid} finished ---'; {tail} 2>/dev/null || echo 'no log found'")


SLURM = Scheduler(
    kind="slurm",
    submit=("cd {workdir} && sbatch {account_flag} {resource} --time={walltime} "
            "--job-name={name} --output={logdir}/{name}.o%j {jobsdir}/{script} {args}"),
    status="squeue -u $USER -o '%.10i %.9P %.20j %.8T %.10M %.10l %.6D %R'",
    cancel="scancel {jobid}",
    is_running="squeue -h -j {jobid} 2>/dev/null | grep -q .",
    log="tail -n {lines} {logdir}/*.o{jobid}",
    jobid_regex=r"Submitted batch job (\d+)",
    accounting=("sacct -u $USER -S {since} -P --format="
                "JobID,JobName,State,ExitCode,Elapsed,Timelimit,AllocNodes,NCPUS,ReqTRES,Start,End"),
    account_flag="--account={account}",
)

UGE = Scheduler(
    kind="uge",
    submit=("cd {workdir} && qsub {account_flag} {resource} -l h_rt={walltime} "
            "-N {name} -o {logdir} -j y {jobsdir}/{script} {args}"),
    status="qstat",
    cancel="qdel {jobid}",
    is_running="qstat -j {jobid} >/dev/null 2>&1",
    log="tail -n {lines} {logdir}/*.o{jobid}",
    jobid_regex=r"Your job (\d+)",
    accounting="qacct -j '*' -b {since}",
    account_flag="-g {account}",
)

BUILTIN = {"slurm": SLURM, "uge": UGE}


def get_scheduler(kind: str, overrides: dict[str, str] | None = None) -> Scheduler:
    base = BUILTIN[kind]
    if not overrides:
        return base
    unknown = set(overrides) - set(base.__dataclass_fields__)
    if unknown:
        raise ValueError(f"unknown template keys in [templates]: {sorted(unknown)}")
    return Scheduler(**{**base.__dict__, **overrides})
