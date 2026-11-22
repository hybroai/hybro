"""A raw Mongo collection in memory, for tests that run real repositories.

It covers the query and update operators the approval repositories use and
raises on any other. Wrap it in ``dal.mongo.client.MongoCollectionAdapter`` so
the adapter is the real one. Every read returns a copy, as a real driver does.
Unique indexes come from the repository's own ``create_index`` calls, partial
filters included, and are enforced on every write.
"""

from __future__ import annotations

from copy import deepcopy
from itertools import count
from types import SimpleNamespace
from typing import Any

from pymongo import ReturnDocument
from pymongo.errors import DuplicateKeyError

from dal.mongo.client import MongoCollectionAdapter

_MISSING = object()


def _lookup(document: dict[str, Any], path: str) -> Any:
    value: Any = document
    for part in path.split("."):
        if not isinstance(value, dict) or part not in value:
            return _MISSING
        value = value[part]
    return value


def _equals(value: Any, expected: Any) -> bool:
    if value is _MISSING:
        return expected is None
    if isinstance(value, list) and not isinstance(expected, list):
        return expected in value
    return value == expected


def _comparable(value: Any) -> bool:
    return value is not _MISSING and value is not None


def _matches_operator(value: Any, operator: str, operand: Any) -> bool:
    checks = {
        "$exists": lambda: (value is not _MISSING) == bool(operand),
        "$in": lambda: any(_equals(value, item) for item in operand),
        "$nin": lambda: not any(_equals(value, item) for item in operand),
        "$ne": lambda: not _equals(value, operand),
        "$lte": lambda: _comparable(value) and value <= operand,
        "$lt": lambda: _comparable(value) and value < operand,
        "$gte": lambda: _comparable(value) and value >= operand,
        "$gt": lambda: _comparable(value) and value > operand,
        "$type": lambda: isinstance(value, str),
    }
    if operator not in checks or (operator == "$type" and operand != "string"):
        raise NotImplementedError(f"query operator {operator} {operand!r}")
    return checks[operator]()


def matches(document: dict[str, Any], query: dict[str, Any]) -> bool:
    for key, expected in query.items():
        if key == "$or":
            if not any(matches(document, part) for part in expected):
                return False
            continue
        if key == "$and":
            if not all(matches(document, part) for part in expected):
                return False
            continue
        value = _lookup(document, key)
        if (
            isinstance(expected, dict)
            and expected
            and all(name.startswith("$") for name in expected)
        ):
            if not all(
                _matches_operator(value, name, operand)
                for name, operand in expected.items()
            ):
                return False
        elif not _equals(value, expected):
            return False
    return True


def _set_path(document: dict[str, Any], path: str, value: Any) -> None:
    *parents, leaf = path.split(".")
    for part in parents:
        document = document.setdefault(part, {})
    document[leaf] = value


def _apply_update(document: dict[str, Any], update: dict[str, Any]) -> None:
    for operator, fields in update.items():
        for path, value in fields.items():
            if operator in {"$set", "$setOnInsert"}:
                _set_path(document, path, deepcopy(value))
            elif operator == "$inc":
                current = _lookup(document, path)
                _set_path(
                    document, path, (0 if current is _MISSING else current) + value
                )
            elif operator == "$unset":
                *parents, leaf = path.split(".")
                target = document
                for part in parents:
                    target = target.get(part, {})
                target.pop(leaf, None)
            elif operator == "$push":
                if isinstance(value, dict) and any(k.startswith("$") for k in value):
                    raise NotImplementedError(f"$push modifier {sorted(value)}")
                current = _lookup(document, path)
                items = [] if current is _MISSING else list(current)
                items.append(deepcopy(value))
                _set_path(document, path, items)
            else:
                raise NotImplementedError(f"update operator {operator}")


class _Cursor:
    def __init__(self, documents: list[dict[str, Any]]) -> None:
        self._documents = documents

    def sort(self, keys):
        for field, direction in reversed(list(keys)):
            self._documents.sort(
                key=lambda document, field=field: (
                    _lookup(document, field) not in (_MISSING, None),
                    _lookup(document, field)
                    if _lookup(document, field) not in (_MISSING, None)
                    else 0,
                ),
                reverse=direction < 0,
            )
        return self

    def skip(self, count: int):
        self._documents = self._documents[count:]
        return self

    def limit(self, count: int):
        if count:
            self._documents = self._documents[:count]
        return self

    async def to_list(self, length=None):
        return [deepcopy(document) for document in self._documents]


_object_ids = count(1)


class InMemoryCollection:
    """The subset of a motor collection the approval repositories call."""

    def __init__(self, name: str) -> None:
        self.name = name
        self.documents: list[dict[str, Any]] = []
        # (key fields, partial filter) of each unique index.
        self._unique: list[tuple[tuple[str, ...], dict[str, Any]]] = []

    def _find(self, query: dict[str, Any]) -> list[dict[str, Any]]:
        return [document for document in self.documents if matches(document, query)]

    def _check_unique(self, document: dict[str, Any]) -> None:
        for fields, partial in self._unique:
            if partial and not matches(document, partial):
                continue
            key = tuple(
                None if (value := _lookup(document, field)) is _MISSING else value
                for field in fields
            )
            for other in self.documents:
                if other is document or (partial and not matches(other, partial)):
                    continue
                if key == tuple(
                    None if (value := _lookup(other, field)) is _MISSING else value
                    for field in fields
                ):
                    raise DuplicateKeyError(f"duplicate key on {fields}")

    def _update(self, document: dict[str, Any], update: dict[str, Any]) -> bool:
        before = deepcopy(document)
        _apply_update(
            document, {k: v for k, v in update.items() if k != "$setOnInsert"}
        )
        try:
            self._check_unique(document)
        except DuplicateKeyError:
            document.clear()
            document.update(before)
            raise
        return document != before

    async def find_one(self, query, *args, sort=None, **_kwargs):
        cursor = _Cursor(self._find(query))
        if sort:
            cursor.sort(sort)
        rows = await cursor.limit(1).to_list()
        return rows[0] if rows else None

    def find(self, query, *args, **_kwargs):
        return _Cursor(self._find(query))

    async def count_documents(self, query, **_kwargs) -> int:
        return len(self._find(query))

    async def insert_one(self, document, **_kwargs):
        stored = deepcopy(document)
        stored.setdefault("_id", f"oid-{next(_object_ids)}")
        self._check_unique(stored)
        self.documents.append(stored)
        return SimpleNamespace(inserted_id=stored["_id"])

    async def find_one_and_update(
        self,
        query,
        update,
        *,
        return_document=ReturnDocument.BEFORE,
        upsert=False,
        sort=None,
        **_kwargs,
    ):
        if upsert:
            raise NotImplementedError("upsert")
        cursor = _Cursor(self._find(query))
        if sort:
            cursor.sort(sort)
        found = cursor._documents[:1]
        if not found:
            return None
        document = found[0]
        before = deepcopy(document)
        self._update(document, update)
        return deepcopy(document) if return_document == ReturnDocument.AFTER else before

    async def update_one(self, query, update, *, upsert=False, **_kwargs):
        if upsert:
            raise NotImplementedError("upsert")
        found = self._find(query)[:1]
        if not found:
            return SimpleNamespace(matched_count=0, modified_count=0, upserted_id=None)
        changed = self._update(found[0], update)
        return SimpleNamespace(
            matched_count=1, modified_count=int(changed), upserted_id=None
        )

    async def create_index(self, keys, **kwargs) -> str:
        if kwargs.get("unique"):
            self._unique.append(
                (
                    tuple(field for field, _direction in keys),
                    dict(kwargs.get("partialFilterExpression") or {}),
                )
            )
        return str(kwargs.get("name") or "index")


def collection(name: str) -> MongoCollectionAdapter:
    return MongoCollectionAdapter(InMemoryCollection(name))
