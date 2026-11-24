"""The shipped image and compose example match §spec:deployment.

These read the files a user builds and pastes; a live bring-up of the stack is
checked by hand (it needs Docker), these keep the shape from drifting.
"""
from __future__ import annotations

import json
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent


def _final_stage(dockerfile: str) -> list[str]:
    """The instructions of the Dockerfile's last stage, one logical line each."""
    lines = dockerfile.replace("\\\n", " ").splitlines()
    starts = [i for i, line in enumerate(lines) if line.upper().startswith("FROM ")]
    return [line.strip() for line in lines[starts[-1]:] if line.strip()
            and not line.lstrip().startswith("#")]


def _instruction(stage: list[str], name: str) -> list[str]:
    return [line[len(name):].strip() for line in stage if line.upper().startswith(name + " ")]


def test_image_reads_the_config_from_the_data_mount_by_default() -> None:
    """The entrypoint passes no --config, so every command (and a compose
    healthcheck, which skips the entrypoint) uses the CLI default
    /data/shakedown-config/shakedown.yaml; nothing refers to /config any more."""
    stage = _final_stage((REPO / "Dockerfile").read_text())
    (entrypoint,) = _instruction(stage, "ENTRYPOINT")
    assert json.loads(entrypoint) == ["shakedown"]
    (volume,) = _instruction(stage, "VOLUME")
    assert json.loads(volume) == ["/data"]
    assert not any("/config" in line for line in stage)
    assert not any("SHAKEDOWN_CONFIG" in line for line in stage)


def _compose() -> dict:
    import yaml

    return yaml.safe_load((REPO / "docker-compose.example.yaml").read_text())


SOCKET = "/var/run/docker.sock"


def test_example_compose_has_no_ofelia() -> None:
    """Schedules live in shakedown.yaml and the scheduler container starts them;
    ofelia and its per-collection job labels are gone (§spec:deployment)."""
    text = (REPO / "docker-compose.example.yaml").read_text()
    assert not any("ofelia" in name for name in _compose()["services"])
    assert "ofelia.job" not in text


def test_serve_mounts_the_data_share_once_and_publishes_on_loopback() -> None:
    """One /data mount, so serve can write /data/shakedown-config/ and archive
    and library share a mount; no /config; the port only on host loopback."""
    serve = _compose()["services"]["shakedown-serve"]
    assert serve["volumes"] == ["/share/data:/data"]
    assert serve["ports"] == ["127.0.0.1:8080:8080"]
    assert "SHAKEDOWN_API_TOKEN=${SHAKEDOWN_API_TOKEN:-}" in serve["environment"]
    assert any(e.startswith("TZ=") for e in serve["environment"])


def test_scheduler_shares_serve_image_and_user_and_cannot_write_the_config() -> None:
    """The scheduler runs `shakedown schedule` from the same image as the same
    user, publishes no port, reads the config folder through a read-only mount
    over the data share, and reports its heartbeat as the container's health."""
    services = _compose()["services"]
    serve, scheduler = services["shakedown-serve"], services["shakedown-scheduler"]
    assert scheduler["image"] == serve["image"]
    assert scheduler["user"] == serve["user"]
    assert scheduler["command"] == ["schedule"]
    assert "ports" not in scheduler
    assert scheduler["volumes"] == [
        "/share/data:/data",
        "/share/data/shakedown-config:/data/shakedown-config:ro",
    ]
    assert scheduler["healthcheck"]["test"] == ["CMD", "shakedown", "schedule", "--check"]
    assert scheduler["restart"] == "unless-stopped"
    assert scheduler["stop_grace_period"] == serve["stop_grace_period"] == "2m"
    assert any(e.startswith("TZ=") for e in scheduler["environment"])


def test_no_shakedown_container_mounts_the_docker_socket() -> None:
    services = _compose()["services"]
    for name in ("shakedown-serve", "shakedown-scheduler"):
        assert not any(SOCKET in v for v in services[name].get("volumes", []))


def _env(service: dict) -> dict[str, str]:
    return dict(e.split("=", 1) for e in service.get("environment", []))


WATCHTOWER_LABEL = "com.centurylinklabs.watchtower.enable"


def test_only_the_socket_proxy_mounts_the_docker_socket_read_only_on_an_internal_network() -> None:
    """The proxy alone holds the socket, read-only, on a network with no way out
    and no published port; it refuses exec, volumes, secrets, swarm, and system."""
    compose = _compose()
    services = compose["services"]
    holders = [n for n, s in services.items()
               if any(SOCKET in v for v in s.get("volumes", []))]
    assert holders == ["socket-proxy"]
    proxy = services["socket-proxy"]
    assert proxy["image"].startswith("lscr.io/linuxserver/socket-proxy:")
    assert "@sha256:" in proxy["image"]
    assert proxy["volumes"] == [f"{SOCKET}:{SOCKET}:ro"]
    assert "ports" not in proxy
    assert proxy["networks"] == ["docker-proxy"]
    assert compose["networks"]["docker-proxy"]["internal"] is True
    assert proxy["read_only"] is True
    env = _env(proxy)
    for refused in ("EXEC", "VOLUMES", "SECRETS", "SWARM", "SYSTEM"):
        assert env[refused] == "0", refused


def test_watchtower_updates_only_the_shakedown_containers_daily_through_the_proxy() -> None:
    """Watchtower reaches Docker only through the proxy, updates only containers
    labelled for it — exactly the two Shakedown ones — once a day at 12:00, and
    gives each run the same 120 s to stop as `stop_grace_period`."""
    services = _compose()["services"]
    watchtower = services["watchtower"]
    assert "@sha256:" in watchtower["image"]
    assert not any(SOCKET in v for v in watchtower.get("volumes", []))
    assert "docker-proxy" in watchtower["networks"]
    env = _env(watchtower)
    assert env["DOCKER_HOST"] == "tcp://shakedown-socket-proxy:2375"
    assert env["WATCHTOWER_LABEL_ENABLE"] == "true"
    assert env["WATCHTOWER_SCHEDULE"] == "0 0 12 * * *"
    assert env["WATCHTOWER_TIMEOUT"] == "120s"
    assert "TZ" in env
    labelled = sorted(n for n, s in services.items()
                      if str(s.get("labels", {}).get(WATCHTOWER_LABEL, "")).lower() == "true")
    assert labelled == ["shakedown-scheduler", "shakedown-serve"]
