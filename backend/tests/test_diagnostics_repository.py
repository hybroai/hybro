"""DiagnosticsMongoRepository: capped collections, inserts and queries."""

import logging
import os
from datetime import UTC, datetime, timedelta

import pytest
from bson import ObjectId
from pymongo.errors import BulkWriteError, CollectionInvalid, OperationFailure

from diagnostics.repository.mongo import (
    EVENTS_CAP_BYTES,
    EVENTS_COLLECTION,
    LOGS_CAP_BYTES,
    LOGS_COLLECTION,
    DiagnosticsMongoRepository,
    RecordFilter,
)


class _Cursor:
    def __init__(self, docs: list[dict]) -> None:
        self.docs = docs
        self.sorted_by = None
        self.limited = None

    def sort(self, key, direction):
        self.sorted_by = (key, direction)
        return self

    def limit(self, count):
        self.limited = count
        return self

    async def to_list(self, length):
        return list(self.docs[:length])


class _Collection:
    def __init__(self, name: str, database: "_Database") -> None:
        self.name = name
        self.database = database
        self.capped = False
        self.inserted: list[dict] = []
        self.insert_error: Exception | None = None
        self.find_docs: list[dict] = []
        self.cursor: _Cursor | None = None
        self.query = None

    async def options(self):
        return {"capped": True, "size": 1} if self.capped else {}

    async def insert_many(self, documents, ordered=True):
        assert ordered is False
        if self.insert_error is not None:
            raise self.insert_error
        self.inserted.extend(documents)

    def find(self, query):
        self.query = query
        self.cursor = _Cursor(self.find_docs)
        return self.cursor


class _Database:
    def __init__(self) -> None:
        self.collections: dict[str, _Collection] = {}
        self.created: list[tuple[str, bool, int]] = []
        self.commands: list[tuple] = []
        self.create_error: Exception | None = None

    def get(self, name: str) -> _Collection:
        if name not in self.collections:
            self.collections[name] = _Collection(name, self)
        return self.collections[name]

    async def create_collection(self, name, *, capped, size):
        if self.create_error is not None:
            raise self.create_error
        self.created.append((name, capped, size))
        self.get(name).capped = capped

    async def command(self, *args, **kwargs):
        self.commands.append((args, kwargs))
        if args[0] == "convertToCapped":
            self.get(args[1]).capped = True


class _Adapter:
    def __init__(self, collection: _Collection) -> None:
        self.raw_collection = collection


class _Mongo:
    def __init__(self, database: _Database) -> None:
        self.database = database

    def collection(self, name: str) -> _Adapter:
        return _Adapter(self.database.get(name))


def _repository() -> tuple[DiagnosticsMongoRepository, _Database]:
    database = _Database()
    return DiagnosticsMongoRepository(_Mongo(database)), database


@pytest.mark.asyncio
async def test_ensure_indexes_creates_both_collections_capped():
    repository, database = _repository()

    assert await repository.ensure_indexes() is True

    assert database.created == [
        (EVENTS_COLLECTION, True, EVENTS_CAP_BYTES),
        (LOGS_COLLECTION, True, LOGS_CAP_BYTES),
    ]
    assert EVENTS_CAP_BYTES == 16 * 1024 * 1024
    assert LOGS_CAP_BYTES == 32 * 1024 * 1024


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "error",
    [
        CollectionInvalid("exists"),
        OperationFailure("NamespaceExists", code=48),
    ],
)
async def test_existing_capped_collection_is_success(error):
    repository, database = _repository()
    database.get(EVENTS_COLLECTION).capped = True
    database.get(LOGS_COLLECTION).capped = True
    database.create_error = error

    assert await repository.ensure_indexes() is True
    assert database.commands == []


@pytest.mark.asyncio
async def test_existing_uncapped_collection_is_converted():
    repository, database = _repository()
    database.create_error = CollectionInvalid("exists")

    assert await repository.ensure_indexes() is True

    assert database.commands == [
        (("convertToCapped", EVENTS_COLLECTION), {"size": EVENTS_CAP_BYTES}),
        (("convertToCapped", LOGS_COLLECTION), {"size": LOGS_CAP_BYTES}),
    ]


@pytest.mark.asyncio
async def test_creation_failure_reports_unavailable_without_raising(caplog):
    repository, database = _repository()
    database.create_error = OperationFailure("unauthorized", code=13)

    with caplog.at_level(logging.WARNING):
        assert await repository.ensure_indexes() is False

    [record] = [
        r for r in caplog.records if r.getMessage() == "diagnostics_store_unavailable"
    ]
    assert record.reason == "OperationFailure"
    assert record.levelno == logging.WARNING


@pytest.mark.asyncio
async def test_insert_treats_duplicate_keys_as_written():
    repository, database = _repository()
    database.get(EVENTS_COLLECTION).insert_error = BulkWriteError(
        {"writeErrors": [{"code": 11000}, {"code": 11000}], "writeConcernErrors": []}
    )

    await repository.insert(EVENTS_COLLECTION, [{"_id": ObjectId()}])


@pytest.mark.asyncio
async def test_insert_reraises_other_bulk_errors():
    repository, database = _repository()
    database.get(EVENTS_COLLECTION).insert_error = BulkWriteError(
        {"writeErrors": [{"code": 11000}, {"code": 121}], "writeConcernErrors": []}
    )

    with pytest.raises(BulkWriteError):
        await repository.insert(EVENTS_COLLECTION, [{"_id": ObjectId()}])


@pytest.mark.asyncio
async def test_insert_passes_documents_unordered():
    repository, database = _repository()
    docs = [{"_id": ObjectId()}, {"_id": ObjectId()}]

    await repository.insert(LOGS_COLLECTION, docs)

    assert database.get(LOGS_COLLECTION).inserted == docs


@pytest.mark.asyncio
async def test_find_builds_filter_sort_and_limit():
    repository, database = _repository()
    since = datetime(2026, 1, 1, tzinfo=UTC)
    until = since + timedelta(hours=1)
    before = ObjectId()

    await repository.find(
        EVENTS_COLLECTION,
        RecordFilter(
            service="hybro-backend",
            events=("startup_completed", "shutdown_started"),
            min_level_no=logging.WARNING,
            trace_id="t-1",
            instance_id="ab12cd34",
            since=since,
            until=until,
            before_id=before,
            limit=50,
        ),
    )

    collection = database.get(EVENTS_COLLECTION)
    assert collection.query == {
        "service": "hybro-backend",
        "event": {"$in": ["startup_completed", "shutdown_started"]},
        "level_no": {"$gte": logging.WARNING},
        "trace_id": "t-1",
        "instance_id": "ab12cd34",
        "ts": {"$gte": since, "$lt": until},
        "_id": {"$lt": before},
    }
    assert collection.cursor.sorted_by == ("_id", -1)
    assert collection.cursor.limited == 50


@pytest.mark.asyncio
async def test_find_with_an_empty_filter_and_one_time_bound():
    repository, database = _repository()
    since = datetime(2026, 1, 1, tzinfo=UTC)

    await repository.find(LOGS_COLLECTION, RecordFilter(limit=10))
    assert database.get(LOGS_COLLECTION).query == {}

    await repository.find(LOGS_COLLECTION, RecordFilter(since=since, limit=10))
    assert database.get(LOGS_COLLECTION).query == {"ts": {"$gte": since}}


@pytest.mark.asyncio
async def test_find_returns_naive_timestamps_as_utc():
    repository, database = _repository()
    naive = datetime(2026, 1, 1, 12, 0, 0)
    database.get(EVENTS_COLLECTION).find_docs = [{"_id": ObjectId(), "ts": naive}]

    [doc] = await repository.find(EVENTS_COLLECTION, RecordFilter(limit=10))

    assert doc["ts"] == naive.replace(tzinfo=UTC)


@pytest.mark.integration
@pytest.mark.asyncio
@pytest.mark.skipif(
    os.environ.get("HYBRO_TEST_LIVE_MONGO") != "1",
    reason="set HYBRO_TEST_LIVE_MONGO=1 to run against a live MongoDB",
)
async def test_live_mongo_round_trip():
    from motor.motor_asyncio import AsyncIOMotorClient

    from dal.mongo.client import MongoCollectionAdapter

    client = AsyncIOMotorClient(
        os.environ.get("HYBRO_TEST_MONGO_URL", "mongodb://127.0.0.1:27017")
    )
    database = client[f"hybro_diag_test_{ObjectId()}"]

    class _LiveMongo:
        def collection(self, name):
            return MongoCollectionAdapter(database[name])

    try:
        repository = DiagnosticsMongoRepository(_LiveMongo())
        assert await repository.ensure_indexes() is True
        assert await repository.ensure_indexes() is True
        options = await database[EVENTS_COLLECTION].options()
        assert options["capped"] is True
        now = datetime.now(UTC)
        await repository.insert(
            EVENTS_COLLECTION,
            [{"_id": ObjectId(), "ts": now, "event": "x", "level_no": 20}],
        )
        [doc] = await repository.find(EVENTS_COLLECTION, RecordFilter(limit=5))
        assert doc["ts"].utcoffset() == timedelta(0)
    finally:
        await client.drop_database(database.name)
        client.close()
