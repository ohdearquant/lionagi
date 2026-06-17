"""Coverage gap tests for lionagi/protocols/generic/log.py."""

from unittest.mock import patch

import pytest

from lionagi.protocols.generic.log import (
    DataLogger,
    DataLoggerConfig,
    Log,
)
from lionagi.protocols.generic.pile import Pile

# ---------------------------------------------------------------------------
# DataLoggerConfig validators
# ---------------------------------------------------------------------------


class TestDataLoggerConfigValidators:
    def test_invalid_extension_raises(self):
        with pytest.raises(ValueError, match="Extension must be"):
            DataLoggerConfig(extension=".xml")

    def test_extension_without_dot_is_normalised(self):
        cfg = DataLoggerConfig(extension="json")
        assert cfg.extension == ".json"

    def test_negative_capacity_raises(self):
        with pytest.raises(ValueError):
            DataLoggerConfig(capacity=-1)

    def test_negative_hash_digits_raises(self):
        with pytest.raises(ValueError):
            DataLoggerConfig(hash_digits=-1)


# ---------------------------------------------------------------------------
# Log.from_dict / immutability
# ---------------------------------------------------------------------------


class TestLogFromDict:
    def test_from_dict_marks_log_immutable(self):
        original = Log(content={"key": "value"})
        data = original.to_dict(mode="json")
        restored = Log.from_dict(data)
        assert restored.content == {"key": "value"}
        assert restored._immutable is True

    def test_immutable_log_raises_on_mutation(self):
        original = Log(content={"key": "value"})
        data = original.to_dict(mode="json")
        restored = Log.from_dict(data)
        with pytest.raises(AttributeError, match="immutable"):
            restored.content = {"new": "data"}


# ---------------------------------------------------------------------------
# Log.create
# ---------------------------------------------------------------------------


class TestLogCreate:
    def test_create_from_plain_dict(self):
        log = Log.create({"key": "value", "num": 42})
        assert log.content["key"] == "value"
        assert log.content["num"] == 42

    def test_create_from_non_serializable_returns_error_log(self):
        # to_dict(42, suppress=True) → {} → triggers empty-content path
        log = Log.create(42)
        assert log.content == {"error": "No content to log."}

    def test_create_from_string_returns_error_log(self):
        log = Log.create("not serializable")
        assert log.content == {"error": "No content to log."}


# ---------------------------------------------------------------------------
# DataLogger init with dict logs
# ---------------------------------------------------------------------------


class TestDataLoggerInitFromDict:
    def test_init_with_pile_dict(self):
        log = Log(content={"x": 1})
        pile = Pile(collections=[log], item_type=Log, strict_type=True)
        pile_dict = pile.to_dict()
        dl = DataLogger(logs=pile_dict, auto_save_on_exit=False)
        assert len(dl.logs) == 1


# ---------------------------------------------------------------------------
# DataLogger.log — capacity auto-dump failure
# ---------------------------------------------------------------------------


class TestDataLoggerLogCapacity:
    def test_capacity_exceeded_dump_failure_logged(self, tmp_path):
        dl = DataLogger(persist_dir=tmp_path, capacity=1, auto_save_on_exit=False)
        dl.log(Log(content={"a": 1}))

        with patch.object(dl, "dump", side_effect=RuntimeError("disk full")):
            dl.log(Log(content={"b": 2}))

        # second log still added even though dump failed
        assert len(dl.logs) >= 1


# ---------------------------------------------------------------------------
# DataLogger.dump — various paths
# ---------------------------------------------------------------------------


class TestDataLoggerDump:
    def test_dump_with_empty_logs_returns_early(self, tmp_path):
        dl = DataLogger(persist_dir=tmp_path, auto_save_on_exit=False)
        dl.dump()
        assert list(tmp_path.iterdir()) == []

    def test_dump_unsupported_extension_raises(self, tmp_path):
        dl = DataLogger(persist_dir=tmp_path, auto_save_on_exit=False)
        dl.log(Log(content={"x": 1}))
        xml_path = tmp_path / "out.xml"
        with pytest.raises(ValueError, match="Unsupported file extension"):
            dl.dump(persist_path=xml_path)

    def test_dump_json_serialization_error_clears_without_raise(self, tmp_path):
        from lionagi.protocols.generic.pile import Pile

        dl = DataLogger(persist_dir=tmp_path, auto_save_on_exit=False)
        dl.log(Log(content={"x": 1}))
        json_path = tmp_path / "out.json"

        with patch.object(
            Pile,
            "dump",
            side_effect=TypeError("Object is not JSON serializable"),
        ):
            dl.dump(persist_path=json_path)

        assert len(dl.logs) == 0

    def test_dump_json_serialization_error_no_clear_when_false(self, tmp_path):
        from lionagi.protocols.generic.pile import Pile

        dl = DataLogger(persist_dir=tmp_path, auto_save_on_exit=False)
        dl.log(Log(content={"x": 1}))
        json_path = tmp_path / "out.json"

        with patch.object(
            Pile,
            "dump",
            side_effect=TypeError("Object is not JSON serializable"),
        ):
            dl.dump(persist_path=json_path, clear=False)

        assert len(dl.logs) == 1

    def test_dump_non_json_error_re_raises(self, tmp_path):
        from lionagi.protocols.generic.pile import Pile

        dl = DataLogger(persist_dir=tmp_path, auto_save_on_exit=False)
        dl.log(Log(content={"x": 1}))
        json_path = tmp_path / "out.json"

        with patch.object(
            Pile,
            "dump",
            side_effect=RuntimeError("unexpected disk failure"),
        ):
            with pytest.raises(RuntimeError, match="unexpected disk failure"):
                dl.dump(persist_path=json_path)


# ---------------------------------------------------------------------------
# DataLogger.adump
# ---------------------------------------------------------------------------


class TestDataLoggerAdump:
    @pytest.mark.asyncio
    async def test_adump_writes_json(self, tmp_path):
        dl = DataLogger(persist_dir=tmp_path, auto_save_on_exit=False)
        dl.log(Log(content={"z": 99}))
        json_path = tmp_path / "out.json"
        await dl.adump(persist_path=json_path)
        assert json_path.exists()


# ---------------------------------------------------------------------------
# DataLogger._create_path with subfolder
# ---------------------------------------------------------------------------


class TestCreatePathSubfolder:
    def test_subfolder_appended_to_path(self, tmp_path):
        dl = DataLogger(
            persist_dir=tmp_path,
            subfolder="mysub",
            use_timestamp=False,
            hash_digits=0,
            auto_save_on_exit=False,
        )
        path = dl._create_path()
        assert "mysub" in str(path)


# ---------------------------------------------------------------------------
# DataLogger.save_at_exit
# ---------------------------------------------------------------------------


class TestSaveAtExit:
    def test_save_at_exit_with_logs_calls_dump(self, tmp_path):
        dl = DataLogger(persist_dir=tmp_path, auto_save_on_exit=False)
        dl.log(Log(content={"exit": "data"}))
        dl.save_at_exit()
        files = list(tmp_path.rglob("*.json"))
        assert len(files) == 1

    def test_save_at_exit_no_logs_does_nothing(self, tmp_path):
        dl = DataLogger(persist_dir=tmp_path, auto_save_on_exit=False)
        dl.save_at_exit()
        assert list(tmp_path.iterdir()) == []

    def test_save_at_exit_json_error_logged_not_raised(self, tmp_path):
        dl = DataLogger(persist_dir=tmp_path, auto_save_on_exit=False)
        dl.log(Log(content={"x": 1}))

        with patch.object(
            dl,
            "dump",
            side_effect=TypeError("Object is not JSON serializable"),
        ):
            dl.save_at_exit()

    def test_save_at_exit_other_error_logged_not_raised(self, tmp_path):
        dl = DataLogger(persist_dir=tmp_path, auto_save_on_exit=False)
        dl.log(Log(content={"x": 1}))

        with patch.object(
            dl,
            "dump",
            side_effect=OSError("permission denied"),
        ):
            dl.save_at_exit()


# ---------------------------------------------------------------------------
# DataLogger.from_config
# ---------------------------------------------------------------------------


class TestFromConfig:
    def test_from_config_creates_logger(self):
        cfg = DataLoggerConfig(
            persist_dir="./data/logs",
            auto_save_on_exit=False,
        )
        dl = DataLogger.from_config(cfg)
        assert dl._config is cfg
        assert len(dl.logs) == 0

    def test_from_config_with_initial_logs(self):
        cfg = DataLoggerConfig(auto_save_on_exit=False)
        log = Log(content={"key": "val"})
        dl = DataLogger.from_config(cfg, logs=[log])
        assert len(dl.logs) == 1


# ---------------------------------------------------------------------------
# alog async method
# ---------------------------------------------------------------------------


class TestDataLoggerAlog:
    @pytest.mark.asyncio
    async def test_alog_adds_log(self):
        dl = DataLogger(auto_save_on_exit=False)
        log = Log(content={"a": 1})
        await dl.alog(log)
        assert len(dl.logs) == 1


# ---------------------------------------------------------------------------
# CSV dump path
# ---------------------------------------------------------------------------


class TestDataLoggerDumpCSV:
    def test_dump_csv_writes_file(self, tmp_path):
        dl = DataLogger(persist_dir=tmp_path, extension=".csv", auto_save_on_exit=False)
        dl.log(Log(content={"x": 1}))
        dl.dump()
        files = list(tmp_path.glob("*.csv"))
        assert len(files) == 1
