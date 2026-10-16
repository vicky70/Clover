"""state_check runner for Docker tasks, via the Docker SDK (docker-py).

`run_checks` never raises for Docker trouble: an unreachable daemon or SDK error
becomes status `environment_error`, which the server never counts as a run (eng D8).
"""
from __future__ import annotations

import io
import tarfile
from typing import Any, Callable

import httpx

try:
    import docker
    from docker.errors import DockerException, NotFound
except ImportError:  # pragma: no cover - docker is a hard dependency of the CLI
    docker = None
    DockerException = NotFound = Exception


class EnvironmentProblem(Exception):
    pass


def _image_matches(ref: str, wanted: str) -> bool:
    """'nginx' matches nginx:alpine / docker.io/library/nginx:1.27; 'app:v1' needs that tag."""
    def split(r: str) -> tuple[str, str | None]:
        r = r.split("@", 1)[0]
        name, _, tag = r.rpartition(":") if ":" in r.rsplit("/", 1)[-1] else (r, "", "")
        for prefix in ("docker.io/library/", "docker.io/", "library/"):
            if name.startswith(prefix):
                name = name[len(prefix):]
        return name, tag or None
    rn, rt = split(ref)
    wn, wt = split(wanted)
    return rn == wn and (wt is None or rt == wt)


def _container_images(c) -> list[str]:
    refs = [c.attrs.get("Config", {}).get("Image", "")]
    try:
        refs += list(c.image.tags)
    except (DockerException, AttributeError):
        pass
    return [r for r in refs if r]


def _find(client, name: str | None, image: str | None, *, running: bool) -> list:
    found = []
    for c in client.containers.list(all=not running):
        if name and c.name != name:
            continue
        if image and not any(_image_matches(r, image) for r in _container_images(c)):
            continue
        found.append(c)
    return found


def _desc(name, image) -> str:
    return " ".join(x for x in (f"named {name}" if name else "", f"from image {image}" if image else "") if x)


def check_container_running(client, p: dict) -> tuple[bool, str]:
    hits = _find(client, p.get("name"), p.get("image"), running=True)
    if hits:
        return True, f"running: {hits[0].name}"
    return False, f"no running container {_desc(p.get('name'), p.get('image'))}"


def check_container_exited(client, p: dict) -> tuple[bool, str]:
    hits = _find(client, p.get("name"), p.get("image"), running=False)
    if not hits:
        return False, f"no container {_desc(p.get('name'), p.get('image'))} (did you use --rm?)"
    c = hits[0]
    state = c.attrs.get("State", {})
    if c.status != "exited":
        return False, f"{c.name} is {c.status}, expected exited"
    want = p.get("exit_code", 0)
    if state.get("ExitCode") != want:
        return False, f"{c.name} exited with {state.get('ExitCode')}, expected {want}"
    return True, f"{c.name} exited with {want}"


def check_port_published(client, p: dict) -> tuple[bool, str]:
    key = f"{p['container_port']}/{p.get('protocol', 'tcp')}"
    for c in client.containers.list():
        for binding in (c.attrs.get("NetworkSettings", {}).get("Ports") or {}).get(key) or []:
            if str(binding.get("HostPort")) == str(p["host_port"]):
                return True, f"{c.name} publishes {key} on host port {p['host_port']}"
    return False, f"no running container publishes {key} on host port {p['host_port']}"


def check_http_ok(client, p: dict, http_get: Callable[..., Any] | None = None) -> tuple[bool, str]:
    get = http_get or httpx.get
    try:
        r = get(p["url"], timeout=5.0, follow_redirects=True)
    except httpx.HTTPError as e:
        return False, f"{p['url']} unreachable: {type(e).__name__}"
    if r.status_code >= 400:
        return False, f"{p['url']} returned HTTP {r.status_code}"
    if p.get("contains") and p["contains"] not in r.text:
        return False, f"{p['url']} responded but doesn't contain {p['contains']!r}"
    return True, f"{p['url']} returned HTTP {r.status_code}"


def check_volume_exists(client, p: dict) -> tuple[bool, str]:
    try:
        client.volumes.get(p["name"])
    except NotFound:
        return False, f"no volume named {p['name']}"
    return True, f"volume {p['name']} exists"


def _get_container(client, name: str):
    try:
        return client.containers.get(name)
    except NotFound:
        return None


def check_volume_mounted(client, p: dict) -> tuple[bool, str]:
    c = _get_container(client, p["container"])
    if c is None:
        return False, f"no container named {p['container']}"
    for m in c.attrs.get("Mounts", []):
        if m.get("Type") == "volume" and m.get("Name") == p["volume"] and m.get("Destination") == p["destination"]:
            return True, f"{p['volume']} mounted at {p['destination']} in {p['container']}"
    return False, f"{p['container']} doesn't mount volume {p['volume']} at {p['destination']}"


def check_file_in_container(client, p: dict) -> tuple[bool, str]:
    c = _get_container(client, p["container"])
    if c is None:
        return False, f"no container named {p['container']}"
    try:
        stream, _stat = c.get_archive(p["path"])
    except NotFound:
        return False, f"{p['path']} not found in {p['container']}"
    with tarfile.open(fileobj=io.BytesIO(b"".join(stream))) as tar:
        member = next((m for m in tar.getmembers() if m.isfile()), None)
        if member is None:
            return False, f"{p['path']} is not a file"
        text = tar.extractfile(member).read().decode("utf-8", "replace")
    if p.get("contains") and p["contains"] not in text:
        return False, f"{p['path']} doesn't contain {p['contains']!r}"
    return True, f"{p['path']} found in {p['container']}"


def check_image_exists(client, p: dict) -> tuple[bool, str]:
    try:
        client.images.get(p["tag"])
    except NotFound:
        return False, f"no image tagged {p['tag']}"
    return True, f"image {p['tag']} exists"


CHECKS: dict[str, Callable[[Any, dict], tuple[bool, str]]] = {
    "container_running": check_container_running,
    "container_exited": check_container_exited,
    "port_published": check_port_published,
    "http_ok": check_http_ok,
    "volume_exists": check_volume_exists,
    "volume_mounted": check_volume_mounted,
    "file_in_container": check_file_in_container,
    "image_exists": check_image_exists,
}


def connect():
    if docker is None:
        raise EnvironmentProblem("the docker Python package is not installed")
    try:
        client = docker.from_env()
        client.ping()
    except DockerException as e:
        raise EnvironmentProblem(f"Docker is not running or not reachable ({e})") from e
    return client


def run_checks(task: dict, client=None) -> dict:
    """Run a task spec's state checks. Returns the payload for POST /verify."""
    result = {"task_id": task["id"], "status": "fail", "checks": [], "error": None}
    try:
        client = client or connect()
        for check in task["verifier"]["checks"]:
            fn = CHECKS.get(check["kind"])
            if fn is None:
                raise EnvironmentProblem(f"this CLI doesn't know check '{check['kind']}'; upgrade clover-cli")
            passed, detail = fn(client, check["params"])
            result["checks"].append({"key": check["key"], "kind": check["kind"], "passed": passed,
                                     "unique": check.get("unique", False), "detail": detail})
    except (EnvironmentProblem, DockerException) as e:
        return {**result, "status": "environment_error", "checks": [], "error": str(e)}
    if all(c["passed"] for c in result["checks"]):
        result["status"] = "pass"
    return result
