"""Lazy AWS wiring: /ping still imports no SDK and performs no I/O."""
import hashlib
import json
import logging
import os
import re
import time
from urllib.parse import urlsplit

from .domain import InvalidCommand, JST, command, scheduled_timestamp, snowflake

LOGGER = logging.getLogger("discord_bot.gatcha")
_aws_clients = None


def settings():
    source = snowflake(os.environ.get("GATCHA_SOURCE_GUILD_ID"))
    api_id = os.environ.get("WEB_LOOKUP_API_ID", "")
    region = os.environ.get("WEB_LOOKUP_REGION", "")
    profile = os.environ.get("WEB_PROFILE_BASE_URL", "")
    url = urlsplit(profile)
    if (not re.fullmatch(r"[a-z0-9]+", api_id) or not re.fullmatch(r"[a-z]{2}(?:-[a-z]+)+-[0-9]+", region)
            or url.scheme != "https" or not url.hostname or url.username or url.password
            or url.port not in (None, 443) or url.path not in ("", "/") or url.query or url.fragment):
        raise ValueError("Invalid gatcha web configuration")
    return source, api_id, region, profile.rstrip("/")


def aws():
    global _aws_clients
    if _aws_clients is None:
        import boto3
        from botocore.config import Config
        from .store import Store
        sdk_config = Config(connect_timeout=1, read_timeout=2, retries={"total_max_attempts": 2})
        session = boto3.Session()
        table = session.resource("dynamodb", config=sdk_config).Table(os.environ["GATCHA_TABLE_NAME"])
        _aws_clients = (Store(table, session.client("dynamodb", config=sdk_config)),
                        session.client("sqs", config=sdk_config), session)
    return _aws_clients


def accept(interaction, config):
    from ..responses import message
    try:
        source, *_ = settings()
        job = command(interaction, source, config.guild_ids, time.time())
        limit = interaction.get("attachment_size_limit", 10 * 1024 * 1024)
        if type(limit) is not int or limit <= 0:
            raise InvalidCommand("添付ファイルの上限値が不正です。")
        job["attachment_limit"] = limit
    except InvalidCommand as error:
        return message(str(error))
    _, queue, _ = aws()
    # Give Discord time to persist the initial deferred response before PATCHing it.
    queue.send_message(QueueUrl=os.environ["GATCHA_QUEUE_URL"], MessageBody=json.dumps(job), DelaySeconds=3)
    return {"type": 5, "data": {"flags": 0 if job["action"] == "draw" else 64}}


def worker_handler(event, context):
    from .service import Service
    from .transport import Discord, MemberLookup
    source, api_id, region, profile = settings()
    store, _, session = aws()
    from botocore.config import Config
    token = session.client("ssm", config=Config(connect_timeout=1, read_timeout=2, retries={"total_max_attempts": 2})).get_parameter(
        Name=os.environ["BOT_TOKEN_PARAMETER_NAME"], WithDecryption=True)["Parameter"]
    if token.get("Type") != "SecureString" or not token.get("Value"):
        raise ValueError("Bot token must be SecureString")
    service = Service(store, Discord(token["Value"]), MemberLookup(session, api_id, region), source,
                      set(os.environ["ALLOWED_GUILD_IDS"].split(",")), profile)
    failures = []
    for record in event["Records"]:
        try:
            if record.get("eventSource") != "aws:sqs":
                raise ValueError("Expected SQS event")
            job = json.loads(record["body"])
            service.process(job)
            state = store.get(f"JOB#{job['id']}", "STATE") or {}
            LOGGER.info("gatcha job=%s result=%s", job["id"], state.get("status", "UNKNOWN"))
        except Exception as error:
            # No bodies, tokens, HTTP paths or exception values in logs.
            LOGGER.error("gatcha record=%s exception_type=%s", record["messageId"], type(error).__name__)
            failures.append({"itemIdentifier": record["messageId"]})
    return {"batchItemFailures": failures}


def schedule_handler(event, context):
    source, *_ = settings()
    store, queue, _ = aws()
    stamp = scheduled_timestamp(event.get("scheduled_time"))
    scheduled_at = int(stamp.timestamp())
    now = int(time.time())
    if now < scheduled_at or now > scheduled_at + 300:
        return {"queued": 0, "result": "outside_window"}
    local = stamp.astimezone(JST)
    queued = 0
    allowed = set(os.environ["ALLOWED_GUILD_IDS"].split(","))
    for timer in store.query("TIMERS"):
        if (not timer.get("active", True) or timer["guild_id"] not in allowed
                or {"day": local.weekday(), "hour": local.hour} not in timer["slots"]):
            continue
        job_id = hashlib.sha256(f"{timer['guild_id']}#{scheduled_at}".encode()).hexdigest()[:24]
        job = {"id": job_id, "action": "auto", "guild_id": timer["guild_id"], "channel_id": timer["channel_id"],
               "created_at": scheduled_at, "deadline": scheduled_at + 300, "timer_version": timer["version"]}
        queue.send_message(QueueUrl=os.environ["GATCHA_QUEUE_URL"], MessageBody=json.dumps(job))
        queued += 1
    return {"queued": queued}
