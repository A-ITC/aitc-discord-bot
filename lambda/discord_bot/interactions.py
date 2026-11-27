import json

from .commands.ping import execute
from .config import Config
from .responses import RequestError, message


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate_json_key")
        result[key] = value
    return result


def _reject_constant(_):
    raise ValueError("invalid_json_constant")


def parse_interaction(body: bytes) -> dict:
    try:
        interaction = json.loads(body.decode("utf-8"), object_pairs_hook=_unique_object,
                                 parse_constant=_reject_constant)
    except (ValueError, UnicodeError, RecursionError):
        raise RequestError(400, "invalid_json") from None
    if not isinstance(interaction, dict) or type(interaction.get("type")) is not int:
        raise RequestError(400, "invalid_interaction")
    return interaction


def dispatch(interaction: dict, config: Config) -> dict:
    kind = interaction["type"]
    if kind == 1:
        return {"type": 1}
    if kind != 2:
        raise RequestError(400, "unsupported_interaction")
    application_id = interaction.get("application_id")
    guild_id = interaction.get("guild_id")
    if not isinstance(application_id, str) or not isinstance(guild_id, str):
        raise RequestError(403, "forbidden_context")
    if (application_id != config.application_id or guild_id not in config.guild_ids
            or not isinstance(interaction.get("member"), dict)
            or interaction.get("context", 0) != 0):
        raise RequestError(403, "forbidden_context")
    data = interaction.get("data")
    if (not isinstance(data, dict) or type(data.get("type")) is not int
            or data["type"] not in (1, 2, 3) or not isinstance(data.get("name"), str)
            or not data["name"] or len(data["name"]) > 32):
        raise RequestError(400, "invalid_command")
    if data["type"] == 1 and data["name"] == "ping":
        if "options" in data and data["options"] != []:
            raise RequestError(400, "invalid_options")
        return execute()
    return message("このコマンドは現在利用できません。")
