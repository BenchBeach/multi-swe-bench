from __future__ import annotations

import argparse
import glob
import json
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path


DEFAULT_DATASET_FILES = ["data/datasets/Multi-SWE-bench/java/*.jsonl"]
DEFAULT_REPO_DIR = Path("data/repos")
DEFAULT_OUTPUT_DIR = Path("data/checkout_bug")


@dataclass(frozen=True)
class Instance:
    org: str
    repo: str
    number: int
    base_sha: str

    @property
    def instance_id(self) -> str:
        return f"{self.org}__{self.repo}-{self.number}"

    @property
    def pr_id(self) -> str:
        return f"{self.org}/{self.repo}:pr-{self.number}"

    @property
    def repo_full_name(self) -> str:
        return f"{self.org}/{self.repo}"

    @property
    def github_url(self) -> str:
        return f"https://github.com/{self.org}/{self.repo}.git"


def run_cmd(cmd: list[str], cwd: Path | None = None) -> str:
    result = subprocess.run(
        cmd,
        cwd=cwd,
        check=True,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    return result.stdout


def expand_dataset_files(patterns: list[str]) -> list[Path]:
    files: list[Path] = []
    for pattern in patterns:
        matches = sorted(glob.glob(pattern))
        if not matches:
            raise FileNotFoundError(f"No dataset files matched: {pattern}")
        files.extend(Path(match) for match in matches)
    return files


def load_instances(dataset_files: list[Path], specifics: set[str] | None) -> list[Instance]:
    instances: list[Instance] = []
    seen: set[str] = set()
    for dataset_file in dataset_files:
        with dataset_file.open("r", encoding="utf-8") as handle:
            for line in handle:
                if not line.strip():
                    continue
                data = json.loads(line)
                instance = Instance(
                    org=data["org"],
                    repo=data["repo"],
                    number=int(data["number"]),
                    base_sha=data["base"]["sha"],
                )
                if specifics and not matches_specific(instance, specifics):
                    continue
                if instance.instance_id in seen:
                    continue
                seen.add(instance.instance_id)
                instances.append(instance)
    return instances


def matches_specific(instance: Instance, specifics: set[str]) -> bool:
    candidates = {
        instance.instance_id,
        instance.pr_id,
        f"{instance.org}/{instance.repo}-{instance.number}",
        str(instance.number),
    }
    return any(
        specific in candidate or candidate in specific
        for specific in specifics
        for candidate in candidates
    )


def ensure_repo_cache(instance: Instance, repo_dir: Path) -> Path:
    cached_repo = repo_dir / instance.org / instance.repo
    if (cached_repo / ".git").exists():
        return cached_repo

    cached_repo.parent.mkdir(parents=True, exist_ok=True)
    print(f"[clone-cache] {instance.repo_full_name} -> {cached_repo}")
    run_cmd(["git", "clone", instance.github_url, str(cached_repo)])
    return cached_repo


def checkout_bug_version(
    instance: Instance,
    repo_dir: Path,
    output_dir: Path,
    force: bool,
) -> Path:
    source_repo = ensure_repo_cache(instance, repo_dir)
    destination = output_dir / instance.instance_id

    if destination.exists():
        if not force:
            print(f"[skip] {instance.instance_id}: {destination} already exists")
            return destination
        print(f"[remove] {destination}")
        shutil.rmtree(destination)

    destination.parent.mkdir(parents=True, exist_ok=True)
    print(f"[checkout] {instance.instance_id} @ {instance.base_sha}")
    run_cmd(["git", "clone", str(source_repo), str(destination)])
    run_cmd(["git", "checkout", "-B", f"bug/{instance.instance_id}", instance.base_sha], cwd=destination)
    run_cmd(["git", "reset", "--hard", instance.base_sha], cwd=destination)
    run_cmd(["git", "clean", "-fdx"], cwd=destination)
    return destination


def checkout_bug_main(args: argparse.Namespace) -> None:
    dataset_files = expand_dataset_files(args.dataset_files)
    specifics = set(args.specifics or [])
    instances = load_instances(dataset_files, specifics or None)
    if not instances:
        raise ValueError("No instances selected.")

    print(f"Selected {len(instances)} instance(s).")
    for instance in instances:
        checkout_bug_version(
            instance=instance,
            repo_dir=args.repo_dir,
            output_dir=args.output_dir,
            force=args.force,
        )

    print(f"Done. Bug-version checkouts are under: {args.output_dir}")


def add_checkout_bug_parser(subparsers: argparse._SubParsersAction) -> None:
    parser = subparsers.add_parser(
        "checkout-bug",
        help="Checkout bug-version source trees for selected dataset instances.",
    )
    parser.add_argument(
        "--dataset-files",
        nargs="+",
        default=DEFAULT_DATASET_FILES,
        help="Dataset JSONL files or glob patterns.",
    )
    parser.add_argument(
        "--repo-dir",
        type=Path,
        default=DEFAULT_REPO_DIR,
        help="Local repository cache directory.",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=DEFAULT_OUTPUT_DIR,
        help="Directory where bug-version source trees will be created.",
    )
    parser.add_argument(
        "--specifics",
        nargs="*",
        default=[],
        help="Optional instance filters, e.g. google__gson-1093 or google/gson:pr-1093.",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Delete and recreate an existing checkout directory.",
    )
    parser.set_defaults(func=checkout_bug_main)
