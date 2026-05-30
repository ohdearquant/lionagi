from __future__ import annotations

from lionagi.work.worker import clear_worker_registry, get_worker, worker


def test_worker_decorator_registers_and_name_inference():
    clear_worker_registry()

    @worker()
    def sample_worker(*, prompt: str):
        return {"instruction": prompt}

    registered = get_worker("sample_worker")
    assert registered is not None
    assert registered.name == "sample_worker"
    assert registered.func is sample_worker


def test_worker_decorator_custom_name():
    clear_worker_registry()

    @worker(name="custom_name")
    def base(*, prompt: str):
        return {"instruction": prompt}

    assert get_worker("custom_name") is not None
    assert get_worker("base") is None


def test_worker_with_explicit_retries_timeout():
    clear_worker_registry()

    @worker(retries=2, timeout=5.0)
    def fast(*, prompt: str):
        return {"instruction": prompt}

    registered = get_worker("fast")
    assert registered.retries == 2
    assert registered.timeout == 5.0
