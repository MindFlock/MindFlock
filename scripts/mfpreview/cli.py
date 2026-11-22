"""``scripts/preview.py``: start, shoot and stop the isolated UI preview.

    preview.py up [--scenario sky] [--detach]    start it (Ctrl-C stops it)
    preview.py shot --view sky --theme dark -o sky.png
    preview.py down                               stop it, clean up
    preview.py status | scenarios

``shot`` reuses a running preview of the same scenario, or starts one just
for the shot and stops it afterwards.
"""

from __future__ import annotations

import argparse
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from mfpreview import scenarios, shots
from mfpreview.sandbox import (
    PreviewError,
    Sandbox,
    cleanup_stale,
    default_run_dir,
    lock_held,
    read_state,
    real_tmux,
)

ENTRY = Path(__file__).resolve().parents[1] / "preview.py"


class _Stop(Exception):
    pass


def _raise_stop(signum, _frame):
    raise _Stop(signal.Signals(signum).name)


def _print(obj: Any, as_json: bool) -> None:
    if as_json or not isinstance(obj, str):
        print(json.dumps(obj, indent=None if as_json else 2), flush=True)
    else:
        print(obj, flush=True)


def _run_dir(a) -> Path:
    return Path(a.run_dir) if getattr(a, "run_dir", None) else default_run_dir()


# --------------------------------------------------------------------------- #
# up
# --------------------------------------------------------------------------- #
def boot(a, scenario: str) -> Sandbox:
    sc = scenarios.get(scenario)
    sb = Sandbox(
        scenario=scenario,
        scratch_parent=a.scratch_parent,
        run_dir=_run_dir(a),
        port=a.port,
        onboarded=sc.get("onboarded", True),
        keep=a.keep,
    )
    sb.start()
    try:
        made = scenarios.seed(sb, scenario)
    except BaseException:
        sb.stop()
        raise
    sb.write_state("ready", seeded=made)
    return sb


def cmd_up(a) -> int:
    if a.detach:
        return _up_detached(a)
    for sig in (signal.SIGTERM, signal.SIGHUP):
        signal.signal(sig, _raise_stop)
    sb: Optional[Sandbox] = None
    try:
        sb = boot(a, a.scenario)
        st = read_state(sb.run_dir) or {}
        _print(
            {
                "url": sb.base_url,
                "phone": sb.base_url + "/m",
                "scenario": a.scenario,
                "seeded": st.get("seeded"),
                "scratch": str(sb.scratch),
                "log": str(sb.log_path),
                "stop": "Ctrl-C, or: scripts/preview.py down",
            },
            a.json,
        )
        while True:
            time.sleep(3600)
    except (KeyboardInterrupt, _Stop):
        pass
    except PreviewError as err:
        print("preview: %s" % err, file=sys.stderr, flush=True)
        return 2
    finally:
        if sb is not None:
            report = sb.stop()
            print(
                "preview stopped: %s" % json.dumps(report), file=sys.stderr, flush=True
            )
    return 0


def _up_detached(a) -> int:
    run_dir = _run_dir(a)
    if lock_held(run_dir):
        st = read_state(run_dir) or {}
        print(
            "preview: a preview is already running (scenario %r at %s); "
            "`scripts/preview.py down` first" % (st.get("scenario"), st.get("url")),
            file=sys.stderr,
        )
        return 2
    run_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    argv = [
        sys.executable,
        str(ENTRY),
        "up",
        "--scenario",
        a.scenario,
        "--json",
        "--run-dir",
        str(run_dir),
    ]
    if a.port:
        argv += ["--port", str(a.port)]
    if a.scratch_parent:
        argv += ["--scratch-parent", a.scratch_parent]
    if a.keep:
        argv.append("--keep")
    log = open(run_dir / "supervisor.log", "w")  # noqa: SIM115 — handed to the child
    child = subprocess.Popen(
        argv,
        stdin=subprocess.DEVNULL,
        stdout=log,
        stderr=subprocess.STDOUT,
        start_new_session=True,
    )
    started = time.time()
    deadline = started + a.timeout
    while time.time() < deadline:
        st = read_state(run_dir) or {}
        if st.get("supervisor_pid") == child.pid and st.get("state") == "ready":
            _print(
                {k: st.get(k) for k in ("url", "scenario", "seeded", "scratch", "log")}
                | {
                    "phone": st.get("url", "") + "/m",
                    "stop": "scripts/preview.py down",
                },
                a.json,
            )
            return 0
        if child.poll() is not None:
            print(
                "preview: failed to start:\n%s"
                % (run_dir / "supervisor.log").read_text(errors="replace")[-4000:],
                file=sys.stderr,
            )
            return 2
        time.sleep(0.5)
    os.kill(child.pid, signal.SIGTERM)
    print("preview: not ready after %ss; stopped it" % a.timeout, file=sys.stderr)
    return 2


# --------------------------------------------------------------------------- #
# down / status
# --------------------------------------------------------------------------- #
def _is_supervisor(pid: int) -> bool:
    try:
        cmd = Path("/proc/%d/cmdline" % pid).read_bytes().split(b"\0")
    except OSError:
        return False
    return any(part.endswith(b"preview.py") for part in cmd) and b"up" in cmd


def stop_running(run_dir: Path, timeout: float = 30.0) -> Dict[str, Any]:
    st = read_state(run_dir)
    if not st and not lock_held(run_dir):
        return {"stopped": False, "why": "no preview is running"}
    pid = (st or {}).get("supervisor_pid")
    if lock_held(run_dir) and isinstance(pid, int) and _is_supervisor(pid):
        os.kill(pid, signal.SIGTERM)
        deadline = time.time() + timeout
        while time.time() < deadline and lock_held(run_dir):
            time.sleep(0.2)
        if lock_held(run_dir):
            os.kill(pid, signal.SIGKILL)
            time.sleep(0.5)
    if lock_held(run_dir):
        return {
            "stopped": False,
            "why": "the lock is held by a process that is not a preview",
        }
    report: Dict[str, Any] = {"stopped": True}
    left = read_state(run_dir)
    if left:  # the supervisor died without cleaning up
        report["stale_cleanup"] = cleanup_stale(left, real_tmux())
        try:
            (run_dir / "state.json").unlink()
        except OSError:
            pass
    if st and st.get("scratch"):
        report["scratch_left"] = os.path.exists(st["scratch"])
    return report


def cmd_down(a) -> int:
    report = stop_running(_run_dir(a))
    _print(report, a.json)
    return 0 if report.get("stopped") or "no preview" in str(report.get("why")) else 1


def cmd_status(a) -> int:
    run_dir = _run_dir(a)
    st = read_state(run_dir)
    alive = lock_held(run_dir)
    if not st:
        _print({"running": alive}, a.json)
        return 0
    st["running"] = alive
    _print(st, a.json)
    return 0


def cmd_scenarios(a) -> int:
    for name, sc in scenarios.SCENARIOS.items():
        print("%-16s %s" % (name, sc["summary"]))
    return 0


# --------------------------------------------------------------------------- #
# shot
# --------------------------------------------------------------------------- #
def cmd_shot(a) -> int:
    why = shots.missing()
    if why:
        print("preview: %s" % why, file=sys.stderr)
        return 3
    run_dir = _run_dir(a)
    st = read_state(run_dir) if lock_held(run_dir) else None
    own: Optional[Sandbox] = None
    if st and st.get("state") == "ready":
        if a.scenario and a.scenario != st.get("scenario"):
            print(
                "preview: the running preview is scenario %r, not %r; "
                "`scripts/preview.py down` first" % (st.get("scenario"), a.scenario),
                file=sys.stderr,
            )
            return 2
        base = st["url"]
        scenario = st.get("scenario") or "sky"
    elif st:
        print(
            "preview: a preview is starting; try again when `status` says ready",
            file=sys.stderr,
        )
        return 2
    else:
        for sig in (signal.SIGTERM, signal.SIGHUP):
            signal.signal(sig, _raise_stop)
        scenario = a.scenario or "sky"
        try:
            own = boot(a, scenario)
        except PreviewError as err:
            print("preview: %s" % err, file=sys.stderr)
            return 2
        base = own.base_url
    try:
        results = shots.take(
            base,
            a.view,
            Path(a.out),
            themes=a.theme,
            surfaces=a.surface,
            width=a.width,
            height=a.height,
            full_page=a.full_page,
            settle=a.settle,
            scale=a.scale,
            clicks=a.click or (),
            keep_popups=a.keep_popups,
            first_run=not scenarios.get(scenario).get("onboarded", True),
        )
    except PreviewError as err:
        print("preview: %s" % err, file=sys.stderr)
        return 2
    except (KeyboardInterrupt, _Stop):
        return 130
    finally:
        if own is not None:
            own.stop()
    _print(results if a.json else _shot_lines(results), a.json)
    return 0


def _shot_lines(results: List[Dict[str, Any]]) -> str:
    lines = []
    for r in results:
        lines.append("%s  (%s, %s)" % (r["file"], r["theme"], r["surface"]))
        for e in r["errors"][:5]:
            lines.append("    page error: %s" % e[:300])
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# argv
# --------------------------------------------------------------------------- #
def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="scripts/preview.py",
        description="An isolated scratch MindFlock with sample data, for seeing a UI change. "
        "Never touches the real ~/.mindflock, the real tmux server or ports 9000/8765.",
    )
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument(
        "--run-dir",
        help="where the lock + state file live (default: "
        "$XDG_RUNTIME_DIR/mindflock-preview)",
    )
    common.add_argument("--json", action="store_true", help="machine-readable output")
    boot_opts = argparse.ArgumentParser(add_help=False)
    boot_opts.add_argument("--port", type=int, default=0, help="default: a free port")
    boot_opts.add_argument(
        "--scratch-parent",
        help="where the mfpv-* scratch dir goes "
        "(default: the system temp dir; never inside your home)",
    )
    boot_opts.add_argument(
        "--keep",
        action="store_true",
        help="keep the scratch dir after stopping (for debugging)",
    )
    boot_opts.add_argument("--timeout", type=float, default=180.0)
    sub = p.add_subparsers(dest="cmd", required=True)

    up = sub.add_parser("up", parents=[common, boot_opts], help="start a preview")
    up.add_argument("--scenario", default="sky", choices=list(scenarios.SCENARIOS))
    up.add_argument(
        "--detach",
        action="store_true",
        help="run in the background; print the URL once seeded",
    )
    up.set_defaults(fn=cmd_up)

    shot = sub.add_parser("shot", parents=[common, boot_opts], help="take screenshots")
    shot.add_argument(
        "--scenario",
        default="",
        choices=[""] + list(scenarios.SCENARIOS),
        help="default: the running preview's, else sky",
    )
    shot.add_argument("--view", default="sky", help="one of: " + ", ".join(shots.VIEWS))
    shot.add_argument(
        "--theme", default="dark", help="dark, light, all, or a comma list"
    )
    shot.add_argument(
        "--surface",
        default="",
        help="a surface set (Settings → Appearance), "
        "'all', or a comma list; default: Swallow",
    )
    shot.add_argument("--width", type=int, default=0, help="default 1440 (phone: 390)")
    shot.add_argument("--height", type=int, default=0)
    shot.add_argument("--scale", type=float, default=1.0, help="device pixel ratio")
    shot.add_argument("--full-page", action="store_true")
    shot.add_argument(
        "--settle",
        type=float,
        default=2.0,
        help="seconds to wait after the app has loaded",
    )
    shot.add_argument(
        "--click",
        action="append",
        metavar="SELECTOR",
        help="click this (Playwright selector, e.g. 'text=Stops') before "
        "shooting; repeatable",
    )
    shot.add_argument(
        "--keep-popups",
        action="store_true",
        help="leave the app's 'needs you' pop-up up (default: press Later)",
    )
    shot.add_argument("-o", "--out", default="preview.png")
    shot.set_defaults(fn=cmd_shot)

    down = sub.add_parser("down", parents=[common], help="stop the preview, clean up")
    down.set_defaults(fn=cmd_down)
    status = sub.add_parser("status", parents=[common], help="is one running, where")
    status.set_defaults(fn=cmd_status)
    sc = sub.add_parser("scenarios", help="list the named scenarios")
    sc.set_defaults(fn=cmd_scenarios)
    return p


def main(argv: Optional[List[str]] = None) -> int:
    a = parser().parse_args(argv)
    try:
        return a.fn(a)
    except PreviewError as err:
        print("preview: %s" % err, file=sys.stderr)
        return 2
