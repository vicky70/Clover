"""Real-Docker E2E (eng D9): for each Slice 1 task, build the correct state and expect
a pass, then break the unique check and expect it to fail. Skipped without a daemon.

    pytest -m docker
"""
import io
import time
from datetime import timedelta

import httpx
import pytest
from typer.testing import CliRunner

from app.tracks import load_track
from clover import cli
from clover.checks import docker as checks
from tests.conftest import ADMIN_SECRET, MON_OPEN

pytestmark = pytest.mark.docker

SPECS = {s.id: s.to_dict() for s in load_track("docker")}
CONTAINERS = ["clover-hello", "clover-web", "clover-port", "clover-vol", "clover-app"]
DOCKERFILE = b"FROM nginx:alpine\nRUN echo '<h1>Clover</h1>' > /usr/share/nginx/html/index.html\n"


@pytest.fixture(scope="module")
def dk():
    try:
        client = checks.connect()
    except checks.EnvironmentProblem as e:
        pytest.skip(f"no Docker daemon: {e}")
    for image in ("hello-world", "nginx:alpine", "alpine"):
        client.images.pull(image)
    return client


def _cleanup(dk):
    from docker.errors import NotFound
    for name in CONTAINERS:
        try:
            dk.containers.get(name).remove(force=True)
        except NotFound:
            pass
    for remove in (lambda: dk.volumes.get("clover-data").remove(force=True),
                   lambda: dk.images.remove("clover-app:v1", force=True)):
        try:
            remove()
        except NotFound:
            pass


@pytest.fixture
def clean(dk):
    _cleanup(dk)
    yield dk
    _cleanup(dk)


def wait_http(url, timeout=20):
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            if httpx.get(url, timeout=2).status_code < 400:
                return
        except httpx.HTTPError:
            pass
        time.sleep(0.3)
    raise AssertionError(f"{url} never came up")


def unique_results(result):
    return [c["passed"] for c in result["checks"] if c["unique"]]


def assert_pass_then_break(dk, task_id, setup, break_unique):
    setup(dk)
    good = checks.run_checks(SPECS[task_id], dk)
    assert good["status"] == "pass", good
    break_unique(dk)
    bad = checks.run_checks(SPECS[task_id], dk)
    assert bad["status"] == "fail", bad
    assert unique_results(bad) == [False], bad


def test_docker_01_hello_run(clean):
    def setup(dk):
        c = dk.containers.run("hello-world", name="clover-hello", detach=True)
        c.wait(timeout=30)
    assert_pass_then_break(clean, "docker-01-hello-run", setup,
                           lambda dk: dk.containers.get("clover-hello").remove())


def test_docker_02_detached_nginx(clean):
    assert_pass_then_break(
        clean, "docker-02-detached-nginx",
        lambda dk: dk.containers.run("nginx:alpine", name="clover-web", detach=True),
        lambda dk: dk.containers.get("clover-web").stop(timeout=2))


def test_docker_03_port_mapping(clean):
    def setup(dk):
        dk.containers.run("nginx:alpine", name="clover-port", detach=True, ports={"80/tcp": 8080})
        wait_http("http://localhost:8080")

    def unpublish(dk):
        dk.containers.get("clover-port").remove(force=True)
        # leftover state from docker-02 still satisfies the non-unique check
        dk.containers.run("nginx:alpine", name="clover-web", detach=True)

    assert_pass_then_break(clean, "docker-03-port-mapping", setup, unpublish)


def test_docker_04_named_volume(clean):
    def setup(dk):
        dk.volumes.create("clover-data")
        c = dk.containers.run("alpine", ["sleep", "300"], name="clover-vol", detach=True,
                              volumes={"clover-data": {"bind": "/data", "mode": "rw"}})
        assert c.exec_run(["sh", "-c", "echo clover > /data/hello.txt"]).exit_code == 0

    def drop_volume(dk):
        dk.containers.get("clover-vol").remove(force=True)
        dk.volumes.get("clover-data").remove()

    assert_pass_then_break(clean, "docker-04-named-volume", setup, drop_volume)


def test_docker_05_build_image(clean):
    def setup(dk):
        dk.images.build(fileobj=io.BytesIO(DOCKERFILE), tag="clover-app:v1", rm=True)
        dk.containers.run("clover-app:v1", name="clover-app", detach=True, ports={"80/tcp": 8085})
        wait_http("http://localhost:8085")

    def drop_image(dk):
        dk.containers.get("clover-app").remove(force=True)
        dk.images.remove("clover-app:v1", force=True)

    assert_pass_then_break(clean, "docker-05-build-image", setup, drop_image)


def test_full_loop_clover_verify_to_passed(clean, client, app, clock, monkeypatch, tmp_path, settings):
    """clover init/schedule/verify against the server with real Docker, then the window is PASSED."""
    runner = CliRunner()
    monkeypatch.setenv("CLOVER_HOME", str(tmp_path / "home"))
    monkeypatch.setattr(cli, "http_client", client)
    monkeypatch.setattr(cli, "docker_client", clean)
    assert runner.invoke(cli.app, ["init", "--server", "http://testserver", "--admin-secret", ADMIN_SECRET,
                                   "--name", "Vicky"]).exit_code == 0
    assert runner.invoke(cli.app, ["schedule"]).exit_code == 0
    app.state.scheduler.run_pass(MON_OPEN - timedelta(hours=1))
    clock.set(MON_OPEN + timedelta(minutes=10))
    clean.containers.run("hello-world", name="clover-hello", detach=True).wait(timeout=30)
    r = runner.invoke(cli.app, ["verify"])
    assert r.exit_code == 0, r.output
    decided = app.state.scheduler.run_pass(MON_OPEN + timedelta(hours=1, minutes=15))["decided"]
    assert [o for _, o in decided] == ["PASSED"]
