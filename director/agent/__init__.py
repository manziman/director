"""The machine-local Director agent (issue #38).

A durable job supervisor that accepts, runs, monitors, and retains multiple
Director jobs — including simultaneous jobs against the same Git repository —
with job-scoped storage under `~/.director/agent/`, an orchestrator-friendly
CLI/JSON contract, one read-only web dashboard for every job, and native
user-service installation on Linux (systemd) and macOS (launchd).
"""
