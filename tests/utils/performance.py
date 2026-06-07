"""
Performance Monitoring Utilities for LionAGI Test Suite

Provides tools for monitoring test execution time, memory usage, and identifying
performance regressions in the test suite.
"""

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
        """
        Context manager for monitoring synchronous test performance.

        Args:
            test_name: Name of the test being monitored

        Usage:
            with monitor.monitor_sync("test_my_function"):
                # Test code here
                pass
        """
        # Start monitoring
        start_time = time.time()
        start_memory = None

        if self._memory_enabled:
            start_memory = tracemalloc.take_snapshot()

        process = psutil.Process()
        start_cpu_percent = process.cpu_percent()

        try:
            yield
        finally:
            # End monitoring
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
        """
        Async context manager for monitoring asynchronous test performance.

        Args:
            test_name: Name of the test being monitored

        Usage:
            async with monitor.monitor_async("test_my_async_function"):
                # Async test code here
                await some_async_function()
        """
        # Start monitoring
        start_time = time.time()
        start_memory = None

        if self._memory_enabled:
            start_memory = tracemalloc.take_snapshot()

        process = psutil.Process()
        start_cpu_percent = process.cpu_percent()

        try:
            yield
        finally:
            # End monitoring
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
        """
        Get tests that exceed duration threshold.

        Args:
            threshold: Duration threshold in seconds

        Returns:
            Dictionary of slow tests with their metrics
        """
        return {
            name: metrics
            for name, metrics in self.metrics.items()
            if metrics.get("duration", 0) > threshold
        }

    def get_memory_heavy_tests(self, threshold: float = 50.0) -> dict[str, dict[str, Any]]:
        """
        Get tests that exceed memory usage threshold.

        Args:
            threshold: Memory threshold in MB

        Returns:
            Dictionary of memory-heavy tests with their metrics
        """
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

        # Sort by duration and show top 5
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
    """
    Decorator for monitoring test performance.

    Args:
        test_name: Custom test name (if None, uses function name)

    Usage:
        @monitor_performance()
        def test_my_function():
            # Test code here
            pass

        @monitor_performance("custom_test_name")
        async def test_my_async_function():
            # Async test code here
            await some_function()
    """

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
    """
    Auto-setup performance monitoring for the test session.

    This fixture automatically enables performance monitoring for all tests
    and generates a report at the end of the test session.
    """
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
        """
        Assert that test performance is within specified bounds.

        Args:
            test_name: Name of the test to check
            max_duration: Maximum allowed duration in seconds
            max_memory_mb: Maximum allowed memory usage in MB
            monitor: Performance monitor instance (uses global if None)

        Raises:
            AssertionError: If performance exceeds bounds
        """
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
        """
        Compare current metrics with baseline and check for regressions.

        Args:
            current_metrics: Current test metrics
            baseline_metrics: Baseline metrics to compare against
            tolerance_percent: Allowed performance degradation percentage

        Returns:
            Dictionary indicating which metrics have regressed
        """
        regressions = {}

        for metric_name in ["duration", "memory_rss"]:
            current_value = current_metrics.get(metric_name, 0)
            baseline_value = baseline_metrics.get(metric_name, 0)

            if baseline_value > 0:
                percentage_change = ((current_value - baseline_value) / baseline_value) * 100
                regressions[metric_name] = percentage_change > tolerance_percent

        return regressions
