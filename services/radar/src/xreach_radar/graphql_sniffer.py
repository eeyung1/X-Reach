"""Extract GraphQL JSON documents from explicit network capture bodies."""

from __future__ import annotations

import base64
import hashlib
import json
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from typing import Any
from urllib.parse import parse_qs, urlparse

SUPPORTED_OPERATIONS = frozenset({"HomeTimeline", "HomeLatestTimeline", "TweetDetail"})


@dataclass(frozen=True)
class GraphQLDocument:
    """One decoded response body and its capture provenance."""

    operation: str | None
    payload: Mapping[str, Any]
    source: str


def iter_graphql_documents(capture: Any) -> Iterator[GraphQLDocument]:
    """Yield supported timeline response documents from common capture forms.

    Accepted inputs are raw GraphQL response objects, HAR documents, REA
    historical-capture records, and REA browser-scenario captures. Bodies that
    are absent, redacted, non-UTF-8, or invalid JSON are skipped.
    """

    if not isinstance(capture, Mapping):
        return

    yielded: set[tuple[str | None, str]] = set()
    found_structured_capture = False

    for source, request, response in _iter_http_exchanges(capture):
        found_structured_capture = True
        operation = _operation_from_request(request)
        if operation not in SUPPORTED_OPERATIONS:
            continue
        payload = _decode_response(response)
        if payload is None:
            continue
        key = (operation, _stable_identity(payload))
        if key not in yielded:
            yielded.add(key)
            yield GraphQLDocument(operation, payload, source)

    for source, operation, payload in _iter_scenario_documents(capture):
        found_structured_capture = True
        key = (operation, _stable_identity(payload))
        if key not in yielded:
            yielded.add(key)
            yield GraphQLDocument(operation, payload, source)

    if not found_structured_capture and _looks_like_graphql_payload(capture):
        operation = _operation_from_mapping(capture)
        if operation is None or operation in SUPPORTED_OPERATIONS:
            yield GraphQLDocument(operation, capture, "raw-graphql")


def _iter_http_exchanges(
    root: Mapping[str, Any],
) -> Iterator[tuple[str, Mapping[str, Any], Mapping[str, Any]]]:
    log = root.get("log")
    if isinstance(log, Mapping):
        entries = log.get("entries")
        if isinstance(entries, list):
            yield from _exchange_list(entries, "har")

    records = root.get("records")
    if isinstance(records, list):
        for index, record in enumerate(records):
            if not isinstance(record, Mapping):
                continue
            reported = record.get("reported", record)
            if not isinstance(reported, Mapping):
                continue
            request = reported.get("request")
            response = reported.get("response")
            if isinstance(request, Mapping) and isinstance(response, Mapping):
                yield f"rea-record:{index}", request, response

    for key in ("normalized_result", "data", "value"):
        child = root.get(key)
        if isinstance(child, Mapping):
            yield from _iter_http_exchanges(child)


def _exchange_list(
    entries: list[Any], source: str
) -> Iterator[tuple[str, Mapping[str, Any], Mapping[str, Any]]]:
    for index, entry in enumerate(entries):
        if not isinstance(entry, Mapping):
            continue
        request = entry.get("request")
        response = entry.get("response")
        if isinstance(request, Mapping) and isinstance(response, Mapping):
            yield f"{source}:{index}", request, response


def _iter_scenario_documents(
    root: Mapping[str, Any],
) -> Iterator[tuple[str, str, Mapping[str, Any]]]:
    for capture in _find_scenario_captures(root):
        events = capture["events"]["items"]
        requests: dict[str, tuple[str, str | None]] = {}
        for event in events:
            if not isinstance(event, Mapping):
                continue
            transaction = event.get("transaction_id")
            if not isinstance(transaction, str):
                continue
            kind = event.get("kind")
            if kind == "request":
                url_value = event.get("url")
                url = (
                    url_value.get("url")
                    if isinstance(url_value, Mapping)
                    else url_value
                )
                if isinstance(url, str):
                    requests[transaction] = (url, _operation_from_url(url))
            elif kind == "network-content" and event.get("phase") == "response":
                request = requests.get(transaction)
                if request is None or request[1] not in SUPPORTED_OPERATIONS:
                    continue
                body = event.get("body")
                payload = _decode_scenario_body(body)
                if payload is not None:
                    yield f"rea-scenario:{transaction}", request[1], payload


def _find_scenario_captures(root: Mapping[str, Any]) -> Iterator[Mapping[str, Any]]:
    seen: set[int] = set()
    stack: list[Any] = [root]
    while stack:
        value = stack.pop()
        if isinstance(value, Mapping):
            identity = id(value)
            if identity in seen:
                continue
            seen.add(identity)
            events = value.get("events")
            if (
                isinstance(events, Mapping)
                and isinstance(events.get("items"), list)
                and any(
                    isinstance(item, Mapping) and "transaction_id" in item
                    for item in events["items"]
                )
            ):
                yield value
                continue
            stack.extend(value.values())
        elif isinstance(value, list):
            stack.extend(value)


def _operation_from_request(request: Mapping[str, Any]) -> str | None:
    for key in ("operationName", "operation_name", "name"):
        value = request.get(key)
        if isinstance(value, str) and value in SUPPORTED_OPERATIONS:
            return value
    url = request.get("url")
    return _operation_from_url(url) if isinstance(url, str) else None


def _operation_from_url(url: str) -> str | None:
    parsed = urlparse(url)
    for operation in SUPPORTED_OPERATIONS:
        if operation in parsed.path.split("/"):
            return operation
    query = parse_qs(parsed.query)
    for key in ("operationName", "operation_name"):
        values = query.get(key, ())
        if values and values[0] in SUPPORTED_OPERATIONS:
            return values[0]
    return None


def _operation_from_mapping(value: Mapping[str, Any]) -> str | None:
    for key in ("operationName", "operation_name"):
        operation = value.get(key)
        if isinstance(operation, str):
            return operation
    extensions = value.get("extensions")
    if isinstance(extensions, Mapping):
        operation = extensions.get("operationName")
        if isinstance(operation, str):
            return operation
    return None


def _decode_response(response: Mapping[str, Any]) -> Mapping[str, Any] | None:
    content = response.get("content")
    if isinstance(content, Mapping):
        text = content.get("text")
        if isinstance(text, str):
            if content.get("encoding") == "base64":
                return _decode_json_base64(text)
            return _decode_json_text(text)
    for key in ("body", "text"):
        value = response.get(key)
        if isinstance(value, Mapping):
            decoded = _decode_scenario_body(value)
            if decoded is not None:
                return decoded
        elif isinstance(value, str):
            decoded = _decode_json_text(value)
            if decoded is not None:
                return decoded
    return None


def _decode_scenario_body(body: Any) -> Mapping[str, Any] | None:
    if (
        not isinstance(body, Mapping)
        or body.get("state") != "captured"
        or body.get("redacted") is True
    ):
        return None
    content = body.get("content")
    if not isinstance(content, str):
        return None
    return (
        _decode_json_base64(content)
        if body.get("encoding") == "base64"
        else _decode_json_text(content)
    )


def _decode_json_base64(value: str) -> Mapping[str, Any] | None:
    try:
        decoded = base64.b64decode(value, validate=True).decode("utf-8")
    except (ValueError, UnicodeDecodeError):
        return None
    return _decode_json_text(decoded)


def _decode_json_text(value: str) -> Mapping[str, Any] | None:
    try:
        decoded = json.loads(value)
    except (json.JSONDecodeError, TypeError):
        return None
    return decoded if isinstance(decoded, dict) else None


def _looks_like_graphql_payload(value: Mapping[str, Any]) -> bool:
    return isinstance(value.get("data"), Mapping) or (
        isinstance(value.get("errors"), list) and "data" in value
    )


def _stable_identity(payload: Mapping[str, Any]) -> str:
    encoded = json.dumps(
        payload, sort_keys=True, separators=(",", ":"), default=str
    ).encode()
    return hashlib.sha256(encoded).hexdigest()
