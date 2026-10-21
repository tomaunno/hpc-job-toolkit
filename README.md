# hpc-job-toolkit

Drive a **Slurm** or **Grid Engine (UGE / SGE)** cluster from a laptop with one command:
sync your code, submit a job, wait for it, pull the results, and turn the accounting
records into a usage report.

```
hpcjob submit train_ddp.slurm.sh -r "--gres=gpu:4 --nodes=1" -t 12:00:00 -- train.py --epochs 50
hpcjob watch 8502869
hpcjob pull results/run_42
hpcjob usage --since 2026-08-01
```

It grew out of running a research group's GPU jobs on a university supercomputer
from Windows, where three things kept going wrong: CRLF line endings silently
breaking shell scripts, jobs submitted with a walltime the centre rejects, and
nobody knowing afterwards how many node-hours a project had used. The tool bakes
the fixes in and keeps everything else out of the way.

日本語の説明は [下](#日本語) にあります。

## Features

- **One config file**, `hpcjob.toml`: host, remote directory, scheduler, defaults.
- **Slurm and Grid Engine** out of the box; every scheduler command is a template
  you can override per site without touching code.
- **Safe by default**: walltime is capped (`max_walltime`), `--trial` runs are capped
  harder, `watch` polls the login node no more than once per `poll_seconds`, and
  `usage` refuses to overwrite accounting data with a suspiciously short fetch.
- **Windows-friendly**: uploaded text files are normalised to LF on the cluster, and
  cluster paths that Git Bash rewrote (`/gs/x` -> `C:/.../Git/gs/x`) are restored.
- **`--dry-run`** prints every `ssh` / `scp` command instead of running it.
- **Usage report**: `qacct` / `sacct` output becomes `USAGE.md` and `usage.csv`
  with a configurable cost model (elapsed node-hours or a weighted mix of elapsed
  and requested time, per resource type).
- No dependencies beyond Python 3.11 and an `ssh` that logs in without a password.

## Install

```
pip install git+https://github.com/tomaunno/hpc-job-toolkit
```

or clone and `pip install -e .`. Set up key-based login in `~/.ssh/config`:

```
Host mycluster
    HostName login.cluster.example.edu
    User alice
    IdentityFile ~/.ssh/id_ed25519
```

## Configure

Copy [`examples/hpcjob.toml`](examples/hpcjob.toml) next to your project and edit the
`[remote]` section. The local layout the tool expects:

```
project/
  hpcjob.toml
  src/      *.py etc.   -> copied to <workdir>/
  jobs/     *.sh        -> copied to <workdir>/auto/jobs/
  results/              <- `hpcjob pull` lands here
```

## Commands

| command | what it does |
|---|---|
| `sync` | upload `src/` and `jobs/` (non-recursive), then strip CR from text files |
| `submit SCRIPT [-r RES] [-t WALL] [-n NAME] [--trial] [-- ARGS...]` | sync, submit `jobs/SCRIPT`, print the job id, append a line to `hpcjob-joblog.tsv` |
| `status` | `squeue` / `qstat` |
| `log JOBID [--lines N]` | tail the job's log file |
| `watch JOBID [--max-minutes M]` | block until the job leaves the queue, then show the log tail; with `--max-minutes`, give up after M minutes and exit 3 (the job keeps running) |
| `cancel JOBID...` | `scancel` / `qdel` |
| `pull REMOTE_REL [LOCAL_DIR]` | `scp -r` a result folder back |
| `push LOCAL_DIR REMOTE_REL` | upload an arbitrary folder (non-recursive, LF-normalised) |
| `run -- CMD...` | run a command on the login node |
| `usage [--since DATE]` | fetch accounting, write `USAGE.md` and `usage/usage.csv` |

Global flags: `--config PATH` (default `./hpcjob.toml`), `--dry-run`.

### Resource strings

`-r` is passed to the scheduler verbatim. Slurm: `"--gres=gpu:1 --nodes=1"`,
`"-p gpu -c 8"`. Grid Engine: `"gpu_1=1"` becomes `-l gpu_1=1` (anything already
starting with `-` is left alone).

### Scheduler templates

The defaults live in [`hpcjob/schedulers.py`](hpcjob/schedulers.py). Any of them can be
replaced in `[templates]`:

```toml
[templates]
status = "squeue --me --format='%.10i %.9P %.20j %.8T %.10M'"
accounting = "sacct -u $USER -S {since} -P --format=JobID,JobName,State,ExitCode,Elapsed,Timelimit,AllocNodes,NCPUS,ReqTRES,Start,End"
```

Placeholders: `{workdir} {jobsdir} {logdir} {account_flag} {resource} {walltime} {name} {script} {args} {jobid} {lines} {since}`.

### Cost model

```
cost = nodes * coefficient[resource] * (elapsed_weight * elapsed + requested_weight * requested) / 3600
```

Set `coefficient` per resource string (or its prefix before `=`) in `[cost.coefficient]`.
Elapsed node-hours is the default (`elapsed_weight = 1, requested_weight = 0`);
centres that bill a mix of actual and requested time set both weights.

## Example session

```
$ hpcjob --dry-run submit run_py.slurm.sh -t 0:30:00 -- a.py --epochs 3
[dry-run] ssh -o BatchMode=yes -o ConnectTimeout=15 mycluster 'mkdir -p /home/alice/project ...'
[dry-run] scp -o BatchMode=yes -q src/a.py mycluster:/home/alice/project/
[dry-run] scp -o BatchMode=yes -q jobs/run_py.slurm.sh mycluster:/home/alice/project/auto/jobs/
[dry-run] ssh ... mycluster 'sed -i -e '"'"'s/\r$//'"'"' /home/alice/project/*.py ...'
[dry-run] ssh ... mycluster 'cd /home/alice/project && sbatch --gres=gpu:1 --nodes=1 --time=0:30:00 --job-name=run_py.slurm --output=/home/alice/project/auto/logs/run_py.slurm.o%j /home/alice/project/auto/jobs/run_py.slurm.sh a.py --epochs 3'
synced: 1 source file(s), 1 job script(s)
```

## Tests

```
pip install -e .[test]
pytest
```

Twenty-two tests cover walltime parsing, both schedulers' submit commands and
job-id parsing, the `qacct` and `sacct` parsers, the cost model, and the CLI in
dry-run mode (including the walltime cap and the job log). No cluster is needed.

## Design notes

- Everything that touches the cluster goes through `Remote`, which records every
  command it issues; that is what makes the CLI testable without a scheduler.
- `watch` polls from the laptop, one short `ssh` per `poll_seconds`. An earlier version
  left a `while qstat; sleep` loop on the login node for the whole job, which breaks
  the run-time limit many centres set on login nodes.
- The accounting fetch keeps a raw copy (`usage/accounting_raw.txt`) and will not
  replace it with something less than half its size: a DNS hiccup once wiped a
  month of records this way.
- Nothing here is site-specific. The original per-site script it replaces was a
  160-line PowerShell file with the hostname, group id and paths hard-coded.

## Tooling note

Written with Claude Code as a drafting partner; the design, the site knowledge
behind the safety rules, and the tests are mine. MIT license.

---

## 日本語

Slurm または Grid Engine(UGE/SGE)のクラスタを、手元の PC から 1 コマンドで操作する
ツールです。コードの同期、ジョブ投入、完了待ち、成果物の回収、会計記録からの利用レポート
作成までを扱います。

大学のスパコンで研究室の GPU ジョブを Windows から回していて、毎回つまずいた 3 つ
(CRLF 改行でシェルスクリプトが黙って壊れる、センターの上限を超える walltime で投入して
弾かれる、あとで「どれだけノード時間を使ったか」が誰にも分からない)を仕組みで防ぐために
作りました。

- 設定は `hpcjob.toml` 1 枚。ホスト、リモートの作業ディレクトリ、スケジューラ、既定値
- Slurm と Grid Engine に対応。スケジューラのコマンドはすべてテンプレートで、サイトごとの
  違いは設定ファイルの `[templates]` で上書きできます(コードを触らない)
- 安全側の既定: walltime の上限(`max_walltime`)、`--trial` はさらに短い上限、`watch` の
  ポーリングは `poll_seconds` 以下の頻度(ログインノードへの配慮)、`usage` は取得結果が
  極端に短いとき既存データを上書きしません
- `watch` は手元から `poll_seconds` ごとに短い `ssh` で確かめます(ログインノードに常駐する
  ループを残さない)。`--max-minutes` で待つ上限を決められ、超えたら終了コード 3 で戻ります
- `[remote]` の `ssh` / `scp` で使う実行ファイルを選べます(Windows で ssh-agent を使うなら
  `C:/Windows/System32/OpenSSH/ssh.exe`)
- アップロードしたテキストファイルはクラスタ側で LF に正規化(Windows 対策)。Git Bash が
  書き換えたクラスタ側のパス(`/gs/x` → `C:/.../Git/gs/x`)も元に戻します
- `--dry-run` で実行される `ssh` / `scp` コマンドをすべて表示
- `qacct` / `sacct` の出力を `USAGE.md` と `usage.csv` に変換。コストは資源種別ごとの係数と
  「実行時間と要求時間の重み付き和」で計算(センターの課金方式に合わせて設定)

使い方は上の英語セクションの表と同じです。`pip install -e .[test]` → `pytest` で、
クラスタ無しで 22 件のテストが走ります。

Claude Code を下書きの相棒として使って書きました。設計、安全ルールの元になった運用経験、
テストは私のものです。MIT ライセンス。
