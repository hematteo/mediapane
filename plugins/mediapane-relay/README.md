# mediapane-relay

A [mediapane](../../README.md) plugin for clusters whose batch jobs can't reach your pane.

Compute nodes usually can't see the login node's `/tmp`, so a `mediapane ssh` socket doesn't help
a job running under Slurm. Instead, give the job a spool folder on a filesystem both can see:

```sh
# in the job script (or your shell rc on the cluster)
export MEDIAPANE_SPOOL=~/.cache/mediapane/spool
python train.py          # mediapane.show(...) / mp ... as usual
```

When no pane is reachable, every push is written to that folder instead (one `.mp` file per
push, in the wire format). On a machine that can reach the pane, forward them:

```sh
mediapane ssh login-node      # from your laptop: the pane's socket is forwarded
mediapane relay               # on the login node, inside that session (say, in tmux)
```

`mediapane relay` checks the folder every `--interval` seconds (0.5 by default), sends complete
pushes oldest first, and deletes each once the pane has accepted it. Pushes the pane rejects
are moved to `failed/` in the spool. If no pane is reachable it says so once and keeps them
until one is. Partial files left by writers that died are removed after an hour.
`--once` forwards what's there and exits (status 1 if a pane couldn't be reached), for cron or
a job epilogue.

Options: `--spool DIR` (default `$MEDIAPANE_SPOOL`), `--interval SECONDS`, `--once`.

## Install

```sh
uv tool install mediapane --with mediapane-relay
```

Only the machine running the relay needs the plugin. Jobs only need mediapane itself.

Development: `uv sync && uv run pytest` in this directory.
