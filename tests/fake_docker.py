"""A small fake of the docker-py client surface that clover/checks/docker.py uses."""
import io
import tarfile

from docker.errors import NotFound


class FakeImage:
    def __init__(self, tags):
        self.tags = tags


class FakeContainer:
    def __init__(self, name, image, status="running", exit_code=0, ports=None, mounts=None, files=None):
        self.name = name
        self.status = status
        self.image = FakeImage([image])
        self.attrs = {
            "Config": {"Image": image},
            "State": {"ExitCode": exit_code},
            "NetworkSettings": {"Ports": {k: [{"HostIp": "0.0.0.0", "HostPort": str(v)}]
                                          for k, v in (ports or {}).items()}},
            "Mounts": [{"Type": "volume", "Name": n, "Destination": d} for n, d in (mounts or {}).items()],
        }
        self.files = files or {}

    def get_archive(self, path):
        if path not in self.files:
            raise NotFound(f"{path} not found")
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w") as tar:
            data = self.files[path].encode()
            info = tarfile.TarInfo(path.rsplit("/", 1)[-1])
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
        return iter([buf.getvalue()]), {"name": path}


class _Containers:
    def __init__(self, items):
        self.items = items

    def list(self, all=False, **_):
        return [c for c in self.items if all or c.status == "running"]

    def get(self, name):
        for c in self.items:
            if c.name == name:
                return c
        raise NotFound(name)


class _Named:
    def __init__(self, names):
        self.names = set(names)

    def get(self, name):
        if name not in self.names:
            raise NotFound(name)
        return name


class FakeDocker:
    def __init__(self, containers=(), volumes=(), images=()):
        self.containers = _Containers(list(containers))
        self.volumes = _Named(volumes)
        self.images = _Named(images)
