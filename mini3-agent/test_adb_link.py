import io
from pathlib import Path
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import Mock

from adb_link import ADBInUse, ADBLinkError, Lease, Mini3Link, clean_response, split_frame


class FramingTests(unittest.TestCase):
    def test_prompt_redraw_preserves_a_real_ack(self):
        raw = b'ListenAI:/$ local_agent ui sleeping 0\r\nListenAI:/$ LOCAL_AGENT_UI_OK\r\r\n'
        self.assertEqual(clean_response(raw), 'LOCAL_AGENT_UI_OK\n')
        redrawn = b'ListenAI:/$ old input\x1b[2K\rListenAI:/$ LOCAL_AGENT_UI_OK\r\n'
        self.assertEqual(clean_response(redrawn), 'LOCAL_AGENT_UI_OK\n')

    def test_prompt_prefixed_marker_is_still_a_complete_frame(self):
        frame = split_frame(b'LOCAL_AGENT_UI_OK\r\nListenAI:/$ 3.0.2-620341ff\r\n', '3.0.2-620341ff')
        self.assertEqual(clean_response(frame[0]), 'LOCAL_AGENT_UI_OK\n')

    def test_fragmented_response_waits_for_complete_marker(self):
        raw = b'ListenAI:/$ local_agent poll\r\nLOCAL_AGENT_IDLE BUTTON=7 PHASE=7\r\r\nListenAI:/$ version\r\n3.0.2-620341ff\r'
        self.assertIsNone(split_frame(raw, '3.0.2-620341ff'))
        frame = split_frame(raw + b'\r\nListenAI:/$', '3.0.2-620341ff')
        self.assertEqual(frame[2], '3.0.2-620341ff')
        self.assertIn('LOCAL_AGENT_IDLE BUTTON=7 PHASE=7', clean_response(frame[0]))
        self.assertNotIn('ListenAI:', clean_response(frame[0]))

    def test_logs_and_ansi_are_not_mistaken_for_version_marker(self):
        raw = (b'\x1b[2K\rI/fw: Firmware version: 3.0.2-620341ff\r\n'
               b'LOCAL_AGENT_UI_OK\r\n\x1b[36m3.0.2-620341ff\x1b[0m\r\n')
        frame = split_frame(raw)
        self.assertEqual(frame[2], '3.0.2-620341ff')
        self.assertIn('LOCAL_AGENT_UI_OK', clean_response(frame[0]))

    def test_next_frame_bytes_are_not_consumed(self):
        first = b'FIRST\r\n3.0.2-620341ff\r\n'
        second = b'SECOND\r\n3.0.2-620341ff\r\n'
        frame = split_frame(first + second, '3.0.2-620341ff')
        self.assertEqual((first + second)[frame[1]:], second)


class LeaseTests(unittest.TestCase):
    def test_second_owner_is_rejected_until_first_closes(self):
        with tempfile.TemporaryDirectory() as directory:
            a = Lease(Path(directory) / 'owner.lock')
            b = Lease(Path(directory) / 'owner.lock')
            try:
                a.acquire()
                with self.assertRaises(ADBInUse): b.acquire()
                a.close(); b.acquire()
            finally:
                a.close(); b.close()


class LinkTests(unittest.TestCase):
    def make_link(self):
        folder = tempfile.TemporaryDirectory(); self.addCleanup(folder.cleanup)
        link = Mini3Link('adb', Path(folder.name) / 'owner.lock', timeout=.08)
        self.addCleanup(link.close)
        link._version = '3.0.2-620341ff'
        writes = []

        def write(data):
            writes.append(data)
            with link._condition:
                name = data.split(b'\r')[0]
                link._rx.extend(name.upper() + b'\r\n3.0.2-620341ff\r\n')
                link._condition.notify_all()
            return len(data)

        process = SimpleNamespace(stdin=SimpleNamespace(write=Mock(side_effect=write), flush=Mock(), close=Mock()),
                                  stdout=None, poll=lambda: 0)
        link._process = process
        link._open = Mock()
        return link, writes

    def test_concurrent_calls_are_serialized_without_crossed_replies(self):
        link, writes = self.make_link()
        replies = {}
        threads = [threading.Thread(target=lambda n=n: replies.update({n: link.execute('command' + str(n))})) for n in range(20)]
        for thread in threads: thread.start()
        for thread in threads: thread.join(timeout=2)
        self.assertEqual(len(writes), 20)
        self.assertEqual(len(replies), 20)
        for n, reply in replies.items(): self.assertEqual(reply, 'COMMAND' + str(n) + '\n')

    def test_timeout_never_resends_a_mutation(self):
        link, writes = self.make_link()
        link._process.stdin.write.side_effect = lambda data: writes.append(data)
        with self.assertRaises(ADBLinkError): link.execute('local_agent control volume set 70')
        self.assertEqual(writes, [b'local_agent control volume set 70\r\nversion\r\n'])
        self.assertEqual(link.stats['timeouts'], 1)

    def test_eof_does_not_return_a_partial_ack(self):
        link, writes = self.make_link()
        def lost(data):
            writes.append(data)
            with link._condition:
                link._rx.extend(b'LOCAL_AGENT_CONTROL_OK VOLUME=')
                link._eof = True; link._condition.notify_all()
        link._process.stdin.write.side_effect = lost
        with self.assertRaises(ADBLinkError): link.execute('local_agent control volume set 70')
        self.assertEqual(len(writes), 1)

    def test_overflow_fails_closed_even_if_ack_was_buffered(self):
        link, writes = self.make_link()
        def overflow(data):
            with link._condition:
                link._rx.extend(b'OK\r\n3.0.2-620341ff\r\n')
                link._overflow = True
        link._process.stdin.write.side_effect = overflow
        with self.assertRaises(ADBLinkError): link.execute('local_agent stop')

    def test_control_characters_are_rejected_before_open_or_write(self):
        link, writes = self.make_link()
        for bad in ('local_agent stop\rreboot', 'local_agent stop\nversion', 'reboot;version', 'x\0y'):
            with self.assertRaises(ValueError): link.execute(bad)
        link._open.assert_not_called()
        self.assertEqual(writes, [])

    def test_version_query_uses_the_synced_value_without_duplicate_markers(self):
        link, writes = self.make_link()
        self.assertEqual(link.execute('version'), '3.0.2-620341ff\n')
        self.assertEqual(writes, [])


if __name__ == '__main__':
    unittest.main()
