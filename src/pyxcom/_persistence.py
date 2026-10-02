"""Shared record boundaries and durable replacement for saved artifacts."""

import json
import os
from contextlib import contextmanager
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import IO, Iterable, Iterator


def jsonl_lines(path: str | Path) -> Iterator[tuple[int, str]]:
    """Yield nonblank LF records; Unicode separators remain string content."""
    with Path(path).open(encoding="utf-8", newline="\n") as stream:
        for number, line in enumerate(stream, 1):
            if line.strip(" \t\r\n"):
                yield number, line


def read_jsonl(path: str | Path) -> Iterator[dict]:
    """Read object records strictly, reporting malformed physical record lines."""
    for number, line in jsonl_lines(path):
        try:
            record = json.loads(line)
        except json.JSONDecodeError as exc:
            raise json.JSONDecodeError(
                f"{Path(path).name}, record {number}: {exc.msg}", exc.doc, exc.pos
            ) from exc
        if not isinstance(record, dict):
            raise ValueError(f"{Path(path).name}, record {number}: expected an object")
        yield record


def append_jsonl(
    path: Path, records: Iterable[dict], *, ensure_ascii: bool = False
) -> None:
    """Append complete LF records, preserving a valid unterminated last record."""
    encoded = "".join(
        json.dumps(record, ensure_ascii=ensure_ascii) + "\n" for record in records
    ).encode("utf-8")
    with path.open("a+b") as stream:
        end = stream.tell()
        if encoded and end:
            stream.seek(end - 1)
            if stream.read(1) != b"\n":
                stream.write(b"\n")
        if encoded:
            stream.write(encoded)
        stream.flush()
        os.fsync(stream.fileno())


@contextmanager
def atomic_text(
    path: Path, *, encoding: str = "utf-8", newline: str | None = None
) -> Iterator[IO[str]]:
    """Publish a text artifact after writing; clean up after every failure stage."""
    temporary = None
    try:
        with NamedTemporaryFile(
            "w", encoding=encoding, newline=newline, dir=path.parent, delete=False
        ) as stream:
            temporary = Path(stream.name)
            yield stream.file
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    except BaseException:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
        raise


def atomic_json(path: Path, value: dict) -> None:
    """Replace a JSON file only after a complete write and durable flush."""
    with atomic_text(path) as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2)
