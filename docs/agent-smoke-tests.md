# Manual smoke tests — `director agent`

The automated suite covers the agent with temporary repositories, fake runners,
and rendered service definitions; it never mutates the host's service manager.
These manual checks cover what only a real machine can: systemd/launchd
installation, restart recovery, and end-to-end runs with real providers.

Conventions: run from any directory; `<repo>` is a real git repository with a
working `.director` provider/gate configuration. Use a scratch
`DIRECTOR_AGENT_HOME` if you don't want to touch `~/.director/agent`.

## 1. Service installation (Linux, systemd)

```bash
director agent install --port 8642
systemctl --user status director-agent.service   # active (running)
cat ~/.config/systemd/user/director-agent.service # absolute ExecStart, Restart=on-failure
director agent status --json | jq .running        # true
director agent install --port 8642                # idempotent: "unit unchanged"
```

Kill the process (`kill <pid>` from `agent status`) and confirm systemd restarts
it within a few seconds (`Restart=on-failure`).

## 2. Service installation (macOS, launchd)

```bash
director agent install --port 8642
launchctl print gui/$(id -u)/io.github.manziman.director-agent | head
plutil -lint ~/Library/LaunchAgents/io.github.manziman.director-agent.plist
director agent status --json | jq .running        # true
```

Kill the serve process and confirm launchd relaunches it (KeepAlive on
unsuccessful exit). Log out/in and confirm the agent starts at login.

## 3. Service environment

```bash
printf 'PATH=%s\n' "$PATH" > ~/.director/agent/agent.env
director agent restart
director agent status --json | jq .warnings       # no PATH/provider warnings
```

Add a malformed line (`just words`) to `agent.env` and confirm
`director agent serve` refuses to start with the file and line number.

## 4. Concurrent same-repo jobs

```bash
director agent submit --repo <repo> --input task-a.md --json   # note job A
director agent submit --repo <repo> --input task-b.md --json   # note job B
director agent list --state running       # both running (capacity permitting)
```

While they run, verify in `<repo>`: `git status` clean, current branch and HEAD
unchanged, and `git branch` shows both `director/job-…` branches when the jobs
finish. Open `http://127.0.0.1:8642/` — both jobs listed; each `/job/<id>/`
page shows its own DAG/logs/cost.

## 5. Status inspection and wait semantics

```bash
director agent show <job> --json | jq '{state, dag, cost_total, recommended_action}'
director agent wait <job> --timeout 5; echo $?    # 6 while running (job untouched)
director agent wait <job>; echo $?                # 0 succeeded / 1 otherwise
director agent events <job> --after 0             # JSON Lines, increasing seq
```

## 6. Restart recovery

While a job is running: `director agent stop` (or `systemctl --user stop`),
then start the agent again.

- The still-live runner is re-adopted (`show` → `runner_alive: true`).
- `kill -9` a runner mid-job, restart the agent: the job goes `interrupted`,
  is re-queued automatically, and resumes from its persisted planning/run state
  without a duplicate runner.

## 7. Cancellation

```bash
director agent cancel <running-job>    # exits 0, state "cancelled"
pgrep -g <runner-pgid>                 # empty: whole process tree is gone
director agent cancel <same-job>       # idempotent, still "cancelled"
```

## 8. Uninstall

```bash
director agent uninstall
systemctl --user status director-agent.service   # not found (Linux)
launchctl print gui/$(id -u)/io.github.manziman.director-agent  # error (macOS)
director agent uninstall                          # idempotent: "not installed"
director agent list                               # completed jobs still readable
```
