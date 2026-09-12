import ordin.search as search_module
from ordin.availability import EnvironmentInfo
from ordin.schema import validate_named_schema
from ordin.search import search


def _command(name: str, *, platforms=None):
    entry = {
        "schema_version": "ordin.command_card.v1",
        "command": name,
        "summary": "Inspect demo state.",
        "aliases": ["demo inspect"],
        "intents": ["inspect demo state"],
        "default_risk": "low",
        "risk_tags": ["inspection"],
        "examples": [],
        "templates": [],
    }
    if platforms is not None:
        entry["platforms"] = platforms
    return entry


def test_installed_command_wins_an_otherwise_equal_rank(monkeypatch):
    monkeypatch.setattr(
        search_module,
        "load_commands",
        lambda: [_command("alpha"), _command("beta")],
    )
    monkeypatch.setattr(search_module, "load_synonyms", lambda: {})

    results = search(
        "inspect demo state",
        limit=2,
        environment=EnvironmentInfo(os="linux", distro_id="ubuntu"),
        which=lambda command: "/usr/bin/beta" if command == "beta" else None,
    )

    assert [result.command for result in results] == ["beta", "alpha"]
    assert results[0].available is True
    assert results[1].available is False


def test_search_result_with_availability_validates_schema(monkeypatch):
    monkeypatch.setattr(
        search_module,
        "load_commands",
        lambda: [_command("demo")],
    )
    monkeypatch.setattr(search_module, "load_synonyms", lambda: {})

    result = search(
        "inspect demo state",
        limit=1,
        environment=EnvironmentInfo(os="linux", distro_id="ubuntu"),
        which=lambda command: "/usr/bin/demo",
    )[0]
    payload = result.as_dict()

    assert payload["available"] is True
    assert payload["executable_path"] == "/usr/bin/demo"
    assert payload["platform_compatible"] is None
    assert validate_named_schema("search_result", payload) == []


def test_distro_specific_command_stays_visible_when_incompatible(monkeypatch):
    monkeypatch.setattr(
        search_module,
        "load_commands",
        lambda: [
            _command(
                "apt",
                platforms={
                    "os": ["linux"],
                    "distro_ids": ["debian", "ubuntu"],
                    "distro_like": ["debian"],
                },
            )
        ],
    )
    monkeypatch.setattr(search_module, "load_synonyms", lambda: {})

    result = search(
        "inspect demo state",
        limit=1,
        environment=EnvironmentInfo(os="linux", distro_id="fedora", distro_like=("rhel",)),
        which=lambda command: None,
    )[0]

    assert result.command == "apt"
    assert result.available is False
    assert result.platform_compatible is False
    assert "fedora" in result.availability_reason


def test_search_refreshes_changed_metadata_and_executable_availability(monkeypatch):
    entry = _command("alpha")
    installed = {"alpha": "/usr/bin/alpha"}
    monkeypatch.setattr(search_module, "load_commands", lambda: [entry])
    monkeypatch.setattr(search_module, "load_synonyms", lambda: {})
    environment = EnvironmentInfo(os="linux")

    first = search("inspect demo state", environment=environment, which=installed.get)[0]
    assert first.command == "alpha"
    assert first.available is True

    entry["command"] = "beta"
    entry["summary"] = "Inspect changed configuration."
    entry["intents"] = ["inspect changed configuration"]
    installed.clear()

    second = search("inspect changed configuration", environment=environment, which=installed.get)[
        0
    ]
    assert second.command == "beta"
    assert second.summary == "Inspect changed configuration."
    assert second.available is False
    assert second.executable_path is None
    assert 'exact intent "inspect changed configuration"' in second.why


def test_debian_family_metadata_is_exposed_for_apt():
    results = search(
        "install system package curl on ubuntu",
        limit=5,
        environment=EnvironmentInfo(os="linux", distro_id="ubuntu", distro_like=("debian",)),
        which=lambda command: f"/usr/bin/{command}" if command in {"apt", "apt-get"} else None,
    )
    apt_results = [result for result in results if result.command in {"apt", "apt-get"}]

    assert apt_results
    assert all(result.available is True for result in apt_results)
    assert all(result.platform_compatible is True for result in apt_results)
    assert all("ubuntu" in result.availability_reason for result in apt_results)
