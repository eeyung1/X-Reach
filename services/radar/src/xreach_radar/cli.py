"""Command-line interface for passive inspection and capture scouting."""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from dataclasses import asdict
from datetime import datetime
from pathlib import Path

from .cdp_client import ReaClient, ReaCommandError
from .seed_scout import ScoutConfig, SeedScout
from .simclusters import SimClusterIndex, SimClusterScout, semantic_id


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="xreach-radar")
    subcommands = parser.add_subparsers(dest="command", required=True)

    inspect = subcommands.add_parser("inspect", help="run REA inspect-web-page")
    inspect.add_argument("endpoint")
    inspect.add_argument("target_id")
    inspect.add_argument("--observation-ms", type=int, default=1_000)
    inspect.add_argument("--allowed-origin", action="append", default=[])
    inspect.add_argument("--rea-command", default="rea")

    scout = subcommands.add_parser("scout", help="rank roots in a saved capture")
    scout.add_argument("capture", type=Path)
    scout.add_argument("--max-age-minutes", type=float, default=30.0)
    scout.add_argument("--min-views-per-minute", type=float, default=500.0)
    scout.add_argument("--max-replies", type=int, default=50)
    scout.add_argument("--cluster", action="append", default=[])
    scout.add_argument(
        "--semantic-id",
        help="six comma-separated SID codes; returns only aligned viral roots",
    )
    scout.add_argument(
        "--simcluster-catalog",
        type=Path,
        help="optional JSON catalog mapping active cluster names to SID prefixes",
    )
    scout.add_argument("--min-prefix-levels", type=int, default=2)
    scout.add_argument(
        "--now",
        type=datetime.fromisoformat,
        help="timezone-aware ISO timestamp used for reproducible scouting",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    arguments = build_parser().parse_args(argv)
    try:
        if arguments.command == "inspect":
            result = ReaClient((arguments.rea_command,)).inspect_web_page(
                arguments.endpoint,
                arguments.target_id,
                observation_ms=arguments.observation_ms,
                allowed_origins=arguments.allowed_origin,
            )
            _print_json(result)
            return 0

        capture = json.loads(arguments.capture.read_text(encoding="utf-8"))
        config = ScoutConfig(
            max_age_minutes=arguments.max_age_minutes,
            min_views_per_minute=arguments.min_views_per_minute,
            max_replies=arguments.max_replies,
            target_simclusters=frozenset(arguments.cluster),
        )
        if arguments.semantic_id:
            codes = semantic_id(
                tuple(int(part.strip()) for part in arguments.semantic_id.split(","))
            )
            catalog = (
                SimClusterIndex.from_payload(
                    json.loads(arguments.simcluster_catalog.read_text(encoding="utf-8"))
                )
                if arguments.simcluster_catalog
                else SimClusterIndex()
            )
            seeds = SimClusterScout(
                catalog,
                config,
                min_prefix_levels=arguments.min_prefix_levels,
            ).query(capture, codes, now=arguments.now)
        else:
            seeds = SeedScout(config).scan(capture, now=arguments.now)
        _print_json([asdict(seed) for seed in seeds])
        return 0
    except (
        OSError,
        TypeError,
        ValueError,
        json.JSONDecodeError,
        ReaCommandError,
    ) as error:
        print(str(error), file=sys.stderr)
        return 2


def _print_json(value: object) -> None:
    print(json.dumps(value, indent=2, default=_json_default, sort_keys=True))


def _json_default(value: object) -> str:
    if isinstance(value, datetime):
        return value.isoformat()
    raise TypeError(f"cannot serialize {type(value).__name__}")
