"""Mutation fuzzing (NFR Robustness; Requirements 10.3–10.5): malformed files from good ones.

A case is a seed file plus one mutation: byte flips, a field overwritten with a boundary value,
truncation, a block duplicated or cut, or zip-aware damage (a member's packed bytes, a header field,
a member's content rewritten inside an otherwise valid zip). Every case is drawn from
random.Random(f"{SEED}:{suite}:{family}:{index}"), so a report names exactly what reproduces it, and
the failing input itself is saved under build/fuzz/failures/.

A case passes when the code under test returns or raises one of the exceptions its module
documents. Anything else (another exception, or no answer within the time limit) is a failure.

Settings (environment):
  SOVANDER_FUZZ_ITERATIONS         cases per format family (default 200; the nightly job: 10000)
  SOVANDER_FUZZ_ENGINE_ITERATIONS  the same for the engine suite (default 3, or ITERATIONS when set)
  SOVANDER_FUZZ_BUDGET_S           stop a family after this many seconds (default: no limit)
  SOVANDER_FUZZ_SEED               base seed (default 20261017)
"""

from __future__ import annotations

import contextlib
import hashlib
import io
import json
import os
import random
import signal
import struct
import time
import traceback
import warnings
import zipfile
from collections import Counter
from collections.abc import Callable, Iterator
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
FIXTURES = ROOT / "tests" / "fixtures"
OUT = ROOT / "build" / "fuzz"
SEED = int(os.environ.get("SOVANDER_FUZZ_SEED", "20261017"))
MAX_SAVED = 20  # failing inputs kept per family

_BOUNDARY = (0, 1, 0x7F, 0x80, 0xFF, 0x7FFF, 0x8000, 0xFFFF, 0x7FFFFFFF, 0x80000000, 0xFFFFFFFF)


def iterations(engine: bool = False) -> int:
    common = os.environ.get("SOVANDER_FUZZ_ITERATIONS")
    if engine:
        return int(os.environ.get("SOVANDER_FUZZ_ENGINE_ITERATIONS") or common or 3)
    return int(common or 200)


def budget() -> float | None:
    value = float(os.environ.get("SOVANDER_FUZZ_BUDGET_S") or 0)
    return value or None


# Mutations: each takes the seed's bytes and a Random, returns the damaged bytes


def flip(data: bytes, rng: random.Random) -> bytes:
    buf = bytearray(data)
    for _ in range(rng.randint(1, min(64, len(buf) // 512 + 2))):
        i = rng.randrange(len(buf))
        buf[i] = buf[i] ^ (1 << rng.randrange(8)) if rng.random() < 0.5 else rng.randrange(256)
    return bytes(buf)


def boundary(data: bytes, rng: random.Random) -> bytes:
    """A 1, 2 or 4-byte field (a length, a count, an offset) set to a value at the edge of its range."""
    width = rng.choice((1, 2, 4))
    if len(data) < width:
        return flip(data, rng)
    at = rng.randrange(len(data) - width + 1)
    value = rng.choice(_BOUNDARY + (len(data), len(data) - at)) & ((1 << 8 * width) - 1)
    return data[:at] + value.to_bytes(width, rng.choice(("little", "big"))) + data[at + width:]


def truncate(data: bytes, rng: random.Random) -> bytes:
    return data[: rng.randrange(len(data))]


def splice(data: bytes, rng: random.Random) -> bytes:
    """Cut a block out, or copy one somewhere else: everything after it moves."""
    a = rng.randrange(len(data))
    b = min(len(data), a + rng.randint(1, 4096))
    if rng.random() < 0.5:
        return data[:a] + data[b:]
    at = rng.randrange(len(data) + 1)
    return data[:at] + data[a:b] + data[at:]


def _members(data: bytes) -> list[zipfile.ZipInfo]:
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            return [i for i in z.infolist() if i.compress_size]
    except (zipfile.BadZipFile, OSError, ValueError):
        return []


def zip_packed(data: bytes, rng: random.Random) -> bytes:
    """Damage inside one member's packed bytes; the index still describes the member as it was."""
    members = _members(data)
    if not members:
        return flip(data, rng)
    info = rng.choice(members)
    if info.header_offset + 30 > len(data):
        return flip(data, rng)
    name_len, extra_len = struct.unpack_from("<2H", data, info.header_offset + 26)
    start = info.header_offset + 30 + name_len + extra_len
    end = min(len(data), start + info.compress_size)
    if end <= start:
        return flip(data, rng)
    member = flip(data[start:end], rng) if rng.random() < 0.7 else truncate(data[start:end], rng)
    member = member.ljust(end - start, b"\0")
    return data[:start] + member + data[end:]


# (offset, width) of the fields worth lying about, in the central record and the local header
_CENTRAL_FIELDS = ((8, 2), (10, 2), (16, 4), (20, 4), (24, 4), (28, 2), (30, 2), (32, 2), (42, 4))
_LOCAL_FIELDS = ((6, 2), (8, 2), (14, 4), (18, 4), (22, 4), (26, 2), (28, 2))


def zip_field(data: bytes, rng: random.Random) -> bytes:
    """A size, method, flag, offset or name length in the index or a local header set to a boundary value."""
    end = data.rfind(b"PK\x05\x06")
    if end < 0 or end + 22 > len(data):
        return boundary(data, rng)
    count, size, offset = struct.unpack_from("<HII", data, end + 10)
    records, p = [], offset
    while len(records) < count and p + 46 <= len(data) and data[p:p + 4] == b"PK\x01\x02":
        records.append(p)
        lengths = struct.unpack_from("<3H", data, p + 28)
        p += 46 + sum(lengths)
    if not records:
        return boundary(data, rng)
    record = rng.choice(records)
    if rng.random() < 0.6:
        at, width = rng.choice(_CENTRAL_FIELDS)
        at += record
    else:
        local = struct.unpack_from("<I", data, record + 42)[0]
        if local + 30 > len(data):
            return boundary(data, rng)
        at, width = rng.choice(_LOCAL_FIELDS)
        at += local
    value = rng.choice(_BOUNDARY + (len(data),)) & ((1 << 8 * width) - 1)
    return data[:at] + value.to_bytes(width, "little") + data[at + width:]


def zip_member(data: bytes, rng: random.Random) -> bytes:
    """A valid zip whose one member is damaged: the zip layer passes, the XML or stream parser does not."""
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            items = [(i, z.read(i)) for i in z.infolist()]
    except (zipfile.BadZipFile, OSError, ValueError, RuntimeError, NotImplementedError, EOFError):
        return flip(data, rng)  # encrypted or unusual seeds: plain damage instead
    victims = [n for n, (_, content) in enumerate(items) if content]
    if not victims:
        return flip(data, rng)
    target = rng.choice(victims)
    damage = rng.choice((flip, boundary, truncate, splice))
    out = io.BytesIO()
    with warnings.catch_warnings(), zipfile.ZipFile(out, "w") as z:
        warnings.simplefilter("ignore")  # seeds with repeated names (odd-names.zip) are meant to be odd
        for n, (info, content) in enumerate(items):
            z.writestr(info, damage(content, rng) if n == target else content)
    return out.getvalue()


BYTE_MUTATIONS: tuple[Callable[[bytes, random.Random], bytes], ...] = (flip, boundary, truncate, splice)
ZIP_MUTATIONS = BYTE_MUTATIONS + (zip_packed, zip_field, zip_member)


def mutate(data: bytes, rng: random.Random) -> tuple[str, bytes]:
    pool = ZIP_MUTATIONS if data.startswith(b"PK\x03\x04") else BYTE_MUTATIONS
    mutation = rng.choice(pool)
    return mutation.__name__, mutation(data, rng)


# Running a campaign


class Hang(Exception):
    """The code under test did not answer within the case's time limit."""


@contextlib.contextmanager
def deadline(seconds: float) -> Iterator[None]:
    """Raise Hang in the main thread after `seconds` (SIGALRM; pytest runs tests in the main thread)."""

    def expire(_signum, _frame):
        raise Hang(f"no answer within {seconds:g} s")

    previous = signal.signal(signal.SIGALRM, expire)
    signal.setitimer(signal.ITIMER_REAL, seconds)
    try:
        yield
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous)


def where(exc: BaseException) -> str:
    """file:line of the deepest frame in sovander/ (the bug's place), else the deepest frame."""
    frames = traceback.extract_tb(exc.__traceback__)
    ours = [f for f in frames if "/sovander/" in f.filename.replace("\\", "/")]
    frame = (ours or frames or [None])[-1]
    if frame is None:
        return "?"
    path = Path(frame.filename)
    with contextlib.suppress(ValueError):
        path = path.relative_to(ROOT)
    return f"{path}:{frame.lineno}"


def run_campaign(suite: str, family: str, seeds: list[Path], target: Callable[[bytes, str], str | None],
                 allowed: tuple[type[BaseException], ...], timeout: float, cases: int) -> dict:
    """Run `cases` mutated inputs through `target(data, name)`; the report is returned and written."""
    blobs = [(path.name, path.read_bytes()) for path in seeds]
    outcomes: Counter = Counter()
    failures: list[dict] = []
    limit = budget()
    start = time.monotonic()
    done = 0
    for index in range(cases):
        if limit and time.monotonic() - start > limit:
            break
        rng = random.Random(f"{SEED}:{suite}:{family}:{index}")
        name, data = blobs[index % len(blobs)]
        mutation, damaged = mutate(data, rng)
        try:
            with deadline(timeout):
                outcome = target(damaged, name) or "ok"
        except allowed as exc:
            outcome = type(exc).__name__
        except Exception as exc:  # noqa: BLE001 - every undocumented exception is the finding
            outcome = "FAIL"
            failures.append(_failure(suite, family, index, name, mutation, damaged, exc, len(failures)))
        outcomes[outcome] += 1
        done += 1
    report = {
        "suite": suite, "family": family, "seed": SEED, "cases": done, "asked": cases,
        "seconds": round(time.monotonic() - start, 1), "seeds": [n for n, _ in blobs],
        "outcomes": dict(outcomes.most_common()), "failures": failures,
    }
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / f"{suite}-{family}.json").write_text(json.dumps(report, indent=1) + "\n", encoding="utf-8")
    return report


def _failure(suite: str, family: str, index: int, seed: str, mutation: str, data: bytes,
             exc: BaseException, saved: int) -> dict:
    entry = {"index": index, "seed": seed, "mutation": mutation, "where": where(exc),
             "error": f"{type(exc).__name__}: {exc}"[:300], "sha256": hashlib.sha256(data).hexdigest()}
    if saved < MAX_SAVED:
        sample = OUT / "failures" / f"{suite}-{family}-{index}-{mutation}{Path(seed).suffix}"
        sample.parent.mkdir(parents=True, exist_ok=True)
        sample.write_bytes(data)
        entry["sample"] = str(sample.relative_to(ROOT))
    return entry


def describe(report: dict) -> str:
    """One line per distinct bug (error type and place), for the assertion message."""
    seen: dict[tuple[str, str], dict] = {}
    for f in report["failures"]:
        seen.setdefault((f["error"].split(":")[0], f["where"]), f)
    head = f"{report['suite']}/{report['family']}"
    lines = [f"{head}: {len(report['failures'])} of {report['cases']} cases failed"]
    lines += [f"  {kind} at {place} (case {f['index']}, {f['mutation']} of {f['seed']}): {f['error']}"
              for (kind, place), f in seen.items()]
    return "\n".join(lines)
