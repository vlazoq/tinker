# Testing Patterns Skill

You are an expert in writing effective pytest tests. Apply these patterns.

## Test File Structure
```python
# tests/test_<module_name>.py

import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from mymodule import MyClass


# ── Fixtures ──────────────────────────────────────────────────────────────────

@pytest.fixture
def my_instance():
    """Create a fresh instance for each test."""
    return MyClass(param="value")


@pytest.fixture
def mock_external_service():
    """Mock any external service (HTTP, DB, filesystem)."""
    with patch("mymodule.external_service") as mock:
        mock.return_value = {"status": "ok"}
        yield mock


# ── Tests ─────────────────────────────────────────────────────────────────────

class TestMyClass:
    def test_happy_path(self, my_instance):
        result = my_instance.do_thing("valid input")
        assert result == "expected output"

    def test_raises_on_invalid_input(self, my_instance):
        with pytest.raises(ValueError, match="expected error message"):
            my_instance.do_thing(None)

    def test_uses_external_service(self, my_instance, mock_external_service):
        my_instance.fetch_data()
        mock_external_service.assert_called_once_with(expected_arg="value")
```

## Test Naming Convention
Use: `test_<function>_<scenario>`

Good names:
- `test_router_returns_404_for_unknown_path`
- `test_router_raises_value_error_on_empty_method`
- `test_cache_returns_none_when_key_missing`
- `test_cache_returns_stored_value_after_set`

Bad names:
- `test_1`, `test_router`, `my_test`, `test_it_works`

## What to Test
For every public function, test at minimum:
1. **Happy path**: correct input → correct output
2. **Edge cases**: empty input, zero, None, empty list, boundary values
3. **Error cases**: invalid input → correct exception with correct message
4. **Side effects**: did it call the right external functions?

## Async Tests
```python
import pytest

@pytest.mark.asyncio
async def test_async_function():
    result = await my_async_function("input")
    assert result == "expected"

# For async context managers
@pytest.mark.asyncio
async def test_async_context_manager():
    async with MyContextManager() as ctx:
        result = await ctx.do_thing()
    assert result == "expected"
```

## Mocking Patterns

### Mock HTTP calls (httpx)
```python
from unittest.mock import AsyncMock, patch

@pytest.mark.asyncio
async def test_http_call():
    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.json.return_value = {"key": "value"}

    with patch("httpx.AsyncClient") as mock_client:
        mock_client.return_value.__aenter__.return_value.get = AsyncMock(
            return_value=mock_response
        )
        result = await my_function_that_calls_http()
    assert result == "expected"
```

### Mock file system
```python
from unittest.mock import mock_open, patch

def test_reads_file():
    mock_data = "file content here"
    with patch("builtins.open", mock_open(read_data=mock_data)):
        result = my_function_that_reads_file("path.txt")
    assert result == "expected from file content"
```

### Mock SQLite
```python
from unittest.mock import MagicMock, patch

def test_db_query():
    mock_rows = [{"id": "1", "name": "test"}]
    with patch("sqlite3.connect") as mock_connect:
        mock_connect.return_value.execute.return_value.fetchall.return_value = mock_rows
        result = my_function_that_queries_db()
    assert len(result) == 1
```

## pytest Configuration (conftest.py)
```python
# tests/conftest.py
import pytest

@pytest.fixture(scope="session")
def test_db(tmp_path_factory):
    """Create a temporary test database for the whole test session."""
    db_path = tmp_path_factory.mktemp("data") / "test.sqlite"
    # ... setup ...
    yield str(db_path)
    # ... teardown ...
```

## Parametrized Tests
```python
@pytest.mark.parametrize("input,expected", [
    ("hello",  "HELLO"),
    ("world",  "WORLD"),
    ("",       ""),
    ("123",    "123"),
])
def test_uppercase(input, expected):
    assert my_uppercase(input) == expected
```
