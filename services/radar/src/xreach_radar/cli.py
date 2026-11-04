"""Command-line interface for passive inspection and capture scouting."""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
from collections.abc import Sequence
from dataclasses import asdict
from datetime import datetime
from pathlib import Path

from .archive_ingest import ingest_archive
from .cdp_client import ReaClient, ReaCommandError
from .daemon import daemon_from_environment, run_daemon
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

    archive = subcommands.add_parser(
        "ingest-archive",
        help="summarize observed impressions from an X tweets.js or CSV export",
    )
    archive.add_argument("archive", type=Path)

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

    daemon = subcommands.add_parser(
        "daemon", help="continuously ingest captures into the API radar stream"
    )
    daemon.add_argument("--capture", type=Path, action="append", default=[])
    daemon.add_argument("--rea-scenario", type=Path)
    daemon.add_argument("--rea-command", default="rea")
    daemon.add_argument("--api-url")
    daemon.add_argument("--api-token")
    daemon.add_argument("--poll-seconds", type=float)
    daemon.add_argument("--once", action="store_true")
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

        if arguments.command == "ingest-archive":
            _print_json(asdict(ingest_archive(arguments.archive)))
            return 0

        if arguments.command == "daemon":
            logging.basicConfig(
                level=os.environ.get("XREACH_LOG_LEVEL", "INFO").upper()
            )
            daemon = daemon_from_environment(
                capture_paths=arguments.capture,
                scenario_path=arguments.rea_scenario,
                rea_command=(arguments.rea_command,),
                api_url=arguments.api_url,
                api_token=arguments.api_token,
                poll_seconds=arguments.poll_seconds,
            )
            cycle = asyncio.run(run_daemon(daemon, once=arguments.once))
            if cycle is not None:
                _print_json(asdict(cycle))
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
        RuntimeError,
    ) as error:
        print(str(error), file=sys.stderr)
        return 2


def _print_json(value: object) -> None:
    print(json.dumps(value, indent=2, default=_json_default, sort_keys=True))


def _json_default(value: object) -> str:
    if isinstance(value, datetime):
        return value.isoformat()
    raise TypeError(f"cannot serialize {type(value).__name__}")
