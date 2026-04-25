# serviceman

Lightweight process manager for background services with auto-restart and logging.

## Install

```bash
./install.sh
```

## Usage

```bash
# Add a service
sm add myserver "python3 server.py"
sm add myserver "npm start" -c /path/to/project

# Start/stop
sm start myserver
sm stop myserver
sm restart myserver

# Manage all services
sm start-all
sm stop-all
sm restart-all

# Status
sm status           # all services
sm status myserver  # specific service

# List all services
sm list

# View logs
sm logs myserver        # last 50 lines
sm logs myserver -n 100 # last 100 lines
sm logs myserver -f     # follow (tail -f)
sm logs myserver --clear # clear logs

# Edit service config
sm edit myserver -c "new command"
sm edit myserver -w /new/working/dir

# Auto-start on login (macOS launchd)
sm enable myserver   # start on login
sm disable myserver  # remove from login

# Remove
sm remove myserver
```

## Features

- Auto-restart on crash with exponential backoff (1s -> 30s)
- Combined stdout/stderr logging
- Auto-start on login via launchd
- No dependencies (Python stdlib only)

## Storage

All data stored in `~/.serviceman/`:
- `services.json` - service definitions
- `pids/` - PID files
- `logs/` - log files
