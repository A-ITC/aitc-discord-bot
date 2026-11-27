"""API Gateway HTTP API v2 handler. No network calls at import or invocation."""

import json
import logging
import time
import traceback
from pathlib import PurePath

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from discord_bot.config import Config, SNOWFLAKE
from discord_bot.interactions import dispatch, parse_interaction
from discord_bot.responses import RequestError, http_response
from discord_bot.verification import raw_body, verify_request

CONFIG = Config.from_environment()
VERIFY_KEY = Ed25519PublicKey.from_public_bytes(CONFIG.public_key)
LOGGER = logging.getLogger("discord_bot")
LOGGER.setLevel(logging.INFO)
_cold_start = True


def lambda_handler(event, context):
    global _cold_start
    started = time.perf_counter()
    record = {"request_id": getattr(context, "aws_request_id", None), "cold_start": _cold_start}
    _cold_start = False
    status, reason = 500, "internal_error"
    try:
        if not isinstance(event, dict) or event.get("version") != "2.0":
            raise RequestError(400, "invalid_event")
        request_context = event.get("requestContext")
        if not isinstance(request_context, dict) or not isinstance(request_context.get("http"), dict):
            raise RequestError(400, "invalid_event")
        http = request_context["http"]
        if http.get("method") != "POST" or event.get("rawPath") != "/interactions":
            raise RequestError(404, "not_found")
        body = raw_body(event)
        verify_request(event, body, VERIFY_KEY)
        interaction = parse_interaction(body)
        interaction_id = interaction.get("id")
        if isinstance(interaction_id, str) and SNOWFLAKE.fullmatch(interaction_id):
            record["interaction_id"] = interaction_id
        if isinstance(interaction.get("data"), dict) and interaction["data"].get("name") == "ping":
            record["command"] = "ping"
        response = http_response(200, dispatch(interaction, CONFIG))
        status, reason = 200, "ok"
    except RequestError as error:
        status, reason = error.status, error.reason
        response = http_response(status, {"error": reason})
    except Exception as error:
        # Exception messages can contain request data. Keep stack locations, not values/locals.
        record["exception_type"] = type(error).__name__
        record["stack"] = [{"file": PurePath(frame.filename).name, "line": frame.lineno,
                            "function": frame.name} for frame in traceback.extract_tb(error.__traceback__)]
        response = http_response(500, {"error": "internal_error"})
    record.update(status=status, result=reason, duration_ms=round((time.perf_counter() - started) * 1000, 3))
    LOGGER.log(logging.ERROR if status >= 500 else logging.INFO, json.dumps(record, ensure_ascii=False))
    return response
