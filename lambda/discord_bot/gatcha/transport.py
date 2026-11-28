"""Bounded HTTPS requests without redirects or secret-bearing error messages."""
import json
from http.client import HTTPSConnection, HTTPException
import re
import uuid
from urllib.parse import urlsplit, urlencode

from botocore.auth import SigV4Auth
from botocore.awsrequest import AWSRequest

from .domain import Retryable, UnknownDelivery, TooLarge, snowflake

MAX_UPLOAD = 24 * 1024 * 1024


class DiscordError(Exception):
    def __init__(self, status, code=None):
        self.status, self.code = status, code
        super().__init__(f"discord_http_{status}")


def request(host, method, path, headers, body=None, *, limit=1024 * 1024, writing=False):
    connection = HTTPSConnection(host, timeout=5)
    try:
        connection.request(method, path, body=body, headers=headers)
        response = connection.getresponse()
        raw = response.read(limit + 1)
        if len(raw) > limit:
            if writing:
                raise UnknownDelivery("response_too_large")
            raise TooLarge("response_too_large")
        return response.status, raw
    except (OSError, HTTPException):
        raise (UnknownDelivery("discord_transport") if writing else Retryable("read_transport")) from None
    finally:
        connection.close()


class Discord:
    def __init__(self, token):
        self.token = token

    def call(self, method, path, payload=None, files=(), *, webhook=False):
        writing = method in ("POST", "PATCH")
        headers = {"User-Agent": "DiscordBot (https://github.com/A-ITC/aitc-discord-bot, 2.0)"}
        if not webhook:
            headers["Authorization"] = "Bot " + self.token
        body = None
        if payload is not None:
            data = dict(payload)
            if files:
                data["attachments"] = [{"id": i, "filename": f["filename"]} for i, f in enumerate(files)]
                boundary = uuid.uuid4().hex
                parts = [f"--{boundary}\r\nContent-Disposition: form-data; name=\"payload_json\"\r\nContent-Type: application/json\r\n\r\n".encode(),
                         json.dumps(data, ensure_ascii=False).encode(), b"\r\n"]
                for i, file in enumerate(files):
                    parts.extend([f'--{boundary}\r\nContent-Disposition: form-data; name="files[{i}]"; filename="{file["filename"]}"\r\nContent-Type: application/octet-stream\r\n\r\n'.encode(), file["data"], b"\r\n"])
                parts.append(f"--{boundary}--\r\n".encode())
                body = b"".join(parts)
                headers["Content-Type"] = f"multipart/form-data; boundary={boundary}"
                if len(body) >= 25 * 1024 * 1024:
                    raise TooLarge("request_limit")
            else:
                headers["Content-Type"] = "application/json"
                body = json.dumps(data, ensure_ascii=False).encode()
        status, raw = request("discord.com", method, "/api/v10" + path, headers, body, writing=writing)
        try:
            result = json.loads(raw) if raw else {}
        except (ValueError, UnicodeError):
            raise (UnknownDelivery("invalid_write_response") if writing else Retryable("invalid_read_response")) from None
        if status == 429:
            raise Retryable("discord_rate_limit")
        if status >= 500:
            raise (UnknownDelivery("discord_write_5xx") if writing else Retryable("discord_read_5xx"))
        if status == 413 or (status == 400 and result.get("code") in (40005, 50045)):
            raise TooLarge("discord_upload_limit")
        if not 200 <= status < 300:
            raise DiscordError(status, result.get("code"))
        if not isinstance(result, dict):
            raise (UnknownDelivery("invalid_write_response") if writing else Retryable("invalid_read_response"))
        return result

    def channel(self, channel_id):
        return self.call("GET", f"/channels/{snowflake(channel_id)}")

    def message(self, channel_id, message_id):
        return self.call("GET", f"/channels/{snowflake(channel_id)}/messages/{snowflake(message_id)}")

    def reply(self, job, payload, files=()):
        return self.call("PATCH", f"/webhooks/{job['application_id']}/{job['token']}/messages/@original",
                         payload, files, webhook=True)

    def post(self, job, payload, files=()):
        return self.call("POST", f"/channels/{snowflake(job['channel_id'])}/messages",
                         {**payload, "nonce": job["id"][:25], "enforce_nonce": True}, files)

    def attachments(self, message, per_file_limit=MAX_UPLOAD):
        result, total = [], 0
        attachments = message.get("attachments", [])
        if not isinstance(attachments, list) or len(attachments) > 10:
            raise TooLarge("attachment_count")
        for i, attachment in enumerate(attachments):
            size = attachment.get("size")
            if type(size) is not int or size < 0 or size > min(per_file_limit, MAX_UPLOAD) or total + size > MAX_UPLOAD:
                raise TooLarge("attachment_limit")
            url = urlsplit(attachment["url"])
            if (url.scheme != "https" or url.hostname not in ("cdn.discordapp.com", "media.discordapp.net")
                    or url.port not in (None, 443) or url.username or url.password
                    or not re.fullmatch(r"/attachments/[0-9]{17,20}/[0-9]{17,20}/[^/]+", url.path)):
                raise Retryable("untrusted_attachment_url")
            status, data = request(url.hostname, "GET", url.path + ("?" + url.query if url.query else ""), {},
                                   limit=min(per_file_limit, MAX_UPLOAD - total))
            if status != 200:
                raise Retryable("attachment_unavailable")
            total += len(data)
            original = attachment.get("filename", "file")
            filename = f"{i}-" + re.sub(r"[^A-Za-z0-9._-]", "_", original)[:100]
            result.append({"filename": filename, "data": data,
                           "image": attachment.get("content_type", "").startswith("image/"),
                           "spoiler": original.startswith("SPOILER_")})
        return result


class MemberLookup:
    def __init__(self, session, api_id, region):
        self.session, self.region = session, region
        self.host = f"{api_id}.execute-api.{region}.amazonaws.com"

    def resolve(self, discord_id):
        path = "/members-only/members/discord?" + urlencode({"id": snowflake(discord_id)})
        signed = AWSRequest(method="GET", url="https://" + self.host + path)
        SigV4Auth(self.session.get_credentials().get_frozen_credentials(), "execute-api", self.region).add_auth(signed)
        status, raw = request(self.host, "GET", path, dict(signed.headers), limit=8192)
        if status == 404:
            return None
        if status != 200:
            raise Retryable("member_lookup_unavailable")
        try:
            member_id = json.loads(raw)["id"]
            if not isinstance(member_id, str) or not 1 <= len(member_id) <= 128 or re.search(r"[/\\?#\x00-\x1f]", member_id):
                raise ValueError()
            return member_id
        except (ValueError, KeyError, TypeError):
            raise Retryable("member_lookup_invalid") from None
