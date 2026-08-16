"""Run an operator-provided M7 offline benchmark suite.

Usage:
    uv run python scripts/energy_benchmark.py package.module:build_suite
    uv run python scripts/energy_benchmark.py package.module:build_suite --output report.json

The factory must return ``EnergyBenchmarkSuite``.  Keeping case construction in
an operator-local module lets prepared benchmarks use ScriptedLLM fixtures or
real configured workers without putting credentials or challenge material in
this repository.
"""

from __future__ import annotations

import argparse
import asyncio
import importlib
import sys
from pathlib import Path
from typing import Callable, Sequence, TextIO

from dswarm.swarm.energy_benchmark import (
    EnergyBenchmarkSuite,
    benchmark_result_json,
    run_energy_benchmark,
)


def _factory_from_spec(spec: str) -> Callable[[], EnergyBenchmarkSuite]:
    module_name, separator, attribute = spec.partition(":")
    if not separator or not module_name or not attribute:
        raise ValueError("suite must use module:factory syntax")
    module = importlib.import_module(module_name)
    factory = getattr(module, attribute)
    if not callable(factory):
        raise TypeError(f"benchmark suite factory is not callable: {spec}")
    return factory


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run an independent M7 offline scheduling reorder estimate",
    )
    parser.add_argument("suite", help="operator suite factory as module:factory")
    parser.add_argument(
        "--output", type=Path, default=None,
        help="write UTF-8 JSON to this file instead of stdout",
    )
    return parser


def main(argv: Sequence[str] | None = None, *, stdout: TextIO | None = None) -> int:
    args = _parser().parse_args(argv)
    output_stream = stdout if stdout is not None else sys.stdout
    suite = _factory_from_spec(args.suite)()
    if not isinstance(suite, EnergyBenchmarkSuite):
        raise TypeError("benchmark suite factory must return EnergyBenchmarkSuite")
    result = asyncio.run(run_energy_benchmark(
        suite.cases,
        config=suite.config,
        top_k=suite.top_k,
    ))
    payload = benchmark_result_json(result) + "\n"
    if args.output is None:
        output_stream.write(payload)
    else:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(payload, encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
