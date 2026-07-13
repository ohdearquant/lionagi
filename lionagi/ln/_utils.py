import contextlib
import copy as _copy
import importlib
import importlib.util
import types
import uuid
import warnings
from collections.abc import Awaitable, Callable, Mapping
from datetime import datetime, timezone
from functools import wraps
from pathlib import Path as StdPath
from types import UnionType
from typing import Annotated, Any, ParamSpec, TypeVar, Union, get_args, get_origin
from uuid import UUID

from anyio import Path as AsyncPath

P = ParamSpec("P")
R = TypeVar("R")
T = TypeVar("T")

__all__ = (
    "acreate_path",
    "async_synchronized",
    "coerce_created_at",
    "copy",
    "create_path",
    "extract_types",
    "get_bins",
    "import_module",
    "is_import_installed",
    "is_same_dtype",
    "is_union_type",
    "load_type_from_string",
    "now_utc",
    "register_type_prefix",
    "synchronized",
    "to_uuid",
    "union_members",
)


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _build_safe_path(
    directory: StdPath | str,
    filename: str,
    extension: str | None,
    timestamp: bool,
    time_prefix: bool,
    timestamp_format: str | None,
    random_hash_digits: int,
) -> StdPath:
    """Shared, symlink-safe path construction for create_path/acreate_path.

    Validates the filename and resolves the final target without touching the
    filesystem. Both the sync and async constructors call this so they share
    identical traversal/containment semantics (ADR-0050 D5) — fix the check
    once, here, rather than per-variant.
    """
    from lionagi.libs.path_safety import contain_and_resolve

    directory = StdPath(directory)

    # Resolve BEFORE filename can redirect directory into a subdirectory;
    # all containment checks validate against this fixed root.
    base_root = directory.resolve()

    def _contained(candidate: StdPath) -> StdPath:
        # contain_and_resolve() is the shared containment predicate (symlink-
        # safe resolve + relative_to); re-raise with this module's historical
        # wording so existing callers matching on message text keep working.
        try:
            return contain_and_resolve(candidate, base_root)
        except ValueError as exc:
            raise ValueError(
                f"Resolved path {candidate.resolve()} escapes base directory "
                f"{base_root}. Refusing to create path."
            ) from exc

    if "/" in filename:
        parts = filename.split("/")
        # Reject '.' or '..' to prevent directory traversal
        for component in parts:
            if component in (".", ".."):
                raise ValueError(
                    f"Filename components must not be '.' or '..'; "
                    f"got component {component!r} in {filename!r}."
                )
        sub_dir, filename = (
            parts[:-1],
            parts[-1],
        )
        directory = directory / "/".join(sub_dir)

    if "\\" in filename:
        raise ValueError("Filename cannot contain directory separators.")

    if filename in (".", ".."):
        raise ValueError(f"Filename must not be '.' or '..'; got {filename!r}.")

    # Both dir and candidate must resolve within base_root (symlink-safe) —
    # the one shared containment predicate, applied twice: once for a
    # slash-redirected directory, once for the final candidate.
    dir_resolved = _contained(directory.resolve())

    if "." in filename:
        name, ext = filename.rsplit(".", 1)
    else:
        name = filename
        ext = extension or ""
    ext = f".{ext.lstrip('.')}" if ext else ""

    if timestamp:
        ts_str = datetime.now().strftime(timestamp_format or "%Y%m%d%H%M%S")
        name = f"{ts_str}_{name}" if time_prefix else f"{name}_{ts_str}"

    if random_hash_digits > 0:
        random_suffix = uuid.uuid4().hex[:random_hash_digits]
        name = f"{name}-{random_suffix}"

    full_path = dir_resolved / f"{name}{ext}"
    return _contained(full_path.resolve())


async def acreate_path(
    directory: StdPath | AsyncPath | str,
    filename: str,
    extension: str | None = None,
    timestamp: bool = False,
    dir_exist_ok: bool = True,
    file_exist_ok: bool = False,
    time_prefix: bool = False,
    timestamp_format: str | None = None,
    random_hash_digits: int = 0,
    timeout: float | None = None,
) -> AsyncPath:
    """Async create_path: same validation, same return contract.

    Returns a fully resolved absolute path even when *directory* is relative;
    callers that need a cwd-relative representation must derive it themselves.
    """
    from .concurrency import move_on_after

    async def _impl() -> AsyncPath:
        full_path = AsyncPath(
            _build_safe_path(
                StdPath(str(directory)),
                filename,
                extension,
                timestamp,
                time_prefix,
                timestamp_format,
                random_hash_digits,
            )
        )

        await full_path.parent.mkdir(parents=True, exist_ok=dir_exist_ok)

        if await full_path.exists() and not file_exist_ok:
            raise FileExistsError(f"File {full_path} already exists and file_exist_ok is False.")

        return full_path

    if timeout is None:
        return await _impl()

    with move_on_after(timeout) as cancel_scope:
        result = await _impl()
    if cancel_scope.cancelled_caught:
        raise TimeoutError(f"acreate_path timed out after {timeout}s")
    return result


def get_bins(input_: list[str], upper: int) -> list[list[int]]:
    """Bin string indices by cumulative length limit."""
    current = 0
    bins = []
    current_bin = []
    for idx, item in enumerate(input_):
        if current + len(item) < upper:
            current_bin.append(idx)
            current += len(item)
        else:
            bins.append(current_bin)
            current_bin = [idx]
            current = len(item)
    if current_bin:
        bins.append(current_bin)
    return bins


def import_module(
    package_name: str,
    module_name: str = None,
    import_name: str | list = None,
) -> Any:
    try:
        full_import_path = f"{package_name}.{module_name}" if module_name else package_name

        if import_name:
            import_name = [import_name] if not isinstance(import_name, list) else import_name
            a = __import__(
                full_import_path,
                fromlist=import_name,
            )
            if len(import_name) == 1:
                return getattr(a, import_name[0])
            return [getattr(a, name) for name in import_name]
        else:
            return __import__(full_import_path)

    except ImportError as e:
        raise ImportError(f"Failed to import module {full_import_path}: {e}") from e


def is_import_installed(package_name: str) -> bool:
    return importlib.util.find_spec(package_name) is not None


# --- Dynamic type loading ---

_TYPE_CACHE: dict[str, type] = {}

_DEFAULT_ALLOWED_PREFIXES: frozenset[str] = frozenset({"lionagi."})
_ALLOWED_MODULE_PREFIXES: set[str] = set(_DEFAULT_ALLOWED_PREFIXES)


def register_type_prefix(prefix: str) -> None:
    """Register module prefix for dynamic type loading allowlist."""
    if not prefix.endswith("."):
        raise ValueError(f"Prefix must end with '.': {prefix}")
    _ALLOWED_MODULE_PREFIXES.add(prefix)


def load_type_from_string(type_str: str) -> type:
    """Load type from fully qualified path; only allowlisted prefixes."""
    if type_str in _TYPE_CACHE:
        return _TYPE_CACHE[type_str]

    if not isinstance(type_str, str):
        raise ValueError(f"Expected string, got {type(type_str)}")

    if "." not in type_str:
        raise ValueError(f"Invalid type path (no module): {type_str}")

    if not any(type_str.startswith(prefix) for prefix in _ALLOWED_MODULE_PREFIXES):
        raise ValueError(
            f"Module '{type_str}' not in allowed prefixes: {sorted(_ALLOWED_MODULE_PREFIXES)}"
        )

    try:
        module_path, class_name = type_str.rsplit(".", 1)
        module = importlib.import_module(module_path)
        if module is None:
            raise ImportError(f"Module '{module_path}' not found")

        type_class = getattr(module, class_name)
        if not isinstance(type_class, type):
            raise ValueError(f"'{type_str}' is not a type")

        _TYPE_CACHE[type_str] = type_class
        return type_class

    except (ValueError, ImportError, AttributeError) as e:
        raise ValueError(f"Failed to load type '{type_str}': {e}") from e


# --- Type extraction ---


def extract_types(item_type: Any) -> set[type]:
    """Extract concrete types from type annotations (Union, list, set)."""

    def is_union(t: Any) -> bool:
        origin = get_origin(t)
        return origin is Union or isinstance(t, UnionType)

    extracted: set[type] = set()

    if isinstance(item_type, set):
        for t in item_type:
            if is_union(t):
                extracted.update(get_args(t))
            else:
                extracted.add(t)
        return extracted

    if isinstance(item_type, list):
        for t in item_type:
            if is_union(t):
                extracted.update(get_args(t))
            else:
                extracted.add(t)
        return extracted

    if is_union(item_type):
        return set(get_args(item_type))

    return {item_type}


# --- UUID / datetime coercion ---


def to_uuid(value: Any) -> UUID:
    warnings.warn(
        "lionagi.ln.to_uuid is deprecated. For raw UUID/string values, use "
        "lionagi.protocols.ids.to_uuid instead; for generic objects (an "
        "Observable-like object with an .id attribute), use "
        "lionagi.protocols.ids.canonical_id instead. The two are not "
        "equivalent replacements.",
        DeprecationWarning,
        stacklevel=2,
    )
    if isinstance(value, UUID):
        return value
    if isinstance(value, str):
        return UUID(value)
    if hasattr(value, "id"):
        v = value.id
        if isinstance(v, UUID):
            return v
        if isinstance(v, str):
            return UUID(v)
    raise ValueError("Cannot get ID from item.")


def coerce_created_at(v: Any) -> datetime:
    """Coerce value to UTC-aware datetime (datetime, timestamp, or ISO string)."""
    if isinstance(v, datetime):
        return v.replace(tzinfo=timezone.utc) if v.tzinfo is None else v

    if isinstance(v, int | float):
        return datetime.fromtimestamp(v, tz=timezone.utc)

    if isinstance(v, str):
        with contextlib.suppress(ValueError):
            return datetime.fromtimestamp(float(v), tz=timezone.utc)
        with contextlib.suppress(ValueError):
            return datetime.fromisoformat(v)
        raise ValueError(f"String '{v}' is neither timestamp nor ISO format")

    raise ValueError(f"Expected datetime/timestamp/string, got {type(v).__name__}")


# --- Synchronization decorators ---


def synchronized(func: Callable[P, R]) -> Callable[P, R]:
    """Thread-safe method decorator; requires ``self._lock``."""

    @wraps(func)
    def wrapper(*args: P.args, **kwargs: P.kwargs) -> R:
        self = args[0]
        with self._lock:
            return func(*args, **kwargs)

    return wrapper


def async_synchronized(
    func: Callable[P, Awaitable[R]],
) -> Callable[P, Awaitable[R]]:
    """Async-safe method decorator; requires ``self._async_lock``."""

    @wraps(func)
    async def wrapper(*args: P.args, **kwargs: P.kwargs) -> R:
        self = args[0]
        async with self._async_lock:  # type: ignore[attr-defined]
            return await func(*args, **kwargs)

    return wrapper


def copy(obj: T, /, *, deep: bool = True, num: int = 1) -> T | list[T]:
    if num < 1:
        raise ValueError("Number of copies must be at least 1")
    copy_func = _copy.deepcopy if deep else _copy.copy
    return [copy_func(obj) for _ in range(num)] if num > 1 else copy_func(obj)


def is_same_dtype(
    input_: list[T] | dict[Any, T],
    dtype: type | None = None,
    return_dtype: bool = False,
) -> bool | tuple[bool, type | None]:
    if not input_:
        return (True, None) if return_dtype else True

    if isinstance(input_, Mapping):
        values_iter = iter(input_.values())
        first_val = next(values_iter, None)
        if dtype is None:
            dtype = type(first_val) if first_val is not None else None
        result = (dtype is None or isinstance(first_val, dtype)) and all(
            isinstance(v, dtype) for v in values_iter
        )
    else:
        first_val = input_[0]
        if dtype is None:
            dtype = type(first_val) if first_val is not None else None
        result = all(isinstance(e, dtype) for e in input_)

    return (result, dtype) if return_dtype else result


def is_union_type(tp) -> bool:
    """True for typing.Union[...] and PEP 604 unions (A | B)."""
    origin = get_origin(tp)
    return origin is Union or origin is getattr(types, "UnionType", object())


_NoneType = type(None)
_PEP604UnionType = getattr(types, "UnionType", None)


def _unwrap_annotated(tp):
    while get_origin(tp) is Annotated:
        tp = get_args(tp)[0]
    return tp


def union_members(
    tp, *, unwrap_annotated: bool = True, drop_none: bool = False
) -> tuple[type, ...]:
    """Return member types of a Union (typing.Union or A|B). Empty tuple if not a Union."""
    tp = _unwrap_annotated(tp) if unwrap_annotated else tp
    origin = get_origin(tp)
    if origin is not Union and origin is not _PEP604UnionType:
        return ()
    members = get_args(tp)
    if unwrap_annotated:
        members = tuple(_unwrap_annotated(m) for m in members)
    if drop_none:
        members = tuple(m for m in members if m is not _NoneType)
    return members


def create_path(
    directory: StdPath | str,
    filename: str,
    extension: str = None,
    timestamp: bool = False,
    dir_exist_ok: bool = True,
    file_exist_ok: bool = False,
    time_prefix: bool = False,
    timestamp_format: str | None = None,
    random_hash_digits: int = 0,
) -> StdPath:
    """Generate a file path under directory with optional timestamp and random suffix.

    Shares symlink-safe traversal/containment validation with acreate_path
    (see _build_safe_path) — a filename with `..`/absolute components, or a
    directory reached only through a symlink escape, is rejected here just as
    it is in the async constructor.

    Returns a fully resolved absolute path even when *directory* is relative;
    callers that need a cwd-relative representation must derive it themselves.
    """
    full_path = _build_safe_path(
        StdPath(directory),
        filename,
        extension,
        timestamp,
        time_prefix,
        timestamp_format,
        random_hash_digits,
    )

    full_path.parent.mkdir(parents=True, exist_ok=dir_exist_ok)
    if full_path.exists() and not file_exist_ok:
        raise FileExistsError(f"File {full_path} already exists and file_exist_ok is False.")

    return full_path
