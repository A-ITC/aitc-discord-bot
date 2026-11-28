"""Idempotent jobs, serialized draws and durable delivery states."""
from datetime import datetime, timezone
import logging
import time

from .domain import (InvalidCommand, Retryable, TooLarge, UnknownDelivery, candidates,
                     choose, source_url)
from .render import payload
from .transport import DiscordError

LOGGER = logging.getLogger("discord_bot.gatcha")
TERMINAL = {"SUCCESS", "FAILED", "UNKNOWN", "EXPIRED", "SKIPPED"}


class Service:
    def __init__(self, store, discord, lookup, source_guild, guild_ids, profile_base, *, clock=time.time):
        self.store, self.discord, self.lookup = store, discord, lookup
        self.source_guild, self.guild_ids, self.profile_base = source_guild, guild_ids, profile_base
        self.clock = clock

    def process(self, job):
        now = int(self.clock())
        record = self.store.get(f"JOB#{job['id']}", "STATE") or {}
        state = record.get("status")
        if state in TERMINAL:
            return
        if state == "DELIVERED":
            self.store.complete_delivery(job["id"], record["history"])
            return
        if state == "SENDING":
            if now <= record["leaseUntil"]:
                raise Retryable("delivery_in_progress")
            self.store.update_job(job["id"], status="UNKNOWN", result="worker_interrupted_during_send")
            return
        if now > job["deadline"]:
            self.store.update_job(job["id"], status="EXPIRED", result="deadline_exceeded")
            return
        record = self.store.claim(job["id"], now)
        if record is None:
            current = self.store.get(f"JOB#{job['id']}", "STATE") or {}
            if current.get("status") not in TERMINAL:
                raise Retryable("job_busy")
            return
        try:
            if job["action"] in ("draw", "auto"):
                self.deliver(job, record)
            else:
                self.manage(job)
        except UnknownDelivery:
            self.store.update_job(job["id"], status="UNKNOWN", result="discord_delivery_unknown")
            LOGGER.error("gatcha job=%s result=UNKNOWN", job["id"])
        except Retryable:
            current = self.store.get(f"JOB#{job['id']}", "STATE") or {}
            if current.get("status") != "DELIVERED":
                self.store.update_job(job["id"], status="RETRY")
            raise
        except (DiscordError, InvalidCommand) as error:
            # Only controlled text is returned; HTTP exception details are never echoed.
            text = str(error) if isinstance(error, InvalidCommand) else "Discordの権限・チャンネル設定を確認してください。"
            self.fail(job, text)
        except Exception:
            current = self.store.get(f"JOB#{job['id']}", "STATE") or {}
            if current.get("status") == "SENDING":
                self.store.update_job(job["id"], status="UNKNOWN", result="storage_or_worker_failure_after_send_started")
                LOGGER.error("gatcha job=%s result=UNKNOWN", job["id"])
                return
            if current.get("status") not in TERMINAL | {"DELIVERED"}:
                self.store.update_job(job["id"], status="RETRY")
            raise

    def fail(self, job, text):
        self.store.update_job(job["id"], status="FAILED", result="not_delivered")
        if job["action"] != "auto":
            self.discord.reply(job, {"content": text, "allowed_mentions": {"parse": []}})

    def text_channel(self, channel_id, guild_id):
        channel = self.discord.channel(channel_id)
        if channel.get("guild_id") != guild_id or channel.get("type") not in (0, 5):
            raise InvalidCommand("対象サーバーのテキストチャンネルを指定してください。スレッドは対象外です。")
        return channel

    def managed_put(self, job, pk, sk, **values):
        if not self.store.put_revision(pk, sk, job["id"], **values):
            raise InvalidCommand("より新しい管理操作が反映済みのため、この操作は適用しませんでした。")

    def manage(self, job):
        action, args = job["action"], job["args"]
        if job["guild_id"] != self.source_guild:
            raise InvalidCommand("管理操作はAITCサーバーで実行してください。")
        if action in ("channel_add", "channel_remove", "register", "remove"):
            channel_id = job["channel_id"]
            self.text_channel(channel_id, self.source_guild)
            if action.startswith("channel_"):
                self.managed_put(job, "SOURCES", f"CHANNEL#{channel_id}", channel_id=channel_id, active=action == "channel_add")
                text = "このチャンネルを登録元に追加しました。" if action == "channel_add" else "このチャンネルの作品を抽選対象から外しました。"
            elif action == "register":
                source = self.store.get("SOURCES", f"CHANNEL#{channel_id}") or {}
                if not source.get("active"):
                    raise InvalidCommand("先に gatcha channel add でこのチャンネルを登録してください。")
                message = self.discord.message(channel_id, args["message_id"])
                if message.get("channel_id") != channel_id or message.get("id") != args["message_id"]:
                    raise InvalidCommand("対象メッセージが一致しません。")
                if not message.get("content") and not message.get("attachments"):
                    raise InvalidCommand("この投稿には配信できる本文・添付ファイルがありません。")
                self.managed_put(job, "WORKS", f"MESSAGE#{args['message_id']}", message_id=args["message_id"], channel_id=channel_id,
                               active=True, source_url=source_url(self.source_guild, channel_id, args["message_id"]))
                text = "作品を手動抽選・自動投稿の対象として登録しました。"
            else:
                work = self.store.get("WORKS", f"MESSAGE#{args['message_id']}")
                if work is not None and work["channel_id"] != channel_id:
                    raise InvalidCommand("対象メッセージが一致しません。")
                if work is not None:
                    self.managed_put(job, "WORKS", f"MESSAGE#{args['message_id']}", **{
                        key: value for key, value in work.items() if key not in ("PK", "SK", "active", "revision")}, active=False)
                else:
                    self.managed_put(job, "WORKS", f"MESSAGE#{args['message_id']}", message_id=args["message_id"],
                                     channel_id=channel_id, active=False,
                                     source_url=source_url(self.source_guild, channel_id, args["message_id"]))
                text = "作品を抽選対象から外しました。"
        elif action in ("timer_config", "timer_remove"):
            guild_id = args["guild"]
            if guild_id not in self.guild_ids:
                raise InvalidCommand("投稿先は許可済みサーバーから指定してください。")
            if action == "timer_config":
                self.text_channel(args["channel"], guild_id)
                self.managed_put(job, "TIMERS", f"GUILD#{guild_id}", guild_id=guild_id, channel_id=args["channel"],
                                 slots=args["slots"], version=job["id"], active=True)
                text = "投稿先チャンネルと曜日・時刻を置き換えました（JST、週1枠）。"
            else:
                # A tombstone prevents an older queued config from recreating this timer.
                self.managed_put(job, "TIMERS", f"GUILD#{guild_id}", guild_id=guild_id, active=False, version=job["id"])
                text = "このサーバーの自動投稿を停止しました。"
        else:
            raise InvalidCommand("未対応の操作です。")
        self.discord.reply(job, {"content": text, "allowed_mentions": {"parse": []}})
        self.store.update_job(job["id"], status="SUCCESS")

    def timer_matches(self, job):
        timer = self.store.get("TIMERS", f"GUILD#{job['guild_id']}")
        return (timer and timer.get("active", True) and timer["version"] == job["timer_version"]
                and timer["channel_id"] == job["channel_id"])

    def deliver(self, job, record):
        mode = "AUTO" if job["action"] == "auto" else "MANUAL"
        guild, job_id = job["guild_id"], job["id"]
        if guild not in self.guild_ids:
            self.store.update_job(job_id, status="SKIPPED", result="guild_not_allowed")
            return
        if mode == "AUTO" and not self.timer_matches(job):
            self.store.update_job(job_id, status="SKIPPED", result="timer_changed")
            return
        self.store.lock(guild, mode, job_id, int(self.clock()))
        try:
            if mode == "MANUAL" and not self.store.cooldown(guild, job_id, int(self.clock())):
                self.fail(job, "このサーバーの抽選は30秒に1回です。少し待ってから実行してください。")
                return
            channels = {c["channel_id"] for c in self.store.query("SOURCES") if c.get("active")}
            works = self.store.query("WORKS")
            recent = [item["message_id"] for item in self.store.query(f"HISTORY#{mode}#{guild}", latest=10)]
            skipped = set(record.get("skipped", []))
            started = time.monotonic()
            while True:
                choices = candidates(works, channels, recent, skipped)
                if not choices:
                    self.fail(job, "現在配信できる登録作品がありません。")
                    return
                if time.monotonic() - started > 35:
                    raise Retryable("preparation_budget")
                work = choose(choices)
                try:
                    self.text_channel(work["channel_id"], self.source_guild)
                    message = self.discord.message(work["channel_id"], work["message_id"])
                    if not message.get("content") and not message.get("attachments"):
                        skipped.add(work["message_id"])
                        self.store.update_job(job_id, skipped=sorted(skipped))
                        continue
                    files = self.discord.attachments(message, int(job.get("attachment_limit", 24 * 1024 * 1024)))
                    member_id = self.lookup.resolve(message["author"]["id"])
                    outgoing = payload(message, files, self.profile_base, member_id)
                except DiscordError as error:
                    if error.status == 404 and error.code == 10008:
                        work["active"] = False
                        self.store.disable_work(work["SK"])
                    elif error.status not in (403, 404):
                        raise
                    skipped.add(work["message_id"])
                    self.store.update_job(job_id, skipped=sorted(skipped))
                    continue
                except TooLarge:
                    skipped.add(work["message_id"])
                    self.store.update_job(job_id, skipped=sorted(skipped))
                    continue
                # Recheck changes made while attachments were being prepared.
                source = self.store.get("SOURCES", f"CHANNEL#{work['channel_id']}") or {}
                current_work = self.store.get("WORKS", work["SK"]) or {}
                if not source.get("active") or not current_work.get("active"):
                    skipped.add(work["message_id"])
                    continue
                if mode == "AUTO" and not self.timer_matches(job):
                    self.store.update_job(job_id, status="SKIPPED", result="timer_changed")
                    return
                if int(self.clock()) > job["deadline"]:
                    self.store.update_job(job_id, status="EXPIRED")
                    return
                history = {"guild_id": guild, "channel_id": job["channel_id"], "mode": mode,
                           "message_id": work["message_id"], "source_url": work["source_url"],
                           "timestamp_ms": int(self.clock() * 1000),
                           "posted_at": datetime.fromtimestamp(self.clock(), timezone.utc).isoformat()}
                # This durable boundary precedes every write that could publish a work.
                self.store.update_job(job_id, status="SENDING", history=history)
                try:
                    sent = self.discord.post(job, outgoing, files) if mode == "AUTO" else self.discord.reply(job, outgoing, files)
                except TooLarge:
                    skipped.add(work["message_id"])
                    self.store.update_job(job_id, status="RUNNING", skipped=sorted(skipped))
                    continue
                history["posted_message_id"] = sent["id"]
                self.store.update_job(job_id, status="DELIVERED", history=history)
                self.store.complete_delivery(job_id, history)
                return
        finally:
            self.store.unlock(guild, mode, job_id)
