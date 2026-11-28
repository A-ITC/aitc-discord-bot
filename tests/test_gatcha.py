import copy
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sys
import unittest
from unittest.mock import Mock, patch
from botocore.credentials import Credentials
from botocore.exceptions import ClientError

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'lambda'))
from discord_bot.config import Config
from discord_bot.gatcha.domain import (ADMIN_ROLE_ID, InvalidCommand, Retryable, TooLarge,
                                       UnknownDelivery, candidates, command)
from discord_bot.gatcha.service import Service
from discord_bot.gatcha.store import Store
from discord_bot.gatcha.transport import Discord, DiscordError, MemberLookup
from discord_bot.gatcha.render import payload
from discord_bot.gatcha import runtime

SOURCE = '222222222222222222'
DEST = '222222222222222223'
CHANNEL = '333333333333333333'
APP = '111111111111111111'
AUTHOR = '555555555555555555'
SENT = '666666666666666666'
NOW = 1000000


class MemoryStore:
    def __init__(self):
        self.items, self.locks, self.cooldowns = {}, {}, {}
        self.table = Mock()
        self.table.delete_item.side_effect = lambda Key: self.items.pop((Key['PK'], Key['SK']), None)

    def put(self, pk, sk, **values):
        self.items[pk, sk] = copy.deepcopy(dict(PK=pk, SK=sk, **values))

    def get(self, pk, sk):
        return copy.deepcopy(self.items.get((pk, sk)))

    def put_revision(self, pk, sk, revision, **values):
        revision = str(revision).zfill(20)
        old = self.items.get((pk, sk), {})
        if old.get('revision', '') > revision:
            return False
        self.put(pk, sk, **{**values, 'revision': revision})
        return True

    def disable_work(self, sk):
        self.items['WORKS', sk]['active'] = False

    def query(self, pk, *, latest=None):
        values = [copy.deepcopy(value) for (key, _), value in self.items.items() if key == pk]
        return sorted(values, key=lambda value: value['SK'], reverse=True)[:latest] if latest else values

    def update_job(self, job_id, **values):
        key = (f'JOB#{job_id}', 'STATE')
        self.items.setdefault(key, {}).update(copy.deepcopy(values))

    def claim(self, job_id, now):
        record = self.get(f'JOB#{job_id}', 'STATE') or {}
        if record.get('status') not in (None, 'RETRY'):
            return None
        self.update_job(job_id, status='RUNNING', leaseUntil=now + 80)
        return self.get(f'JOB#{job_id}', 'STATE')

    def lock(self, guild, mode, job_id, now):
        if (guild, mode) in self.locks:
            raise Retryable('busy')
        self.locks[guild, mode] = job_id

    def unlock(self, guild, mode, job_id):
        self.locks.pop((guild, mode), None)

    def cooldown(self, guild, job_id, now):
        until, owner = self.cooldowns.get(guild, (0, None))
        if until > now and owner != job_id:
            return False
        self.cooldowns[guild] = (now + 30, job_id)
        return True

    def complete_delivery(self, job_id, history):
        self.put(f"HISTORY#{history['mode']}#{history['guild_id']}", f"{history['timestamp_ms']:016d}#{job_id}", **history)
        self.update_job(job_id, status='SUCCESS')


def interaction(name='gatcha', options=None, *, guild=SOURCE, roles=()):
    return {'id': '777777777777777777', 'type': 2, 'application_id': APP,
            'guild_id': guild, 'channel_id': CHANNEL, 'token': 'test-token',
            'member': {'roles': list(roles)},
            'data': {'type': 1, 'name': name, 'options': options or []}}


def job(action='draw', job_id='job1', guild=DEST, **extra):
    return {'id': job_id, 'action': action, 'guild_id': guild, 'channel_id': CHANNEL,
            'created_at': NOW, 'deadline': NOW + 300, 'args': {}, 'token': 'test-token',
            'application_id': APP, **extra}


def add_work(store, i=0):
    message_id = str(444444444444444440 + i)
    store.put('WORKS', f'MESSAGE#{message_id}', message_id=message_id, channel_id=CHANNEL,
              active=True, source_url=f'https://discord.com/channels/{SOURCE}/{CHANNEL}/{message_id}')
    return message_id


class CommandTests(unittest.TestCase):
    def test_draw_is_open_to_all_allowed_guild_members(self):
        value = command(interaction(options=[{'type': 1, 'name': 'draw'}], guild=DEST), SOURCE, {SOURCE, DEST}, NOW)
        self.assertEqual(value['action'], 'draw')

    def test_management_requires_exact_role_and_source_guild(self):
        options = [{'type': 2, 'name': 'channel', 'options': [{'type': 1, 'name': 'add'}]}]
        for guild, roles in ((SOURCE, ()), (DEST, (ADMIN_ROLE_ID,))):
            with self.subTest(guild=guild), self.assertRaises(InvalidCommand):
                command(interaction(options=options, guild=guild, roles=roles), SOURCE, {SOURCE, DEST}, NOW)
        value = command(interaction(options=options, roles=(ADMIN_ROLE_ID,)), SOURCE, {SOURCE, DEST}, NOW)
        self.assertEqual(value['action'], 'channel_add')

    def test_context_commands_have_the_same_role_check(self):
        request = interaction(roles=(ADMIN_ROLE_ID,))
        request['data'] = {'type': 3, 'name': 'gatcha register', 'target_id': '444444444444444444'}
        self.assertEqual(command(request, SOURCE, {SOURCE}, NOW)['action'], 'register')
        request['member']['roles'] = []
        with self.assertRaises(InvalidCommand):
            command(request, SOURCE, {SOURCE}, NOW)

    def timer_request(self, day='MON', hour=9):
        options = [
            {'type': 3, 'name': 'guild', 'value': DEST},
            {'type': 3, 'name': 'channel', 'value': CHANNEL},
            {'type': 3, 'name': 'day', 'value': day},
            {'type': 4, 'name': 'hour', 'value': hour},
        ]
        return interaction(options=[{'type': 2, 'name': 'timer', 'options': [
            {'type': 1, 'name': 'config', 'options': options}]}], roles=(ADMIN_ROLE_ID,))

    def test_timer_choices_cover_each_weekday_and_whole_hour(self):
        definition = json.loads((Path(__file__).resolve().parents[1] / 'commands/gatcha.json').read_text(encoding='utf-8'))
        timer = next(group for group in definition['options'] if group['name'] == 'timer')
        config = next(action for action in timer['options'] if action['name'] == 'config')
        options = {option['name']: option for option in config['options']}
        self.assertEqual(set(options), {'guild', 'channel', 'day', 'hour'})
        self.assertTrue(all(option['required'] for option in options.values()))
        self.assertEqual(options['day']['type'], 3)
        self.assertEqual(options['hour']['type'], 4)
        self.assertEqual(options['day']['choices'], [
            {'name': label, 'value': day} for label, day in zip(
                ['月曜', '火曜', '水曜', '木曜', '金曜', '土曜', '日曜'],
                ['MON', 'TUE', 'WED', 'THU', 'FRI', 'SAT', 'SUN'])])
        self.assertEqual(options['hour']['choices'], [
            {'name': f'{hour:02d}:00', 'value': hour} for hour in range(24)])
        for day_index, choice in enumerate(options['day']['choices']):
            for hour_choice in options['hour']['choices']:
                with self.subTest(day=choice['value'], hour=hour_choice['value']):
                    result = command(self.timer_request(choice['value'], hour_choice['value']), SOURCE, {SOURCE, DEST}, NOW)
                    self.assertEqual(result['action'], 'timer_config')
                    self.assertEqual(result['args'], {'guild': DEST, 'channel': CHANNEL,
                                     'slots': [{'day': day_index, 'hour': hour_choice['value']}]})

    def test_timer_rejects_invalid_choices_and_option_types(self):
        invalid = [('day', value) for value in ('MON,FRI', 'UNKNOWN', '', None, 0)]
        invalid += [('hour', value) for value in (-1, 24, '09', '09:30', 9.5, True, None)]
        for name, value in invalid:
            with self.subTest(name=name, value=value), self.assertRaises(InvalidCommand):
                command(self.timer_request(**{name: value}), SOURCE, {SOURCE, DEST}, NOW)
        request = self.timer_request()
        request['data']['options'][0]['options'][0]['options'][-1]['type'] = 3
        with self.assertRaises(InvalidCommand):
            command(request, SOURCE, {SOURCE, DEST}, NOW)

    def test_timer_requires_both_choices_and_rejects_legacy_schedule(self):
        for name in ('day', 'hour'):
            request = self.timer_request()
            options = request['data']['options'][0]['options'][0]['options']
            options[:] = [option for option in options if option['name'] != name]
            with self.subTest(missing=name), self.assertRaises(InvalidCommand):
                command(request, SOURCE, {SOURCE, DEST}, NOW)
        request = self.timer_request()
        request['data']['options'][0]['options'][0]['options'][2:] = [
            {'type': 3, 'name': 'schedule', 'value': 'MON@09,FRI@18'}]
        with self.assertRaises(InvalidCommand):
            command(request, SOURCE, {SOURCE, DEST}, NOW)

    def test_timer_choices_still_require_source_guild_role_and_allowed_destination(self):
        requests = [self.timer_request() for _ in range(3)]
        requests[0]['member']['roles'] = []
        requests[1]['guild_id'] = DEST
        requests[2]['data']['options'][0]['options'][0]['options'][0]['value'] = '999999999999999999'
        for request in requests:
            with self.subTest(request=request['data']), self.assertRaises(InvalidCommand):
                command(request, SOURCE, {SOURCE, DEST}, NOW)

    def test_history_threshold_is_ten_and_disabled_channels_do_not_count(self):
        works = [{'message_id': str(i), 'channel_id': CHANNEL, 'active': True} for i in range(11)]
        recent = [str(i) for i in range(10)]
        self.assertEqual(len(candidates(works[:10], {CHANNEL}, recent)), 10)
        self.assertEqual([w['message_id'] for w in candidates(works, {CHANNEL}, recent)], ['10'])
        self.assertEqual(candidates(works, set(), recent), [])

    def test_intake_defers_public_draw_and_delays_worker_until_ack(self):
        request = interaction(options=[{'type': 1, 'name': 'draw'}])
        queue = Mock()
        with patch.object(runtime, 'settings', return_value=(SOURCE, 'api123', 'ap-northeast-1', 'https://a-i-t-c.com')), \
             patch.object(runtime, 'aws', return_value=(None, queue, None)), patch.dict(os.environ, GATCHA_QUEUE_URL='queue'):
            result = runtime.accept(request, Config(APP, b'0' * 32, frozenset({SOURCE})))
        self.assertEqual(result, {'type': 5, 'data': {'flags': 0}})
        self.assertEqual(queue.send_message.call_args.kwargs['DelaySeconds'], 3)

    def test_signed_http_gatcha_call_reaches_queue(self):
        from test_interactions import app, event_for
        queue = Mock()
        request = interaction(options=[{'type': 1, 'name': 'draw'}])
        with patch.object(runtime, 'settings', return_value=(SOURCE, 'api123', 'ap-northeast-1', 'https://a-i-t-c.com')), \
             patch.object(runtime, 'aws', return_value=(None, queue, None)), patch.dict(os.environ, GATCHA_QUEUE_URL='queue'):
            response = app.lambda_handler(event_for(request), Mock(aws_request_id='request'))
        self.assertEqual(response['statusCode'], 200)
        self.assertEqual(json.loads(response['body'])['type'], 5)
        self.assertEqual(queue.send_message.call_count, 1)


class DeliveryTests(unittest.TestCase):
    def setUp(self):
        self.store = MemoryStore()
        self.store.put('SOURCES', f'CHANNEL#{CHANNEL}', channel_id=CHANNEL, active=True)
        self.discord, self.lookup = Mock(), Mock()
        self.discord.channel.side_effect = lambda channel: {'id': channel, 'guild_id': SOURCE, 'type': 0}
        self.discord.message.side_effect = lambda channel, message: {
            'id': message, 'channel_id': channel, 'content': '最新の作品本文', 'attachments': [],
            'author': {'id': AUTHOR, 'username': 'alice', 'avatar': None}}
        self.discord.attachments.return_value = []
        self.discord.reply.return_value = self.discord.post.return_value = {'id': SENT}
        self.lookup.resolve.return_value = 'alice'
        self.service = Service(self.store, self.discord, self.lookup, SOURCE, {SOURCE, DEST}, 'https://a-i-t-c.com', clock=lambda: NOW)
        self.selection = patch('discord_bot.gatcha.service.choose', side_effect=lambda values: values[0])
        self.selection.start()
        self.addCleanup(self.selection.stop)

    def state(self, identifier='job1'):
        return self.store.get(f'JOB#{identifier}', 'STATE')['status']

    def timer(self, version='v1'):
        self.store.put('TIMERS', f'GUILD#{DEST}', guild_id=DEST, channel_id=CHANNEL, version=version, slots=[{'day': 0, 'hour': 9}])

    def test_duplicate_job_posts_and_records_history_only_once(self):
        add_work(self.store)
        self.service.process(job())
        self.service.process(job())
        self.assertEqual(self.discord.reply.call_count, 1)
        history = self.store.query(f'HISTORY#MANUAL#{DEST}')
        self.assertEqual(len(history), 1)
        self.assertEqual(history[0]['posted_message_id'], SENT)
        self.assertEqual(self.state(), 'SUCCESS')

    def test_manual_and_automatic_histories_are_independent(self):
        ids = [add_work(self.store, i) for i in range(11)]
        for i, identifier in enumerate(ids[:10]):
            self.store.put(f'HISTORY#MANUAL#{DEST}', f'{i:016d}#old', message_id=identifier)
        self.timer()
        self.service.process(job('auto', timer_version='v1'))
        self.assertEqual(self.store.query(f'HISTORY#AUTO#{DEST}')[0]['message_id'], ids[0])
        self.service.process(job(job_id='manual'))
        self.assertEqual(self.store.query(f'HISTORY#MANUAL#{DEST}', latest=1)[0]['message_id'], ids[10])

    def test_cooldown_is_shared_between_users_and_channels_in_a_guild(self):
        add_work(self.store)
        self.service.process(job())
        self.service.process(job(job_id='other-user'))
        self.assertEqual(len(self.store.query(f'HISTORY#MANUAL#{DEST}')), 1)
        self.assertEqual(self.state('other-user'), 'FAILED')

    def test_deleted_message_is_retained_disabled_and_redrawn(self):
        first, second = add_work(self.store), add_work(self.store, 1)
        normal = self.discord.message.side_effect
        self.discord.message.side_effect = lambda channel, identifier: (_ for _ in ()).throw(DiscordError(404, 10008)) if identifier == first else normal(channel, identifier)
        self.service.process(job())
        self.assertFalse(self.store.get('WORKS', f'MESSAGE#{first}')['active'])
        self.assertEqual(self.store.query(f'HISTORY#MANUAL#{DEST}')[0]['message_id'], second)

    def test_oversize_is_only_skipped_for_this_draw(self):
        first, second = add_work(self.store), add_work(self.store, 1)
        self.discord.attachments.side_effect = [TooLarge(), []]
        self.service.process(job())
        self.assertTrue(self.store.get('WORKS', f'MESSAGE#{first}')['active'])
        self.assertEqual(self.store.query(f'HISTORY#MANUAL#{DEST}')[0]['message_id'], second)

    def test_send_rejection_for_size_redraws_without_persistent_disabling(self):
        first, second = add_work(self.store), add_work(self.store, 1)
        self.discord.reply.side_effect = [TooLarge(), {'id': SENT}]
        self.service.process(job())
        self.assertEqual(self.store.query(f'HISTORY#MANUAL#{DEST}')[0]['message_id'], second)
        self.assertTrue(self.store.get('WORKS', f'MESSAGE#{first}')['active'])

    def test_unknown_delivery_is_never_resent_or_added_to_success_history(self):
        add_work(self.store)
        self.discord.reply.side_effect = UnknownDelivery()
        self.service.process(job())
        self.service.process(job())
        self.assertEqual(self.discord.reply.call_count, 1)
        self.assertEqual(self.state(), 'UNKNOWN')
        self.assertEqual(self.store.query(f'HISTORY#MANUAL#{DEST}'), [])

    def test_worker_interrupted_during_send_becomes_unknown_after_lease(self):
        self.store.update_job('job1', status='SENDING', leaseUntil=NOW - 1)
        self.service.process(job())
        self.assertEqual(self.state(), 'UNKNOWN')
        self.discord.reply.assert_not_called()

    def test_completed_send_recovers_history_after_storage_failure_without_resend(self):
        add_work(self.store)
        complete = self.store.complete_delivery
        self.store.complete_delivery = Mock(side_effect=[Retryable(), None])
        with self.assertRaises(Retryable):
            self.service.process(job())
        # DELIVERED is recoverable and must never be changed back to RETRY.
        self.assertEqual(self.state(), 'DELIVERED')
        self.store.complete_delivery = complete
        self.service.process(job())
        self.assertEqual(self.discord.reply.call_count, 1)
        self.assertEqual(len(self.store.query(f'HISTORY#MANUAL#{DEST}')), 1)

    def test_auto_deadline_and_timer_change_prevent_late_or_stale_posts(self):
        add_work(self.store)
        self.timer('new')
        self.service.process(job('auto', timer_version='old'))
        self.assertEqual(self.state(), 'SKIPPED')
        self.service.process(job('auto', job_id='late', timer_version='new', deadline=NOW - 1))
        self.assertEqual(self.state('late'), 'EXPIRED')
        self.discord.post.assert_not_called()

    def test_channel_removal_preserves_registered_works(self):
        identifier = add_work(self.store)
        self.store.put('SOURCES', f'CHANNEL#{CHANNEL}', channel_id=CHANNEL, active=False)
        self.service.process(job())
        self.assertEqual(self.state(), 'FAILED')
        self.assertTrue(self.store.get('WORKS', f'MESSAGE#{identifier}')['active'])

    def test_threads_cannot_be_registered_as_sources(self):
        self.discord.channel.return_value = {'id': CHANNEL, 'guild_id': SOURCE, 'type': 11}
        self.discord.channel.side_effect = None
        self.service.process(job('channel_add', guild=SOURCE))
        self.assertEqual(self.state(), 'FAILED')

    def test_missing_web_member_still_delivers_work_without_profile_button(self):
        add_work(self.store)
        self.lookup.resolve.return_value = None
        self.service.process(job())
        outgoing = self.discord.reply.call_args.args[1]
        self.assertEqual(len(outgoing['components'][0]['components']), 2)
        self.assertEqual(self.state(), 'SUCCESS')

    def test_timer_config_replaces_all_slots_and_timer_remove_stops_it(self):
        self.discord.channel.side_effect = lambda channel: {'id': channel, 'guild_id': DEST, 'type': 0}
        self.store.put('TIMERS', f'GUILD#{DEST}', guild_id=DEST, channel_id=CHANNEL,
                       version='old', slots=[{'day': 0, 'hour': 9}, {'day': 4, 'hour': 18}])
        slots = [{'day': 6, 'hour': 23}]
        self.service.process(job('timer_config', guild=SOURCE, args={'guild': DEST, 'channel': CHANNEL, 'slots': slots}))
        configured = self.store.get('TIMERS', f'GUILD#{DEST}')
        self.assertEqual(configured['slots'], slots)
        self.assertEqual(configured['version'], 'job1')
        self.assertIn('週1枠', self.discord.reply.call_args.args[1]['content'])
        self.service.process(job('timer_remove', job_id='remove', guild=SOURCE, args={'guild': DEST}))
        self.assertFalse(self.store.get('TIMERS', f'GUILD#{DEST}')['active'])
        self.assertNotIn('slots', self.store.get('TIMERS', f'GUILD#{DEST}'))

    def test_register_requires_channel_allowlist_and_retains_url(self):
        identifier = '444444444444444444'
        self.service.process(job('register', guild=SOURCE, args={'message_id': identifier}))
        work = self.store.get('WORKS', f'MESSAGE#{identifier}')
        self.assertTrue(work['active'])
        self.assertTrue(work['source_url'].endswith(identifier))
        self.store.put('SOURCES', f'CHANNEL#{CHANNEL}', channel_id=CHANNEL, active=False)
        self.service.process(job('register', job_id='denied', guild=SOURCE, args={'message_id': identifier}))
        self.assertEqual(self.state('denied'), 'FAILED')

    def test_busy_guild_is_retried_before_consuming_cooldown(self):
        self.store.locks[DEST, 'MANUAL'] = 'another-job'
        with self.assertRaises(Retryable):
            self.service.process(job())
        self.assertEqual(self.state(), 'RETRY')
        self.assertEqual(self.store.cooldowns, {})
        self.discord.reply.assert_not_called()

    def test_temporary_read_failure_retries_without_disabling_work(self):
        identifier = add_work(self.store)
        self.discord.message.side_effect = Retryable('read timeout')
        with self.assertRaises(Retryable):
            self.service.process(job())
        self.assertTrue(self.store.get('WORKS', f'MESSAGE#{identifier}')['active'])
        self.assertEqual(self.state(), 'RETRY')
        self.discord.reply.assert_not_called()

    def test_empty_or_unreadable_content_does_not_publish_header_only(self):
        add_work(self.store)
        self.discord.message.side_effect = lambda channel, identifier: {'id': identifier, 'content': '', 'attachments': []}
        self.service.process(job())
        self.assertEqual(self.store.query(f'HISTORY#MANUAL#{DEST}'), [])
        self.assertNotIn('components', self.discord.reply.call_args.args[1])

    def test_older_queued_config_cannot_restore_a_removed_timer(self):
        self.service.process(job('timer_remove', job_id='777777777777777779', guild=SOURCE, args={'guild': DEST}))
        self.discord.channel.side_effect = lambda channel: {'id': channel, 'guild_id': DEST, 'type': 0}
        self.service.process(job('timer_config', job_id='777777777777777778', guild=SOURCE,
                                 args={'guild': DEST, 'channel': CHANNEL, 'slots': [{'day': 0, 'hour': 9}]}))
        self.assertFalse(self.store.get('TIMERS', f'GUILD#{DEST}')['active'])

    def test_older_channel_add_cannot_undo_newer_channel_removal(self):
        self.service.process(job('channel_remove', job_id='777777777777777779', guild=SOURCE))
        self.service.process(job('channel_add', job_id='777777777777777778', guild=SOURCE))
        self.assertFalse(self.store.get('SOURCES', f'CHANNEL#{CHANNEL}')['active'])

    def test_remove_before_queued_register_keeps_work_excluded(self):
        identifier = '444444444444444444'
        self.service.process(job('remove', job_id='777777777777777779', guild=SOURCE, args={'message_id': identifier}))
        self.service.process(job('register', job_id='777777777777777778', guild=SOURCE, args={'message_id': identifier}))
        self.assertFalse(self.store.get('WORKS', f'MESSAGE#{identifier}')['active'])


class ProtocolTests(unittest.TestCase):
    def test_file_and_image_components_reference_uploaded_files(self):
        message = {'author': {'id': AUTHOR, 'username': 'alice'}, 'content': '作品 @everyone'}
        files = [{'filename': 'image.png', 'image': True, 'spoiler': False},
                 {'filename': 'audio.mp3', 'image': False, 'spoiler': True}]
        result = payload(message, files, 'https://a-i-t-c.com', 'alice')
        self.assertEqual(result['flags'], 32768)
        self.assertEqual(result['allowed_mentions'], {'parse': []})
        parts = result['components'][0]['components']
        self.assertEqual(parts[2]['items'][0]['media']['url'], 'attachment://image.png')
        self.assertEqual(parts[3]['file']['url'], 'attachment://audio.mp3')
        self.assertTrue(parts[3]['spoiler'])
        self.assertTrue(parts[-1]['accessory']['url'].endswith('profile?id=alice'))

    def test_transport_distinguishes_uncertain_writes_and_retryable_reads(self):
        client = Discord('private-token')
        with patch('discord_bot.gatcha.transport.request', return_value=(503, b'{}')):
            with self.assertRaises(UnknownDelivery):
                client.call('POST', '/channels/x/messages', {'content': 'work'})
            with self.assertRaises(Retryable):
                client.call('GET', '/channels/x/messages/y')

    def test_large_metadata_skips_download_and_untrusted_hosts_are_rejected(self):
        client = Discord('token')
        with patch('discord_bot.gatcha.transport.request') as network:
            with self.assertRaises(TooLarge):
                client.attachments({'attachments': [{'size': 100}]}, 10)
            with self.assertRaises(Retryable):
                client.attachments({'attachments': [{'size': 1, 'url': 'https://example.com/evil'}]})
            network.assert_not_called()

    def test_history_and_terminal_state_are_written_in_one_transaction(self):
        table, client = Mock(), Mock()
        table.name = 'gatcha'
        store = Store(table, client)
        history = {'mode': 'MANUAL', 'guild_id': DEST, 'message_id': 'work', 'timestamp_ms': NOW * 1000}
        store.complete_delivery('job', history)
        actions = client.transact_write_items.call_args.kwargs['TransactItems']
        self.assertEqual(len(actions), 2)
        self.assertEqual(actions[0]['Put']['Item']['PK'], {'S': f'HISTORY#MANUAL#{DEST}'})
        self.assertIn('Update', actions[1])

    def test_scheduler_uses_jst_and_deterministic_job_ids(self):
        stamp = datetime(2026, 11, 22, 15, tzinfo=timezone.utc)
        store, queue = MemoryStore(), Mock()
        store.put('TIMERS', f'GUILD#{DEST}', guild_id=DEST, channel_id=CHANNEL,
                  slots=[{'day': 0, 'hour': 0}], version='v1')
        with patch.object(runtime, 'settings', return_value=(SOURCE, 'api', 'ap-northeast-1', 'https://a-i-t-c.com')), \
             patch.object(runtime, 'aws', return_value=(store, queue, None)), \
             patch.object(runtime.time, 'time', return_value=stamp.timestamp() + 30), \
             patch.dict(os.environ, ALLOWED_GUILD_IDS=f'{SOURCE},{DEST}', GATCHA_QUEUE_URL='queue'):
            for _ in range(2):
                self.assertEqual(runtime.schedule_handler({'scheduled_time': stamp.isoformat()}, None)['queued'], 1)
        jobs = [json.loads(call.kwargs['MessageBody']) for call in queue.send_message.call_args_list]
        self.assertEqual(jobs[0]['id'], jobs[1]['id'])
        self.assertEqual(jobs[0]['deadline'], int(stamp.timestamp()) + 300)

    def test_only_conditional_conflicts_are_treated_as_duplicate_claims(self):
        table = Mock()
        table.update_item.side_effect = ClientError({'Error': {'Code': 'ConditionalCheckFailedException'}}, 'UpdateItem')
        self.assertIsNone(Store(table, Mock()).claim('job', NOW))
        table.update_item.side_effect = ClientError({'Error': {'Code': 'AccessDeniedException'}}, 'UpdateItem')
        with self.assertRaises(ClientError):
            Store(table, Mock()).claim('job', NOW)

    def test_lookup_is_sigv4_signed_and_only_404_omits_profile(self):
        session = Mock()
        session.get_credentials.return_value = Credentials('test-key', 'test-secret', 'session-token')
        lookup = MemberLookup(session, 'abc123', 'ap-northeast-1')
        with patch('discord_bot.gatcha.transport.request', return_value=(200, b'{"id":"alice"}')) as network:
            self.assertEqual(lookup.resolve(AUTHOR), 'alice')
            host, method, path, headers = network.call_args.args
            self.assertEqual(host, 'abc123.execute-api.ap-northeast-1.amazonaws.com')
            self.assertEqual(path, '/members-only/members/discord?id=' + AUTHOR)
            self.assertIn('AWS4-HMAC-SHA256', headers['Authorization'])
        with patch('discord_bot.gatcha.transport.request', return_value=(404, b'{}')):
            self.assertIsNone(lookup.resolve(AUTHOR))
        with patch('discord_bot.gatcha.transport.request', return_value=(503, b'{}')), self.assertRaises(Retryable):
            lookup.resolve(AUTHOR)

    def test_webhook_multipart_contains_real_files_without_bot_authorization(self):
        file = {'filename': 'audio.mp3', 'data': b'audio-data'}
        with patch('discord_bot.gatcha.transport.request', return_value=(200, b'{"id":"123"}')) as network:
            Discord('private-bot-token').reply(job(), {'flags': 32768, 'components': []}, [file])
        headers, body = network.call_args.args[3:5]
        self.assertNotIn('Authorization', headers)
        self.assertIn(b'audio-data', body)
        self.assertIn(b'files[0]', body)
        self.assertIn(b'"filename": "audio.mp3"', body)


if __name__ == '__main__':
    unittest.main()
