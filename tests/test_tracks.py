import pytest

from app.tracks import TrackError, load_track, parse_spec


def spec(checks, **extra):
    return {"id": "t-1", "skill": "docker", "title": "T", "verifier": {"type": "state_check", "checks": checks}, **extra}


def test_docker_track_loads_in_order_with_a_unique_check_each():
    specs = load_track("docker")
    assert [s.id[:9] for s in specs] == ["docker-01", "docker-02", "docker-03", "docker-04", "docker-05"]
    for s in specs:
        assert s.unique_keys, s.id


def test_port_mapping_spec_matches_design():
    s = {t.id: t for t in load_track("docker")}["docker-03-port-mapping"]
    assert [c.kind for c in s.checks] == ["container_running", "port_published", "http_ok"]
    assert s.unique_keys == {"1:port_published"}
    assert s.checks[1].params == {"container_port": 80, "host_port": 8080}


def test_rejects_spec_without_unique_check():
    with pytest.raises(TrackError, match="unique"):
        parse_spec(spec([{"container_running": {"image": "nginx"}}]))


@pytest.mark.parametrize("vtype", ["test_suite", "quiz", "ai_review"])
def test_rejects_reserved_verifier_types(vtype):
    data = spec([{"container_running": {"image": "nginx", "unique": True}}])
    data["verifier"]["type"] = vtype
    with pytest.raises(TrackError, match="reserved"):
        parse_spec(data)


def test_rejects_unknown_check_kind_param_and_key():
    with pytest.raises(TrackError, match="unknown check kind"):
        parse_spec(spec([{"teleport": {"unique": True}}]))
    with pytest.raises(TrackError, match="unknown params"):
        parse_spec(spec([{"container_running": {"imgae": "nginx", "unique": True}}]))
    with pytest.raises(TrackError, match="unknown keys"):
        parse_spec(spec([{"container_running": {"image": "nginx", "unique": True}}], titel="x"))


def test_rejects_non_mapping_and_bad_yaml(tmp_path):
    with pytest.raises(TrackError, match="mapping"):
        parse_spec(None)
    (tmp_path / "docker").mkdir()
    (tmp_path / "docker" / "a.yaml").write_text("id: [unclosed", encoding="utf-8")
    with pytest.raises(TrackError, match="invalid YAML"):
        load_track("docker", tmp_path)
