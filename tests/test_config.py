# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""Config loading, dirty tracking, atomic writes, and schema refusal."""

import dataclasses
import json
from pathlib import Path

import pytest

from relduty_deployer.config import (
    SCHEMA_VERSION,
    Config,
    ConfigError,
    ConfigStore,
    build_projects,
    default_config,
)
from relduty_deployer.projects import PROJECT_SPECS, ProjectSettings


@pytest.fixture
def store(tmp_path):
    return ConfigStore(tmp_path / "relduty-deployer-config.json")


def test_first_run_writes_the_file_and_starts_clean(store):
    assert not store.path.exists()

    config = store.load()

    assert store.path.exists()
    assert store.is_dirty(config) is False
    assert set(config.projects) == {spec.name for spec in PROJECT_SPECS}
    assert config.projects["tooltool"].path == Path.home() / "dev" / "tooltool"
    assert config.projects["tooltool"].remote == "origin"


def test_home_paths_are_written_back_as_tilde(store):
    store.load()
    raw = json.loads(store.path.read_text())
    assert raw["projects"]["shipit"]["path"] == "~/dev/shipit"
    assert raw["schema_version"] == SCHEMA_VERSION


def test_tilde_paths_are_expanded_on_load(store):
    store.path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "projects": {spec.name: {"path": f"~/src/{spec.name}", "remote": "upstream", "enabled": True} for spec in PROJECT_SPECS},
            }
        )
    )

    config = store.load()

    assert config.projects["balrog"].path == Path.home() / "src" / "balrog"
    assert config.projects["balrog"].remote == "upstream"
    assert store.is_dirty(config) is False


def test_editing_a_setting_marks_the_config_dirty(store):
    config = store.load()
    assert store.is_dirty(config) is False

    config.projects["shipit"] = dataclasses.replace(config.projects["shipit"], remote="fork")

    assert store.is_dirty(config) is True


def test_saving_makes_the_config_clean_again(store):
    config = store.load()
    config.projects["shipit"] = dataclasses.replace(config.projects["shipit"], enabled=False)
    assert store.is_dirty(config) is True

    store.save(config)

    assert store.is_dirty(config) is False
    assert json.loads(store.path.read_text())["projects"]["shipit"]["enabled"] is False


def test_a_project_missing_from_disk_is_defaulted_and_shows_as_dirty(store):
    # A project added to the code after the config was last written.
    store.path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "projects": {spec.name: {"path": f"~/dev/{spec.name}"} for spec in PROJECT_SPECS if spec.name != "tooltool"},
            }
        )
    )

    config = store.load()

    assert config.projects["tooltool"].path == Path.home() / "dev" / "tooltool"
    assert store.is_dirty(config) is True
    assert any("tooltool was missing" in note for note in store.notes)


def test_unknown_projects_are_reported_and_ignored(store):
    store.path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "projects": {
                    **{spec.name: {"path": f"~/dev/{spec.name}"} for spec in PROJECT_SPECS},
                    "retired-thing": {"path": "~/dev/retired-thing"},
                },
            }
        )
    )

    config = store.load()

    assert "retired-thing" not in config.projects
    assert any("retired-thing" in note for note in store.notes)


def test_a_newer_schema_is_refused_rather_than_silently_downgraded(store):
    store.path.write_text(json.dumps({"schema_version": SCHEMA_VERSION + 1, "projects": {}}))

    with pytest.raises(ConfigError, match="upgrade relduty-deployer"):
        store.load()


def test_a_missing_schema_version_is_refused(store):
    store.path.write_text(json.dumps({"projects": {}}))

    with pytest.raises(ConfigError, match="no integer schema_version"):
        store.load()


def test_malformed_json_names_the_file(store):
    store.path.write_text("{not json")

    with pytest.raises(ConfigError, match="could not read"):
        store.load()


def test_a_non_object_project_entry_is_refused(store):
    store.path.write_text(json.dumps({"schema_version": 1, "projects": {"shipit": "~/dev/shipit"}}))

    with pytest.raises(ConfigError, match="project 'shipit' should be a JSON object"):
        store.load()


def test_save_leaves_no_temporary_files_behind(store):
    config = store.load()
    store.save(config)

    assert [p.name for p in store.path.parent.iterdir()] == [store.path.name]


def test_save_replaces_the_file_rather_than_truncating_it(store):
    config = store.load()
    original_inode = store.path.stat().st_ino

    config.fetch_timeout_seconds = 5
    store.save(config)

    assert store.path.stat().st_ino != original_inode
    assert json.loads(store.path.read_text())["fetch_timeout_seconds"] == 5


def test_build_projects_pairs_specs_with_settings():
    config = default_config()

    projects = build_projects(config)

    assert [p.name for p in projects] == [spec.name for spec in PROJECT_SPECS]
    assert projects[0].source_ref() == "refs/remotes/origin/master"


def test_build_projects_skips_projects_absent_from_the_config():
    config = Config(projects={"shipit": ProjectSettings(path=Path("/tmp/shipit"))})

    assert [p.name for p in build_projects(config)] == ["shipit"]
