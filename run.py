#!/usr/bin/env python3
"""Local entry point for project-specific experiment helpers."""

from __future__ import annotations

import argparse

from scripts.checkout_bug_versions import add_checkout_bug_parser
from scripts.major_experiment import add_major_build_parser, add_major_run_parser


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Entry point for local Multi-SWE-bench experiment scripts."
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    add_checkout_bug_parser(subparsers)
    add_major_build_parser(subparsers)
    add_major_run_parser(subparsers)
    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
