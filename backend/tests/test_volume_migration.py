"""0.3 volume migration helper: space check, copy, counts, port, and marker."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import runtime_entrypoint as entry
import volume_migration as migration


@pytest.fixture
def volumes(tmp_path, monkeypatch):
    source_db, source_files = tmp_path / "source-db", tmp_path / "source-files"
    (source_db / "journal").mkdir(parents=True)
    (source_db / "collection-0.wt").write_bytes(b"x" * 100)
    (source_db / "journal" / "WiredTigerLog.1").write_bytes(b"y" * 50)
    (source_files / "room-1").mkdir(parents=True)
    (source_files / "room-1" / "a.png").write_bytes(b"z" * 10)
    data, runtime = tmp_path / "data", tmp_path / "runtime"
    data.mkdir()
    runtime.mkdir()
    monkeypatch.setattr(migration, "SOURCE_DB", source_db)
    monkeypatch.setattr(migration, "SOURCE_FILES", source_files)
    monkeypatch.setattr(entry, "DATA_DIR", data)
    monkeypatch.setattr(entry, "VOLUME_MARKER", data / "hybro-volume.json")
    monkeypatch.setattr(entry, "RUN_DIR", tmp_path / "run")
    monkeypatch.setattr(entry, "RUNTIME_DIR", runtime)
    return data, runtime


def counting(counts):
    def prepare(*, inspect):
        class Database:
            def list_collection_names(self):
                return list(counts)

            def __getitem__(self, name):
                class Collection:
                    def count_documents(self, _filter):
                        return counts[name]

                return Collection()

        class Client:
            def __getitem__(self, name):
                assert name == "hybro"
                return Database()

        return inspect(Client())

    return prepare


def marker(data: Path) -> dict:
    return json.loads((data / "hybro-volume.json").read_text())


def test_migration_copies_counts_and_marks_complete(volumes):
    data, runtime = volumes
    (runtime / "config.json").write_text(
        json.dumps({"instances": {"release": {"ports": {"frontend": 3456}}}})
    )
    details = migration.migrate("release", prepare=counting({"rooms": 2, "agents": 5}))
    assert (data / "db" / "journal" / "WiredTigerLog.1").read_bytes() == b"y" * 50
    assert (data / "files" / "room-1" / "a.png").read_bytes() == b"z" * 10
    assert details == {k: v for k, v in marker(data).items() if k != "complete"}
    assert marker(data) == {
        "complete": True,
        "migrated_from": "release",
        "collections": {"agents": 5, "rooms": 2},
        "frontend_port": 3456,
    }
    assert not (data / "hybro-volume.json.partial").exists()


def test_missing_room_files_volume_still_migrates(volumes, monkeypatch, tmp_path):
    data, _runtime = volumes
    monkeypatch.setattr(migration, "SOURCE_FILES", tmp_path / "not-mounted")
    migration.migrate("dev", prepare=counting({}))
    assert (data / "files").is_dir()
    assert marker(data)["migrated_from"] == "dev"
    assert "frontend_port" not in marker(data)


def test_unopenable_copy_leaves_no_marker(volumes):
    data, _runtime = volumes

    def broken(*, inspect):
        raise entry.EntrypointError("MongoDB preparation failed: mongod exited")

    with pytest.raises(migration.MigrationError, match="cannot be opened"):
        migration.migrate("release", prepare=broken)
    assert not (data / "hybro-volume.json").exists()


def test_space_check_reports_the_numbers():
    migration.check_space(10 * 2**20, 20 * 2**20)
    with pytest.raises(migration.MigrationError) as error:
        migration.check_space(10 * 2**20, 19 * 2**20)
    assert "10 MiB" in str(error.value) and "20 MiB" in str(error.value)
    assert "19 MiB is free" in str(error.value)


def test_space_check_runs_before_copying(volumes, monkeypatch):
    data, _runtime = volumes
    monkeypatch.setattr(
        migration.shutil, "disk_usage", lambda _p: type("U", (), {"free": 10})()
    )
    with pytest.raises(migration.MigrationError, match="Not enough"):
        migration.migrate("release", prepare=counting({}))
    assert not (data / "db").exists()


def test_tree_size_counts_files_and_ignores_missing_paths(volumes, tmp_path):
    assert migration.tree_size(migration.SOURCE_DB) == 150
    assert migration.tree_size(tmp_path / "absent") == 0


@pytest.mark.parametrize(
    ("text", "port"),
    [
        (None, None),
        ("not json", None),
        ("{}", None),
        ('{"instances": {"dev": {"ports": {"frontend": 3200}}}}', None),
        ('{"instances": {"release": {"ports": {"frontend": true}}}}', None),
        ('{"instances": {"release": {"ports": {"frontend": 4000}}}}', 4000),
    ],
)
def test_custom_port_reads_only_the_matching_instance(text, port):
    assert migration.custom_port(text, "release") == port


def test_main_prints_only_the_port_on_stdout(volumes, monkeypatch, capsys):
    monkeypatch.setattr(
        migration,
        "migrate",
        lambda instance: {"collections": {"rooms": 3}, "frontend_port": 3456},
    )
    assert migration.main(["volume_migration.py", "release"]) == 0
    out = capsys.readouterr()
    assert out.out == "port=3456\n"
    assert "1 collections, 3 documents" in out.err


def test_main_reports_failures_and_bad_usage(monkeypatch, capsys):
    def fail(_instance):
        raise migration.MigrationError("Not enough free Docker disk space")

    monkeypatch.setattr(migration, "migrate", fail)
    assert migration.main(["volume_migration.py", "release"]) == 1
    assert "hybro: Not enough" in capsys.readouterr().err
    assert migration.main(["volume_migration.py", "other"]) == 2


@pytest.mark.parametrize(
    ("text", "stale"),
    [
        (None, False),
        ("{}", False),
        ('{"backend": {"mongodb_url": "mongodb://mongo:27017/?replicaSet=rs0"}}', True),
        ('{"backend": {"mongodb_url": "mongodb://db.example.com:27017"}}', False),
    ],
)
def test_compose_mongo_url_is_detected(text, stale):
    assert migration.names_compose_mongo(text) is stale


def test_main_warns_about_a_compose_mongo_url(volumes, monkeypatch, capsys):
    _data, runtime = volumes
    (runtime / "config.json").write_text(
        '{"backend": {"mongodb_url": "mongodb://mongo:27017"}}'
    )
    monkeypatch.setattr(migration, "migrate", lambda instance: {"collections": {}})
    assert migration.main(["volume_migration.py", "release"]) == 0
    out = capsys.readouterr()
    assert out.out == ""
    assert "names the 0.3 host `mongo`" in out.err
