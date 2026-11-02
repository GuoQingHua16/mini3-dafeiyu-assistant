import unittest
from adb_link import ADBInUse, ADBLinkError, select_device


class DeviceSelectionTests(unittest.TestCase):
    def test_only_the_unique_compatible_application_is_selected(self):
        listing = ('List of devices attached\nPHONE device model:phone\n'
                   'BOOT-TEST device model:listenai\nMINI3_TEST device model:listenai\n')
        self.assertEqual(select_device(listing), 'MINI3_TEST')

    def test_ambiguous_and_offline_devices_are_rejected(self):
        with self.assertRaises(ADBInUse):
            select_device('FIRST device model:listenai\nSECOND device model:listenai\n')
        with self.assertRaises(ADBLinkError):
            select_device('FIRST offline model:listenai\n')

    def test_explicit_selection_stays_bound_to_that_device(self):
        listing = 'FIRST device model:listenai\nSECOND device model:listenai\n'
        self.assertEqual(select_device(listing, 'SECOND'), 'SECOND')
        with self.assertRaises(ADBLinkError):
            select_device('FIRST device model:listenai\n', 'SECOND')


if __name__ == '__main__': unittest.main()
