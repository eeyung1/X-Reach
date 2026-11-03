import base64
import json

from xreach_radar.graphql_sniffer import iter_graphql_documents

PAYLOAD = {"data": {"home": {"home_timeline_urt": {"instructions": []}}}}


def test_reads_supported_har_response_and_ignores_other_operations():
    capture = {
        "log": {
            "entries": [
                {
                    "request": {"url": "https://x.com/i/api/graphql/hash/HomeTimeline"},
                    "response": {
                        "content": {
                            "encoding": "base64",
                            "text": base64.b64encode(
                                json.dumps(PAYLOAD).encode()
                            ).decode(),
                        }
                    },
                },
                {
                    "request": {
                        "url": "https://x.com/i/api/graphql/hash/UserByScreenName"
                    },
                    "response": {"content": {"text": json.dumps(PAYLOAD)}},
                },
            ]
        }
    }

    documents = list(iter_graphql_documents(capture))
    assert [(item.operation, item.source) for item in documents] == [
        ("HomeTimeline", "har:0")
    ]
    assert documents[0].payload == PAYLOAD


def test_reads_rea_scenario_response_body():
    encoded = base64.b64encode(json.dumps(PAYLOAD).encode()).decode()
    capture = {
        "normalized_result": {
            "events": {
                "items": [
                    {
                        "kind": "request",
                        "transaction_id": "tx-1",
                        "url": {"url": "https://x.com/i/api/graphql/hash/TweetDetail"},
                    },
                    {
                        "kind": "network-content",
                        "phase": "response",
                        "transaction_id": "tx-1",
                        "body": {
                            "state": "captured",
                            "encoding": "base64",
                            "content": encoded,
                        },
                    },
                ]
            }
        }
    }

    documents = list(iter_graphql_documents(capture))
    assert len(documents) == 1
    assert documents[0].operation == "TweetDetail"
    assert documents[0].payload == PAYLOAD


def test_inspect_page_metadata_without_bodies_is_not_inferred():
    capture = {
        "normalized_result": {
            "network": {
                "requests": [{"url": "https://x.com/i/api/graphql/hash/HomeTimeline"}]
            }
        }
    }
    assert list(iter_graphql_documents(capture)) == []


def test_redacted_scenario_body_is_not_parsed():
    capture = {
        "events": {
            "items": [
                {
                    "kind": "request",
                    "transaction_id": "tx-1",
                    "url": {"url": "https://x.com/i/api/graphql/hash/HomeTimeline"},
                },
                {
                    "kind": "network-content",
                    "phase": "response",
                    "transaction_id": "tx-1",
                    "body": {
                        "state": "captured",
                        "redacted": True,
                        "content": json.dumps(PAYLOAD),
                    },
                },
            ]
        }
    }
    assert list(iter_graphql_documents(capture)) == []
