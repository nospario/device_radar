"""Tests for the WiFi vendor lookup. Run: python3 -m unittest discover -s tests -v"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import bt_wifi  # noqa: E402

SAMPLE = """\
OUI/MA-L                                                    Organization
company_id                                                  Organization
                                                            Address

28-66-E3   (hex)\t\tAzureWave Technology Inc.
2866E3     (base 16)\t\tAzureWave Technology Inc.
\t\t\t\t8F., No. 94, Baozhong Rd.
\t\t\t\tTaipei    231
\t\t\t\tTW

0C-80-63   (hex)\t\tTP-LINK TECHNOLOGIES CO.,LTD.
0C8063     (base 16)\t\tTP-LINK TECHNOLOGIES CO.,LTD.

02-00-5E   (hex)\t\tVendor With Local Bit Set
"""


class OuiTableTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "oui.txt"
        self.path.write_text(SAMPLE, encoding="utf-8")
        patcher = mock.patch.object(bt_wifi, "_oui_table", bt_wifi._load_oui_table(self.path))
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_parses_only_hex_lines(self) -> None:
        table = bt_wifi._load_oui_table(self.path)
        self.assertEqual(table["28:66:E3"], "AzureWave Technology Inc.")
        self.assertEqual(table["0C:80:63"], "TP-LINK TECHNOLOGIES CO.,LTD.")
        self.assertNotIn("2866E3", table)
        self.assertEqual(len(table), 3)

    def test_lookup_is_case_insensitive(self) -> None:
        self.assertEqual(bt_wifi.lookup_oui_vendor("28:66:e3:aa:37:65"), "AzureWave Technology Inc.")

    def test_private_address_has_no_vendor_even_if_prefix_listed(self) -> None:
        # 02-00-5E is in the table, but a locally administered address is never a real vendor
        self.assertIsNone(bt_wifi.lookup_oui_vendor("02:00:5E:11:22:33"))

    def test_unknown_prefix_is_none(self) -> None:
        self.assertIsNone(bt_wifi.lookup_oui_vendor("D4:92:5E:00:00:01"))

    def test_falls_back_to_builtin_short_list(self) -> None:
        self.assertEqual(bt_wifi.lookup_oui_vendor("00:03:93:00:00:01"), bt_wifi.OUI_VENDORS["00:03:93"])

    def test_missing_file_gives_empty_table_without_crashing(self) -> None:
        with self.assertLogs("bt_wifi", level="WARNING"):
            self.assertEqual(bt_wifi._load_oui_table(Path(self.tmp.name) / "nope.txt"), {})


class PrivateMacTests(unittest.TestCase):
    def test_locally_administered_bit(self) -> None:
        for mac in ("8E:44:51:E7:87:9D", "6E:42:95:AF:2B:46", "3A:33:34:6F:02:09", "02:00:00:00:00:00", "FE:00:00:00:00:00"):
            self.assertTrue(bt_wifi.is_private_mac(mac), mac)
        for mac in ("28:66:E3:AA:37:65", "0C:80:63:13:5E:70", "C0:C7:DB:02:98:70", "00:68:EB:6E:9E:F5", "90:48:6C:15:39:9E"):
            self.assertFalse(bt_wifi.is_private_mac(mac), mac)

    def test_garbage_is_not_private(self) -> None:
        for bad in ("", "zz:11", "not a mac"):
            self.assertFalse(bt_wifi.is_private_mac(bad), bad)


if __name__ == "__main__":
    unittest.main()
