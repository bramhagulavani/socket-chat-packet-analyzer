"""
tests/test_targeted_send.py
===========================
Unit tests for the "choose the recipient before you send" rule.

These cover the decision layer only -- the roster lookup, the command parser and
the verdict hand-off -- so they need no sockets.  Run with:

    python -m unittest discover tests -v
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from common_info import (
    FileVerdict,
    describe_client,
    format_ack,
    format_file_header,
    format_roster_names,
    format_roster_table,
    format_send_error,
    format_send_verdict,
    normalize_target,
    parse_ctrl,
    parse_file_opts,
    parse_label,
    parse_roster_peers,
    parse_send_command,
    render_connected,
    render_help,
    render_roster,
    render_room_notice,
    render_target_label,
    resolve_target,
    target_token,
)


def roster(*specs):
    """[(id, name), ...] -> client records, the shape both servers store."""
    return [{'id': i, 'name': n, 'addr': ('127.0.0.1', 9000 + i), 'joined_at': 0}
            for i, n in specs]


class NormalizeTargetTests(unittest.TestCase):
    def test_accepted_spellings(self):
        self.assertEqual(normalize_target(None), ('all', None))
        self.assertEqual(normalize_target(''), ('all', None))
        self.assertEqual(normalize_target('all'), ('all', None))
        self.assertEqual(normalize_target('#3'), ('id', 3))
        self.assertEqual(normalize_target('3'), ('id', 3))
        self.assertEqual(normalize_target('@Kite'), ('name', 'Kite'))
        self.assertEqual(normalize_target('Kite'), ('name', 'Kite'))

    def test_nonsense_is_not_a_target(self):
        # A target with a space in it could never survive the control-message
        # format, so it must not be mistaken for a broadcast.
        self.assertEqual(normalize_target('@Big Name'), (None, None))


class ResolveTargetTests(unittest.TestCase):
    def setUp(self):
        self.clients = roster((1, 'Falcon'), (2, 'Otter'), (3, 'Kite'))

    def resolve(self, spec, sender_id=1, other_count=2):
        return resolve_target(self.clients, sender_id, normalize_target(spec),
                              other_count)

    def test_broadcast_reaches_everyone_else(self):
        record, error = self.resolve(None)
        self.assertIsNone(record)          # None means "the whole room"
        self.assertIsNone(error)

        record, error = self.resolve('all')
        self.assertIsNone(record)
        self.assertIsNone(error)

    def test_target_by_id_resolves_to_exactly_one_client(self):
        record, error = self.resolve('#2')
        self.assertIsNone(error)
        self.assertEqual(record['id'], 2)
        self.assertEqual(record['name'], 'Otter')

    def test_target_by_name_is_case_insensitive(self):
        record, error = self.resolve('@otter')
        self.assertIsNone(error)
        self.assertEqual(record['id'], 2)

    def test_target_is_the_only_recipient(self):
        # A resolved record means exactly one recipient; the caller relays to
        # [record] alone, never to the full roster.
        record, error = self.resolve('#3')
        self.assertIsNotNone(record)
        self.assertNotEqual(record['id'], 1)

    def test_self_target_is_refused(self):
        record, error = self.resolve('#1')
        self.assertIsNone(record)
        self.assertEqual(error, ('self_target', '1'))

        record, error = self.resolve('@Falcon')
        self.assertIsNone(record)
        self.assertEqual(error, ('self_target', 'Falcon'))

    def test_unknown_target_is_refused(self):
        record, error = self.resolve('#99')
        self.assertIsNone(record)
        self.assertEqual(error, ('unknown_target', '99'))

        record, error = self.resolve('@Zebu')
        self.assertIsNone(record)
        self.assertEqual(error, ('unknown_target', 'Zebu'))

    def test_name_prefix_does_not_match(self):
        # '@Kite' must not silently pick Kite-2 out of a wrapped name pool.
        record, error = resolve_target(roster((1, 'Falcon'), (2, 'Kite-2')),
                                       1, normalize_target('@Kite'), 1)
        self.assertIsNone(record)
        self.assertEqual(error, ('unknown_target', 'Kite'))

    def test_ambiguous_name_is_refused(self):
        # pick_name() can produce Falcon and Falcon-2 at once, so an exact
        # lowercase match can legitimately be a tie -- refuse instead of guessing.
        record, error = resolve_target(roster((1, 'Me'), (2, 'Kite'), (3, 'Kite')),
                                       1, normalize_target('@Kite'), 2)
        self.assertIsNone(record)
        self.assertEqual(error, ('ambiguous_target', 'Kite'))

    def test_broadcast_with_nobody_else_is_refused(self):
        record, error = resolve_target(roster((1, 'Falcon')), 1, None, 0)
        self.assertIsNone(record)
        self.assertEqual(error, ('no_recipients', ''))

    def test_refusals_carry_a_space_free_argument(self):
        # /ERR travels as a whitespace-delimited control message, so the arg has
        # to survive that format intact.
        for spec in ('#99', '@Zebu', '#1'):
            _record, error = self.resolve(spec)
            _code, arg = error
            self.assertNotIn(' ', str(arg))


class LabelTests(unittest.TestCase):
    def test_token_round_trip(self):
        record = {'id': 7, 'name': 'Ibex'}
        self.assertEqual(target_token(record), 'Ibex#7')
        self.assertEqual(parse_label('Ibex#7'), ('Ibex', 7))
        self.assertEqual(parse_label(target_token(None)), (None, None))

    def test_render(self):
        self.assertEqual(render_target_label('Ibex#7'), '@Ibex (#7)')
        self.assertEqual(render_target_label('7'), '#7')
        self.assertEqual(render_target_label('all'), 'everyone')

    def test_header_carries_attribution_and_target(self):
        sender = {'id': 1, 'name': 'Falcon'}
        header = format_file_header('notes.txt', 2048, sender, 'Otter#2')
        self.assertEqual(header, '/FILE notes.txt 2048 from=Falcon#1 to=Otter#2')

        options = parse_file_opts(header.split(' ')[3:])
        self.assertEqual(options['from'], 'Falcon#1')
        self.assertEqual(options['to'], 'Otter#2')

    def test_broadcast_header_has_no_to_field(self):
        # A broadcast is identified by the ABSENCE of to= -- there is no
        # 'to=all' spelling that could be confused for a private transfer.
        header = format_file_header('notes.txt', 10, {'id': 1, 'name': 'Falcon'})
        self.assertNotIn('to=', header)
        self.assertIn('from=Falcon#1', header)   # attribution still travels

    def test_header_without_a_sender_is_minimal(self):
        self.assertEqual(format_file_header('notes.txt', 10), '/FILE notes.txt 10')


class SendCommandTests(unittest.TestCase):
    def test_plain_sendfile_is_a_broadcast(self):
        self.assertEqual(parse_send_command('/sendfile notes.txt'),
                         ('notes.txt', None, None))

    def test_target_suffix(self):
        self.assertEqual(parse_send_command('/sendfile notes.txt to=#2'),
                         ('notes.txt', '#2', None))
        self.assertEqual(parse_send_command('/sendfile notes.txt to=@Otter'),
                         ('notes.txt', '@Otter', None))

    def test_path_with_spaces_and_spaced_out_selector(self):
        self.assertEqual(parse_send_command('/sendfile my notes.txt to = #2'),
                         ('my notes.txt', '#2', None))

    def test_sendto_alias(self):
        self.assertEqual(parse_send_command('/sendto #2 notes.txt'),
                         ('notes.txt', '#2', None))

    def test_missing_arguments_are_usage_errors(self):
        for command in ('/sendfile', '/sendfile   ', '/sendto #2',
                        '/sendto notes.txt'):
            filepath, target, error = parse_send_command(command)
            self.assertIsNone(filepath, command)
            self.assertIsNotNone(error, command)

    def test_dangling_selector_never_becomes_a_broadcast(self):
        # '/sendfile a.txt to=' must not degrade into "send to everybody".
        filepath, target, error = parse_send_command('/sendfile a.txt to=')
        self.assertIsNone(filepath)
        self.assertIsNone(target)
        self.assertIsNotNone(error)

    def test_ordinary_chat_is_not_a_send_command(self):
        self.assertEqual(parse_send_command('hello everyone'),
                         (None, None, None))
        self.assertEqual(parse_send_command('/quit'), (None, None, None))


class VerdictTests(unittest.TestCase):
    def test_take_matches_on_filename(self):
        verdict = FileVerdict()
        verdict.resolve('ERR', {'name': 'other.txt', 'code': 'unknown_target'})
        verdict.resolve('SEND', {'name': 'notes.txt', 'target': 'Otter#2'})

        ok, tag, fields = verdict.take('notes.txt', timeout=0.5)
        self.assertTrue(ok)
        self.assertEqual(tag, 'SEND')
        self.assertEqual(fields['target'], 'Otter#2')

    def test_take_times_out_when_nothing_arrives(self):
        verdict = FileVerdict()
        ok, tag, fields = verdict.take('notes.txt', timeout=0.1)
        self.assertFalse(ok)
        self.assertIsNone(tag)

    def test_a_verdict_is_consumed_only_once(self):
        verdict = FileVerdict()
        verdict.resolve('SEND', {'name': 'notes.txt'})
        self.assertTrue(verdict.take('notes.txt', timeout=0.5)[0])
        self.assertFalse(verdict.take('notes.txt', timeout=0.1)[0])


class RosterAbstractionTests(unittest.TestCase):
    """
    The server keeps the connection detail; the clients never receive it.
    """

    def setUp(self):
        self.clients = roster((1, 'Falcon'), (2, 'Otter'))
        # Something distinctive to search for if it ever leaks onto the wire.
        for record in self.clients:
            record['addr'] = ('10.0.7.9', 51200 + record['id'])
            record['joined_at'] = 1757000000

    def test_roster_payload_carries_only_id_and_name(self):
        wire = render_roster(self.clients, 1).decode()
        self.assertIn('peers=1=Falcon,2=Otter', wire)
        self.assertNotIn('10.0.7.9', wire)          # no addresses
        self.assertNotIn('51200', wire)             # no ports
        self.assertNotIn('1757000000', wire)        # no join times

    def test_roster_says_whether_it_was_asked_for(self):
        # why=ask is the only case a client prints, so an unsolicited roster
        # update after every join cannot bury the terminal.
        _tag, asked = parse_ctrl(render_roster(self.clients, 1, why='ask'))
        _tag, pushed = parse_ctrl(render_roster(self.clients, 1, why='update'))
        self.assertEqual(asked['why'], 'ask')
        self.assertEqual(pushed['why'], 'update')

    def test_parsed_roster_has_no_connection_detail(self):
        _tag, fields = parse_ctrl(render_roster(self.clients, 1))
        peers = parse_roster_peers(fields)
        self.assertEqual([p['name'] for p in peers], ['Falcon', 'Otter'])
        self.assertEqual([p['id'] for p in peers], [1, 2])
        for peer in peers:
            self.assertNotIn('addr', peer)
            self.assertNotIn('joined_at', peer)

    def test_client_view_is_names_only(self):
        text = format_roster_names(self.clients, 1)
        self.assertIn('Falcon (#1)', text)
        self.assertIn('Otter (#2)', text)
        self.assertIn('<- you', text)
        for needle in ('10.0.7.9', '51200', '1757000000'):
            self.assertNotIn(needle, text)

    def test_client_view_does_not_leak_uptime_or_join_time(self):
        text = format_roster_names(self.clients, 1)
        self.assertNotIn('UPTIME', text)
        self.assertNotIn('JOINED', text)
        self.assertNotIn('ADDRESS', text)

    def test_server_view_keeps_the_detail(self):
        text = format_roster_table(self.clients, now=1757000042)
        self.assertIn('10.0.7.9', text)
        self.assertIn('UPTIME', text)
        self.assertIn('51201', text)      # the remote port

    def test_brief_description_has_no_address(self):
        # describe_client() is what goes into a broadcast join/leave notice.
        text = describe_client(self.clients[0])
        self.assertEqual(text, 'Falcon (#1)')
        self.assertNotIn('10.0.7.9', text)


class ClientDisplayTests(unittest.TestCase):
    """
    What the client prints. Short, and free of anything it does not need.
    """

    def test_connect_line_is_one_short_line(self):
        line = render_connected('Otter', 2, 'TCP')
        self.assertEqual(line, 'Otter (#2) connected - TCP chat')
        self.assertEqual(len(line.splitlines()), 1)

    def test_connect_line_carries_no_server_detail(self):
        line = render_connected('Otter', 2, 'UDP')
        for needle in ('127.0.0.1', 'UPTIME', 'JOINED', 'ADDRESS'):
            self.assertNotIn(needle, line)

    def test_room_notice_is_just_a_name_and_a_verb(self):
        record = {'id': 3, 'name': 'Kite', 'addr': ('10.0.7.9', 51200)}
        self.assertEqual(render_room_notice(record, 'joined'), 'Kite (#3) joined')
        self.assertEqual(render_room_notice(record, 'left'), 'Kite (#3) left')
        self.assertNotIn('10.0.7.9', render_room_notice(record, 'joined'))

    def test_help_lists_the_commands(self):
        text = render_help()
        for needle in ('/clients', '/sendfile', '/quit'):
            self.assertIn(needle, text)

    def test_help_is_not_printed_at_startup(self):
        # The point of the upgrade: /help exists, but nothing dumps it unasked and
        # the old startup instruction block is gone.
        import tcp_chat.client as tcp_client
        import udp_chat.client as udp_client
        for module in (tcp_client, udp_client):
            with open(module.__file__, encoding='utf-8') as handle:
                source = handle.read()
            label = module.__name__
            self.assertIn("'/help'", source, label)
            self.assertNotIn('[READY]', source, label)
            self.assertNotIn('render_ready_notice', source, label)
            self.assertNotIn('READY_COMMANDS', source, label)
            self.assertNotIn('("Commands",', source, label)


class MessageRenderingTests(unittest.TestCase):
    def test_verdict_names_the_single_recipient(self):
        text = format_send_verdict({'name': 'notes.txt', 'target': 'Otter#2',
                                    'recipients': '1'})
        self.assertIn('@Otter (#2)', text)
        self.assertIn('only', text)

    def test_broadcast_verdict_says_everybody(self):
        text = format_send_verdict({'name': 'notes.txt', 'target': 'all',
                                    'recipients': '2'})
        self.assertIn('every other client', text)

    def test_refusal_is_explicit_about_nothing_being_sent(self):
        for code, arg in (('unknown_target', '99'), ('self_target', '1'),
                          ('no_recipients', ''), ('ambiguous_target', 'Kite')):
            text = format_send_error({'name': 'notes.txt', 'code': code,
                                      'arg': arg})
            self.assertIn('NOT sent', text)
            self.assertIn('notes.txt', text)

    def test_ack_mentions_a_private_audience(self):
        text = format_ack({'name': 'notes.txt', 'status': 'ok', 'bytes': '10',
                           'recipients': '1', 'target': 'Otter#2'})
        self.assertIn('@Otter (#2) only', text)

        text = format_ack({'name': 'notes.txt', 'status': 'ok', 'bytes': '10',
                           'recipients': '2', 'target': 'all'})
        self.assertNotIn('only', text)


if __name__ == '__main__':
    unittest.main()
