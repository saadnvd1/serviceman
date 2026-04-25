#!/usr/bin/env python3
"""serviceman - Daemon-based service manager with auto-restart and logging.

Architecture: single daemon (smd) manages all child processes directly.
CLI (sm) sends commands to daemon via Unix socket. No per-service monitors,
no forking from CLI, no zombie processes, no SSH hangs.

Inspired by Supervisor's daemon+client model.
"""

import argparse
import json
import os
import select
import signal
import socket
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

# Paths
SM_DIR = Path.home() / ".serviceman"
SERVICES_FILE = SM_DIR / "services.json"
PIDS_DIR = SM_DIR / "pids"
LOGS_DIR = SM_DIR / "logs"
SOCK_PATH = SM_DIR / "sm.sock"
DAEMON_PID_FILE = SM_DIR / "daemon.pid"
LAUNCHD_DIR = Path.home() / "Library" / "LaunchAgents"


def ensure_dirs():
    SM_DIR.mkdir(parents=True, exist_ok=True)
    PIDS_DIR.mkdir(exist_ok=True)
    LOGS_DIR.mkdir(exist_ok=True)


def load_services():
    if SERVICES_FILE.exists():
        return json.loads(SERVICES_FILE.read_text())
    return {}


def save_services(services):
    ensure_dirs()
    SERVICES_FILE.write_text(json.dumps(services, indent=2))


def get_log_file(name):
    return LOGS_DIR / f"{name}.log"


def get_plist_file(name):
    return LAUNCHD_DIR / f"com.serviceman.{name}.plist"


# ============================================================
# DAEMON
# ============================================================

class Daemon:
    """Single long-running process that manages all service children."""

    def __init__(self):
        self.children = {}   # name → Popen object
        self.backoffs = {}   # name → current backoff seconds
        self.stopping = {}   # name → stop_time (for SIGKILL escalation)
        self.autorestart = set()  # names that should auto-restart
        self.running = True

    def start(self):
        ensure_dirs()
        # Install SIGCHLD handler to reap children immediately
        signal.signal(signal.SIGCHLD, self._sigchld)
        signal.signal(signal.SIGTERM, self._sigterm)
        signal.signal(signal.SIGINT, self._sigterm)

        # Clean up stale socket
        SOCK_PATH.unlink(missing_ok=True)

        # Start listening
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.bind(str(SOCK_PATH))
        sock.listen(5)
        sock.setblocking(False)

        DAEMON_PID_FILE.write_text(str(os.getpid()))

        # Auto-start services that were running before daemon restart
        self._autostart_running()

        # Main loop
        while self.running:
            self._check_restarts()
            self._check_kill_escalation()

            # Poll for client connections (1s timeout for periodic checks)
            try:
                readable, _, _ = select.select([sock], [], [], 1.0)
            except (OSError, ValueError):
                break

            if readable:
                try:
                    conn, _ = sock.accept()
                    self._handle_client(conn)
                except OSError:
                    pass

        # Shutdown: stop all children
        for name in list(self.children):
            self._stop_child(name)
        sock.close()
        SOCK_PATH.unlink(missing_ok=True)
        DAEMON_PID_FILE.unlink(missing_ok=True)

    def _sigchld(self, signum, frame):
        """Reap all dead children immediately — prevents zombies."""
        while True:
            try:
                pid, status = os.waitpid(-1, os.WNOHANG)
                if pid == 0:
                    break
            except ChildProcessError:
                break

    def _sigterm(self, signum, frame):
        self.running = False

    def _autostart_running(self):
        """On daemon startup, restart services that have PID files (were running)."""
        services = load_services()
        for name in services:
            pid_file = PIDS_DIR / f"{name}.pid"
            if pid_file.exists():
                pid_file.unlink(missing_ok=True)
                self._start_child(name)

    def _start_child(self, name):
        """Spawn a child process for a service."""
        if name in self.children and self.children[name].poll() is None:
            return f"already running (PID {self.children[name].pid})"

        services = load_services()
        if name not in services:
            return f"service '{name}' not found"

        svc = services[name]
        log_path = get_log_file(name)

        try:
            log = open(log_path, "a")
            log.write(f"\n[{datetime.now().isoformat()}] Starting: {svc['cmd']}\n")
            log.flush()

            proc = subprocess.Popen(
                svc["cmd"], shell=True, cwd=svc["cwd"],
                stdout=log, stderr=log,
                stdin=subprocess.DEVNULL,
                start_new_session=True,
            )
            self.children[name] = proc
            self.backoffs[name] = 1
            self.autorestart.add(name)

            # Write PID file for status checks
            (PIDS_DIR / f"{name}.pid").write_text(str(proc.pid))

            return f"started (PID {proc.pid})"
        except Exception as e:
            return f"failed: {e}"

    def _stop_child(self, name):
        """Send SIGTERM to child process group."""
        self.autorestart.discard(name)

        if name not in self.children:
            # No tracked child — check for orphans by PID file
            pid_file = PIDS_DIR / f"{name}.pid"
            if pid_file.exists():
                try:
                    pid = int(pid_file.read_text().strip())
                    os.killpg(pid, signal.SIGTERM)
                except (ProcessLookupError, ValueError, PermissionError, OSError):
                    pass
                pid_file.unlink(missing_ok=True)
            return "stopped"

        proc = self.children[name]
        if proc.poll() is not None:
            # Already dead
            del self.children[name]
            (PIDS_DIR / f"{name}.pid").unlink(missing_ok=True)
            return "stopped (was already dead)"

        try:
            os.killpg(proc.pid, signal.SIGTERM)
        except (ProcessLookupError, PermissionError, OSError):
            pass

        # Schedule SIGKILL escalation after 5s
        self.stopping[name] = time.monotonic()
        return "stopping"

    def _check_kill_escalation(self):
        """Escalate to SIGKILL for children that didn't die after SIGTERM."""
        now = time.monotonic()
        done = []
        for name, stop_time in self.stopping.items():
            if now - stop_time < 5:
                continue
            if name in self.children:
                proc = self.children[name]
                if proc.poll() is None:
                    try:
                        os.killpg(proc.pid, signal.SIGKILL)
                    except (ProcessLookupError, PermissionError, OSError):
                        pass
                del self.children[name]
            (PIDS_DIR / f"{name}.pid").unlink(missing_ok=True)
            done.append(name)
        for name in done:
            del self.stopping[name]

    def _check_restarts(self):
        """Restart children that died unexpectedly."""
        for name in list(self.children):
            if name in self.stopping:
                # Being intentionally stopped — check if done
                proc = self.children[name]
                if proc.poll() is not None:
                    del self.children[name]
                    (PIDS_DIR / f"{name}.pid").unlink(missing_ok=True)
                    if name in self.stopping:
                        del self.stopping[name]
                continue

            proc = self.children[name]
            if proc.poll() is not None:
                # Died unexpectedly
                rc = proc.returncode
                log_path = get_log_file(name)
                try:
                    with open(log_path, "a") as log:
                        log.write(f"[{datetime.now().isoformat()}] Exited with code {rc}\n")
                except OSError:
                    pass

                del self.children[name]
                (PIDS_DIR / f"{name}.pid").unlink(missing_ok=True)

                if name in self.autorestart:
                    backoff = self.backoffs.get(name, 1)
                    time.sleep(min(backoff, 0.5))  # brief pause, don't block loop long
                    self._start_child(name)
                    self.backoffs[name] = min(backoff * 2, 30)

    def _handle_client(self, conn):
        """Process one command from a CLI client."""
        try:
            data = conn.recv(4096).decode()
            if not data:
                conn.close()
                return

            msg = json.loads(data)
            cmd = msg.get("cmd")
            name = msg.get("name")

            if cmd == "start":
                result = self._start_child(name)
                conn.sendall(json.dumps({"ok": True, "msg": result}).encode())

            elif cmd == "stop":
                result = self._stop_child(name)
                # Wait briefly for process to actually die
                if name in self.children:
                    proc = self.children[name]
                    try:
                        proc.wait(timeout=3)
                    except subprocess.TimeoutExpired:
                        pass
                    if proc.poll() is not None:
                        del self.children[name]
                        (PIDS_DIR / f"{name}.pid").unlink(missing_ok=True)
                        if name in self.stopping:
                            del self.stopping[name]
                        result = "stopped"
                conn.sendall(json.dumps({"ok": True, "msg": result}).encode())

            elif cmd == "restart":
                self._stop_child(name)
                if name in self.children:
                    proc = self.children[name]
                    try:
                        proc.wait(timeout=3)
                    except subprocess.TimeoutExpired:
                        try:
                            os.killpg(proc.pid, signal.SIGKILL)
                        except (ProcessLookupError, PermissionError, OSError):
                            pass
                    if proc.poll() is not None:
                        del self.children[name]
                    if name in self.stopping:
                        del self.stopping[name]
                (PIDS_DIR / f"{name}.pid").unlink(missing_ok=True)
                result = self._start_child(name)
                conn.sendall(json.dumps({"ok": True, "msg": result}).encode())

            elif cmd == "status":
                services = load_services()
                statuses = {}
                names = [name] if name else list(services.keys())
                for n in names:
                    if n in self.children and self.children[n].poll() is None:
                        statuses[n] = {"running": True, "pid": self.children[n].pid}
                    else:
                        statuses[n] = {"running": False}
                conn.sendall(json.dumps({"ok": True, "statuses": statuses}).encode())

            elif cmd == "ping":
                conn.sendall(json.dumps({"ok": True, "msg": "pong"}).encode())

            else:
                conn.sendall(json.dumps({"ok": False, "msg": f"unknown cmd: {cmd}"}).encode())

        except Exception as e:
            try:
                conn.sendall(json.dumps({"ok": False, "msg": str(e)}).encode())
            except Exception:
                pass
        finally:
            conn.close()


def _daemon_running():
    """Check if daemon is alive."""
    if not DAEMON_PID_FILE.exists():
        return False
    try:
        pid = int(DAEMON_PID_FILE.read_text().strip())
        os.kill(pid, 0)
        return True
    except (ProcessLookupError, ValueError):
        DAEMON_PID_FILE.unlink(missing_ok=True)
        return False


def _ensure_daemon():
    """Start daemon if not running. Returns True if daemon is available."""
    if _daemon_running():
        return True

    # Daemonize: fork, setsid, redirect stdio, run daemon
    pid = os.fork()
    if pid > 0:
        # Parent — wait a moment for daemon to start listening
        os.waitpid(pid, 0)
        for _ in range(20):
            if SOCK_PATH.exists():
                return True
            time.sleep(0.1)
        return False

    # Child — double fork to fully detach
    os.setsid()
    pid2 = os.fork()
    if pid2 > 0:
        os._exit(0)

    # Grandchild — the actual daemon
    devnull = os.open(os.devnull, os.O_RDWR)
    os.dup2(devnull, 0)
    os.dup2(devnull, 1)
    os.dup2(devnull, 2)
    os.close(devnull)

    daemon = Daemon()
    daemon.start()
    os._exit(0)


def _send_cmd(cmd, name=None, timeout=10):
    """Send command to daemon via Unix socket. Auto-starts daemon if needed."""
    if not _ensure_daemon():
        print("Error: could not start daemon", file=sys.stderr)
        sys.exit(1)

    try:
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(timeout)
        sock.connect(str(SOCK_PATH))
        msg = {"cmd": cmd}
        if name:
            msg["name"] = name
        sock.sendall(json.dumps(msg).encode())
        data = sock.recv(8192).decode()
        sock.close()
        return json.loads(data)
    except (ConnectionRefusedError, FileNotFoundError):
        # Daemon socket stale — clean up and retry once
        SOCK_PATH.unlink(missing_ok=True)
        DAEMON_PID_FILE.unlink(missing_ok=True)
        if not _ensure_daemon():
            print("Error: could not start daemon", file=sys.stderr)
            sys.exit(1)
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(timeout)
        sock.connect(str(SOCK_PATH))
        msg = {"cmd": cmd}
        if name:
            msg["name"] = name
        sock.sendall(json.dumps(msg).encode())
        data = sock.recv(8192).decode()
        sock.close()
        return json.loads(data)


# ============================================================
# CLI COMMANDS
# ============================================================

def cmd_add(args):
    services = load_services()
    if args.name in services and not args.force:
        print(f"Service '{args.name}' already exists. Use -f to overwrite.")
        sys.exit(1)

    services[args.name] = {
        "cmd": args.command,
        "cwd": args.cwd or os.getcwd(),
        "added": datetime.now().isoformat()
    }
    save_services(services)
    print(f"Added service '{args.name}'")


def cmd_remove(args):
    services = load_services()
    if args.name not in services:
        print(f"Service '{args.name}' not found")
        sys.exit(1)

    # Check if running via daemon
    resp = _send_cmd("status", args.name)
    statuses = resp.get("statuses", {})
    if statuses.get(args.name, {}).get("running"):
        print(f"Service '{args.name}' is running. Stop it first.")
        sys.exit(1)

    del services[args.name]
    save_services(services)
    get_log_file(args.name).unlink(missing_ok=True)
    print(f"Removed service '{args.name}'")


def cmd_start(args):
    resp = _send_cmd("start", args.name)
    msg = resp.get("msg", "")
    if "not found" in msg:
        print(f"Service '{args.name}' not found")
        sys.exit(1)
    print(f"Started '{args.name}'" if "started" in msg else msg)


def cmd_stop(args):
    resp = _send_cmd("stop", args.name)
    print(f"Stopped '{args.name}'")


def cmd_restart(args):
    resp = _send_cmd("restart", args.name)
    msg = resp.get("msg", "")
    print(f"Restarted '{args.name}'" if "started" in msg else f"Restarted '{args.name}': {msg}")


def cmd_status(args):
    services = load_services()

    if args.name:
        if args.name not in services:
            print(f"Service '{args.name}' not found")
            sys.exit(1)

    resp = _send_cmd("status", args.name)
    statuses = resp.get("statuses", {})

    for name, st in statuses.items():
        if st.get("running"):
            pid = st.get("pid", "?")
            status = f"\033[32mrunning\033[0m (PID {pid})"
        else:
            status = "\033[31mstopped\033[0m"
        print(f"{name}: {status}")


def cmd_list(args):
    services = load_services()
    if not services:
        print("No services registered")
        return

    # Get statuses from daemon
    try:
        resp = _send_cmd("status")
        statuses = resp.get("statuses", {})
    except Exception:
        statuses = {}

    for name, svc in services.items():
        st = statuses.get(name, {})
        running = st.get("running", False)
        status = "\033[32m●\033[0m" if running else "\033[31m●\033[0m"
        pid_info = f" (PID {st['pid']})" if running and "pid" in st else ""
        print(f"{status} {name}{pid_info}")
        print(f"  cmd: {svc['cmd']}")
        print(f"  cwd: {svc['cwd']}")


def cmd_logs(args):
    services = load_services()
    if args.name not in services:
        print(f"Service '{args.name}' not found")
        sys.exit(1)

    log_file = get_log_file(args.name)

    if args.clear:
        log_file.unlink(missing_ok=True)
        print(f"Cleared logs for '{args.name}'")
        return

    if not log_file.exists():
        print(f"No logs for '{args.name}'")
        return

    if args.follow:
        try:
            subprocess.run(
                ["tail", "-f", str(log_file)],
                stdin=subprocess.DEVNULL,
            )
        except KeyboardInterrupt:
            pass
    else:
        lines = log_file.read_text().splitlines()
        for line in lines[-args.lines:]:
            print(line)


def cmd_start_all(args):
    services = load_services()
    if not services:
        print("No services registered")
        return
    for name in services:
        resp = _send_cmd("start", name)
        msg = resp.get("msg", "")
        print(f"  {name}: {msg}")


def cmd_stop_all(args):
    services = load_services()
    if not services:
        print("No services registered")
        return
    for name in services:
        _send_cmd("stop", name)
        print(f"  Stopped '{name}'")


def cmd_restart_all(args):
    services = load_services()
    if not services:
        print("No services registered")
        return
    for name in services:
        resp = _send_cmd("restart", name)
        msg = resp.get("msg", "")
        print(f"  {name}: {msg}")


def cmd_enable(args):
    services = load_services()
    if args.name not in services:
        print(f"Service '{args.name}' not found")
        sys.exit(1)

    svc = services[args.name]
    plist_path = get_plist_file(args.name)
    LAUNCHD_DIR.mkdir(parents=True, exist_ok=True)

    plist_content = f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>com.serviceman.{args.name}</string>
    <key>ProgramArguments</key>
    <array>
        <string>{sys.executable}</string>
        <string>{SM_DIR}/serviceman.py</string>
        <string>start</string>
        <string>{args.name}</string>
    </array>
    <key>RunAtLoad</key>
    <true/>
    <key>WorkingDirectory</key>
    <string>{svc['cwd']}</string>
</dict>
</plist>
"""
    plist_path.write_text(plist_content)
    subprocess.run(["launchctl", "load", str(plist_path)], capture_output=True)
    print(f"Enabled '{args.name}' - will start on login")


def cmd_disable(args):
    services = load_services()
    if args.name not in services:
        print(f"Service '{args.name}' not found")
        sys.exit(1)

    plist_path = get_plist_file(args.name)
    if not plist_path.exists():
        print(f"Service '{args.name}' not enabled")
        return

    subprocess.run(["launchctl", "unload", str(plist_path)], capture_output=True)
    plist_path.unlink(missing_ok=True)
    print(f"Disabled '{args.name}' - won't start on login")


def cmd_edit(args):
    services = load_services()
    if args.name not in services:
        print(f"Service '{args.name}' not found")
        sys.exit(1)

    svc = services[args.name]
    changed = False

    if args.command:
        svc["cmd"] = args.command
        changed = True

    if args.cwd:
        svc["cwd"] = args.cwd
        changed = True

    if changed:
        services[args.name] = svc
        save_services(services)
        print(f"Updated '{args.name}'")
        # Check if running
        try:
            resp = _send_cmd("status", args.name)
            if resp.get("statuses", {}).get(args.name, {}).get("running"):
                print("Restart the service for changes to take effect")
        except Exception:
            pass
    else:
        print(f"Service '{args.name}':")
        print(f"  cmd: {svc['cmd']}")
        print(f"  cwd: {svc['cwd']}")


def cmd_daemon(args):
    """Run the daemon in the foreground (for debugging)."""
    daemon = Daemon()
    daemon.start()


def main():
    ensure_dirs()

    parser = argparse.ArgumentParser(prog="sm", description="Simple service manager")
    subs = parser.add_subparsers(dest="cmd", help="Commands")

    p = subs.add_parser("add", help="Add a service")
    p.add_argument("name", help="Service name")
    p.add_argument("command", help="Command to run")
    p.add_argument("-c", "--cwd", help="Working directory")
    p.add_argument("-f", "--force", action="store_true", help="Overwrite existing")
    p.set_defaults(func=cmd_add)

    p = subs.add_parser("remove", help="Remove a service")
    p.add_argument("name", help="Service name")
    p.set_defaults(func=cmd_remove)

    p = subs.add_parser("start", help="Start a service")
    p.add_argument("name", help="Service name")
    p.set_defaults(func=cmd_start)

    p = subs.add_parser("stop", help="Stop a service")
    p.add_argument("name", help="Service name")
    p.set_defaults(func=cmd_stop)

    p = subs.add_parser("restart", help="Restart a service")
    p.add_argument("name", help="Service name")
    p.set_defaults(func=cmd_restart)

    p = subs.add_parser("status", help="Show service status")
    p.add_argument("name", nargs="?", help="Service name (optional)")
    p.set_defaults(func=cmd_status)

    p = subs.add_parser("list", help="List all services")
    p.set_defaults(func=cmd_list)

    p = subs.add_parser("logs", help="View service logs")
    p.add_argument("name", help="Service name")
    p.add_argument("-f", "--follow", action="store_true", help="Follow log output")
    p.add_argument("-n", "--lines", type=int, default=50, help="Number of lines")
    p.add_argument("--clear", action="store_true", help="Clear logs")
    p.set_defaults(func=cmd_logs)

    p = subs.add_parser("start-all", help="Start all services")
    p.set_defaults(func=cmd_start_all)

    p = subs.add_parser("stop-all", help="Stop all services")
    p.set_defaults(func=cmd_stop_all)

    p = subs.add_parser("restart-all", help="Restart all services")
    p.set_defaults(func=cmd_restart_all)

    p = subs.add_parser("enable", help="Enable auto-start on login")
    p.add_argument("name", help="Service name")
    p.set_defaults(func=cmd_enable)

    p = subs.add_parser("disable", help="Disable auto-start on login")
    p.add_argument("name", help="Service name")
    p.set_defaults(func=cmd_disable)

    p = subs.add_parser("edit", help="Edit service config")
    p.add_argument("name", help="Service name")
    p.add_argument("-c", "--command", help="New command")
    p.add_argument("-w", "--cwd", help="New working directory")
    p.set_defaults(func=cmd_edit)

    p = subs.add_parser("daemon", help="Run daemon in foreground (debug)")
    p.set_defaults(func=cmd_daemon)

    args = parser.parse_args()
    if not args.cmd:
        parser.print_help()
        return

    args.func(args)


if __name__ == "__main__":
    main()
