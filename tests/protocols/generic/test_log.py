import pytest

from lionagi.protocols.generic.element import Element
from lionagi.protocols.generic.log import Log, LogManager, LogManagerConfig


# Test LogManagerConfig
class TestLogManagerConfig:
    def test_default_initialization(self):
        config = LogManagerConfig()
        assert config.persist_dir == "./data/logs"
        assert config.subfolder is None
        assert config.file_prefix is None
        assert config.capacity is None
        assert config.extension == ".json"
        assert config.use_timestamp is True
        assert config.hash_digits == 5
        assert config.auto_save_on_exit is True
        assert config.clear_after_dump is True

    def test_custom_initialization(self):
        config = LogManagerConfig(
            persist_dir="/custom/path",
            subfolder="sub",
            file_prefix="test",
            capacity=100,
            extension=".csv",
            use_timestamp=False,
            hash_digits=3,
            auto_save_on_exit=False,
            clear_after_dump=False,
        )
        assert config.persist_dir == "/custom/path"
        assert config.subfolder == "sub"
        assert config.file_prefix == "test"
        assert config.capacity == 100
        assert config.extension == ".csv"
        assert config.use_timestamp is False
        assert config.hash_digits == 3
        assert config.auto_save_on_exit is False
        assert config.clear_after_dump is False

    def test_extension_validation(self):
        # Test adding dot to extension
        config = LogManagerConfig(extension="csv")
        assert config.extension == ".csv"

        # Test keeping existing dot
        config = LogManagerConfig(extension=".json")
        assert config.extension == ".json"

    def test_edge_cases(self):
        # Test zero capacity
        config = LogManagerConfig(capacity=0)
        assert config.capacity == 0

        # Test zero hash digits
        config = LogManagerConfig(hash_digits=0)
        assert config.hash_digits == 0

        # Test negative capacity
        with pytest.raises(ValueError):
            LogManagerConfig(capacity=-1)

        # Test negative hash digits
        with pytest.raises(ValueError):
            LogManagerConfig(hash_digits=-1)


# Test Log
class TestLog:
    def test_create_from_dict(self):
        data = {"content": {"key": "value"}}
        log = Log.from_dict(data)
        assert log.content == data["content"]
        assert log._immutable is True

    def test_create_from_element(self):
        class TestElement(Element):
            field: str = "value"

        element = TestElement()
        log = Log.create(element)
        assert "field" in log.content
        assert log._immutable is False

    def test_immutability(self):
        log = Log.from_dict({"content": {"key": "value"}})

        # Test modifying content
        with pytest.raises(AttributeError):
            log.content = {"new": "value"}

        # Test adding new attribute
        with pytest.raises(AttributeError):
            log.new_field = "value"

    def test_invalid_creation(self):
        # Test invalid content type
        with pytest.raises(ValueError):
            Log(content="invalid")

        # Test invalid from_dict input
        with pytest.raises(ValueError):
            Log.from_dict("invalid")


# Test LogManager
class TestLogManager:
    @pytest.fixture
    def temp_dir(self, tmp_path):
        return tmp_path / "logs"

    def test_default_initialization(self):
        manager = LogManager()
        assert manager._config.persist_dir == "./data/logs"
        assert manager._config.capacity is None
        assert len(manager.logs) == 0

    def test_custom_initialization(self):
        manager = LogManager(persist_dir="/custom/path", capacity=100, extension=".json")
        assert manager._config.persist_dir == "/custom/path"
        assert manager._config.capacity == 100
        assert manager._config.extension == ".json"

    def test_sync_logging(self, temp_dir):
        manager = LogManager(persist_dir=temp_dir)
        log = Log(content={"key": "value"})
        manager.log(log)
        assert len(manager.logs) == 1
        assert manager.logs[0].content == {"key": "value"}

    @pytest.mark.asyncio
    async def test_async_logging(self, temp_dir):
        manager = LogManager(persist_dir=temp_dir)
        log = Log(content={"key": "value"})
        await manager.alog(log)
        assert len(manager.logs) == 1
        assert manager.logs[0].content == {"key": "value"}

    def test_file_persistence_json(self, temp_dir):
        manager = LogManager(persist_dir=temp_dir, extension=".json")
        log = Log(content={"key": "value"})
        manager.log(log)
        manager.dump()

        file = next(temp_dir.glob("*.json"))
        assert file.exists()
        assert file.stat().st_size > 0

    def test_file_persistence_csv(self, temp_dir):
        manager = LogManager(persist_dir=temp_dir, extension=".csv")
        log = Log(content={"key": "value"})
        manager.log(log)
        manager.dump()

        file = next(temp_dir.glob("*.csv"))
        assert file.exists()
        assert file.stat().st_size > 0

    def test_capacity_auto_dump(self, temp_dir):
        manager = LogManager(persist_dir=temp_dir, capacity=1)
        manager.log(Log(content={"key1": "value1"}))
        manager.log(Log(content={"key2": "value2"}))

        assert len(manager.logs) == 1
        assert manager.logs[0].content == {"key2": "value2"}
        assert len(list(temp_dir.glob("*.json"))) == 1
