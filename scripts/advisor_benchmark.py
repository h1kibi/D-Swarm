"""Run an operator-local M8 Advisor benchmark suite.

Examples:
    uv run python scripts/advisor_benchmark.py local_benchmarks.m8_suite:build_suite
    uv run python scripts/advisor_benchmark.py local_benchmarks.m8_suite:build_suite --output eval_runs/m8-advisor/report.json

M8 v1 guards Python-level ``sys.stdout``/``sys.stderr`` writes only.  Operator
suite modules and planner adapters are trusted local code and must also avoid
native ``os.write`` and child-process output.
"""

from __future__ import annotations

import argparse
import asyncio
from contextlib import redirect_stderr, redirect_stdout
import importlib
import os
from pathlib import Path
import sys
import tempfile
from typing import Sequence

_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from dswarm.swarm.advisor_benchmark import (  # noqa: E402
    AdvisorBenchmarkSuite,
    benchmark_result_json,
    run_advisor_benchmark,
)


class _DiscardGuard:
    """Discard text without retaining it; remember only whether output occurred."""

    def __init__(self) -> None:
        self.wrote_any = False

    def write(self, value: object) -> int:
        text = str(value)
        if text:
            self.wrote_any = True
        return len(text)

    def flush(self) -> None:
        return None

    def isatty(self) -> bool:
        return False


class _CliFailure(RuntimeError):
    def __init__(self, code: str) -> None:
        self.code = str(code)
        super().__init__(self.code)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="advisor_benchmark.py",
        description="Run an offline M8 Advisor benchmark suite.",
    )
    parser.add_argument("suite", help="operator suite factory as module:factory")
    parser.add_argument(
        "--output",
        help="atomically write UTF-8 JSON to this file instead of stdout",
    )
    return parser


def _load_suite(spec: str) -> AdvisorBenchmarkSuite:
    if spec.count(":") != 1:
        raise _CliFailure("invalid_suite_spec")
    module_name, factory_name = spec.split(":", 1)
    if not module_name.strip() or not factory_name.strip():
        raise _CliFailure("invalid_suite_spec")

    guard = _DiscardGuard()
    imported = None
    factory = None
    suite = None
    failure = ""
    with redirect_stdout(guard), redirect_stderr(guard):
        try:
            imported = importlib.import_module(module_name)
        except (KeyboardInterrupt, SystemExit):
            raise
        except BaseException:
            failure = "suite_import_failed"
        if not failure:
            try:
                factory = getattr(imported, factory_name)
            except AttributeError:
                failure = "suite_factory_missing"
        if not failure and not callable(factory):
            failure = "suite_factory_not_callable"
        if not failure:
            try:
                suite = factory()
            except (KeyboardInterrupt, SystemExit):
                raise
            except BaseException:
                failure = "suite_factory_failed"
    if guard.wrote_any:
        raise _CliFailure("suite_factory_wrote_output")
    if failure:
        raise _CliFailure(failure)
    if not isinstance(suite, AdvisorBenchmarkSuite):
        raise _CliFailure("invalid_suite_type")
    return suite


def _strict_descendant(candidate: Path, root: Path) -> bool:
    return candidate != root and candidate.is_relative_to(root)


def _validated_output_path(value: str) -> Path:
    try:
        target = Path(value).expanduser().resolve(strict=False)
    except (OSError, RuntimeError, TypeError, ValueError):
        raise _CliFailure("output_path_not_allowed") from None
    allowed = (
        (_REPO_ROOT / "eval_runs").resolve(strict=False),
        (_REPO_ROOT / "sessions").resolve(strict=False),
    )
    if not any(_strict_descendant(target, root) for root in allowed):
        raise _CliFailure("output_path_not_allowed")
    return target


def _fsync_parent_best_effort(parent: Path) -> None:
    flags = getattr(os, "O_RDONLY", 0)
    directory_flag = getattr(os, "O_DIRECTORY", 0)
    try:
        fd = os.open(parent, flags | directory_flag)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


def _atomic_write(target: Path, data: bytes) -> None:
    temporary: Path | None = None
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        fd, raw_path = tempfile.mkstemp(
            dir=target.parent,
            prefix=f".{target.name}.",
            suffix=".tmp",
        )
        temporary = Path(raw_path)
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, target)
        temporary = None
        _fsync_parent_best_effort(target.parent)
    except BaseException:
        if temporary is not None:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass
        raise


def _run_guarded(suite: AdvisorBenchmarkSuite):
    guard = _DiscardGuard()
    result = None
    failure: BaseException | None = None
    with redirect_stdout(guard), redirect_stderr(guard):
        try:
            result = asyncio.run(run_advisor_benchmark(suite))
        except (KeyboardInterrupt, SystemExit) as exc:
            failure = exc
        except BaseException as exc:
            failure = exc
    if guard.wrote_any:
        raise _CliFailure("benchmark_wrote_output")
    if failure is not None:
        if isinstance(failure, (KeyboardInterrupt, SystemExit)):
            raise failure
        raise _CliFailure("benchmark_failed")
    return result


def _emit_error(code: str) -> int:
    sys.stderr.write(str(code) + "\n")
    return 2


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        suite = _load_suite(args.suite)
        target = _validated_output_path(args.output) if args.output else None
        result = _run_guarded(suite)
        try:
            encoded = benchmark_result_json(result) + "\n"
        except BaseException:
            raise _CliFailure("result_serialization_failed") from None
        if target is None:
            sys.stdout.write(encoded)
        else:
            try:
                _atomic_write(target, encoded.encode("utf-8"))
            except BaseException:
                raise _CliFailure("output_write_failed") from None
        return 0
    except _CliFailure as exc:
        return _emit_error(exc.code)


if __name__ == "__main__":
    raise SystemExit(main())
