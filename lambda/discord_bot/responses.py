import json


class RequestError(Exception):
    def __init__(self, status: int, reason: str):
        self.status = status
        self.reason = reason
        super().__init__(reason)


def http_response(status: int, body: dict) -> dict:
    return {
        "statusCode": status,
        "headers": {"content-type": "application/json"},
        "isBase64Encoded": False,
        "body": json.dumps(body, ensure_ascii=False, separators=(",", ":")),
    }


def message(content: str) -> dict:
    return {
        "type": 4,
        "data": {"content": content, "flags": 64, "allowed_mentions": {"parse": []}},
    }
