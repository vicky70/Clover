"""clover: laptop CLI for Clover (Slice 1)."""
from __future__ import annotations

import json
from datetime import datetime, timezone

import typer

from clover import api as api_mod
from clover.api import Api, ApiError, Offline
from clover.checks import docker as checks
from clover.queue import Queue

app = typer.Typer(help="Clover: scheduled skill tasks, verified, with a witness.", no_args_is_help=True)
witness_app = typer.Typer(help="Your witness.", no_args_is_help=True)
admin_app = typer.Typer(help="Founder tools.", no_args_is_help=True)
app.add_typer(witness_app, name="witness")
app.add_typer(admin_app, name="admin")

# Test seams: tests swap these for a TestClient-backed Api and a fake Docker client.
http_client = None
docker_client = None


def _api() -> Api:
    try:
        return Api.from_config(http_client)
    except ApiError as e:
        _fail(e.detail)


def _fail(msg: str, code: int = 1):
    typer.secho(msg, fg=typer.colors.RED, err=True)
    raise typer.Exit(code)


def _call(fn, *args):
    try:
        return fn(*args)
    except Offline:
        _fail("Can't reach the Clover server. Check your connection and try again.")
    except ApiError as e:
        _fail(e.detail)


def _queue() -> Queue:
    return Queue(api_mod.home() / "queue.jsonl")


@app.command()
def init(server: str = typer.Option(..., help="Server URL, e.g. https://clover.example.com"),
         admin_secret: str = typer.Option(..., prompt=True, hide_input=True, help="Server admin secret"),
         name: str = typer.Option(..., prompt="Your first name")):
    """Pair this laptop with the server (Slice 1: needs the server's admin secret)."""
    out = _call(Api(server, None, http_client).init, name, admin_secret)
    api_mod.save_config({"server": server, "token": out["token"]})
    typer.secho("Paired. Next: `clover schedule` and `clover witness add`.", fg=typer.colors.GREEN)


@app.command()
def schedule(days: str = typer.Option("mon,tue,wed,thu,fri", help="Comma-separated weekdays"),
             start: str = typer.Option("19:00", help="Window start, HH:MM IST"),
             minutes: int = typer.Option(60, help="Window length in minutes")):
    """Set your recurring task windows."""
    out = _call(_api().schedule, [d.strip() for d in days.split(",") if d.strip()], start, minutes)
    typer.echo("Upcoming windows:")
    for w in out["windows"]:
        typer.echo(f"  {w['label']}")


@app.command()
def pause(day: str = typer.Argument(..., help="Date to pause, YYYY-MM-DD (before its window opens)")):
    """Declare a pause day (max 2 per week). Paused windows send no alert."""
    _call(_api().pause, day)
    typer.echo(f"Paused {day}. Your witness will see the pause in the weekly summary.")


@witness_app.command("add")
def witness_add(name: str, phone: str = typer.Argument(..., help="With country code, e.g. +919876543210")):
    """Invite a witness. Send them the link; their own WhatsApp message is their consent."""
    out = _call(_api().witness_add, name, phone)
    typer.echo(f"{name} is invited. Send them this link yourself:")
    typer.secho(f"  {out['link'] or out['text']}", bold=True)
    typer.echo("They become your witness when they send that message. The invite expires in 7 days.")


@witness_app.command("status")
def witness_status():
    """Show your witness's state."""
    out = _call(_api().witness_status)
    if out.get("state") is None:
        typer.echo("No witness yet. Run `clover witness add <name> <phone>`.")
    else:
        typer.echo(f"{out['name']}: {out['state']}")
        if out["state"] == "expired":
            typer.echo("The invite expired without a reply. Run `clover witness add` again.")
    if out.get("banner"):
        typer.secho(out["banner"], fg=typer.colors.YELLOW)


def _cache_task(out: dict) -> None:
    api_mod.home().mkdir(parents=True, exist_ok=True)
    (api_mod.home() / "task.json").write_text(json.dumps(out), encoding="utf-8")


def _cached_task() -> dict | None:
    p = api_mod.home() / "task.json"
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else None


@app.command()
def task():
    """Show the current task and its instructions."""
    out = _call(_api().current_task)
    _cache_task(out)
    t = out.get("task")
    if t is None:
        typer.echo("Track complete. Nice work.")
        return
    if out.get("window"):
        typer.echo(f"Now: {out['window']['label']}")
    elif out.get("next_window"):
        typer.echo(f"Next window: {out['next_window']['label']}")
    typer.secho(f"{t['id']}: {t['title']}  (~{t['est_minutes']} min)", bold=True)
    typer.echo(t["instructions"])


def _send_queued(api: Api) -> None:
    results, left = _queue().flush(api.verify, Offline)
    for r in results:
        if isinstance(r, ApiError):
            typer.secho(f"A queued run was not counted: {r.detail}", fg=typer.colors.YELLOW)
        else:
            typer.echo(f"Sent a queued run (counted: {r['counted']}).")
    if left:
        typer.echo(f"{left} run(s) still queued.")


@app.command()
def verify(task_id: str = typer.Argument(None, help="Defaults to the current task")):
    """Check your Docker state for the task and report it to the server."""
    api = _api()
    try:
        _send_queued(api)
        out = api.current_task()
        _cache_task(out)
    except Offline:
        out = _cached_task()
        if out is None:
            _fail("Offline and no cached task. Run `clover task` once while online.")
    except ApiError as e:
        _fail(e.detail)
    spec = out.get("task")
    if spec is None or (task_id and not spec["id"].startswith(task_id)):
        _fail(f"{task_id or 'That task'} is not your current task" + (f" ({spec['id']})." if spec else "."))

    result = checks.run_checks(spec, docker_client)
    result["client_created_at"] = datetime.now(timezone.utc).isoformat()

    if result["status"] == "environment_error":
        typer.secho(f"{result['error']}", fg=typer.colors.YELLOW)
        typer.echo("This run is reported but never counts as an attempt. Start Docker and run verify again.")
    else:
        for c in result["checks"]:
            mark = typer.style("PASS", fg=typer.colors.GREEN) if c["passed"] else typer.style("MISSING", fg=typer.colors.RED)
            typer.echo(f"  {mark}  {c['detail']}")

    try:
        sent = api.verify(result)
    except Offline:
        _queue().push(result)
        typer.secho("Offline: queued, will send next time. It counts only if the server receives it "
                    "before the window closes + 15 min.", fg=typer.colors.YELLOW)
        raise typer.Exit(2)
    except ApiError as e:
        _fail(f"Not counted: {e.detail}")
    if result["status"] == "pass":
        typer.secho(f"All checks pass. Recorded ({sent['counted']}).", fg=typer.colors.GREEN)
    elif result["status"] == "fail":
        typer.echo("Not done yet. Fix what's MISSING and run `clover verify` again.")
        raise typer.Exit(1)
    else:
        raise typer.Exit(3)


@admin_app.command("outbox")
def admin_outbox():
    """Show recent witness messages and when the scheduler last finished a pass."""
    out = _call(_api().admin_outbox)
    age = out["last_pass_age_s"]
    line = f"Last scheduler pass: {out['last_pass_at'] or 'never'}" + (f" ({int(age)}s ago)" if age is not None else "")
    if out["loop_stale"]:
        typer.secho(line + "  STALE: the scheduler loop may be stuck; restart the server.", fg=typer.colors.RED)
    else:
        typer.echo(line)
    for r in out["rows"]:
        state = "sent " + r["sent_at"] if r["sent_at"] else (r["skipped_reason"] or f"pending (attempts {r['attempts']})")
        typer.echo(f"  #{r['id']} window={r['window_id']} {r['kind']} -> {r['witness']}: {state}"
                   + (f"  last error: {r['last_error']}" if r["last_error"] and not r["sent_at"] else ""))


@admin_app.command("void")
def admin_void(window_id: int):
    """Settle a SERVER_OUTAGE window as VOID."""
    _call(_api().admin_void, window_id)
    typer.echo(f"Window {window_id} voided.")


if __name__ == "__main__":
    app()
