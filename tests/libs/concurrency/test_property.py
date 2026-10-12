import anyio
import pytest

try:
    from hypothesis import given, settings
    from hypothesis import strategies as st
except ImportError:
    pytest.skip("hypothesis not installed", allow_module_level=True)

from lionagi.ln import bounded_map

# The hypothesis tests here run on asyncio only. anyio's pytest plugin wraps a hypothesis
# test's inner function once, with the runner of the first backend it meets, and later
# cases reuse that wrapper, so a [trio] case would run on asyncio.


@pytest.mark.asyncio
@given(
    values=st.lists(st.integers(), max_size=50),
    limit=st.integers(min_value=1, max_value=10),
)
@settings(max_examples=20, deadline=None)  # deadline=None is crucial for async tests
async def test_bounded_map_preserves_order_property(values, limit):
    """Property-based test that bounded_map preserves order."""

    async def echo(x):
        # Use a near-zero sleep to ensure the async machinery is engaged
        # without slowing down the tests.
        await anyio.sleep(0)
        return x

    out = await bounded_map(echo, values, limit=limit)
    assert out == values
