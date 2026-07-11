# Copyright (c) 2025 - 2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Coverage tests for lionagi.ln._utils."""

import threading
import warnings
from datetime import datetime, timezone
from typing import Union
from uuid import UUID, uuid4

import anyio
import pytest

from lionagi.ln import _utils as lu
from lionagi.ln._utils import (
    _ALLOWED_MODULE_PREFIXES,
    acreate_path,
    async_synchronized,
    coerce_created_at,
    extract_types,
    load_type_from_string,
    register_type_prefix,
    synchronized,
    to_uuid,
)


class TestRegisterTypePrefix:
    def test_accepts_trailing_dot_prefix(self):
        register_type_prefix("mytestpkg.")
        assert "mytestpkg." in _ALLOWED_MODULE_PREFIXES
        _ALLOWED_MODULE_PREFIXES.discard("mytestpkg.")

    def test_rejects_missing_dot(self):
        with pytest.raises(ValueError, match="Prefix must end"):
            register_type_prefix("bad")


class TestLoadTypeFromString:
    def setup_method(self):
        lu._TYPE_CACHE.clear()

    def test_loads_allowed_type(self):
        cls = load_type_from_string("lionagi.ln._utils.UUID")
        assert cls is UUID

    def test_cached_on_second_call(self):
        first = load_type_from_string("lionagi.ln._utils.UUID")
        assert "lionagi.ln._utils.UUID" in lu._TYPE_CACHE
        second = load_type_from_string("lionagi.ln._utils.UUID")
        assert first is second

    def test_rejects_non_string(self):
        with pytest.raises(ValueError, match="Expected string"):
            load_type_from_string(42)  # type: ignore[arg-type]

    def test_rejects_missing_module(self):
        with pytest.raises(ValueError, match="no module"):
            load_type_from_string("bareword")

    def test_rejects_disallowed_prefix(self):
        with pytest.raises(ValueError, match="not in allowed prefixes"):
            load_type_from_string("os.path.join")

    def test_raises_on_missing_attribute(self):
        with pytest.raises(ValueError, match="Failed to load"):
            load_type_from_string("lionagi.ln._utils.DefinitelyNotHere")

    def test_raises_if_not_a_type(self):
        # `now_utc` is a function, not a type.
        with pytest.raises(ValueError, match="Failed to load"):
            load_type_from_string("lionagi.ln._utils.now_utc")


class TestExtractTypes:
    def test_single_type(self):
        assert extract_types(int) == {int}

    def test_union_types(self):
        result = extract_types(Union[int, str])
        assert result == {int, str}

    def test_pep604_union(self):
        result = extract_types(int | str)
        assert result == {int, str}

    def test_list_of_types(self):
        result = extract_types([int, str])
        assert result == {int, str}

    def test_list_with_union(self):
        result = extract_types([Union[int, str], float])
        assert result == {int, str, float}

    def test_set_of_types(self):
        result = extract_types({int, str})
        assert result == {int, str}

    def test_set_with_union(self):
        result = extract_types({Union[int, str]})
        assert result == {int, str}


class TestToUuid:
    def test_passthrough_uuid(self):
        u = uuid4()
        with pytest.warns(DeprecationWarning):
            assert to_uuid(u) is u

    def test_from_string(self):
        u = uuid4()
        with pytest.warns(DeprecationWarning):
            assert to_uuid(str(u)) == u

    def test_from_object_with_uuid_id(self):
        u = uuid4()

        class Obj:
            pass

        o = Obj()
        o.id = u
        with pytest.warns(DeprecationWarning):
            assert to_uuid(o) is u

    def test_from_object_with_string_id(self):
        u = uuid4()

        class Obj:
            pass

        o = Obj()
        o.id = str(u)
        with pytest.warns(DeprecationWarning):
            assert to_uuid(o) == u

    def test_raises_on_invalid(self):
        with pytest.warns(DeprecationWarning), pytest.raises(ValueError, match="Cannot get ID"):
            to_uuid(42)

    def test_warns_deprecation_with_replacement_guidance(self):
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            to_uuid(uuid4())

        deprecations = [w for w in caught if issubclass(w.category, DeprecationWarning)]
        assert len(deprecations) == 1
        message = str(deprecations[0].message)
        assert "lionagi.protocols.ids.to_uuid" in message
        assert "canonical_id" in message
        assert deprecations[0].filename == __file__


class TestCoerceCreatedAt:
    def test_naive_datetime_becomes_utc(self):
        naive = datetime(2025, 1, 1, 12, 0, 0)
        result = coerce_created_at(naive)
        assert result.tzinfo == timezone.utc

    def test_aware_datetime_passthrough(self):
        aware = datetime(2025, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
        assert coerce_created_at(aware) is aware

    def test_from_int_timestamp(self):
        result = coerce_created_at(1704067200)
        assert result.tzinfo == timezone.utc

    def test_from_float_timestamp(self):
        result = coerce_created_at(1704067200.5)
        assert result.tzinfo == timezone.utc

    def test_from_numeric_string(self):
        result = coerce_created_at("1704067200")
        assert result.tzinfo == timezone.utc

    def test_from_iso_string(self):
        result = coerce_created_at("2025-01-01T12:00:00+00:00")
        assert result.year == 2025

    def test_invalid_string_raises(self):
        with pytest.raises(ValueError, match="neither timestamp nor ISO"):
            coerce_created_at("not-a-date")

    def test_unsupported_type_raises(self):
        with pytest.raises(ValueError, match="Expected datetime"):
            coerce_created_at([1, 2, 3])


class TestSynchronizedDecorator:
    def test_sync_decorator_uses_lock(self):
        class Counter:
            def __init__(self):
                self._lock = threading.Lock()
                self.value = 0

            @synchronized
            def inc(self, n):
                self.value += n
                return self.value

        c = Counter()
        assert c.inc(5) == 5
        assert c.inc(2) == 7

    def test_sync_decorator_thread_safety(self):
        class Counter:
            def __init__(self):
                self._lock = threading.Lock()
                self.value = 0

            @synchronized
            def inc(self):
                self.value += 1

        c = Counter()
        threads = [threading.Thread(target=c.inc) for _ in range(50)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert c.value == 50


class TestAsyncSynchronizedDecorator:
    def test_async_decorator_uses_lock(self):
        class AsyncCounter:
            def __init__(self):
                self._async_lock = anyio.Lock()
                self.value = 0

            @async_synchronized
            async def inc(self, n):
                self.value += n
                return self.value

        async def main():
            c = AsyncCounter()
            assert await c.inc(3) == 3
            assert await c.inc(4) == 7

        anyio.run(main)


class TestAcreatePathTimeout:
    def test_timeout_raises(self, tmp_path):
        # Force timeout by requesting 0 seconds — the sleep in async mkdir/exists
        # will never complete within that window.
        async def main():
            with pytest.raises(TimeoutError, match="timed out"):
                await acreate_path(str(tmp_path), "f.txt", timeout=0.0)

        anyio.run(main)

    def test_timeout_none_passes_through(self, tmp_path):
        async def main():
            path = await acreate_path(str(tmp_path), "f.txt", timeout=None)
            assert str(path).endswith("f.txt")

        anyio.run(main)
