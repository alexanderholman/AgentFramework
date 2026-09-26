"""Named document stores with runtime-verified capabilities and explicit sessions."""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from contextlib import AbstractAsyncContextManager
from datetime import datetime
from types import MappingProxyType
from typing import Any

from af.domain.document.contracts import (
    AggregationPipeline,
    BulkOperation,
    BulkWriteResult,
    DocumentDriver,
    DocumentFeature,
    DocumentFilter,
    DocumentProjection,
    IndexDefinition,
    QueryResult,
    ReadPreference,
    SessionContext,
    TimeSeriesOptions,
    WriteConcern,
    WriteResult,
)
from af.framework.container import ProviderRegistrar, Resolver, Scope
from af.framework.managers import (
    CapabilitySet,
    DriverRegistration,
    Manager,
    ManagerConfigurationError,
)
from af.observability.signals import TelemetryPublisher


class DocumentManager(Manager[DocumentDriver]):
    """Application-owned named document connections."""

    def __init__(
        self,
        *,
        connections: Mapping[str, object],
        default: str | None,
        deployment_profile: str,
        policy_version: str,
        clock: Callable[[], datetime] | None = None,
        telemetry: TelemetryPublisher | None = None,
    ) -> None:
        super().__init__(
            manager_id="document",
            connections=connections,
            default=default,
            deployment_profile=deployment_profile,
            policy_version=policy_version,
            clock=clock,
            telemetry=telemetry,
        )

    def install(self, registration: DriverRegistration[DocumentDriver]) -> None:
        if type(registration) is not DriverRegistration or registration.manager_id != "document":
            raise ManagerConfigurationError(
                protected_cause=ValueError(
                    "driver registration does not target the document manager"
                )
            )
        self.extend(
            registration.driver_name,
            registration.factory,
            manifest=registration.manifest,
            verifier=registration.verifier,
            disposer=registration.disposer,
        )

    def connection(
        self,
        name: str | None = None,
        *,
        requires: CapabilitySet | Iterable[str | DocumentFeature] = (),
    ) -> DocumentDriver:
        return super().connection(
            name,
            requires=(
                requires
                if isinstance(requires, CapabilitySet)
                else CapabilitySet(requires)
                if isinstance(requires, str)
                else CapabilitySet(
                    feature.value if isinstance(feature, DocumentFeature) else feature
                    for feature in requires
                )
            ),
        )

    async def insert_one(
        self,
        collection: str,
        document: dict[str, Any],
        write_concern: WriteConcern = WriteConcern.ACKNOWLEDGED,
        *,
        session: SessionContext | None = None,
        name: str | None = None,
    ) -> WriteResult:
        return await self.connection(name, requires=(DocumentFeature.CRUD,)).insert_one(
            collection, document, write_concern, session=session
        )

    async def insert_many(
        self,
        collection: str,
        documents: list[dict[str, Any]],
        ordered: bool = True,
        write_concern: WriteConcern = WriteConcern.ACKNOWLEDGED,
        *,
        session: SessionContext | None = None,
        name: str | None = None,
    ) -> WriteResult:
        return await self.connection(name, requires=(DocumentFeature.CRUD,)).insert_many(
            collection, documents, ordered, write_concern, session=session
        )

    async def find_one(
        self,
        collection: str,
        filter: DocumentFilter | None = None,
        projection: DocumentProjection | None = None,
        read_preference: ReadPreference = ReadPreference.PRIMARY,
        *,
        session: SessionContext | None = None,
        name: str | None = None,
    ) -> dict[str, Any] | None:
        return await self.connection(name, requires=(DocumentFeature.CRUD,)).find_one(
            collection, filter, projection, read_preference, session=session
        )

    async def find(
        self,
        collection: str,
        filter: DocumentFilter | None = None,
        projection: DocumentProjection | None = None,
        sort: dict[str, int] | None = None,
        limit: int = 0,
        skip: int = 0,
        read_preference: ReadPreference = ReadPreference.PRIMARY,
        *,
        session: SessionContext | None = None,
        name: str | None = None,
    ) -> QueryResult:
        return await self.connection(name, requires=(DocumentFeature.CRUD,)).find(
            collection, filter, projection, sort, limit, skip, read_preference, session=session
        )

    async def count_documents(
        self,
        collection: str,
        filter: DocumentFilter | None = None,
        *,
        session: SessionContext | None = None,
        name: str | None = None,
    ) -> int:
        return await self.connection(name, requires=(DocumentFeature.CRUD,)).count_documents(
            collection, filter, session=session
        )

    async def update_one(
        self,
        collection: str,
        filter: DocumentFilter,
        update: dict[str, Any],
        upsert: bool = False,
        write_concern: WriteConcern = WriteConcern.ACKNOWLEDGED,
        *,
        session: SessionContext | None = None,
        name: str | None = None,
    ) -> WriteResult:
        return await self.connection(name, requires=(DocumentFeature.CRUD,)).update_one(
            collection, filter, update, upsert, write_concern, session=session
        )

    async def update_many(
        self,
        collection: str,
        filter: DocumentFilter,
        update: dict[str, Any],
        upsert: bool = False,
        write_concern: WriteConcern = WriteConcern.ACKNOWLEDGED,
        *,
        session: SessionContext | None = None,
        name: str | None = None,
    ) -> WriteResult:
        return await self.connection(name, requires=(DocumentFeature.CRUD,)).update_many(
            collection, filter, update, upsert, write_concern, session=session
        )

    async def replace_one(
        self,
        collection: str,
        filter: DocumentFilter,
        replacement: dict[str, Any],
        upsert: bool = False,
        write_concern: WriteConcern = WriteConcern.ACKNOWLEDGED,
        *,
        session: SessionContext | None = None,
        name: str | None = None,
    ) -> WriteResult:
        return await self.connection(name, requires=(DocumentFeature.CRUD,)).replace_one(
            collection, filter, replacement, upsert, write_concern, session=session
        )

    async def delete_one(
        self,
        collection: str,
        filter: DocumentFilter,
        write_concern: WriteConcern = WriteConcern.ACKNOWLEDGED,
        *,
        session: SessionContext | None = None,
        name: str | None = None,
    ) -> WriteResult:
        return await self.connection(name, requires=(DocumentFeature.CRUD,)).delete_one(
            collection, filter, write_concern, session=session
        )

    async def delete_many(
        self,
        collection: str,
        filter: DocumentFilter,
        write_concern: WriteConcern = WriteConcern.ACKNOWLEDGED,
        *,
        session: SessionContext | None = None,
        name: str | None = None,
    ) -> WriteResult:
        return await self.connection(name, requires=(DocumentFeature.CRUD,)).delete_many(
            collection, filter, write_concern, session=session
        )

    async def aggregate(
        self,
        collection: str,
        pipeline: AggregationPipeline,
        *,
        session: SessionContext | None = None,
        name: str | None = None,
    ) -> list[dict[str, Any]]:
        return await self.connection(name, requires=(DocumentFeature.AGGREGATION,)).aggregate(
            collection, pipeline, session=session
        )

    async def create_index(
        self,
        collection: str,
        definition: IndexDefinition,
        *,
        session: SessionContext | None = None,
        name: str | None = None,
    ) -> str:
        return await self.connection(name, requires=(DocumentFeature.INDEX,)).create_index(
            collection, definition, session=session
        )

    async def list_indexes(
        self, collection: str, *, session: SessionContext | None = None, name: str | None = None
    ) -> list[dict[str, Any]]:
        return await self.connection(name, requires=(DocumentFeature.INDEX,)).list_indexes(
            collection, session=session
        )

    async def drop_index(
        self,
        collection: str,
        index_name: str,
        *,
        session: SessionContext | None = None,
        name: str | None = None,
    ) -> bool:
        return await self.connection(name, requires=(DocumentFeature.INDEX,)).drop_index(
            collection, index_name, session=session
        )

    async def start_session(self, *, name: str | None = None) -> SessionContext:
        return await self.connection(name, requires=(DocumentFeature.SESSION,)).start_session()

    async def bulk_write(
        self,
        collection: str,
        operations: list[BulkOperation],
        ordered: bool = True,
        *,
        session: SessionContext | None = None,
        name: str | None = None,
    ) -> BulkWriteResult:
        return await self.connection(name, requires=(DocumentFeature.BULK,)).bulk_write(
            collection, operations, ordered, session=session
        )

    async def create_time_series_collection(
        self, collection: str, options: TimeSeriesOptions, *, name: str | None = None
    ) -> bool:
        return await self.connection(
            name, requires=(DocumentFeature.TIME_SERIES,)
        ).create_time_series_collection(collection, options)

    async def get_time_series_options(
        self, collection: str, *, name: str | None = None
    ) -> TimeSeriesOptions | None:
        return await self.connection(
            name, requires=(DocumentFeature.TIME_SERIES,)
        ).get_time_series_options(collection)

    def transaction(
        self, *, name: str | None = None
    ) -> AbstractAsyncContextManager[SessionContext]:
        return self.connection(name, requires=(DocumentFeature.TRANSACTION,)).transaction()


def bind_document_manager(
    registrar: ProviderRegistrar,
    *,
    connections: Mapping[str, object],
    default: str | None,
    deployment_profile: str,
    policy_version: str,
    registrations: Iterable[DriverRegistration[DocumentDriver]],
    clock: Callable[[], datetime] | None = None,
    telemetry: TelemetryPublisher | None = None,
) -> None:
    """Bind one application-owned DocumentManager from inert registrations."""

    snapshot = tuple(registrations)
    if (
        not snapshot
        or any(
            type(registration) is not DriverRegistration or registration.manager_id != "document"
            for registration in snapshot
        )
        or len({registration.driver_name for registration in snapshot}) != len(snapshot)
    ):
        raise ManagerConfigurationError(
            protected_cause=ValueError(
                "document registrations must be non-empty, unique, and targeted"
            )
        )

    template = DocumentManager(
        connections=connections,
        default=default,
        deployment_profile=deployment_profile,
        policy_version=policy_version,
        clock=clock,
    )
    connection_snapshot: Mapping[str, object] = MappingProxyType(
        {name: definition[1] for name, definition in template._definitions.items()}
    )
    template.close()

    def create_manager(_resolver: Resolver) -> DocumentManager:
        manager = DocumentManager(
            connections=connection_snapshot,
            default=default,
            deployment_profile=deployment_profile,
            policy_version=policy_version,
            clock=clock,
            telemetry=telemetry,
        )
        for registration in snapshot:
            manager.install(registration)
        return manager

    registrar.bind(
        DocumentManager,
        create_manager,
        scope=Scope.APPLICATION,
        owned=True,
        replace=False,
    )
