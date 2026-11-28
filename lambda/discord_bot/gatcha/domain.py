"""Pure command validation, schedule parsing and random selection."""
from datetime import datetime, timezone, timedelta
import random
import re

from ..config import SNOWFLAKE

ADMIN_ROLE_ID = "840827308251283456"
JST = timezone(timedelta(hours=9))
DAYS = ("MON", "TUE", "WED", "THU", "FRI", "SAT", "SUN")


class InvalidCommand(ValueError):
    pass


class Retryable(Exception):
    pass


class UnknownDelivery(Exception):
    """A write may have reached Discord; it must not be sent again."""


class TooLarge(Exception):
    pass


def snowflake(value):
    if not isinstance(value, str) or not SNOWFLAKE.fullmatch(value):
        raise InvalidCommand("IDは文字列のDiscord IDを指定してください。")
    return value


def schedule(day, hour):
    if not isinstance(day, str) or day not in DAYS:
        raise InvalidCommand("曜日は月曜〜日曜の選択肢から指定してください。")
    if type(hour) is not int or not 0 <= hour <= 23:
        raise InvalidCommand("時刻は00:00〜23:00の選択肢から指定してください。")
    return [{"day": DAYS.index(day), "hour": hour}]


def option_map(values, allowed):
    if not isinstance(values, list):
        raise InvalidCommand("コマンド引数が不正です。")
    result = {}
    for item in values:
        if not isinstance(item, dict) or item.get("name") not in allowed or item["name"] in result:
            raise InvalidCommand("コマンド引数が不正です。")
        result[item["name"]] = item
    return result


def command(interaction, source_guild, allowed_guilds, now):
    data = interaction["data"]
    args = {}
    if data["type"] == 3 and data["name"] in ("gatcha register", "gatcha remove"):
        action = data["name"].split()[1]
        args["message_id"] = snowflake(data.get("target_id"))
    elif data["type"] == 1 and data["name"] == "gatcha":
        top = option_map(data.get("options", []), {"draw", "channel", "timer"})
        if len(top) != 1:
            raise InvalidCommand("サブコマンドを指定してください。")
        group, entry = next(iter(top.items()))
        if group == "draw" and entry.get("type") == 1:
            options = option_map(entry.get("options", []), {"server"})
            server = options.get("server", {"type": 3, "value": "aitc"})
            if server.get("type") != 3 or server.get("value") != "aitc":
                raise InvalidCommand("登録元のサーバーはaitcを指定してください。")
            action = "draw"
        else:
            if entry.get("type") != 2 or group not in ("channel", "timer"):
                raise InvalidCommand("未対応のサブコマンドです。")
            names = {"add", "remove"} if group == "channel" else {"config", "remove"}
            children = option_map(entry.get("options", []), names)
            if len(children) != 1:
                raise InvalidCommand("サブコマンドを指定してください。")
            child, entry = next(iter(children.items()))
            if entry.get("type") != 1:
                raise InvalidCommand("コマンド引数が不正です。")
            action = f"{group}_{child}"
            names = {"guild", "channel", "day", "hour"} if action == "timer_config" else {"guild"} if group == "timer" else set()
            options = option_map(entry.get("options", []), names)
            if set(options) != names:
                raise InvalidCommand("必要な引数を指定してください。")
            for name, item in options.items():
                expected_type, value_type = (4, int) if name == "hour" else (3, str)
                if item.get("type") != expected_type or type(item.get("value")) is not value_type:
                    raise InvalidCommand("引数の型が不正です。")
                args[name] = item["value"]
            if group == "timer":
                args["guild"] = snowflake(args["guild"])
                if args["guild"] not in allowed_guilds:
                    raise InvalidCommand("投稿先は許可済みサーバーから指定してください。")
                if action == "timer_config":
                    args["channel"] = snowflake(args["channel"])
                    args["slots"] = schedule(args.pop("day"), args.pop("hour"))
    else:
        raise InvalidCommand("未対応のコマンドです。")
    if action != "draw":
        roles = interaction.get("member", {}).get("roles", [])
        if interaction["guild_id"] != source_guild or not isinstance(roles, list) or ADMIN_ROLE_ID not in roles:
            raise InvalidCommand("AITCサーバーの指定管理者ロールが必要です。")
    token = interaction.get("token")
    if not isinstance(token, str) or not re.fullmatch(r"[A-Za-z0-9._-]{1,256}", token):
        raise InvalidCommand("Interaction Tokenが不正です。")
    return {
        "id": snowflake(interaction.get("id")), "action": action,
        "guild_id": snowflake(interaction["guild_id"]),
        "channel_id": snowflake(interaction.get("channel_id")),
        "token": token, "application_id": snowflake(interaction["application_id"]),
        "created_at": int(now), "deadline": int(now) + 840, "args": args,
    }


def candidates(works, channels, recent, skipped=()):
    live = [w for w in works if w.get("active") and w["channel_id"] in channels]
    excluded = set(recent) if len(live) > 10 else set()
    return [w for w in live if w["message_id"] not in excluded and w["message_id"] not in skipped]


def choose(values):
    return random.SystemRandom().choice(values)


def source_url(guild, channel, message):
    return f"https://discord.com/channels/{snowflake(guild)}/{snowflake(channel)}/{snowflake(message)}"


def scheduled_timestamp(value):
    if not isinstance(value, str):
        raise InvalidCommand("Missing scheduled_time")
    stamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if stamp.tzinfo is None or stamp.minute or stamp.second or stamp.microsecond:
        raise InvalidCommand("Expected an hourly scheduled_time")
    return stamp
