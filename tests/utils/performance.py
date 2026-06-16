"""Performance monitoring utilities for the LionAGI test suite."""

import asyncio
import functools
import time
import tracemalloc
from collections.abc import Callable
from contextlib import asynccontextmanager, contextmanager
from typing import Any, TypeVar

import psutil
import pytest

T = TypeVar("T")


class TestPerformanceMonitor:
    """Monitor performance metrics for test execution."""

    def __init__(self):
        self.metrics: dict[str, dict[str, Any]] = {}
        self._memory_enabled = False

    def enable_memory_tracking(self):
        """Enable memory tracking for tests."""
        if not tracemalloc.is_tracing():
            tracemalloc.start()
            self._memory_enabled = True

    def disable_memory_tracking(self):
        """Disable memory tracking."""
        if tracemalloc.is_tracing():
            tracemalloc.stop()
            self._memory_enabled = False

    @contextmanager
    def monitor_sync(self, test_name: str):
        """Sync context manager that records duration and memory for the named test."""
        start_time = time.time()
        start_memory = None

        if self._memory_enabled:
            start_memory = tracemalloc.take_snapshot()

        process = psutil.Process()
        start_cpu_percent = process.cpu_percent()

        try:
            yield
        finally:
            end_time = time.time()
            duration = end_time - start_time

            end_cpu_percent = process.cpu_percent()
            memory_info = process.memory_info()

            metrics = {
                "duration": duration,
                "memory_rss": memory_info.rss / 1024 / 1024,  # MB
                "memory_vms": memory_info.vms / 1024 / 1024,  # MB
                "cpu_percent": end_cpu_percent - start_cpu_percent,
            }

            if self._memory_enabled and start_memory:
                end_memory = tracemalloc.take_snapshot()
                memory_diff = end_memory.compare_to(start_memory, "lineno")
                metrics["memory_peak"] = (
                    sum(stat.size_diff for stat in memory_diff) / 1024 / 1024
                )  # MB

            self.metrics[test_name] = metrics

    @asynccontextmanager
    async def monitor_async(self, test_name: str):
        """Async context manager that records duration and memory for the named test."""
        start_time = time.time()
        start_memory = None

        if self._memory_enabled:
            start_memory = tracemalloc.take_snapshot()

        process = psutil.Process()
        start_cpu_percent = process.cpu_percent()

        try:
            yield
        finally:
            end_time = time.time()
            duration = end_time - start_time

            end_cpu_percent = process.cpu_percent()
            memory_info = process.memory_info()

            metrics = {
                "duration": duration,
                "memory_rss": memory_info.rss / 1024 / 1024,  # MB
                "memory_vms": memory_info.vms / 1024 / 1024,  # MB
                "cpu_percent": end_cpu_percent - start_cpu_percent,
            }

            if self._memory_enabled and start_memory:
                end_memory = tracemalloc.take_snapshot()
                memory_diff = end_memory.compare_to(start_memory, "lineno")
                metrics["memory_peak"] = (
                    sum(stat.size_diff for stat in memory_diff) / 1024 / 1024
                )  # MB

            self.metrics[test_name] = metrics

    def get_metrics(self, test_name: str) -> dict[str, Any] | None:
        """Get performance metrics for a specific test."""
        return self.metrics.get(test_name)

    def get_all_metrics(self) -> dict[str, dict[str, Any]]:
        """Get all collected performance metrics."""
        return self.metrics.copy()

    def get_slow_tests(self, threshold: float = 1.0) -> dict[str, dict[str, Any]]:
        """Return tests whose duration exceeds the given threshold in seconds."""
        return {
            name: metrics
            for name, metrics in self.metrics.items()
            if metrics.get("duration", 0) > threshold
        }

    def get_memory_heavy_tests(self, threshold: float = 50.0) -> dict[str, dict[str, Any]]:
        """Return tests whose RSS memory usage exceeds the given threshold in MB."""
        return {
            name: metrics
            for name, metrics in self.metrics.items()
            if metrics.get("memory_rss", 0) > threshold
        }

    def generate_report(self) -> str:
        """Generate a performance report for all monitored tests."""
        if not self.metrics:
            return "No performance metrics collected."

        total_tests = len(self.metrics)
        total_duration = sum(m.get("duration", 0) for m in self.metrics.values())
        avg_duration = total_duration / total_tests if total_tests > 0 else 0

        slow_tests = self.get_slow_tests(1.0)
        memory_heavy = self.get_memory_heavy_tests(50.0)

        report = f"""
LionAGI Test Performance Report
===============================

Total Tests Monitored: {total_tests}
Total Duration: {total_duration:.2f}s
Average Duration: {avg_duration:.2f}s

Slow Tests (>1.0s): {len(slow_tests)}
Memory Heavy Tests (>50MB): {len(memory_heavy)}

Top 5 Slowest Tests:
"""

        sorted_tests = sorted(
            self.metrics.items(),
            key=lambda x: x[1].get("duration", 0),
            reverse=True,
        )[:5]

        for name, metrics in sorted_tests:
            duration = metrics.get("duration", 0)
            memory = metrics.get("memory_rss", 0)
            report += f"  {name}: {duration:.2f}s, {memory:.1f}MB\n"

        if slow_tests:
            report += "\nSlow Tests Details:\n"
            for name, metrics in slow_tests.items():
                duration = metrics.get("duration", 0)
                memory = metrics.get("memory_rss", 0)
                report += f"  {name}: {duration:.2f}s, {memory:.1f}MB\n"

        return report


# Global monitor instance
_performance_monitor = TestPerformanceMonitor()


def performance_monitor():
    """Get the global performance monitor instance."""
    return _performance_monitor


def monitor_performance(test_name: str | None = None):
    """Decorator that wraps sync or async test functions with performance monitoring."""

    def decorator(func: Callable[..., T]) -> Callable[..., T]:
        name = test_name or func.__name__

        if asyncio.iscoroutinefunction(func):

            @functools.wraps(func)
            async def async_wrapper(*args, **kwargs):
                async with _performance_monitor.monitor_async(name):
                    return await func(*args, **kwargs)

            return async_wrapper
        else:

            @functools.wraps(func)
            def sync_wrapper(*args, **kwargs):
                with _performance_monitor.monitor_sync(name):
                    return func(*args, **kwargs)

            return sync_wrapper

    return decorator


@pytest.fixture(scope="session", autouse=True)
def setup_performance_monitoring():
    """Enable memory tracking for the test session and print a report at teardown."""
    # Setup
    monitor = performance_monitor()
    monitor.enable_memory_tracking()

    yield monitor

    # Teardown - generate report
    report = monitor.generate_report()
    print("\n" + report)

    # Optionally save report to file
    try:
        with open("test_performance_report.txt", "w") as f:
            f.write(report)
        print("Performance report saved to test_performance_report.txt")
    except Exception as e:
        print(f"Failed to save performance report: {e}")

    monitor.disable_memory_tracking()


@pytest.fixture
def performance_monitor_fixture():
    """Fixture to provide performance monitor to individual tests."""
    return performance_monitor()


# Pytest plugin hooks for automatic performance monitoring
def pytest_runtest_setup(item):
    """Hook called before each test runs."""
    # Enable memory tracking if not already enabled
    monitor = performance_monitor()
    if not monitor._memory_enabled:
        monitor.enable_memory_tracking()


def pytest_runtest_teardown(item):
    """Hook called after each test completes."""
    # Collect metrics for the test that just ran
    test_name = f"{item.module.__name__}::{item.name}"
    monitor = performance_monitor()

    # If the test wasn't explicitly monitored, add basic timing
    if test_name not in monitor.metrics:
        # Basic timing information is available through pytest
        duration = getattr(item, "_test_duration", 0)
        if duration > 0:
            monitor.metrics[test_name] = {"duration": duration}


class PerformanceRegression:
    """Utility for detecting performance regressions."""

    @staticmethod
    def assert_performance_within_bounds(
        test_name: str,
        max_duration: float,
        max_memory_mb: float = None,
        monitor: TestPerformanceMonitor = None,
    ):
        """Raise AssertionError if the named test's duration or memory exceeds the given bounds."""
        if monitor is None:
            monitor = performance_monitor()

        metrics = monitor.get_metrics(test_name)
        if not metrics:
            raise AssertionError(f"No performance metrics found for test: {test_name}")

        duration = metrics.get("duration", 0)
        if duration > max_duration:
            raise AssertionError(
                f"Test {test_name} duration {duration:.2f}s exceeds limit {max_duration}s"
            )

        if max_memory_mb is not None:
            memory = metrics.get("memory_rss", 0)
            if memory > max_memory_mb:
                raise AssertionError(
                    f"Test {test_name} memory usage {memory:.1f}MB exceeds limit {max_memory_mb}MB"
                )

    @staticmethod
    def compare_with_baseline(
        current_metrics: dict[str, Any],
        baseline_metrics: dict[str, Any],
        tolerance_percent: float = 10.0,
    ) -> dict[str, bool]:
        """Return a dict of metric names to bool indicating whether each has regressed beyond the tolerance."""
        regressions = {}

        for metric_name in ["duration", "memory_rss"]:
            current_value = current_metrics.get(metric_name, 0)
            baseline_value = baseline_metrics.get(metric_name, 0)

            if baseline_value > 0:
                percentage_change = ((current_value - baseline_value) / baseline_value) * 100
                regressions[metric_name] = percentage_change > tolerance_percent

        return regressions
