# serviceman

> Lightweight process manager with auto-restart, logging, and launchd integration

<!--
<p align="center">
	<img src="media/demo.gif" width="600">
</p>
-->

## Install

```sh
git clone https://github.com/saadnvd1/serviceman.git
cd serviceman
./install.sh
```

Make sure `~/bin` is in your `PATH`.

## Usage

```sh
sm <command> [options]
```

### Commands

| Command | Description |
|---------|-------------|
| `add <name> "<cmd>"` | Register a service (`-c` for working dir) |
| `start <name>` | Start a service |
| `stop <name>` | Stop a service |
| `restart <name>` | Restart a service |
| `status [name]` | Show status (all or specific) |
| `list` | List all services |
| `logs <name>` | View logs (`-n` lines, `-f` follow, `--clear`) |
| `edit <name>` | Modify config (`-c` cmd, `-w` dir) |
| `enable <name>` | Auto-start on login (macOS launchd) |
| `disable <name>` | Remove from login items |
| `remove <name>` | Unregister a service |
| `start-all` | Start all services |
| `stop-all` | Stop all services |
| `restart-all` | Restart all services |

### Examples

```sh
# Run a web server with auto-restart
sm add myapp "npm start" -c /path/to/project
sm start myapp

# Check what's running
sm list

# Follow logs
sm logs myapp -f

# Start on login
sm enable myapp
```

## How it works

Single Python daemon (`smd`) manages all child processes via a Unix socket. The CLI (`sm`) sends commands to the daemon. Crashed services auto-restart with exponential backoff (1s → 30s). Combined stdout/stderr captured per-service in `~/.serviceman/logs/`.

## Features

- **Auto-restart** on crash with exponential backoff
- **Combined logging** — stdout and stderr per service
- **launchd integration** — auto-start on macOS login
- **Single file**, zero dependencies, Python stdlib only

## Storage

All state lives in `~/.serviceman/`:

| Path | Contents |
|------|----------|
| `services.json` | Service definitions |
| `pids/` | PID files |
| `logs/` | Per-service log files |

## Related

- [harry-bot](https://github.com/saadnvd1/harry-bot) - Personal AI assistant that uses serviceman for deployment

## License

MIT
