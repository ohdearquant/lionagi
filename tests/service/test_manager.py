"""Tests for lionagi.service.manager module."""

from unittest.mock import MagicMock, create_autospec, patch

import pytest

from lionagi.service.imodel import iModel
from lionagi.service.manager import iModelManager


class TestiModelManagerInit:
    def test_init_with_args(self):
        # Create mock iModel instances with proper spec
        mock_model1 = create_autospec(iModel, instance=True)
        mock_model1.endpoint = MagicMock()
        mock_model1.endpoint.endpoint = "model1"
        mock_model2 = create_autospec(iModel, instance=True)
        mock_model2.endpoint = MagicMock()
        mock_model2.endpoint.endpoint = "model2"

        with patch("lionagi.service.manager.is_same_dtype", return_value=True):
            manager = iModelManager(mock_model1, mock_model2)

            assert "model1" in manager.registry
            assert "model2" in manager.registry
            assert manager.registry["model1"] is mock_model1
            assert manager.registry["model2"] is mock_model2

    def test_init_with_args_invalid_type_raises(self):
        not_a_model = "not an iModel"

        with patch("lionagi.service.manager.is_same_dtype", return_value=False):
            with pytest.raises(TypeError, match="Input models are not instances of iModel"):
                iModelManager(not_a_model)

    def test_init_with_kwargs(self):
        mock_chat_model = create_autospec(iModel, instance=True)
        mock_parse_model = create_autospec(iModel, instance=True)

        manager = iModelManager(chat=mock_chat_model, parse=mock_parse_model)

        assert "chat" in manager.registry
        assert "parse" in manager.registry
        assert manager.registry["chat"] is mock_chat_model
        assert manager.registry["parse"] is mock_parse_model

    def test_init_with_args_and_kwargs(self):
        mock_model1 = create_autospec(iModel, instance=True)
        mock_model1.endpoint = MagicMock()
        mock_model1.endpoint.endpoint = "model1"
        mock_custom_model = create_autospec(iModel, instance=True)

        with patch("lionagi.service.manager.is_same_dtype", return_value=True):
            manager = iModelManager(mock_model1, custom=mock_custom_model)

            assert "model1" in manager.registry
            assert "custom" in manager.registry
            assert manager.registry["model1"] is mock_model1
            assert manager.registry["custom"] is mock_custom_model


class TestiModelManagerRegisterIModel:
    def test_register_imodel_valid(self):
        manager = iModelManager()
        mock_model = create_autospec(iModel, instance=True)

        manager.register_imodel("test_model", mock_model)

        assert "test_model" in manager.registry
        assert manager.registry["test_model"] is mock_model

    def test_register_imodel_invalid_type_raises(self):
        manager = iModelManager()
        not_a_model = "not an iModel"

        with pytest.raises(TypeError, match="Input model is not an instance of iModel"):
            manager.register_imodel("invalid", not_a_model)

    def test_register_imodel_overwrites_existing(self):
        manager = iModelManager()
        mock_model1 = create_autospec(iModel, instance=True)
        mock_model2 = create_autospec(iModel, instance=True)

        manager.register_imodel("model", mock_model1)
        assert manager.registry["model"] is mock_model1

        manager.register_imodel("model", mock_model2)
        assert manager.registry["model"] is mock_model2


class TestiModelManagerShutdown:
    """Shutdown must stop every iModel's background executor so the CLI
    process can exit cleanly. The replenisher task is non-daemon-async
    relative to anyio.run: leaving it scheduled hangs anyio.run forever.
    """

    @pytest.mark.asyncio
    async def test_shutdown_stops_every_executor_replenisher_task(self):
        """Regression for the CLI-hang bug. cancel()+await on the
        replenisher task re-raises CancelledError in Python 3.11+; the
        old shutdown caught only Exception so the first close aborted
        the loop and subsequent iModels leaked their executor task.
        """
        chat = iModel(provider="openai", model="gpt-4.1-mini", api_key="t")
        parse = iModel(provider="openai", model="gpt-4.1-mini", api_key="t")
        manager = iModelManager(chat=chat, parse=parse)

        await chat.executor.start()
        await parse.executor.start()

        # Must not raise — CancelledError is BaseException, not Exception.
        await manager.shutdown()

        assert chat.executor.processor.is_stopped()
        assert parse.executor.processor.is_stopped()
        assert chat.executor.processor._rate_limit_replenisher_task is None
        assert parse.executor.processor._rate_limit_replenisher_task is None

    @pytest.mark.asyncio
    async def test_shutdown_continues_when_one_close_fails(self, caplog):
        """If one iModel.close() raises, the remaining models still get
        closed (don't leak executors on one bad endpoint).
        """
        import logging

        good = iModel(provider="openai", model="gpt-4.1-mini", api_key="t")
        await good.executor.start()

        bad = MagicMock()

        async def boom():
            raise RuntimeError("close failed")

        bad.close = boom
        manager = iModelManager()
        # Bypass the iModel isinstance check — we want a stand-in that
        # only the manager's iteration touches.
        manager.registry["bad"] = bad
        manager.register_imodel("good", good)

        with caplog.at_level(logging.WARNING, logger="lionagi.service"):
            await manager.shutdown()

        assert good.executor.processor.is_stopped()
        assert any("iModel shutdown failed" in r.message for r in caplog.records)


# ---------------------------------------------------------------------------
# Edge cases: concurrent register/shutdown, already-closed models,
# registry mutation during iteration
# ---------------------------------------------------------------------------


class TestiModelManagerEdgeCases:
    @pytest.mark.asyncio
    async def test_concurrent_register_and_shutdown(self):
        manager = iModelManager()

        models = [iModel(provider="openai", model="gpt-4.1-mini", api_key="t") for _ in range(3)]
        for i, m in enumerate(models):
            await m.executor.start()
            manager.register_imodel(f"model_{i}", m)

        mock_extra = MagicMock(spec=iModel)

        async def delayed_close():
            await asyncio.sleep(0.01)

        mock_extra.close = delayed_close

        async def register_during_shutdown():
            await asyncio.sleep(0.005)
            manager.register_imodel("extra", mock_extra)

        import asyncio

        await asyncio.gather(
            manager.shutdown(),
            register_during_shutdown(),
            return_exceptions=True,
        )

        for m in models:
            assert m.executor.processor.is_stopped()

    @pytest.mark.asyncio
    async def test_shutdown_already_closed_imodels(self):
        imodel = iModel(provider="openai", model="gpt-4.1-mini", api_key="t")
        await imodel.executor.start()
        await imodel.close()
        assert imodel.executor.processor.is_stopped()

        manager = iModelManager()
        manager.register_imodel("already_closed", imodel)

        await manager.shutdown()
        assert imodel.executor.processor.is_stopped()

    @pytest.mark.asyncio
    async def test_shutdown_empty_registry_is_noop(self):
        manager = iModelManager()
        await manager.shutdown()
        assert len(manager.registry) == 0

    def test_registry_mutation_after_init(self):
        manager = iModelManager()
        m1 = iModel(provider="openai", model="gpt-4.1-mini", api_key="t")
        m2 = iModel(provider="openai", model="gpt-4o", api_key="t")

        manager.register_imodel("a", m1)
        manager.register_imodel("b", m2)
        assert len(manager.registry) == 2

        del manager.registry["a"]
        assert len(manager.registry) == 1
        assert "b" in manager.registry
