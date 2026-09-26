"""Named document connections use the common runtime capability manager."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from af.domain.document.contracts import DocumentFeature
from af.domain.document.manager import DocumentManager
from af.framework.managers import CapabilityValidationError, DriverResolutionError
from af.infrastructure.mongo.provider import mongo_document_registration


def make_manager():
    manager = DocumentManager(
        connections={
            "primary": {"driver": "mongodb", "url": "mongodb://localhost", "database": "app"},
            "archive": {"driver": "mongodb", "url": "mongodb://localhost", "database": "archive"},
        },
        default="primary",
        deployment_profile="test",
        policy_version="v1",
    )
    manager.install(mongo_document_registration())
    return manager


@pytest.mark.asyncio
async def test_named_connections_use_selected_database_and_preserve_document_values() -> None:
    client = MagicMock()
    client.admin.command.return_value = {"maxWireVersion": 17}
    collection = client.__getitem__.return_value.__getitem__.return_value
    collection.with_options.return_value = collection
    collection.insert_one.return_value.inserted_id = 7
    with patch("af.infrastructure.mongo.driver.MongoClient", return_value=client):
        manager = make_manager()
        primary = manager.connection(requires=(DocumentFeature.CRUD,))
        assert primary is manager.connection()
        result = await manager.insert_one("records", {"_id": 7}, name="archive")
        assert result.inserted_id == 7
        assert manager.connection("archive") is not primary
        assert {call.args[0] for call in client.__getitem__.call_args_list} == {"app", "archive"}
        manager.close()
    client.close.assert_called()


def test_unsupported_transaction_fails_during_connection_selection() -> None:
    client = MagicMock()
    client.admin.command.return_value = {"maxWireVersion": 17}
    with patch("af.infrastructure.mongo.driver.MongoClient", return_value=client):
        manager = make_manager()
        with pytest.raises(CapabilityValidationError):
            manager.connection(requires=(DocumentFeature.TRANSACTION,))
        client.start_session.assert_not_called()
        manager.close()


def test_unknown_name_and_invalid_configuration_are_explicit() -> None:
    manager = make_manager()
    with pytest.raises(DriverResolutionError):
        manager.connection("missing")
    manager.close()


def test_public_application_provider_is_lazy_and_owns_document_connections(tmp_path) -> None:
    from af.bootstrap import create_application
    from af.config import Settings
    from af.domain.document.manager import bind_document_manager
    from af.framework.container import ProviderRegistrar, ServiceProvider

    class Documents(ServiceProvider):
        provider_id = "test.documents"

        def register(self, registrar: ProviderRegistrar) -> None:
            bind_document_manager(
                registrar,
                connections={
                    "main": {
                        "driver": "mongodb",
                        "url": "mongodb://localhost",
                        "database": "provider_test",
                    }
                },
                default="main",
                deployment_profile="test",
                policy_version="v1",
                registrations=(mongo_document_registration(),),
            )

    client = MagicMock()
    client.admin.command.return_value = {"maxWireVersion": 17}
    with patch("af.infrastructure.mongo.driver.MongoClient", return_value=client) as factory:
        application = create_application(
            settings=Settings(database_url=f"sqlite:///{tmp_path / 'app.sqlite'}"),
            providers=(Documents(),),
        )
        factory.assert_not_called()
        manager = application.container.resolve(DocumentManager)
        factory.assert_not_called()
        manager.connection()
        factory.assert_called_once()
        application.shutdown()
        client.close.assert_called_once()


def test_document_single_string_capability_is_not_split_into_letters() -> None:
    client = MagicMock()
    client.admin.command.return_value = {"maxWireVersion": 17}
    with patch("af.infrastructure.mongo.driver.MongoClient", return_value=client):
        manager = make_manager()
        assert manager.connection(requires="crud") is manager.connection()
        manager.close()
