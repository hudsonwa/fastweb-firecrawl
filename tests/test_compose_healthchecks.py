"""Compose healthchecks must be runnable as written: YAML double quotes turn \\r\\n into real CR/LF, which broke the
browser-egress Python one-liner (always unhealthy)."""
import pathlib
import re
import socket
import subprocess
import sys
import threading

import yaml

COMPOSE = pathlib.Path(__file__).resolve().parents[1] / "docker-compose.yml"


def healthchecks():
    services = yaml.safe_load(COMPOSE.read_text())["services"]
    return {name: s["healthcheck"]["test"] for name, s in services.items() if "healthcheck" in s}


def test_python_healthchecks_compile():
    found = 0
    for name, test in healthchecks().items():
        if test[:3] == ["CMD", "python", "-c"]:
            compile(test[3], f"{name}-healthcheck", "exec")
            found += 1
    assert found >= 1


def test_egress_healthcheck_passes_against_a_200_stats_server():
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)
    port = srv.getsockname()[1]

    def serve():
        c, _ = srv.accept()
        c.recv(1024)
        c.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\n{}")
        c.close()

    threading.Thread(target=serve, daemon=True).start()
    src = healthchecks()["browser-egress"][3].replace("3128", str(port))
    r = subprocess.run([sys.executable, "-c", src], timeout=10)
    assert r.returncode == 0


# ---------- every variable in the README Configuration table reaches the container ----------
README = COMPOSE.parent / "README.md"


def readme_table_vars() -> dict[str, str]:
    """{variable: its table row} for the README Configuration table; `FASTWEB_X_Y` / `_Z` shorthand means
    `FASTWEB_X_Z` (the group prefix is kept)."""
    text = README.read_text()
    table = text.split("## Configuration", 1)[1].split("\n## ", 1)[0]
    out: dict[str, str] = {}
    for row in table.splitlines():
        if not row.startswith("| `"):
            continue
        names = re.findall(r"`([A-Z_]+)`", row.split("|")[1])
        for n in names:
            full = "_".join(names[0].split("_")[:2]) + n if n.startswith("_") else n
            out[full] = row
    return out


def test_readme_table_parses():
    vs = readme_table_vars()
    assert len(vs) > 40 and "FASTWEB_SEARCH_FALLBACK_TTL" in vs and "FASTWEB_PREFETCH_PER_HOST" in vs


def test_every_readme_variable_is_passed_through_by_compose():
    """Setting a documented variable in .env must have an effect: it is interpolated somewhere in compose, or compose
    sets it to a fixed in-network value that the README row names ("compose: ...")."""
    raw = COMPOSE.read_text()
    env = yaml.safe_load(raw)["services"]["fastweb"]["environment"]
    missing = []
    for name, row in readme_table_vars().items():
        if "${" + name + ":" in raw:
            continue
        if name in env and "compose:" in row:
            continue
        missing.append(name)
    assert not missing, f"README variables compose ignores: {missing}"


def test_every_passed_through_fastweb_variable_is_documented():
    documented = set(readme_table_vars()) | set(re.findall(r"`(FASTWEB_[A-Z_]+)`", (COMPOSE.parent / "docs" /
                                                                                       "design.md").read_text()))
    env = yaml.safe_load(COMPOSE.read_text())["services"]["fastweb"]["environment"]
    assert not {k for k in env if k.startswith("FASTWEB_")} - documented
