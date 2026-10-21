import os
import textwrap

import pytest

from hpcjob.accounting import job_cost, parse_qacct, parse_sacct, summarise
from hpcjob.cli import App, main, undo_msys_conversion
from hpcjob.config import load_config, parse_walltime
from hpcjob.remote import RemoteError
from hpcjob.schedulers import SLURM, UGE, get_scheduler

QACCT_SAMPLE = textwrap.dedent("""\
    ==============================================================
    qname        gpu.q
    hostname     r8n3
    group        proj-x
    owner        alice
    jobname      train_ddp
    jobnumber    8502869
    account      sge
    qsub_time    08/26/2026 05:06:14
    start_time   08/26/2026 05:07:01
    end_time     08/26/2026 05:31:40
    granted_pe   NONE
    slots        1
    failed       0
    exit_status  0
    ru_wallclock 1479
    category     -l gpu_1=1,h_rt=2400 -g proj-x
    ==============================================================
    qname        gpu.q
    jobname      selftest
    jobnumber    8503330
    qsub_time    08/26/2026 06:29:53
    start_time   08/26/2026 06:30:10
    end_time     08/26/2026 06:30:44
    slots        1
    failed       100 : assumedly after job
    exit_status  1
    ru_wallclock 34
    category     -l node_q=1,h_rt=1800
""")

SACCT_SAMPLE = textwrap.dedent("""\
    JobID|JobName|State|ExitCode|Elapsed|Timelimit|AllocNodes|NCPUS|ReqTRES|Start|End
    1001|train|COMPLETED|0:0|01:30:00|04:00:00|2|64|billing=64,cpu=64,gres/gpu=4,node=2|2026-08-01T10:00:00|2026-08-01T11:30:00
    1001.batch|batch|COMPLETED|0:0|01:30:00||2|64||2026-08-01T10:00:00|2026-08-01T11:30:00
    1002|eval|TIMEOUT|0:0|00:10:00|00:10:00|1|8|billing=8,cpu=8,node=1|2026-08-01T12:00:00|2026-08-01T12:10:00
    1003|smoke|CANCELLED by 1234|0:0|00:00:05|00:03:00|1|1|cpu=1,node=1|2026-08-01T13:00:00|2026-08-01T13:00:05
""")


# ---- walltime ------------------------------------------------------------------
@pytest.mark.parametrize("text, seconds", [("4:00:00", 14400), ("0:03:00", 180), ("90", 90),
                                           ("10:30", 630), ("1-02:00:00", 93600)])
def test_parse_walltime(text, seconds):
    assert parse_walltime(text) == seconds


# ---- schedulers ---------------------------------------------------------------------
def test_uge_submit_command():
    cmd = UGE.submit_command(workdir="/gs/p", jobsdir="/gs/p/auto/jobs", logdir="/gs/p/auto/logs",
                             account="grp", resource="node_q=1", walltime="2:00:00", name="run",
                             script="run.sh", args=["a.py", "--x", "1"])
    assert cmd == ("cd /gs/p && qsub -g grp -l node_q=1 -l h_rt=2:00:00 -N run "
                   "-o /gs/p/auto/logs -j y /gs/p/auto/jobs/run.sh a.py --x 1")
    assert UGE.parse_jobid('Your job 8502869 ("run") has been submitted') == "8502869"


def test_slurm_submit_command_without_account():
    cmd = SLURM.submit_command(workdir="/home/u/p", jobsdir="/home/u/p/auto/jobs", logdir="/home/u/p/auto/logs",
                               account="", resource="--gres=gpu:1 -N 1", walltime="4:00:00", name="train",
                               script="train.sh", args=[])
    assert cmd == ("cd /home/u/p && sbatch --gres=gpu:1 -N 1 --time=4:00:00 --job-name=train "
                   "--output=/home/u/p/auto/logs/train.o%j /home/u/p/auto/jobs/train.sh")
    assert SLURM.parse_jobid("Submitted batch job 1001") == "1001"
    assert SLURM.parse_jobid("sbatch: error") is None


def test_template_override_and_validation():
    s = get_scheduler("slurm", {"status": "squeue --me"})
    assert s.status == "squeue --me" and s.cancel == SLURM.cancel
    with pytest.raises(ValueError):
        get_scheduler("uge", {"nonsense": "x"})


def test_tail_command_reads_the_job_log():
    cmd = UGE.tail_command(jobid="42", logdir="/l", lines=80)
    assert cmd.startswith("tail -n 80 /l/*.o42") and "no log found" in cmd


# ---- accounting -------------------------------------------------------------------
def test_parse_qacct():
    recs = parse_qacct(QACCT_SAMPLE)
    assert [r.jobid for r in recs] == ["8502869", "8503330"]
    ok, bad = recs
    assert ok.ok and ok.elapsed_s == 1479 and ok.requested_s == 2400 and ok.resource == "gpu_1=1"
    assert not bad.ok and bad.state == "FAILED" and bad.exit_code == 1 and bad.resource == "node_q=1"


def test_parse_sacct_skips_steps_and_normalises_state():
    recs = parse_sacct(SACCT_SAMPLE)
    assert [r.jobid for r in recs] == ["1001", "1002", "1003"]
    assert recs[0].ok and recs[0].nodes == 2 and recs[0].elapsed_s == 5400 and recs[0].requested_s == 14400
    assert recs[1].state == "TIMEOUT" and recs[2].state == "CANCELLED"


def test_cost_model_and_summary():
    recs = parse_qacct(QACCT_SAMPLE)
    coef = {"gpu_1": 0.2, "node_q": 0.25}
    # elapsed-only billing
    assert job_cost(recs[0], coef, 1.0, 0.0) == pytest.approx(0.2 * 1479 / 3600)
    # mixed elapsed/requested billing
    assert job_cost(recs[0], coef, 0.7, 0.1) == pytest.approx(0.2 * (0.7 * 1479 + 0.1 * 2400) / 3600)
    report = summarise(recs, coef)
    assert "jobs: **2**" in report and "| gpu_1=1 | 1 |" in report and "FAILED" in report


# ---- CLI in dry-run mode ----------------------------------------------------------------
@pytest.fixture
def project(tmp_path):
    (tmp_path / "hpcjob.toml").write_text(textwrap.dedent("""\
        [remote]
        host = "cluster"
        workdir = "/home/u/proj"
        scheduler = "uge"
        account = "grp"
        [defaults]
        resource = "node_q=1"
        walltime = "1:00:00"
        max_walltime = "24:00:00"
        poll_seconds = 60
        [cost.coefficient]
        node_q = 0.25
        """), encoding="utf-8")
    (tmp_path / "jobs").mkdir()
    (tmp_path / "jobs" / "run.sh").write_text("#!/bin/bash\npython \"$@\"\n")
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "a.py").write_text("print('hi')\n")
    return tmp_path


def test_dry_run_submit_builds_commands_and_logs(project, capsys):
    cfg = load_config(str(project / "hpcjob.toml"))
    app = App(cfg, dry_run=True)
    app.submit("run.sh", None, "0:30:00", None, False, ["a.py", "--epochs", "3"])
    issued = [" ".join(c) for c in app.remote.log]
    assert any("scp" in c and c.endswith("cluster:/home/u/proj/") for c in issued)          # src sync
    assert any("scp" in c and c.endswith("cluster:/home/u/proj/auto/jobs/") for c in issued)  # job sync
    assert any("sed -i" in c for c in issued)                                                # CRLF -> LF
    assert any("qsub -g grp -l node_q=1 -l h_rt=0:30:00 -N run" in c and c.endswith("run.sh a.py --epochs 3")
               for c in issued)
    log = (project / "hpcjob-joblog.tsv").read_text().strip().split("\t")
    assert log[1] == "(dry-run)" and log[2] == "run.sh" and log[5] == "a.py --epochs 3"


def test_walltime_cap_is_enforced(project):
    cfg = load_config(str(project / "hpcjob.toml"))
    app = App(cfg, dry_run=True)
    with pytest.raises(SystemExit):
        app.submit("run.sh", None, "30:00:00", None, False, [])
    with pytest.raises(SystemExit):
        app.submit("run.sh", None, "0:10:00", None, True, [])      # trial cap is 3 min


def test_main_dry_run_status(project, capsys):
    rc = main(["--config", str(project / "hpcjob.toml"), "--dry-run", "status"])
    assert rc == 0
    assert "[dry-run] ssh -o BatchMode=yes" in capsys.readouterr().out


def test_configured_ssh_and_scp_executables_are_used(project):
    toml = project / "hpcjob.toml"
    toml.write_text(toml.read_text().replace(
        'host = "cluster"', 'host = "cluster"\nssh = "C:/OpenSSH/ssh.exe"\nscp = "C:/OpenSSH/scp.exe"'))
    app = App(load_config(str(toml)), dry_run=True)
    app.sync()
    firsts = {c[0] for c in app.remote.log}
    assert firsts == {"C:/OpenSSH/ssh.exe", "C:/OpenSSH/scp.exe"}


def test_uge_is_running_looks_the_job_up_in_qstat():
    cmd = UGE.is_running.format(jobid="42")
    assert cmd.startswith("out=$(qstat 2>/dev/null) || exit 2;")
    assert "awk -v j=42 '$1 == j {f = 1} END {exit !f}'" in cmd


def test_watch_polls_locally_then_shows_the_log(project, monkeypatch, capsys):
    app = App(load_config(str(project / "hpcjob.toml")))
    answers = iter([0, 2, 0, 1])                       # running, check failed, running, gone
    checks, sleeps = [], []
    monkeypatch.setattr(app.remote, "code", lambda cmd: checks.append(cmd) or next(answers))
    monkeypatch.setattr(app.remote, "run", lambda cmd, **kw: "last log line\n")
    monkeypatch.setattr("hpcjob.cli.time.sleep", sleeps.append)
    assert app.watch("42") == 0
    assert checks == [UGE.is_running.format(jobid="42")] * 4 and sleeps == [60, 60, 60]
    out = capsys.readouterr().out
    assert "could not check job 42" in out and "last log line" in out


def test_watch_stops_after_repeated_check_failures(project, monkeypatch):
    app = App(load_config(str(project / "hpcjob.toml")))
    monkeypatch.setattr(app.remote, "code", lambda cmd: 255)
    monkeypatch.setattr("hpcjob.cli.time.sleep", lambda s: None)
    with pytest.raises(RemoteError):
        app.watch("42")


def test_watch_gives_up_after_max_minutes(project, monkeypatch, capsys):
    app = App(load_config(str(project / "hpcjob.toml")))
    clock = iter(range(0, 10_000, 60))                 # each call advances one poll
    monkeypatch.setattr(app.remote, "code", lambda cmd: 0)
    monkeypatch.setattr("hpcjob.cli.time.sleep", lambda s: None)
    monkeypatch.setattr("hpcjob.cli.time.monotonic", lambda: next(clock))
    assert app.watch("42", max_minutes=3) == 3
    assert "STILL_RUNNING" in capsys.readouterr().out


def test_undo_msys_conversion_restores_cluster_paths():
    root = "C:/Program Files/Git"
    got = undo_msys_conversion(["C:/Program Files/Git/gs/fs/g/run", "--data=C:/Program Files/Git/work/8",
                                "relative/x", "C:/Users/me/local", "a /b"], root)
    assert got == ["/gs/fs/g/run", "--data=/work/8", "relative/x", "C:/Users/me/local", "a /b"]
    assert undo_msys_conversion(["/gs/fs/x"], None) == ["/gs/fs/x"]       # not under Git Bash


def test_missing_job_script_is_reported(project):
    cfg = load_config(str(project / "hpcjob.toml"))
    with pytest.raises(SystemExit):
        App(cfg, dry_run=True).submit("nope.sh", None, None, None, False, [])
