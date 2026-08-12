"""Run private, approved recipe replay cases without leaking business bodies.

``compiled-recipes.json`` describes an executable candidate, but it is not
proof that the candidate reproduces an approved historical result.  This
runner executes the *same portable executor* against controlled historical
inputs, compares only normalized result hashes and counts, and writes an
auditable report that deliberately contains no request text, source paths,
rule rows, or result rows.

The private case fixture is an input to this runner, not a distributable
artifact.  A platform approval workflow must create and retain that fixture
or its signed receipt.  It commits the request, every readable runtime source
mapping and its content fingerprint.  The report is safe to include in a
capability package because it contains only integrity metadata and comparison
summaries.
"""

from __future__ import annotations

import argparse
from contextlib import contextmanager
import hashlib
import importlib.util
import json
import math
import os
import re
import sys
import tempfile
from pathlib import Path
from typing import Any, Iterable, Sequence


SCHEMA_VERSION = 1
REPORT_KIND = "compiled_recipe_replay_report"
CASES_KIND = "compiled_recipe_replay_cases"
MAX_CASES = 256
MAX_FIXTURE_BYTES = 4 * 1024 * 1024
MAX_REQUEST_BYTES = 64 * 1024
MAX_EXECUTOR_FILE_BYTES = 16 * 1024 * 1024
MAX_EXECUTOR_CLOSURE_BYTES = 48 * 1024 * 1024
HEX_SHA256 = re.compile(r"^[0-9a-f]{64}$")
SOURCE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{1,127}$")
SCRIPT_DIR = Path(__file__).resolve().parent
DEFAULT_EXECUTOR = (
    SCRIPT_DIR.parent / "assets" / "portable-scenario-executor" / "scripts" / "execute_scenario.py"
)
REPLAY_ISOLATION_ENV = "BFE_REPLAY_WORKER_ISOLATED"


class ReplayError(ValueError):
    """A fail-closed replay validation error with a report-safe code."""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


class ExecutorSnapshot:
    """The exact candidate bytes imported by one replay run.

    The report fingerprints originate from these bytes, rather than from a
    source path that may be replaced after it was hashed.  ``executor_path``
    is private temporary state and is never serialized.
    """

    __slots__ = ("executor_path", "sha256", "closure_sha256")

    def __init__(self, executor_path: Path, sha256: str, closure_sha256: str):
        self.executor_path = executor_path
        self.sha256 = sha256
        self.closure_sha256 = closure_sha256


def require_isolated_replay_worker() -> None:
    """Refuse to execute private cases outside the designated isolated worker.

    This is an operational attestation, not a substitute for sandbox policy:
    the platform's worker image must enforce no network, no signing key and
    read-only mounts.  Keeping this explicit prevents an accidental local run
    of arbitrary candidate code against private historical requests.
    """

    if os.environ.get("BUSINESS_FLOW_PLATFORM_APPROVAL_HMAC_KEY"):
        raise ReplayError("replay_runner_signing_environment_forbidden")
    if os.environ.get(REPLAY_ISOLATION_ENV, "").strip().casefold() not in {"1", "true", "yes"}:
        raise ReplayError("replay_worker_isolation_required")


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def read_bytes_once(path: Path, *, code: str, max_bytes: int) -> bytes:
    """Capture one bounded byte sequence without a later path re-read.

    Callers must either consume the returned bytes directly or materialize
    them into the runner's private snapshot.  In particular, this deliberately
    does not hash a file and then ask a JSON parser or import loader to read
    the original path again.
    """

    try:
        with path.open("rb") as handle:
            raw = handle.read(max_bytes + 1)
    except OSError as exc:
        raise ReplayError(code) from exc
    if len(raw) > max_bytes:
        raise ReplayError(code)
    return raw


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as handle:
            while chunk := handle.read(1024 * 1024):
                digest.update(chunk)
    except OSError as exc:
        raise ReplayError("source_unavailable") from exc
    return digest.hexdigest()


def _helper_source(path: Path, filename: str, asset_directory: str) -> Path | None:
    """Resolve the helper exactly as the portable executor would.

    Final bundles place helpers beside ``execute_scenario.py``.  Source-tree
    development keeps them under the sibling portable-reader assets, so that
    fallback is captured into the snapshot as well instead of being imported
    from ambient code after the candidate was fingerprinted.
    """

    scripts_root = path.parent
    direct = scripts_root / filename
    if direct.is_file():
        return direct
    for parent_index in (1, 2):
        try:
            candidate = scripts_root.parents[parent_index] / asset_directory / "scripts" / filename
        except IndexError:
            continue
        if candidate.is_file():
            return candidate
    return None


def executor_closure_sources(path: Path) -> list[tuple[str, Path]]:
    """List all local source files that this executor can import at runtime.

    Logical names describe the private execution layout, not the original
    filesystem.  This makes a source-tree fallback reader part of the same
    closure as a release that carries that reader beside the executor.
    """

    path = path.expanduser().resolve()
    scripts_root = path.parent
    executor_root = scripts_root.parent
    recipe_runtime_path = scripts_root / "recipe_runtime.py"
    if not path.is_file() or not recipe_runtime_path.is_file():
        raise ReplayError("executor_unavailable")
    sources: list[tuple[str, Path]] = [
        ("scripts/execute_scenario.py", path),
        ("scripts/recipe_runtime.py", recipe_runtime_path),
    ]
    for filename, asset_directory in (
        ("query_tabular.py", "portable-tabular-reader"),
        ("extract_documents.py", "portable-document-reader"),
    ):
        helper = _helper_source(path, filename, asset_directory)
        if helper is not None:
            sources.append((f"scripts/{filename}", helper))
    requirements = executor_root / "requirements.txt"
    if requirements.is_file():
        sources.append(("requirements.txt", requirements))
    return sources


def executor_closure_digest(entries: Sequence[tuple[str, bytes]]) -> str:
    digest = hashlib.sha256()
    for relative, raw in sorted(entries, key=lambda item: item[0]):
        digest.update(relative.encode("utf-8"))
        digest.update(b"\0")
        digest.update(raw)
        digest.update(b"\0")
    return digest.hexdigest()


def executor_closure_fingerprint(path: Path) -> str:
    """Fingerprint the concrete portable executor closure from one byte read."""

    entries = [
        (relative, read_bytes_once(source, code="executor_unavailable", max_bytes=MAX_EXECUTOR_FILE_BYTES))
        for relative, source in executor_closure_sources(path)
    ]
    if sum(len(raw) for _, raw in entries) > MAX_EXECUTOR_CLOSURE_BYTES:
        raise ReplayError("executor_unavailable")
    return executor_closure_digest(entries)


def read_json_object(path: Path, *, code: str, max_bytes: int = MAX_FIXTURE_BYTES) -> dict[str, Any]:
    _, _, payload = read_json_object_bytes_with_digest(path, code=code, max_bytes=max_bytes)
    return payload


def read_json_object_bytes_with_digest(
    path: Path, *, code: str, max_bytes: int = MAX_FIXTURE_BYTES,
) -> tuple[bytes, str, dict[str, Any]]:
    """Parse the exact JSON bytes whose digest is placed in the report."""

    raw = read_bytes_once(path, code=code, max_bytes=max_bytes)
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise ReplayError(code) from exc
    if not isinstance(value, dict):
        raise ReplayError(code)
    return raw, sha256_bytes(raw), value


def read_json_object_with_digest(
    path: Path, *, code: str, max_bytes: int = MAX_FIXTURE_BYTES,
) -> tuple[str, dict[str, Any]]:
    """Compatibility wrapper for callers that do not need the raw snapshot."""

    _, digest, payload = read_json_object_bytes_with_digest(path, code=code, max_bytes=max_bytes)
    return digest, payload


def write_private_snapshot_bytes(path: Path, raw: bytes) -> Path:
    """Write private replay bytes once, then make the materialized copy read-only.

    The snapshot lives below a freshly-created ``TemporaryDirectory``.  It is
    intentionally never referenced by a report or exception message.
    """

    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("xb") as handle:
            handle.write(raw)
        os.chmod(path, 0o444)
    except OSError as exc:
        raise ReplayError("private_snapshot_unavailable") from exc
    return path


def seal_private_snapshot(root: Path) -> None:
    """Remove ordinary write bits after every control/executor file is present."""

    try:
        for item in root.rglob("*"):
            if item.is_file():
                os.chmod(item, 0o444)
    except OSError:
        # The OS worker isolation is the security boundary.  Some Windows
        # filesystems expose only a partial chmod model, but the snapshot
        # remains in the runner-owned private temporary directory.
        pass


def materialize_control_snapshots(
    root: Path, *, catalog: bytes, replay_contract: bytes, cases: bytes,
    trace_review: bytes, runtime_contract: bytes, flow_contract: bytes,
) -> dict[str, Path]:
    """Persist exact control bytes for any component that needs a path later."""

    controls = {
        "catalog": ("compiled-recipes.json", catalog),
        "replay_contract": ("recipe-replay-contract.json", replay_contract),
        "cases": ("private-replay-cases.json", cases),
        "trace_review": ("trace-review.json", trace_review),
        "runtime_contract": ("runtime-contract.json", runtime_contract),
        "flow_contract": ("flow-contract.json", flow_contract),
    }
    return {
        name: write_private_snapshot_bytes(root / "controls" / filename, raw)
        for name, (filename, raw) in controls.items()
    }


def materialize_executor_snapshot(path: Path, root: Path) -> ExecutorSnapshot:
    """Freeze every executor byte that may be imported during a replay.

    All helper files are copied into one ``scripts`` directory.  That prevents
    the source-tree fallback import paths in ``execute_scenario.py`` from
    reaching an ambient reader after the report fingerprint was calculated.
    """

    entries: list[tuple[str, bytes]] = []
    for relative, source in executor_closure_sources(path):
        raw = read_bytes_once(source, code="executor_unavailable", max_bytes=MAX_EXECUTOR_FILE_BYTES)
        entries.append((relative, raw))
    if sum(len(raw) for _, raw in entries) > MAX_EXECUTOR_CLOSURE_BYTES:
        raise ReplayError("executor_unavailable")
    entry_map = dict(entries)
    executor_raw = entry_map.get("scripts/execute_scenario.py")
    if executor_raw is None:
        raise ReplayError("executor_unavailable")
    for relative, raw in entries:
        write_private_snapshot_bytes(root / relative, raw)
    return ExecutorSnapshot(
        executor_path=root / "scripts" / "execute_scenario.py",
        sha256=sha256_bytes(executor_raw),
        closure_sha256=executor_closure_digest(entries),
    )


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def normalized_value(value: Any) -> Any:
    """Turn executor output into a stable JSON value without changing facts."""

    if value is None or isinstance(value, (bool, int, str)):
        return value
    if isinstance(value, float):
        if math.isnan(value):
            return {"$number": "NaN"}
        if math.isinf(value):
            return {"$number": "Infinity" if value > 0 else "-Infinity"}
        return value
    if isinstance(value, dict):
        return {str(key): normalized_value(item) for key, item in sorted(value.items(), key=lambda pair: str(pair[0]))}
    if isinstance(value, (list, tuple)):
        return [normalized_value(item) for item in value]
    return str(value)


def normalize_deterministic_result(result: dict[str, Any]) -> dict[str, Any]:
    """Build the result projection whose SHA-256 is compared to the oracle.

    The public report only carries the digest of this value.  Keeping the
    normalization here narrowly scoped means a harmless row order variation
    does not invalidate a replay, while changes to values, projections,
    summary counts, coverage, or materialized family terms do.
    """

    rows_raw = result.get("rows") if isinstance(result.get("rows"), list) else []
    rows = [normalized_value(item) for item in rows_raw]
    rows.sort(key=canonical_json)
    summary = result.get("summary") if isinstance(result.get("summary"), dict) else {}
    coverage = result.get("coverage") if isinstance(result.get("coverage"), dict) else {}
    rule_family = result.get("rule_family") if isinstance(result.get("rule_family"), dict) else {}
    template_id = str(rule_family.get("template_id") or result.get("recipe_id") or "")
    return {
        "recipe_id": template_id,
        "kind": normalized_value(result.get("kind")),
        "source_id": normalized_value(result.get("source_id")),
        "group_by": normalized_value(result.get("group_by") if isinstance(result.get("group_by"), list) else []),
        "emit": normalized_value(result.get("emit")),
        "columns": normalized_value(result.get("columns") if isinstance(result.get("columns"), list) else []),
        "rows": rows,
        "summary": {
            "matched_row_count": normalized_value(summary.get("matched_row_count")),
            "matched_group_count": normalized_value(summary.get("matched_group_count")),
            "measure_field": normalized_value(summary.get("measure_field")),
            "measure_sum": normalized_value(summary.get("measure_sum")),
        },
        "coverage": {
            "complete_for_all_matching_runtime_rows": normalized_value(
                coverage.get("complete_for_all_matching_runtime_rows")
            ),
            "returned_row_count": normalized_value(coverage.get("returned_row_count")),
            "total_matched_row_count": normalized_value(coverage.get("total_matched_row_count")),
            "truncated": normalized_value(coverage.get("truncated")),
        },
        "rule_family": {
            "template_id": normalized_value(rule_family.get("template_id")),
            "extracted_terms": normalized_value(
                rule_family.get("extracted_terms") if isinstance(rule_family.get("extracted_terms"), list) else []
            ),
        } if rule_family else None,
    }


def normalized_result_digest(result: dict[str, Any]) -> str:
    return sha256_bytes(canonical_json(normalize_deterministic_result(result)).encode("utf-8"))


def request_digest(request: str) -> str:
    return sha256_bytes(request.encode("utf-8"))


def is_sha256(value: Any) -> bool:
    return bool(HEX_SHA256.fullmatch(str(value or "").casefold()))


def safe_relative_path(value: Any) -> Path:
    path = Path(str(value or ""))
    if not str(path) or path.is_absolute() or ".." in path.parts:
        raise ReplayError("source_binding_invalid")
    return path


def parse_global_bindings(values: Sequence[str]) -> dict[str, str]:
    bindings: dict[str, str] = {}
    for raw in values:
        source_id, separator, relative = str(raw).partition("=")
        source_id = source_id.strip()
        relative = relative.strip()
        if not separator or not SOURCE_ID.fullmatch(source_id):
            raise ReplayError("source_binding_invalid")
        safe_relative_path(relative)
        if source_id in bindings:
            raise ReplayError("source_binding_invalid")
        bindings[source_id] = relative
    return bindings


def fixture_bindings(value: Any) -> dict[str, str]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ReplayError("source_binding_invalid")
    bindings: dict[str, str] = {}
    for source_id, relative in value.items():
        normalized_id = str(source_id)
        if not SOURCE_ID.fullmatch(normalized_id) or not isinstance(relative, str):
            raise ReplayError("source_binding_invalid")
        safe_relative_path(relative)
        bindings[normalized_id] = relative
    return bindings


def fixture_source_fingerprints(value: Any) -> dict[str, dict[str, Any]]:
    """Read private source commitments without putting paths into the report."""

    if not isinstance(value, dict):
        raise ReplayError("fixture_source_fingerprints_invalid")
    fingerprints: dict[str, dict[str, Any]] = {}
    for source_id, raw in value.items():
        normalized_id = str(source_id)
        if not SOURCE_ID.fullmatch(normalized_id) or not isinstance(raw, dict):
            raise ReplayError("fixture_source_fingerprints_invalid")
        digest = str(raw.get("sha256") or "").casefold()
        size_bytes = raw.get("size_bytes")
        if (
            not is_sha256(digest)
            or isinstance(size_bytes, bool)
            or not isinstance(size_bytes, int)
            or size_bytes < 0
        ):
            raise ReplayError("fixture_source_fingerprints_invalid")
        fingerprints[normalized_id] = {"sha256": digest, "size_bytes": size_bytes}
    return fingerprints


def source_path(source: dict[str, Any], data_root: Path, bindings: dict[str, str]) -> Path:
    source_id = str(source.get("source_id") or "")
    if not source_id:
        raise ReplayError("source_unavailable")
    relative = safe_relative_path(bindings.get(source_id) or source.get("path"))
    root = data_root.resolve()
    path = (root / relative).resolve()
    if not path.is_relative_to(root) or not path.is_file():
        raise ReplayError("source_unavailable")
    return path


def replay_input_source_ids(recipe: dict[str, Any], runtime_contract: dict[str, Any]) -> set[str]:
    """Return every declared runtime source that this executor path can read.

    Rule selection scans all tabular rule sources before it can choose the
    recipe's governing record.  The hybrid route then indexes every declared
    non-tabular runtime source.  Fingerprinting only the selected recipe's
    data would leave these real inputs outside the replay proof.
    """

    required = {
        str(recipe.get("source_id") or ""),
        *(str(item) for item in recipe.get("context_source_ids", []) if str(item)),
    }
    selector = recipe.get("rule_selector") if isinstance(recipe.get("rule_selector"), dict) else {}
    required.add(str(selector.get("source_id") or ""))
    rule_source_ids = {
        str(item) for item in runtime_contract.get("rule_source_ids", []) if str(item)
    }
    source_by_id = {
        str(item.get("source_id") or ""): item
        for item in runtime_contract.get("sources", [])
        if isinstance(item, dict) and str(item.get("source_id") or "")
    }
    for source_id in rule_source_ids:
        source = source_by_id.get(source_id)
        if source is not None and str(source.get("kind") or "").casefold() == "tabular":
            required.add(source_id)
    for source_id, source in source_by_id.items():
        if (
            source.get("runtime_required") is True
            and str(source.get("kind") or "").casefold() != "tabular"
        ):
            required.add(source_id)
    required.discard("")
    return required


def case_source_commitments(
    case: dict[str, Any], recipe: dict[str, Any], runtime_contract: dict[str, Any],
    global_bindings: dict[str, str],
) -> tuple[dict[str, str], dict[str, dict[str, Any]]]:
    """Require the private fixture to bind every executable source exactly.

    ``--bind`` remains available for a platform that needs to declare its
    mount layout, but it is an assertion only: it must list the full source
    set and match the private fixture byte-for-byte.  Actual source selection
    always comes from the fixture, so an unreviewed CLI value cannot redirect
    one recipe to a different historical file.
    """

    bindings = fixture_bindings(case.get("source_bindings"))
    expected_fingerprints = fixture_source_fingerprints(case.get("source_fingerprints"))
    required = replay_input_source_ids(recipe, runtime_contract)
    if set(bindings) != required or set(expected_fingerprints) != required:
        raise ReplayError("fixture_source_bindings_incomplete")
    if global_bindings:
        if set(global_bindings) != required or any(
            global_bindings.get(source_id) != bindings[source_id] for source_id in required
        ):
            raise ReplayError("global_binding_not_fixture_bound")
    return bindings, expected_fingerprints


def validate_fixture_source_commitments(
    fixture_cases: Sequence[dict[str, Any]], catalog: Sequence[dict[str, Any]],
    runtime_contract: dict[str, Any], global_bindings: dict[str, str],
) -> None:
    """Reject an incomplete/overriding binding before any private execution."""

    recipes_by_id = {
        str(item.get("id") or ""): item
        for item in catalog
        if isinstance(item, dict) and str(item.get("id") or "")
    }
    for case in fixture_cases:
        recipe_id = str(case.get("recipe_id") or "")
        recipe = recipes_by_id.get(recipe_id)
        if recipe is None:
            raise ReplayError("recipe_missing_from_catalog")
        case_source_commitments(case, recipe, runtime_contract, global_bindings)


def snapshot_runtime_sources(
    recipe: dict[str, Any], runtime_contract: dict[str, Any], data_root: Path,
    bindings: dict[str, str], expected_fingerprints: dict[str, dict[str, Any]], snapshot_root: Path,
) -> tuple[list[dict[str, Any]], dict[str, str]]:
    """Copy all replay inputs before execution and bind the executor to them.

    The private snapshot avoids a time-of-check/time-of-use race between
    source verification and DuckDB/document extraction.  The fixture commits
    both the source path mapping and its bytes.  We stream one read directly
    into a private copy, hash that same stream, and execute only the copy.
    """

    source_by_id = {
        str(item.get("source_id") or ""): item
        for item in runtime_contract.get("sources", [])
        if isinstance(item, dict) and str(item.get("source_id") or "")
    }
    required_source_ids = replay_input_source_ids(recipe, runtime_contract)
    if set(expected_fingerprints) != required_source_ids:
        raise ReplayError("fixture_source_bindings_incomplete")
    snapshot_bindings = dict(bindings)
    fingerprints: list[dict[str, Any]] = []
    for source_id in sorted(required_source_ids):
        source = source_by_id.get(source_id)
        if source is None:
            raise ReplayError("source_unavailable")
        origin = source_path(source, data_root, bindings)
        expected = expected_fingerprints[source_id]
        suffix = origin.suffix or ".bin"
        destination = snapshot_root / f"{source_id}{suffix}"
        try:
            digest = hashlib.sha256()
            size_bytes = 0
            with origin.open("rb") as reader, destination.open("xb") as writer:
                while chunk := reader.read(1024 * 1024):
                    digest.update(chunk)
                    writer.write(chunk)
                    size_bytes += len(chunk)
            snapshot_digest = digest.hexdigest()
        except OSError as exc:
            raise ReplayError("source_snapshot_failed") from exc
        if (
            snapshot_digest != str(expected["sha256"])
            or size_bytes != int(expected["size_bytes"])
        ):
            raise ReplayError("fixture_source_fingerprint_mismatch")
        try:
            os.chmod(destination, 0o444)
        except OSError:
            # Windows ACLs may not accept chmod; the private temp directory is
            # still isolated and the content digest is checked above.
            pass
        snapshot_bindings[source_id] = destination.name
        fingerprints.append({
            "source_id": source_id,
            "sha256": snapshot_digest,
            "size_bytes": size_bytes,
        })
    if not fingerprints:
        raise ReplayError("source_unavailable")
    return fingerprints, snapshot_bindings


def _expected_executor_helpers(path: Path) -> dict[str, Path]:
    return {
        name: (path.parent / f"{name}.py").resolve()
        for name in ("recipe_runtime", "query_tabular", "extract_documents")
        if (path.parent / f"{name}.py").is_file()
    }


def _module_file(module: Any) -> Path | None:
    raw = str(getattr(module, "__file__", "") or "")
    return Path(raw).resolve() if raw else None


def assert_executor_dependency_resolution(module: Any, path: Path) -> None:
    """Prove all helpers actually imported from this private execution layout."""

    path = path.resolve()
    if _module_file(module) != path:
        raise ReplayError("executor_dependency_resolution_failed")
    for name, expected in _expected_executor_helpers(path).items():
        loaded = sys.modules.get(name)
        if loaded is not None and _module_file(loaded) != expected:
            raise ReplayError("executor_dependency_resolution_failed")


def load_executor(path: Path, *, source_fingerprint: str | None = None):
    """Load a concrete portable executor; never silently use another version.

    ``run`` always provides a private snapshot fingerprint.  The optional
    fallback remains for narrow unit-level callers, but is intentionally not
    used for replay evidence because a mutable original path cannot prove what
    an import loader will subsequently execute.
    """

    path = path.expanduser().resolve()
    if not path.is_file():
        raise ReplayError("executor_unavailable")
    if source_fingerprint is not None and not is_sha256(source_fingerprint):
        raise ReplayError("executor_unavailable")
    module_fingerprint = source_fingerprint or sha256_file(path)
    module_name = f"recipe_replay_executor_{module_fingerprint[:16]}"
    try:
        expected_helpers = _expected_executor_helpers(path)
        # `execute_scenario.py` intentionally uses package-local absolute
        # imports for CLI portability.  A long-lived host may already have a
        # module of the same name cached from a different Skill, so purge only
        # cached modules that do not point at this exact executor directory.
        for name, expected in expected_helpers.items():
            loaded = sys.modules.get(name)
            loaded_file = _module_file(loaded) if loaded else None
            if loaded_file != expected:
                sys.modules.pop(name, None)
        while str(path.parent) in sys.path:
            sys.path.remove(str(path.parent))
        sys.path.insert(0, str(path.parent))
        spec = importlib.util.spec_from_file_location(module_name, path)
        if spec is None or spec.loader is None:
            raise ReplayError("executor_unavailable")
        module = importlib.util.module_from_spec(spec)
        sys.modules[module_name] = module
        spec.loader.exec_module(module)
    except ReplayError:
        raise
    except Exception as exc:  # external dependencies should not leak to report
        raise ReplayError("executor_unavailable") from exc
    if not callable(getattr(module, "execute", None)) or not callable(getattr(module, "recipe_runtime", None)):
        raise ReplayError("executor_unavailable")
    # Force the primary dependency import now, then prove it resolved to this
    # package rather than an earlier Skill with the same top-level name.
    try:
        module.recipe_runtime()
        assert_executor_dependency_resolution(module, path)
    except ReplayError:
        raise
    except Exception as exc:
        raise ReplayError("executor_dependency_resolution_failed") from exc
    return module


@contextmanager
def executor_import_scope(snapshot: ExecutorSnapshot):
    """Keep lazy imports pinned to a private executor snapshot for one run."""

    scripts_root = snapshot.executor_path.parent.resolve()
    helper_names = ("recipe_runtime", "query_tabular", "extract_documents")
    prior_modules = {name: sys.modules.get(name) for name in helper_names}
    prior_path = list(sys.path)
    module = None
    try:
        module = load_executor(snapshot.executor_path, source_fingerprint=snapshot.sha256)
        yield module
        # Table/document helpers can load lazily during executor.execute().
        assert_executor_dependency_resolution(module, snapshot.executor_path)
    finally:
        sys.path[:] = prior_path
        if module is not None:
            sys.modules.pop(str(getattr(module, "__name__", "")), None)
        for name, previous in prior_modules.items():
            loaded = sys.modules.get(name)
            if loaded is not None:
                loaded_file = _module_file(loaded)
                if loaded_file is not None and loaded_file.is_relative_to(scripts_root):
                    sys.modules.pop(name, None)
            if previous is not None:
                sys.modules[name] = previous


def validate_trace_review(
    review: dict[str, Any], review_fingerprint: str, replay_contract: dict[str, Any], cases: dict[str, Any],
) -> set[str]:
    if (
        review.get("schema_version") != 1
        or review.get("kind") != "trace_review"
        or review.get("status") != "approved"
    ):
        raise ReplayError("trace_review_not_approved")
    approval = review.get("approval") if isinstance(review.get("approval"), dict) else {}
    if str(approval.get("decision") or "").casefold() != "approved":
        raise ReplayError("trace_review_not_approved")
    if str(cases.get("trace_review_fingerprint") or "") != review_fingerprint:
        raise ReplayError("trace_review_fingerprint_mismatch")
    trace = review.get("trace") if isinstance(review.get("trace"), dict) else {}
    bundle_id = str(trace.get("bundle_id") or "")
    if not bundle_id:
        raise ReplayError("trace_bundle_mismatch")
    approved_trace = replay_contract.get("approved_trace") if isinstance(replay_contract.get("approved_trace"), dict) else {}
    approved_bundle_ids = {
        str(item) for item in approved_trace.get("bundle_ids", []) if str(item)
    }
    if approved_bundle_ids and bundle_id not in approved_bundle_ids:
        raise ReplayError("trace_bundle_mismatch")
    return {bundle_id}


def validate_replay_contract(
    contract: dict[str, Any], catalog_fingerprint: str,
) -> list[dict[str, Any]]:
    if (
        contract.get("schema_version") != SCHEMA_VERSION
        or contract.get("kind") != REPORT_KIND
        or contract.get("status") not in {"pending_real_replay", "blocked_invalid_recipe_catalog"}
        or str(contract.get("recipe_catalog_fingerprint") or "") != catalog_fingerprint
    ):
        raise ReplayError("invalid_replay_contract")
    cases = contract.get("required_cases")
    if not isinstance(cases, list) or not cases or len(cases) > MAX_CASES:
        raise ReplayError("invalid_replay_contract")
    result: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in cases:
        if not isinstance(item, dict):
            raise ReplayError("invalid_replay_contract")
        case_id = str(item.get("case_id") or "")
        recipe_id = str(item.get("recipe_id") or "")
        trace_bundle_id = str(item.get("trace_bundle_id") or "")
        if not case_id or not recipe_id or not trace_bundle_id or case_id in seen:
            raise ReplayError("invalid_replay_contract")
        seen.add(case_id)
        result.append(item)
    return result


def validate_cases_fixture(
    cases: dict[str, Any], catalog_fingerprint: str, contract_fingerprint: str,
) -> list[dict[str, Any]]:
    if (
        cases.get("schema_version") != SCHEMA_VERSION
        or cases.get("kind") != CASES_KIND
        or str(cases.get("recipe_catalog_fingerprint") or "") != catalog_fingerprint
        or str(cases.get("replay_contract_fingerprint") or "") != contract_fingerprint
    ):
        raise ReplayError("invalid_replay_cases")
    approval = cases.get("approval") if isinstance(cases.get("approval"), dict) else {}
    if (
        str(approval.get("kind") or "") != "approved_recipe_replay_oracle"
        or str(approval.get("decision") or "").casefold() != "approved"
    ):
        raise ReplayError("replay_oracle_not_approved")
    raw_cases = cases.get("cases")
    if not isinstance(raw_cases, list) or not raw_cases or len(raw_cases) > MAX_CASES:
        raise ReplayError("invalid_replay_cases")
    values: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in raw_cases:
        if not isinstance(item, dict):
            raise ReplayError("invalid_replay_cases")
        case_id = str(item.get("case_id") or "")
        request = item.get("request")
        if not case_id or case_id in seen or not isinstance(request, str):
            raise ReplayError("invalid_replay_cases")
        seen.add(case_id)
        if not request.strip() or len(request.encode("utf-8")) > MAX_REQUEST_BYTES:
            raise ReplayError("invalid_replay_cases")
        if str(item.get("request_digest") or "") != request_digest(request):
            raise ReplayError("request_digest_mismatch")
        expected = item.get("expected") if isinstance(item.get("expected"), dict) else {}
        if not is_sha256(expected.get("normalized_result_digest")):
            raise ReplayError("invalid_replay_oracle")
        count = expected.get("result_anchor_count")
        if isinstance(count, bool) or not isinstance(count, int) or count < 0:
            raise ReplayError("invalid_replay_oracle")
        approved_empty = expected.get("approved_empty_result")
        if not isinstance(approved_empty, bool) or approved_empty != (count == 0):
            raise ReplayError("invalid_replay_oracle")
        fixture_bindings(item.get("source_bindings"))
        fixture_source_fingerprints(item.get("source_fingerprints"))
        values.append(item)
    return values


def report_safe_case_fingerprint(case: dict[str, Any]) -> str:
    """Fingerprint private inputs while excluding the plaintext request from output."""

    bindings = fixture_bindings(case.get("source_bindings"))
    source_commitments = fixture_source_fingerprints(case.get("source_fingerprints"))
    safe = {
        "case_id": str(case.get("case_id") or ""),
        "recipe_id": str(case.get("recipe_id") or ""),
        "assertion_id": str(case.get("assertion_id") or ""),
        "trace_bundle_id": str(case.get("trace_bundle_id") or ""),
        "request_digest": str(case.get("request_digest") or ""),
        "expected": case.get("expected") if isinstance(case.get("expected"), dict) else {},
        "source_binding_ids": sorted(bindings),
        # Keep a commitment to private path mapping without serializing that
        # mapping (a filename can itself be sensitive business metadata).
        "source_bindings_fingerprint": sha256_bytes(
            canonical_json(normalized_value(bindings)).encode("utf-8")
        ),
        "source_fingerprints": source_commitments,
    }
    return sha256_bytes(canonical_json(normalized_value(safe)).encode("utf-8"))


def expected_summary(case: dict[str, Any]) -> dict[str, Any]:
    expected = case["expected"]
    return {
        "normalized_result_digest": str(expected["normalized_result_digest"]).casefold(),
        "result_anchor_count": int(expected["result_anchor_count"]),
        "approved_empty_result": bool(expected["approved_empty_result"]),
    }


def failure_entry(case: dict[str, Any], code: str) -> dict[str, Any]:
    """A result row that remains useful to a validator but cannot disclose bodies."""

    return {
        "case_id": str(case.get("case_id") or ""),
        "recipe_id": str(case.get("recipe_id") or ""),
        "assertion_id": str(case.get("assertion_id") or ""),
        "trace_bundle_id": str(case.get("trace_bundle_id") or ""),
        "case_fingerprint": report_safe_case_fingerprint(case),
        "request_digest": str(case.get("request_digest") or ""),
        "expected": expected_summary(case),
        "actual": {
            "normalized_result_digest": "",
            "result_anchor_count": None,
            "matched_group_count": None,
            "complete": False,
        },
        "comparison": {
            "status": "failed",
            "digest_match": False,
            "count_match": False,
            "nonempty_policy_match": False,
            "complete_match": False,
        },
        "execution": {"status": "not_run", "recipe_execution_status": "not_run"},
        "source_fingerprints": [],
        "failure_code": code,
    }


def replay_case(
    case: dict[str, Any], required: dict[str, Any], *, executor: Any, recipes: Any,
    catalog: list[dict[str, Any]], catalog_fingerprint: str, runtime_contract: dict[str, Any],
    flow_contract: dict[str, Any], data_root: Path, global_bindings: dict[str, str], max_rows: int,
) -> dict[str, Any]:
    """Execute exactly one private case and return a report-safe replay row."""

    if (
        str(case.get("case_id") or "") != str(required.get("case_id") or "")
        or str(case.get("recipe_id") or "") != str(required.get("recipe_id") or "")
        or str(case.get("assertion_id") or "") != str(required.get("assertion_id") or "")
        or str(case.get("trace_bundle_id") or "") != str(required.get("trace_bundle_id") or "")
    ):
        raise ReplayError("case_identity_mismatch")
    recipe_id = str(case["recipe_id"])
    recipe = next((item for item in catalog if str(item.get("id") or "") == recipe_id), None)
    if not isinstance(recipe, dict):
        raise ReplayError("recipe_missing_from_catalog")
    bindings, expected_source_fingerprints = case_source_commitments(
        case, recipe, runtime_contract, global_bindings,
    )
    verification = {
        "status": "verified",
        "verified": True,
        "certificate_validated": True,
        "catalog_fingerprint": catalog_fingerprint,
        "verification_path": "recipe-replay-runner:ephemeral-case-gate",
        "verified_recipe_ids": [recipe_id],
    }
    with tempfile.TemporaryDirectory(prefix="recipe-replay-input-") as raw_snapshot:
        snapshot_root = Path(raw_snapshot)
        fingerprints, snapshot_bindings = snapshot_runtime_sources(
            recipe, runtime_contract, data_root, bindings, expected_source_fingerprints, snapshot_root,
        )
        try:
            payload = executor.execute(
                str(case["request"]), snapshot_root, runtime_contract, flow_contract, snapshot_bindings,
                max_rows, True, compiled_recipes=catalog, recipe_verification=verification,
            )
        except Exception as exc:  # deliberately do not serialize arbitrary runtime errors
            raise ReplayError("executor_runtime_failed") from exc
    if not isinstance(payload, dict):
        raise ReplayError("executor_runtime_failed")
    execution = payload.get("recipe_execution") if isinstance(payload.get("recipe_execution"), dict) else {}
    deterministic = payload.get("deterministic_result") if isinstance(payload.get("deterministic_result"), dict) else None
    if (
        payload.get("status") != "completed_deterministically"
        or execution.get("status") != "verified_recipe"
        or execution.get("verified") is not True
        or deterministic is None
    ):
        raise ReplayError("deterministic_replay_not_completed")
    family = deterministic.get("rule_family") if isinstance(deterministic.get("rule_family"), dict) else {}
    actual_recipe_id = str(family.get("template_id") or deterministic.get("recipe_id") or "")
    if actual_recipe_id != recipe_id:
        raise ReplayError("recipe_identity_mismatch")
    summary = deterministic.get("summary") if isinstance(deterministic.get("summary"), dict) else {}
    coverage = deterministic.get("coverage") if isinstance(deterministic.get("coverage"), dict) else {}
    count = summary.get("matched_row_count")
    groups = summary.get("matched_group_count")
    if isinstance(count, bool) or not isinstance(count, int) or count < 0:
        raise ReplayError("invalid_actual_result")
    if isinstance(groups, bool) or not isinstance(groups, int) or groups < 0:
        raise ReplayError("invalid_actual_result")
    complete = (
        coverage.get("complete_for_all_matching_runtime_rows") is True
        and coverage.get("truncated") is not True
        and int(coverage.get("total_matched_row_count", -1)) == count
    )
    actual_digest = normalized_result_digest(deterministic)
    expected = expected_summary(case)
    digest_match = actual_digest == expected["normalized_result_digest"]
    count_match = count == expected["result_anchor_count"]
    nonempty_policy_match = expected["approved_empty_result"] == (count == 0)
    comparison_status = "passed" if digest_match and count_match and nonempty_policy_match and complete else "failed"
    return {
        "case_id": str(case["case_id"]),
        "recipe_id": recipe_id,
        "assertion_id": str(case.get("assertion_id") or ""),
        "trace_bundle_id": str(case["trace_bundle_id"]),
        "case_fingerprint": report_safe_case_fingerprint(case),
        "request_digest": str(case["request_digest"]),
        "expected": expected,
        "actual": {
            "normalized_result_digest": actual_digest,
            "result_anchor_count": count,
            "matched_group_count": groups,
            "complete": complete,
        },
        "comparison": {
            "status": comparison_status,
            "digest_match": digest_match,
            "count_match": count_match,
            "nonempty_policy_match": nonempty_policy_match,
            "complete_match": complete,
        },
        "execution": {
            "status": "completed_deterministically",
            "recipe_execution_status": "verified_recipe",
        },
        "source_fingerprints": fingerprints,
        "failure_code": "" if comparison_status == "passed" else "replay_oracle_mismatch",
    }


def atomic_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def base_report(
    *, catalog_fingerprint: str = "", contract_fingerprint: str = "", review_fingerprint: str = "",
    executor_fingerprint: str = "", executor_closure: str = "", fixture_fingerprint: str = "", runtime_contract_fingerprint: str = "",
    flow_contract_fingerprint: str = "", trace_bundle_ids: Iterable[str] = (),
) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "kind": REPORT_KIND,
        "status": "blocked",
        "recipe_catalog_fingerprint": catalog_fingerprint,
        "replay_contract_fingerprint": contract_fingerprint,
        "trace_review": {
            "fingerprint": review_fingerprint,
            "status": "",
            "trace_bundle_ids": sorted({str(item) for item in trace_bundle_ids if str(item)}),
        },
        "executor": {
            "sha256": executor_fingerprint,
            "closure_sha256": executor_closure,
            "interface": "execute_scenario.execute",
        },
        "runtime_contract": {"sha256": runtime_contract_fingerprint},
        "flow_contract": {"sha256": flow_contract_fingerprint},
        "fixtures": {"sha256": fixture_fingerprint, "case_count": 0},
        "replays": [],
        "verified_recipe_ids": [],
        "failures": [],
    }


def run(argv: Sequence[str] | None = None) -> dict[str, Any]:
    args = build_parser().parse_args(argv)
    catalog_path = Path(args.catalog).expanduser().resolve()
    contract_path = Path(args.replay_contract).expanduser().resolve()
    cases_path = Path(args.cases).expanduser().resolve()
    review_path = Path(args.trace_review).expanduser().resolve()
    runtime_contract_path = Path(args.runtime_contract).expanduser().resolve()
    flow_contract_path = Path(args.flow_contract).expanduser().resolve()
    executor_path = Path(args.executor).expanduser().resolve()
    output_path = Path(args.output).expanduser().resolve()
    report = base_report()
    try:
        require_isolated_replay_worker()
        data_root = Path(args.data_root).expanduser().resolve()
        if not data_root.is_dir():
            raise ReplayError("data_root_unavailable")
        max_rows = int(args.max_rows)
        if max_rows < 1 or max_rows > 200_000:
            raise ReplayError("invalid_max_rows")
        global_bindings = parse_global_bindings(args.bind)
        # Each control file is read once.  Everything below either uses this
        # in-memory value or the private snapshot made from exactly these
        # bytes; no report fingerprint is ever followed by an original-path
        # parse.
        catalog_raw, catalog_fingerprint, catalog_payload = read_json_object_bytes_with_digest(
            catalog_path, code="invalid_recipe_catalog",
        )
        contract_raw, contract_fingerprint, replay_contract = read_json_object_bytes_with_digest(
            contract_path, code="invalid_replay_contract",
        )
        cases_raw, cases_fingerprint, cases_payload = read_json_object_bytes_with_digest(
            cases_path, code="invalid_replay_cases",
        )
        review_raw, review_fingerprint, trace_review = read_json_object_bytes_with_digest(
            review_path, code="trace_review_not_approved",
        )
        runtime_raw, runtime_contract_fingerprint, runtime_contract = read_json_object_bytes_with_digest(
            runtime_contract_path, code="invalid_runtime_contract",
        )
        flow_raw, flow_contract_fingerprint, flow_contract = read_json_object_bytes_with_digest(
            flow_contract_path, code="invalid_flow_contract",
        )
        if catalog_payload.get("schema_version") != 1 or not isinstance(catalog_payload.get("recipes"), list):
            raise ReplayError("invalid_recipe_catalog")
        catalog = [item for item in catalog_payload["recipes"] if isinstance(item, dict)]
        if not catalog:
            raise ReplayError("no_compiled_recipes")
        required_cases = validate_replay_contract(replay_contract, catalog_fingerprint)
        fixture_cases = validate_cases_fixture(cases_payload, catalog_fingerprint, contract_fingerprint)
        trace_bundle_ids = validate_trace_review(trace_review, review_fingerprint, replay_contract, cases_payload)
        required_by_id = {str(item["case_id"]): item for item in required_cases}
        fixture_by_id = {str(item["case_id"]): item for item in fixture_cases}
        if set(required_by_id) != set(fixture_by_id):
            raise ReplayError("required_case_set_mismatch")
        validate_fixture_source_commitments(fixture_cases, catalog, runtime_contract, global_bindings)
        with tempfile.TemporaryDirectory(prefix="recipe-replay-control-") as raw_snapshot:
            snapshot_root = Path(raw_snapshot)
            control_paths = materialize_control_snapshots(
                snapshot_root,
                catalog=catalog_raw,
                replay_contract=contract_raw,
                cases=cases_raw,
                trace_review=review_raw,
                runtime_contract=runtime_raw,
                flow_contract=flow_raw,
            )
            executor_snapshot = materialize_executor_snapshot(executor_path, snapshot_root / "executor")
            seal_private_snapshot(snapshot_root)
            report = base_report(
                catalog_fingerprint=catalog_fingerprint,
                contract_fingerprint=contract_fingerprint,
                review_fingerprint=review_fingerprint,
                executor_fingerprint=executor_snapshot.sha256,
                executor_closure=executor_snapshot.closure_sha256,
                fixture_fingerprint=cases_fingerprint,
                runtime_contract_fingerprint=runtime_contract_fingerprint,
                flow_contract_fingerprint=flow_contract_fingerprint,
            )
            report["trace_review"] = {
                "fingerprint": review_fingerprint,
                "status": "approved",
                "trace_bundle_ids": sorted(trace_bundle_ids),
            }
            report["fixtures"] = {"sha256": cases_fingerprint, "case_count": len(fixture_cases)}
            replays: list[dict[str, Any]] = []
            with executor_import_scope(executor_snapshot) as executor:
                recipes = executor.recipe_runtime()
                # Validate the exact catalog snapshot using the evaluator that
                # will execute it; the original catalog path is never reused.
                loaded_catalog = recipes.load_recipes(control_paths["catalog"])
                if canonical_json(normalized_value(loaded_catalog)) != canonical_json(normalized_value(catalog)):
                    raise ReplayError("invalid_recipe_catalog")
                for case_id in sorted(required_by_id):
                    case = fixture_by_id[case_id]
                    required = required_by_id[case_id]
                    if str(case.get("trace_bundle_id") or "") not in trace_bundle_ids:
                        replay = failure_entry(case, "trace_bundle_mismatch")
                    else:
                        try:
                            replay = replay_case(
                                case, required, executor=executor, recipes=recipes, catalog=loaded_catalog,
                                catalog_fingerprint=catalog_fingerprint, runtime_contract=runtime_contract,
                                flow_contract=flow_contract, data_root=data_root, global_bindings=global_bindings,
                                max_rows=max_rows,
                            )
                        except ReplayError as exc:
                            replay = failure_entry(case, exc.code)
                    replays.append(replay)
        failed = [item for item in replays if item.get("comparison", {}).get("status") != "passed"]
        verified_recipe_ids = sorted({
            str(item.get("recipe_id") or "") for item in replays
            if item.get("comparison", {}).get("status") == "passed" and str(item.get("recipe_id") or "")
        })
        report.update({
            "status": "passed" if not failed else "failed",
            "replays": replays,
            "verified_recipe_ids": verified_recipe_ids if not failed else [],
            "failures": [
                {"case_id": str(item.get("case_id") or ""), "code": str(item.get("failure_code") or "replay_oracle_mismatch")}
                for item in failed
            ],
        })
    except ReplayError as exc:
        report["status"] = "blocked"
        report["verified_recipe_ids"] = []
        report["failures"] = [{"case_id": "", "code": exc.code}]
    atomic_json(output_path, report)
    return report


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--catalog", required=True, help="Exact compiled-recipes.json to replay")
    parser.add_argument("--replay-contract", required=True, help="Current recipe-replay-contract.json")
    parser.add_argument("--cases", required=True, help="Private approved recipe replay case fixture")
    parser.add_argument("--trace-review", required=True, help="Approved trace-review.json")
    parser.add_argument("--runtime-contract", required=True, help="Portable runtime operational-data-contract.json")
    parser.add_argument("--flow-contract", required=True, help="Portable flow contract used by the executor")
    parser.add_argument("--data-root", required=True, help="Root containing the private historical runtime files")
    parser.add_argument("--executor", default=str(DEFAULT_EXECUTOR), help="Exact execute_scenario.py to exercise")
    parser.add_argument(
        "--bind", action="append", default=[],
        help="Optional complete source-id=relative-path assertion; must exactly match the private fixture",
    )
    parser.add_argument("--max-rows", type=int, default=200, help="Bounded deterministic result row limit")
    parser.add_argument("--output", required=True, help="Safe recipe-replay-report.json destination")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    report = run(argv)
    # The output file is the audit artifact; stdout intentionally carries only
    # a compact status summary and cannot expose any private replay body.
    print(json.dumps({
        "status": report.get("status"),
        "verified_recipe_ids": report.get("verified_recipe_ids", []),
        "failure_count": len(report.get("failures", [])),
    }, ensure_ascii=False))
    return 0 if report.get("status") == "passed" else 2


if __name__ == "__main__":  # pragma: no cover - exercised through main in deployment
    raise SystemExit(main())
