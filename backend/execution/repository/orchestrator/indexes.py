"""Indexes for the orchestrator and A2A runtime collections."""

from __future__ import annotations

import logging
from typing import Any

from common.observability import events as E
from common.protocols import MongoDAL
from common.utils.logger import get_logger
from common.utils.mongo_indexes import create_index

logger = get_logger(__name__)


async def ensure_orchestrator_indexes(mongo: MongoDAL) -> None:
    """Drop obsolete Run indexes, then create every orchestrator index."""
    from execution.orchestrator.a2a_runtime.persistence import (
        A2A_RUNTIME_COLLECTIONS,
    )
    from execution.orchestrator.persistence import (
        OBSOLETE_ORCHESTRATOR_RUN_INDEXES,
        ORCHESTRATOR_COLLECTIONS,
        ORCHESTRATOR_RUNS_COLLECTION,
    )

    runs = mongo.collection(ORCHESTRATOR_RUNS_COLLECTION)
    existing_run_indexes = await runs.index_information()
    for obsolete_name in OBSOLETE_ORCHESTRATOR_RUN_INDEXES:
        if obsolete_name not in existing_run_indexes:
            continue
        try:
            await runs.drop_index(obsolete_name)
        except Exception as exc:
            logger.emit(
                E.OBSOLETE_INDEX_DROP_FAILED,
                level=logging.ERROR,
                exc=exc,
                collection_name=ORCHESTRATOR_RUNS_COLLECTION,
                index_name=obsolete_name,
            )
            raise RuntimeError(
                "Critical obsolete orchestrator Run index removal failed"
            ) from exc

    for collection_definition in (*ORCHESTRATOR_COLLECTIONS, *A2A_RUNTIME_COLLECTIONS):
        for index in collection_definition.indexes:
            kwargs: dict[str, Any] = {}
            if index.partial_filter is not None:
                kwargs["partialFilterExpression"] = dict(index.partial_filter)
            await create_index(
                mongo.collection(collection_definition.name),
                collection_definition.name,
                list(index.keys),
                name=index.name,
                unique=index.unique,
                critical=index.unique,
                **kwargs,
            )


__all__ = ["ensure_orchestrator_indexes"]
