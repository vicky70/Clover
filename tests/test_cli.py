from datetime import timedelta

import httpx
import pytest
from typer.testing import CliRunner

from app.tracks import load_track
from clover import cli
from clover.checks import docker as checks
from tests.conftest import ADMIN_SECRET, MON_OPEN
from tests.fake_docker import FakeContainer, FakeDocker

runner = CliRunner()
SPECS = {s.id: s.to_dict() for s in load_track("docker")}


@pytest.fixture
def cli_env(client, monkeypatch, tmp_path):
    monkeypatch.setenv("CLOVER_HOME", str(tmp_path / "home"))
    monkeypatch.setattr(cli, "http_client", client)
    monkeypatch.setattr(cli, "docker_client", None)
    r = runner.invoke(cli.app, ["init", "--server", "http://testserver", "--admin-secret", ADMIN_SECRET,
                                "--name", "Vicky"])
    assert r.exit_code == 0, r.output
    return client


def offline_client():
    def boom(request):
        raise httpx.ConnectError("no route", request=request)
    return httpx.Client(transport=httpx.MockTransport(boom))


# --- checks against the fake docker client ---------------------------------

def test_port_mapping_passes_and_reports_whats_missing(monkeypatch):
    spec = SPECS["docker-03-port-mapping"]
    monkeypatch.setattr(checks.httpx, "get", lambda url, **kw: httpx.Response(200, text="<h1>Welcome to nginx!</h1>"))
    good = FakeDocker([FakeContainer("web", "nginx:alpine", ports={"80/tcp": 8080})])
    assert checks.run_checks(spec, good)["status"] == "pass"
    wrong_port = FakeDocker([FakeContainer("web", "nginx:alpine", ports={"80/tcp": 8081})])
    res = checks.run_checks(spec, wrong_port)
    assert res["status"] == "fail"
    by_kind = {c["kind"]: c for c in res["checks"]}
    assert by_kind["container_running"]["passed"]
    assert not by_kind["port_published"]["passed"]
    assert "host port 8080" in by_kind["port_published"]["detail"]


def test_http_ok_handles_unreachable_and_contains():
    def refused(url, **kw):
        raise httpx.ConnectError("refused")
    assert checks.check_http_ok(None, {"url": "http://localhost:1"}, refused)[0] is False
    page = lambda url, **kw: httpx.Response(200, text="hello")
    assert checks.check_http_ok(None, {"url": "u", "contains": "Clover"}, page)[0] is False
    assert checks.check_http_ok(None, {"url": "u", "contains": "hell"}, page)[0] is True


def test_exited_container_and_exit_code():
    spec = SPECS["docker-01-hello-run"]
    assert checks.run_checks(spec, FakeDocker([FakeContainer("clover-hello", "hello-world", status="exited")]))["status"] == "pass"
    bad = FakeDocker([FakeContainer("clover-hello", "hello-world", status="exited", exit_code=1)])
    assert checks.run_checks(spec, bad)["status"] == "fail"
    assert checks.run_checks(spec, FakeDocker())["checks"][0]["detail"].endswith("(did you use --rm?)")


def test_volume_task_checks_mount_and_file():
    spec = SPECS["docker-04-named-volume"]
    c = FakeContainer("clover-vol", "alpine", mounts={"clover-data": "/data"}, files={"/data/hello.txt": "hi clover"})
    assert checks.run_checks(spec, FakeDocker([c], volumes=["clover-data"]))["status"] == "pass"
    c.files = {}
    res = checks.run_checks(spec, FakeDocker([c], volumes=["clover-data"]))
    assert [x["passed"] for x in res["checks"]] == [True, True, False]


def test_image_matching():
    m = checks._image_matches
    assert m("nginx:alpine", "nginx") and m("docker.io/library/nginx:1.27", "nginx")
    assert not m("nginxx:latest", "nginx")
    assert m("clover-app:v1", "clover-app:v1") and not m("clover-app:v2", "clover-app:v1")
    assert m("localhost:5000/app:v1", "localhost:5000/app")


def test_docker_unreachable_is_environment_error(monkeypatch):
    from docker.errors import DockerException

    def down():
        raise DockerException("Error while fetching server API version")
    monkeypatch.setattr(checks.docker, "from_env", down)
    res = checks.run_checks(SPECS["docker-02-detached-nginx"])
    assert res["status"] == "environment_error" and res["checks"] == []
    assert "not running" in res["error"]


# --- CLI end to end against the server (fake docker) --------------------------

def test_verify_pass_is_recorded_in_window(cli_env, app, clock, monkeypatch):
    assert runner.invoke(cli.app, ["schedule"]).exit_code == 0
    app.state.scheduler.run_pass(MON_OPEN - timedelta(hours=1))
    clock.set(MON_OPEN + timedelta(minutes=10))
    monkeypatch.setattr(cli, "docker_client", FakeDocker([FakeContainer("clover-hello", "hello-world", status="exited")]))
    r = runner.invoke(cli.app, ["verify"])
    assert r.exit_code == 0, r.output
    assert "All checks pass. Recorded (in_window)." in r.output


def test_verify_fail_prints_missing_and_exits_1(cli_env, app, clock, monkeypatch):
    runner.invoke(cli.app, ["schedule"])
    clock.set(MON_OPEN + timedelta(minutes=10))
    monkeypatch.setattr(cli, "docker_client", FakeDocker())
    r = runner.invoke(cli.app, ["verify"])
    assert r.exit_code == 1 and "MISSING" in r.output


def test_verify_environment_error_is_reported_not_counted(cli_env, app, clock, monkeypatch, settings):
    runner.invoke(cli.app, ["schedule"])
    clock.set(MON_OPEN + timedelta(minutes=10))
    monkeypatch.setattr(checks, "connect", lambda: (_ for _ in ()).throw(checks.EnvironmentProblem("Docker is not running")))
    r = runner.invoke(cli.app, ["verify"])
    assert r.exit_code == 3 and "never counts" in r.output
    from app import db
    conn = db.connect(settings.db_path)
    try:
        row = conn.execute("SELECT payload FROM events WHERE type = 'verify_attempted'").fetchone()
        assert '"environment_error"' in row["payload"]
    finally:
        conn.close()


def test_offline_verify_is_queued_then_sent(cli_env, app, clock, monkeypatch):
    runner.invoke(cli.app, ["schedule"])
    clock.set(MON_OPEN + timedelta(minutes=10))
    assert runner.invoke(cli.app, ["task"]).exit_code == 0  # caches the task
    monkeypatch.setattr(cli, "docker_client", FakeDocker([FakeContainer("clover-hello", "hello-world", status="exited")]))
    monkeypatch.setattr(cli, "http_client", offline_client())
    r = runner.invoke(cli.app, ["verify"])
    assert r.exit_code == 2 and "queued" in r.output
    monkeypatch.setattr(cli, "http_client", cli_env)
    clock.set(MON_OPEN + timedelta(minutes=30))
    r = runner.invoke(cli.app, ["verify"])
    assert "Sent a queued run (counted: in_window)." in r.output, r.output


def test_witness_and_admin_commands(cli_env, app, clock):
    r = runner.invoke(cli.app, ["witness", "add", "Anu", "+919876543210"])
    assert r.exit_code == 0 and "https://wa.me/911234567890" in r.output
    r = runner.invoke(cli.app, ["witness", "status"])
    assert "Anu: invited" in r.output and "You are not being witnessed." in r.output
    r = runner.invoke(cli.app, ["admin", "outbox"])
    assert "Last scheduler pass: never" in r.output and "STALE" in r.output
    app.state.scheduler.run_pass(clock.now)
    assert "STALE" not in runner.invoke(cli.app, ["admin", "outbox"]).output
    clock.advance(minutes=10)
    r = runner.invoke(cli.app, ["admin", "outbox"])
    assert "(600s ago)" in r.output and "STALE" in r.output


def test_init_with_wrong_secret_fails(client, monkeypatch, tmp_path):
    monkeypatch.setenv("CLOVER_HOME", str(tmp_path / "h"))
    monkeypatch.setattr(cli, "http_client", client)
    r = runner.invoke(cli.app, ["init", "--server", "http://testserver", "--admin-secret", "x", "--name", "V"])
    assert r.exit_code == 1 and "admin secret required" in r.output
