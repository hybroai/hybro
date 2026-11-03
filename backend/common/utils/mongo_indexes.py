"""Index helpers shared by the repositories that own each collection."""

from __future__ import annotations

import logging
from typing import Any

from common.observability import events as E
from common.protocols import MongoCollection
from common.utils.logger import get_logger

logger = get_logger(__name__)


async def create_index(
    collection: MongoCollection,
    collection_name: str,
    keys: Any,
    *,
    name: str,
    unique: bool = False,
    critical: bool = False,
    **kwargs: Any,
) -> bool:
    """Create one index; a failed ``critical`` index aborts startup."""
    try:
        await collection.create_index(keys, unique=unique, name=name, **kwargs)
        return True
    except Exception as exc:
        if critical:
            logger.emit(
                E.INDEX_CREATION_FAILED,
                level=logging.ERROR,
                exc=exc,
                collection_name=collection_name,
                index_name=name,
                critical=True,
            )
            raise RuntimeError(
                f"Critical index creation failed for {collection_name}.{name}"
            ) from exc
        logger.emit(
            E.INDEX_CREATION_FAILED,
            exc=exc,
            collection_name=collection_name,
            index_name=name,
            critical=False,
        )
        return False


async def ensure_text_index(
    collection: MongoCollection,
    *,
    name: str,
    weights: dict[str, int],
) -> bool:
    """Keep exactly one text index with ``weights``; report readiness."""
    try:
        existing = await collection.index_information()
        desired_weights = dict(sorted(weights.items()))
        matching = existing.get(name)
        matching_keys = tuple(
            (key, direction) for key, direction in ((matching or {}).get("key") or [])
        )
        valid_text_keys = {
            (("_fts", "text"), ("_ftsx", 1)),
            tuple((field, "text") for field in weights),
        }
        if (
            matching
            and dict(sorted((matching.get("weights") or {}).items())) == desired_weights
            and matching_keys in valid_text_keys
        ):
            return True
        for index_name, spec in existing.items():
            keys = spec.get("key") or []
            if spec.get("weights") or any(key == "_fts" for key, _ in keys):
                await collection.drop_index(index_name)
        await collection.create_index(
            [(field, "text") for field in weights],
            name=name,
            unique=False,
            weights=weights,
        )
        return True
    except Exception as exc:
        logger.emit(E.SEARCH_INDEX_CREATION_FAILED, exc=exc, index_name=name)
        return False


__all__ = ["create_index", "ensure_text_index"]
