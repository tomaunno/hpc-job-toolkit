"""Command-line entry point.

    hpcjob [--config hpcjob.toml] [--dry-run] <command> ...

Commands: sync, submit, status, log, watch, cancel, pull, push, run, usage.
"""
from __future__ import annotations

import argparse
import datetime as dt
import glob
import os
import sys

from .accounting import parse_accounting, summarise, write_csv
from .config import Config, load_config, parse_walltime
from .remote import Remote, RemoteError
from .schedulers import get_scheduler


class App:
    def __init__(self, cfg: Config, dry_run: bool = False):
        self.cfg = cfg
        self.remote = Remote(cfg.host, cfg.connect_timeout, dry_run=dry_run)
        self.sched = get_scheduler(cfg.scheduler, cfg.templates)

    # ---- paths ----------------------------------------------------------------
    def _local(self, *parts: str) -> str:
        return os.path.join(self.cfg.root, *parts)

    def _files(self, directory: str, extensions: tuple[str, ...]) -> list[str]:
        if not os.path.isdir(directory):
            return []
        return sorted(p for p in glob.glob(os.path.join(directory, "*"))
                      if os.path.isfile(p) and os.path.splitext(p)[1].lower() in extensions)

    def _text_globs(self, files: list[str]) -> list[str]:
        exts = {os.path.splitext(f)[1].lower() for f in files} & set(self.cfg.text_extensions)
        return [f"*{e}" for e in sorted(exts)]

    # ---- commands -------------------------------------------------------------
    def sync(self) -> None:
        cfg = self.cfg
        src = self._files(self._local(cfg.local_src), cfg.sync_extensions)
        jobs = self._files(self._local(cfg.local_jobs), (".sh",))
        self.remote.run(f"mkdir -p {cfg.workdir} {cfg.remote_jobsdir} {cfg.remote_logdir}")
        self.remote.upload(src, cfg.workdir)
        self.remote.upload(jobs, cfg.remote_jobsdir)
        self.remote.normalise_line_endings(cfg.workdir, self._text_globs(src))
        self.remote.normalise_line_endings(cfg.remote_jobsdir, ["*.sh"] if jobs else [])
        print(f"synced: {len(src)} source file(s), {len(jobs)} job script(s)")

    def submit(self, script: str, resource: str | None, walltime: str | None,
               name: str | None, trial: bool, args: list[str]) -> str | None:
        cfg = self.cfg
        if not os.path.isfile(self._local(cfg.local_jobs, script)):
            raise SystemExit(f"job script not found: {os.path.join(cfg.local_jobs, script)}")
        walltime = walltime or cfg.walltime
        seconds = parse_walltime(walltime)
        cap = parse_walltime(cfg.trial_max_walltime if trial else cfg.max_walltime)
        if seconds > cap:
            raise SystemExit(f"walltime {walltime} exceeds the configured maximum "
                             f"{cfg.trial_max_walltime if trial else cfg.max_walltime}")
        self.sync()
        name = name or os.path.splitext(script)[0]
        cmd = self.sched.submit_command(
            workdir=cfg.workdir, jobsdir=cfg.remote_jobsdir, logdir=cfg.remote_logdir,
            account="" if trial else cfg.account, resource=resource or cfg.resource,
            walltime=walltime, name=name, script=script, args=args)
        out = self.remote.run(cmd)
        if out:
            print(out.strip())
        jobid = self.sched.parse_jobid(out) if out else None
        stamp = dt.datetime.now().isoformat(timespec="seconds")
        with open(self._local(cfg.joblog), "a", encoding="utf-8") as f:
            f.write("\t".join([stamp, jobid or "(dry-run)", script, resource or cfg.resource,
                               walltime, " ".join(args)]) + "\n")
        if jobid:
            print(f"JOBID={jobid}")
        return jobid

    def status(self) -> None:
        print(self.remote.run(self.sched.status).rstrip())

    def log(self, jobid: str, lines: int) -> None:
        print(self.remote.run(self.sched.log.format(
            logdir=self.cfg.remote_logdir, jobid=jobid, lines=lines)).rstrip())

    def watch(self, jobid: str) -> None:
        # One ssh session, polling on the login node no more often than poll_seconds
        # (most centres ask users not to hammer the scheduler).
        print(f"watching job {jobid} (poll every {self.cfg.poll_seconds}s) ...")
        self.remote.stream(self.sched.watch_command(
            jobid=jobid, logdir=self.cfg.remote_logdir, poll=self.cfg.poll_seconds))

    def cancel(self, jobids: list[str]) -> None:
        for j in jobids:
            print(self.remote.run(self.sched.cancel.format(jobid=j)).rstrip())

    def pull(self, remote_rel: str, local_dir: str | None) -> None:
        dest = local_dir or self._local(self.cfg.local_results)
        os.makedirs(dest, exist_ok=True)
        self.remote.download(f"{self.cfg.workdir}/{remote_rel}", dest)
        print(f"pulled {remote_rel} -> {dest}")

    def push(self, local_dir: str, remote_rel: str) -> None:
        files = self._files(local_dir, self.cfg.sync_extensions)
        if not files:
            print(f"push: no matching files in {local_dir}")
            return
        target = f"{self.cfg.workdir}/{remote_rel.strip('/')}"
        self.remote.run(f"mkdir -p {target}")
        self.remote.upload(files, target)
        self.remote.normalise_line_endings(target, self._text_globs(files))
        print(f"pushed {len(files)} file(s) -> {target}")

    def run(self, command: list[str]) -> None:
        print(self.remote.run(" ".join(command)).rstrip())

    def usage(self, since: str | None) -> None:
        cfg = self.cfg
        since = since or (dt.date.today() - dt.timedelta(days=30)).strftime("%Y-%m-%d")
        if cfg.scheduler == "uge":
            since = since.replace("-", "")            # qacct -b wants YYYYMMDD[hhmm]
        raw = self.remote.run(self.sched.accounting.format(since=since), timeout=900)
        usage_dir = self._local(cfg.usage_dir)
        os.makedirs(usage_dir, exist_ok=True)
        raw_path = os.path.join(usage_dir, "accounting_raw.txt")
        # Never let a failed or empty fetch clobber the data already on disk.
        if os.path.exists(raw_path) and len(raw) < 0.5 * os.path.getsize(raw_path):
            raise SystemExit(f"fetched accounting output ({len(raw)} bytes) is much shorter than "
                             f"{raw_path}; refusing to overwrite. Pass --since to narrow the range "
                             f"or delete the file if this is intended.")
        if not self.remote.dry_run:
            with open(raw_path, "w", encoding="utf-8") as f:
                f.write(raw)
        records = parse_accounting(cfg.scheduler, raw)
        report = summarise(records, cfg.cost_coefficient, cfg.cost_elapsed_weight, cfg.cost_requested_weight)
        write_csv(records, os.path.join(usage_dir, "usage.csv"))
        with open(self._local("USAGE.md"), "w", encoding="utf-8") as f:
            f.write(report)
        print(report)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="hpcjob", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--config", default="hpcjob.toml", help="config file (default: ./hpcjob.toml)")
    p.add_argument("--dry-run", action="store_true", help="print the ssh/scp commands instead of running them")
    sub = p.add_subparsers(dest="command", required=True)
    sub.add_parser("sync", help="copy src/ and jobs/ to the cluster (CRLF -> LF for text files)")
    s = sub.add_parser("submit", help="sync, then submit a job script from jobs/")
    s.add_argument("script")
    s.add_argument("--resource", "-r", help="resource request, passed to the scheduler verbatim")
    s.add_argument("--walltime", "-t", help="HH:MM:SS (capped by defaults.max_walltime)")
    s.add_argument("--name", "-n")
    s.add_argument("--trial", action="store_true", help="short test run: no account flag, tight walltime cap")
    s.add_argument("args", nargs="*", help="arguments passed to the job script (use -- before them)")
    sub.add_parser("status", help="queue status")
    lg = sub.add_parser("log", help="tail a job's log"); lg.add_argument("jobid"); lg.add_argument("--lines", type=int, default=100)
    w = sub.add_parser("watch", help="wait for a job to finish, then show its log"); w.add_argument("jobid")
    c = sub.add_parser("cancel", help="cancel job(s)"); c.add_argument("jobids", nargs="+")
    pl = sub.add_parser("pull", help="download results"); pl.add_argument("remote_rel"); pl.add_argument("local_dir", nargs="?")
    ps = sub.add_parser("push", help="upload a local folder (non-recursive)"); ps.add_argument("local_dir"); ps.add_argument("remote_rel")
    r = sub.add_parser("run", help="run an arbitrary command on the login node"); r.add_argument("cmd", nargs="+")
    u = sub.add_parser("usage", help="fetch accounting records, write USAGE.md and usage/usage.csv"); u.add_argument("--since")
    return p


def main(argv: list[str] | None = None) -> int:
    ns = build_parser().parse_args(argv)
    try:
        cfg = load_config(ns.config)
        app = App(cfg, dry_run=ns.dry_run)
        if ns.command == "sync":
            app.sync()
        elif ns.command == "submit":
            app.submit(ns.script, ns.resource, ns.walltime, ns.name, ns.trial, ns.args)
        elif ns.command == "status":
            app.status()
        elif ns.command == "log":
            app.log(ns.jobid, ns.lines)
        elif ns.command == "watch":
            app.watch(ns.jobid)
        elif ns.command == "cancel":
            app.cancel(ns.jobids)
        elif ns.command == "pull":
            app.pull(ns.remote_rel, ns.local_dir)
        elif ns.command == "push":
            app.push(ns.local_dir, ns.remote_rel)
        elif ns.command == "run":
            app.run(ns.cmd)
        elif ns.command == "usage":
            app.usage(ns.since)
    except (RemoteError, ValueError, FileNotFoundError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
