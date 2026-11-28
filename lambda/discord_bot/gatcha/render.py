"""Components V2 with actual attachments, no automatic mentions."""
import re
from urllib.parse import quote

from .domain import TooLarge


def payload(message, files, profile_base, member_id):
    author = message["author"]
    name = author.get("global_name") or author.get("username") or "不明な投稿者"
    name = re.sub(r"[\x00-\x1f]", " ", name)
    name = re.sub(r"([\\`*_{}\[\]()#+.!|>~])", r"\\\1", name)
    user_id = author["id"]
    avatar = author.get("avatar")
    if isinstance(avatar, str) and re.fullmatch(r"(?:a_)?[0-9a-f]{32}", avatar):
        icon = f"https://cdn.discordapp.com/avatars/{user_id}/{avatar}.png"
    else:
        icon = f"https://cdn.discordapp.com/embed/avatars/{(int(user_id) >> 22) % 6}.png"
    title = f"# AITC ランダム投稿\n{name} の投稿"
    body = message.get("content", "")
    if len(title) + len(body) + 30 > 4000:
        raise TooLarge("text_limit")
    components = [{"type": 9, "components": [{"type": 10, "content": title}],
                   "accessory": {"type": 11, "media": {"url": icon}}}]
    if body:
        components.append({"type": 10, "content": body})
    images = [{"media": {"url": f"attachment://{file['filename']}"}, "spoiler": file["spoiler"]}
              for file in files if file["image"]]
    if images:
        components.append({"type": 12, "items": images})
    components.extend({"type": 13, "file": {"url": f"attachment://{file['filename']}"}, "spoiler": file["spoiler"]}
                      for file in files if not file["image"])
    if member_id is not None:
        components.extend([
            {"type": 14},
            {"type": 9, "components": [{"type": 10, "content": "投稿者のプロフィール"}],
             "accessory": {"type": 2, "style": 5, "label": "プロフィールを見る",
                           "url": profile_base.rstrip("/") + "/members-only/members/profile?id=" + quote(member_id, safe="")}},
        ])
    return {"flags": 32768, "allowed_mentions": {"parse": []},
            "components": [{"type": 17, "components": components}]}
