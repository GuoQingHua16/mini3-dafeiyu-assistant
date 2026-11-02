import asyncio
import contextlib
from datetime import datetime
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock, Mock, patch

import agent
from display_info import hud_command
from local_tools import BEIJING, parse_utility
from networking import choose_address, resolve_host_ip
from reminders import Reminders


class NetworkingTests(unittest.TestCase):
    def test_ignores_unusable_addresses_and_deduplicates(self):
        rows = [{'IPAddress': address} for address in ['127.0.0.1', '169.254.1.1', '0.0.0.0',
                                                      '224.0.0.1', '10.2.3.4', '10.2.3.4', 'invalid']]
        self.assertEqual(choose_address(rows), '10.2.3.4')

    def test_missing_and_multiple_physical_addresses_require_explicit_choice(self):
        for rows in [[], [{'IPAddress': '192.168.1.2'}, {'IPAddress': '10.2.3.4'}]]:
            with self.assertRaisesRegex(RuntimeError, 'HostIP'):
                choose_address(rows)

    def test_explicit_address_needs_no_windows_query(self):
        with patch('networking.subprocess.run') as run:
            self.assertEqual(resolve_host_ip('10.2.3.4'), '10.2.3.4')
        run.assert_not_called()

    def test_windows_query_is_limited_to_up_physical_interfaces(self):
        completed = Mock(returncode=0, stdout=json.dumps([{'IPAddress': '10.2.3.4'}]))
        with patch('networking.subprocess.run', return_value=completed) as run:
            self.assertEqual(resolve_host_ip(), '10.2.3.4')
        script = run.call_args.args[0][-1]
        self.assertIn('Get-NetAdapter -Physical', script)
        self.assertIn("Status -eq 'Up'", script)
        self.assertIn('-AddressState Preferred', script)

    def test_query_failure_is_local_and_actionable(self):
        with patch('networking.subprocess.run', return_value=Mock(returncode=1)):
            with self.assertRaisesRegex(RuntimeError, 'HostIP'):
                resolve_host_ip()


class DisplayTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.now = datetime(2026, 10, 27, 9, 5, tzinfo=BEIJING).timestamp()
        self.reminders = Reminders(Path(self.folder.name) / 'tasks.json', clock=lambda: self.now)

    def apply(self, text):
        return self.reminders.apply(parse_utility(text))

    def test_empty_hud_has_beijing_clock_without_an_active_timer(self):
        self.assertEqual(hud_command(self.reminders), 'local_agent hud 32700 -1 0')

    def test_running_pause_and_resume_preserve_remaining_seconds(self):
        self.apply('计时十分钟')
        self.now += 100
        self.assertEqual(hud_command(self.reminders).split()[3:5], ['500', '0'])
        self.apply('暂停计时')
        self.now += 100
        self.assertEqual(hud_command(self.reminders).split()[3:5], ['500', '1'])
        self.apply('继续计时')
        self.now += 50
        self.assertEqual(hud_command(self.reminders).split()[3:5], ['450', '0'])

    def test_timer_wins_over_reminders_and_fallback_uses_earliest_reminder(self):
        self.apply('五分钟后提醒我喝水')
        self.apply('一分钟后提醒我散步')
        self.assertEqual(bytes.fromhex(hud_command(self.reminders).split()[5]).decode(), '散步')
        self.apply('计时十分钟')
        self.assertEqual(bytes.fromhex(hud_command(self.reminders).split()[5]).decode(), '计时')
        self.apply('取消计时')
        self.assertEqual(bytes.fromhex(hud_command(self.reminders).split()[5]).decode(), '散步')

    def test_overdue_and_cancel_are_visible_without_creating_new_tasks(self):
        self.apply('一秒后提醒我喝水')
        self.now += 5
        self.assertEqual(hud_command(self.reminders).split()[3], '0')
        self.apply('取消全部提醒')
        self.assertEqual(hud_command(self.reminders).split()[3], '-1')

    def test_utf8_label_and_seven_day_duration_fit_the_shell_budget(self):
        self.apply('七天后提醒我一二三四五六七八九十')
        command = hud_command(self.reminders)
        self.assertLess(len(command.encode()), 128)
        self.assertEqual(bytes.fromhex(command.split()[5]).decode(), '一二三四五')
        self.assertEqual(command.split()[3], '604800')


class LifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def test_old_firmware_reports_upgrade_instead_of_reconnect_loop(self):
        instance = agent.Agent.__new__(agent.Agent)
        instance.hud_supported = False
        instance.command = AsyncMock(side_effect=[
            'LOCAL_AGENT_IDLE BUTTON=0 CLOUD=DISABLED PET=1 CONTROL=1',
            'local_agent status | stop | play http://PC:8765/audio/token.mp3'])
        with self.assertRaises(RuntimeError) as caught:
            await agent.connect_device(instance, agent.ConversationState(60))
        self.assertNotIsInstance(caught.exception, agent.DeviceDisconnected)
        self.assertFalse(instance.hud_supported)

    async def test_hud_update_is_independent_of_ui_and_paid_dialogue(self):
        instance = agent.Agent.__new__(agent.Agent)
        instance.utilities = Mock()
        instance.utilities.reminders = Mock(items=[], clock=lambda: 0)
        instance.command = AsyncMock(return_value='LOCAL_AGENT_HUD_OK')
        instance.ui = AsyncMock()
        instance.reply = AsyncMock()
        await instance.update_hud()
        instance.ui.assert_not_awaited()
        instance.reply.assert_not_awaited()

    async def test_only_matching_shutdown_token_cancels_the_service(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'stop.request'
            path.write_text('old-instance', encoding='utf-8')
            target = Mock()
            requested = asyncio.Event()
            with patch.object(agent, 'STOP_FILE', path):
                watcher = asyncio.create_task(agent.watch_stop_request(target, requested, 'this-instance'))
                try:
                    await asyncio.sleep(.25)
                    target.cancel.assert_not_called()
                    path.write_text('this-instance', encoding='utf-8')
                    await asyncio.wait_for(watcher, 1)
                finally:
                    watcher.cancel()
                    with contextlib.suppress(asyncio.CancelledError):
                        await watcher
            target.cancel.assert_called_once()
            self.assertTrue(requested.is_set())


if __name__ == '__main__':
    unittest.main()
