"""Stand-ins for the AWS services h2o talks to. No test touches a real one.

A fake that is more permissive than the service it replaces is worse than no
fake at all: it makes a suite pass on code that fails in production. So each of
these reproduces the specific refusal the real service makes, and the tests that
matter are the ones asserting the refusal happens.
"""

from __future__ import annotations

import copy
import hashlib
import re
from pathlib import Path
from typing import Any

from botocore.exceptions import ClientError

REPO_ROOT = Path(__file__).resolve().parents[3]
VOCAB_DIR = REPO_ROOT / "vocab"
DOCS_DIR = REPO_ROOT / "data" / "docs"


def load_vocabulary() -> dict[str, bytes]:
    """The real seed vocabulary. Tests assert against what ships, not a fixture."""
    return {p.name: p.read_bytes() for p in sorted(VOCAB_DIR.glob("*.ttl"))}


def _client_error(code: str, status: int, operation: str) -> ClientError:
    """A real botocore ClientError, so production's `except ClientError` runs.

    Raising a bare Exception here would let code that never catches ClientError
    pass the test and then fail against S3.
    """
    return ClientError(
        {"Error": {"Code": code, "Message": code}, "ResponseMetadata": {"HTTPStatusCode": status}},
        operation,
    )


class FakeS3:
    """S3 with the conditional-write semantics h2o's concurrency control needs.

    `If-Match` and `If-None-Match` are honoured and refused with a real 412,
    because ADR-007 makes the conditional PUT the only thing standing between two
    curators and a lost update. A fake that accepted every write would make the
    lost-update test pass while proving nothing.
    """

    def __init__(self, objects: dict[str, bytes] | None = None) -> None:
        self.objects: dict[str, bytes] = dict(objects or {})
        self.etags: dict[str, str] = {k: self._etag(v) for k, v in self.objects.items()}
        #: Every PutObject attempt, including refused ones. A test asserting a
        #: write reached S3 and was rejected *there* needs to see the attempt.
        self.put_calls: list[dict[str, Any]] = []
        self.get_calls: list[str] = []

    @staticmethod
    def _etag(payload: bytes) -> str:
        return f'"{hashlib.md5(payload).hexdigest()}"'  # noqa: S324 - S3's own scheme

    def get_object(self, *, Bucket: str, Key: str) -> dict[str, Any]:  # noqa: N803
        self.get_calls.append(Key)
        if Key not in self.objects:
            raise _client_error("NoSuchKey", 404, "GetObject")
        payload = self.objects[Key]
        return {"Body": _Body(payload), "ETag": self.etags[Key], "ContentLength": len(payload)}

    def put_object(self, *, Bucket: str, Key: str, Body: bytes, **kwargs: Any) -> dict[str, Any]:  # noqa: N803
        self.put_calls.append({"Key": Key, "Body": Body, **kwargs})
        current = self.etags.get(Key)

        if (expected := kwargs.get("IfMatch")) is not None and current != expected:
            raise _client_error("PreconditionFailed", 412, "PutObject")
        if kwargs.get("IfNoneMatch") == "*" and Key in self.objects:
            raise _client_error("PreconditionFailed", 412, "PutObject")

        self.objects[Key] = Body
        self.etags[Key] = self._etag(Body)
        return {"ETag": self.etags[Key]}


class _Body:
    def __init__(self, payload: bytes) -> None:
        self._payload = payload

    def read(self) -> bytes:
        return self._payload


# ------------------------------------------------------------------ S3 Vectors


class FakeVectors:
    """S3 Vectors, with the metadata filter actually applied.

    The filter is the whole point of h2o's read path: retrieval resolves the
    question against the vocabulary and searches only the chunks that ingestion
    tagged with those concepts. A fake that stored the filter and returned
    everything would make that assertion vacuous while the suite went green, so
    this evaluates `$and`, `$in` and `$eq` and really excludes what does not
    match.

    `$in` against a list-valued key matches when *any* element is in the list,
    which is the behaviour ingestion depends on: a chunk carries every concept
    its claims resolved to, and a question about one of them must find it.
    """

    def __init__(self) -> None:
        self.vectors: dict[str, dict[str, Any]] = {}
        #: Every query, including its filter, so a test can assert on what was
        #: asked rather than only on what came back.
        self.queries: list[dict[str, Any]] = []

    def put_vectors(self, *, vectorBucketName: str, indexName: str, vectors: list[dict]) -> dict:  # noqa: N803
        for entry in vectors:
            self.vectors[entry["key"]] = {
                "data": list(entry["data"]["float32"]),
                "metadata": dict(entry.get("metadata") or {}),
            }
        return {}

    def delete_vectors(self, *, vectorBucketName: str, indexName: str, keys: list[str]) -> dict:  # noqa: N803
        for key in keys:
            self.vectors.pop(key, None)
        return {}

    def query_vectors(self, **kwargs: Any) -> dict[str, Any]:
        self.queries.append(copy.deepcopy(kwargs))
        query = kwargs["queryVector"]["float32"]
        criteria = kwargs.get("filter")

        scored = []
        for key, entry in self.vectors.items():
            if criteria is not None and not _matches_filter(entry["metadata"], criteria):
                continue
            similarity = sum(a * b for a, b in zip(query, entry["data"], strict=False))
            scored.append((1.0 - similarity, key, entry))

        scored.sort(key=lambda item: (item[0], item[1]))
        return {
            "vectors": [
                {"key": key, "distance": distance, "metadata": entry["metadata"]}
                for distance, key, entry in scored[: kwargs.get("topK", 5)]
            ]
        }


def _matches_filter(metadata: dict[str, Any], criteria: dict[str, Any]) -> bool:
    for key, condition in criteria.items():
        if key == "$and":
            if not all(_matches_filter(metadata, clause) for clause in condition):
                return False
            continue
        if key == "$or":
            if not any(_matches_filter(metadata, clause) for clause in condition):
                return False
            continue

        actual = metadata.get(key)
        held = actual if isinstance(actual, list) else [actual]
        for operator, expected in condition.items():
            match operator:
                case "$in":
                    if not set(held) & set(expected):
                        return False
                case "$eq":
                    if expected not in held:
                        return False
                case _:
                    raise NotImplementedError(f"FakeVectors cannot filter with {operator!r}")
    return True


# --------------------------------------------------------------------- DynamoDB


def _split_top_level(clause: str) -> list[str]:
    """Split on commas that are not inside a function call.

    `if_not_exists(surface_form, :surface)` is one action containing a comma, so
    a plain `.split(",")` would tear it in half.
    """
    parts: list[str] = []
    depth = 0
    current: list[str] = []
    for char in clause:
        if char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
        if char == "," and depth == 0:
            parts.append("".join(current).strip())
            current = []
        else:
            current.append(char)
    if tail := "".join(current).strip():
        parts.append(tail)
    return parts


def _clauses(expression: str) -> dict[str, str]:
    tokens = re.split(r"\b(SET|ADD|REMOVE|DELETE)\b", expression)
    return {tokens[i]: tokens[i + 1] for i in range(1, len(tokens) - 1, 2)}


class FakeTable:
    """DynamoDB's UpdateItem, PutItem, Query and Scan, with its refusals intact.

    The update expression is really evaluated rather than pattern-matched against
    one caller, and the two ValidationExceptions that matter are reproduced:

      * **ADD refuses a nested path.** `ADD counts.ingestion :one` is invalid.
        This is not a stylistic rule -- ADD is the only atomic counter DynamoDB
        offers, so a nested counter cannot be incremented without a
        read-modify-write, and the gap queue's whole correctness claim is that
        two concurrent writers cannot lose a count.
      * **A nested SET needs its parent to exist.** `SET evidence.#eid = :e`
        against an item with no `evidence` attribute is invalid.

    Both were found by probing the real table, after the previous fake -- which
    hardcoded one caller's expression -- accepted them happily. That is the
    failure mode this whole module's docstring warns about, so the fake now
    parses instead of recognising.
    """

    def __init__(
        self,
        *,
        hash_key: str,
        range_key: str | None = None,
        indexes: dict[str, tuple[str, str]] | None = None,
    ) -> None:
        self.hash_key = hash_key
        self.range_key = range_key
        #: index name -> (hash attribute, range attribute), for ordering a Query
        self.indexes = dict(indexes or {})
        self.items: dict[tuple[Any, ...], dict[str, Any]] = {}
        #: Every mutating call, so a test can assert a read path performed none.
        self.writes: list[str] = []

    # ------------------------------------------------------------------ helpers

    def _key_of(self, item: dict[str, Any]) -> tuple[Any, ...]:
        if self.range_key is None:
            return (item[self.hash_key],)
        return (item[self.hash_key], item.get(self.range_key))

    def all_items(self) -> list[dict[str, Any]]:
        return [copy.deepcopy(item) for item in self.items.values()]

    # ------------------------------------------------------------------- writes

    def _reject_empty_keys(self, item: dict[str, Any], operation: str) -> None:
        """DynamoDB refuses an empty string in any key attribute, table or index.

        Found the hard way: a run row written with `started_at: ""` to mean "not
        started yet" is a ValidationException, because `started_at` is the runs
        table's by-kind sort key. The lesson generalises -- a key attribute has
        no empty value, so a placeholder has to be a real one.
        """
        attributes = [self.hash_key, *([self.range_key] if self.range_key else [])]
        for index_hash, index_range in self.indexes.values():
            attributes.extend((index_hash, index_range))
        for attribute in attributes:
            if item.get(attribute) == "":
                raise _client_error("ValidationException", 400, operation)

    def put_item(self, *, Item: dict[str, Any], **kwargs: Any) -> dict[str, Any]:  # noqa: N803
        self.writes.append("put_item")
        self._reject_empty_keys(Item, "PutItem")
        condition = kwargs.get("ConditionExpression")
        key = self._key_of(Item)
        if condition is not None and not self._condition_holds(condition, self.items.get(key)):
            raise _client_error("ConditionalCheckFailedException", 400, "PutItem")
        self.items[key] = copy.deepcopy(Item)
        return {}

    def delete_item(self, *, Key: dict[str, Any], **kwargs: Any) -> dict[str, Any]:  # noqa: N803
        self.writes.append("delete_item")
        self.items.pop(self._key_of(Key), None)
        return {}

    def update_item(self, *, Key: dict[str, Any], **kwargs: Any) -> dict[str, Any]:  # noqa: N803
        self.writes.append("update_item")
        names: dict[str, str] = kwargs.get("ExpressionAttributeNames", {})
        values: dict[str, Any] = kwargs.get("ExpressionAttributeValues", {})
        expression: str = kwargs.get("UpdateExpression", "")

        self._reject_empty_keys(Key, "UpdateItem")
        key = self._key_of(Key)
        item = self.items.setdefault(key, dict(Key))

        clauses = _clauses(expression)

        for action in _split_top_level(clauses.get("ADD", "")):
            path, _, raw = action.strip().partition(" ")
            parts = self._path(path, names)
            if len(parts) > 1:
                # The real refusal. ADD is top-level only.
                raise _client_error("ValidationException", 400, "UpdateItem")
            increment = self._evaluate(raw, item, names, values)
            item[parts[0]] = (item.get(parts[0]) or 0) + increment

        for action in _split_top_level(clauses.get("SET", "")):
            path, _, raw = action.partition("=")
            parts = self._path(path, names)
            self._assign(item, parts, self._evaluate(raw, item, names, values))

        for action in _split_top_level(clauses.get("REMOVE", "")):
            parts = self._path(action, names)
            container = self._container(item, parts)
            container.pop(parts[-1], None)

        return {}

    # -------------------------------------------------------- expression pieces

    @staticmethod
    def _path(path: str, names: dict[str, str]) -> list[str]:
        return [names.get(part, part) for part in path.strip().split(".")]

    def _container(self, item: dict[str, Any], parts: list[str]) -> dict[str, Any]:
        target = item
        for part in parts[:-1]:
            nested = target.get(part)
            if not isinstance(nested, dict):
                # "The document path provided in the update expression is
                # invalid for update" -- the parent map has to exist first.
                raise _client_error("ValidationException", 400, "UpdateItem")
            target = nested
        return target

    def _assign(self, item: dict[str, Any], parts: list[str], value: Any) -> None:
        self._container(item, parts)[parts[-1]] = value

    def _read_path(self, item: dict[str, Any], parts: list[str]) -> Any:
        current: Any = item
        for part in parts:
            if not isinstance(current, dict) or part not in current:
                return None
            current = current[part]
        return current

    def _evaluate(
        self, raw: str, item: dict[str, Any], names: dict[str, str], values: dict[str, Any]
    ) -> Any:
        expression = raw.strip()
        if expression.startswith(":"):
            return copy.deepcopy(values[expression])
        for function in ("if_not_exists", "list_append"):
            prefix = f"{function}("
            if expression.startswith(prefix):
                first, second = _split_top_level(expression[len(prefix) : -1])
                if function == "if_not_exists":
                    existing = self._read_path(item, self._path(first, names))
                    if existing is not None:
                        return existing
                    return self._evaluate(second, item, names, values)
                base = self._evaluate(first, item, names, values) or []
                return [*base, *self._evaluate(second, item, names, values)]
        raise NotImplementedError(f"FakeTable cannot evaluate {expression!r}")

    def _condition_holds(self, condition: Any, existing: dict[str, Any] | None) -> bool:
        text = condition if isinstance(condition, str) else ""
        if match := re.fullmatch(r"attribute_not_exists\((\w+)\)", text.strip()):
            return existing is None or match.group(1) not in existing
        if match := re.fullmatch(r"attribute_exists\((\w+)\)", text.strip()):
            return existing is not None and match.group(1) in existing
        raise NotImplementedError(f"FakeTable cannot evaluate condition {condition!r}")

    # -------------------------------------------------------------------- reads

    def get_item(self, *, Key: dict[str, Any], **_: Any) -> dict[str, Any]:  # noqa: N803
        item = self.items.get(self._key_of(Key))
        return {"Item": copy.deepcopy(item)} if item is not None else {}

    def scan(self, **_: Any) -> dict[str, Any]:
        return {"Items": self.all_items()}

    def query(self, **kwargs: Any) -> dict[str, Any]:  # noqa: N803
        constraints = _key_constraints(kwargs["KeyConditionExpression"])
        matched = [item for item in self.all_items() if _matches(item, constraints)]

        index = kwargs.get("IndexName")
        range_attribute = (
            self.indexes[index][1] if index in self.indexes else (self.range_key or self.hash_key)
        )
        matched.sort(key=lambda i: str(i.get(range_attribute, "")))
        if not kwargs.get("ScanIndexForward", True):
            matched.reverse()
        if limit := kwargs.get("Limit"):
            matched = matched[:limit]
        return {"Items": matched}


def _key_constraints(condition: Any) -> list[tuple[str, str, Any]]:
    """Flatten a boto3 Key condition into (attribute, operator, value) triples."""
    found: list[tuple[str, str, Any]] = []

    def walk(node: Any) -> None:
        expression = node.get_expression()
        operator = expression["operator"]
        operands = expression["values"]
        if operator == "AND":
            for operand in operands:
                walk(operand)
            return
        if operator not in ("=", "begins_with"):
            raise NotImplementedError(f"FakeTable cannot query with {operator!r}")
        found.append((operands[0].name, operator, operands[1]))

    walk(condition)
    return found


def _matches(item: dict[str, Any], constraints: list[tuple[str, str, Any]]) -> bool:
    for attribute, operator, expected in constraints:
        actual = item.get(attribute)
        if operator == "=" and actual != expected:
            return False
        if operator == "begins_with" and not str(actual).startswith(str(expected)):
            return False
    return True
