#!/usr/bin/env python3
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PIN = re.compile(r"^([A-Za-z0-9_.-]+)==([^;\s]+)")


def normalized(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def read_pins(path: Path) -> dict[str, str]:
    pins = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        match = PIN.match(line)
        if match:
            pins[normalized(match.group(1))] = match.group(2)
    return pins


def compare(source: dict[str, str], candidate: dict[str, str], label: str) -> list[str]:
    problems = []
    for name, version in sorted(source.items()):
        other = candidate.get(name)
        if other is None:
            problems.append(f"{label}: {name} is missing")
        elif other != version:
            problems.append(f"{label}: {name} is {other}, expected {version}")
    return problems


def compare_shared(
    first: dict[str, str], second: dict[str, str], label: str
) -> list[str]:
    problems = []
    for name in sorted(first.keys() & second.keys()):
        if first[name] != second[name]:
            problems.append(
                f"{label}: {name} is {first[name]} and {second[name]}"
            )
    return problems


runtime = read_pins(ROOT / "requirements.txt")
test = read_pins(ROOT / "requirements-test.txt")
image = read_pins(ROOT / "docker" / "image-lock.txt")
problems = compare(runtime, test, "requirements-test.txt")
problems.extend(compare(runtime, image, "docker/image-lock.txt"))
problems.extend(compare_shared(test, image, "shared test/image pin"))

if problems:
    print("Runtime dependency pins have drifted:", file=sys.stderr)
    for problem in problems:
        print(f"  {problem}", file=sys.stderr)
    raise SystemExit(1)

shared = len(test.keys() & image.keys())
print(f"All {len(runtime)} runtime pins and {shared} shared image pins match.")
