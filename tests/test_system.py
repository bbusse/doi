'''
Tests for doi.main.System

Most of it reads Linux interfaces directly (/proc, SIOCGIFCONF, netlink
sock_diag, raw ICMP) so those tests skip off Linux and are meant to run in
CI or a container, e.g. `podman run --rm python:3.12-slim ...`. The pure
helpers run everywhere
'''

import os
import socket
import struct
import sys
import unittest
from unittest import mock

import doi.main as main
from doi.main import Py3status, System

linux_only = unittest.skipUnless(sys.platform.startswith("linux"),
                                 "reads Linux-only interfaces")


class TestSystemHelpers(unittest.TestCase):
    '''Pure functions, no platform dependency'''

    def test_load_average_shape(self):
        load = System.load_average()
        self.assertTrue(load is None or len(load) == 3)

    def test_valid_ip_address(self):
        self.assertTrue(System.net_valid_ip_address("192.168.0.1"))
        self.assertTrue(System.net_valid_ip_address("::1"))
        self.assertFalse(System.net_valid_ip_address("not-an-ip"))
        self.assertFalse(System.net_valid_ip_address(""))

    def test_net_hex_address(self):
        # /proc/net/tcp stores v4 addresses little-endian, hex
        self.assertEqual(System.net_hex_address("0100007F"), "127.0.0.1")
        self.assertEqual(System.net_hex_address("00000000"), "0.0.0.0")

    def test_net_endpoint(self):
        self.assertEqual(System.net_endpoint("0100007F:1F90"),
                         "127.0.0.1:8080")

    @staticmethod
    def _neigh_msg(family, ifindex, state, dst, lladdr=None):
        def rta(t, data):
            length = 4 + len(data)
            return struct.pack("=HH", length, t) + data + b"\0" * (-length % 4)

        body = struct.pack("=BxHiHBB", family, 0, ifindex, state, 0, 0)
        body += rta(1, dst)                       # NDA_DST
        if lladdr is not None:
            body += rta(2, lladdr)                # NDA_LLADDR
        return body

    def test_neigh_row_v4_and_v6(self):
        v4 = self._neigh_msg(socket.AF_INET, 2, 0x02,
                             socket.inet_aton("192.168.1.1"),
                             bytes.fromhex("aabbccddeeff"))
        self.assertEqual(
            System._neigh_row(v4, {2: "eth0"}),
            {"ip": "192.168.1.1", "mac": "aa:bb:cc:dd:ee:ff",
             "dev": "eth0", "state": "REACHABLE"})

        v6 = self._neigh_msg(socket.AF_INET6, 2, 0x04,
                             socket.inet_pton(socket.AF_INET6, "fe80::1"))
        row = System._neigh_row(v6, {2: "eth0"})
        self.assertEqual((row["ip"], row["mac"], row["state"]),
                         ("fe80::1", "", "STALE"))

    def test_neigh_row_rejects_non_ip_family(self):
        # 17 is AF_PACKET on Linux; the value only has to miss AF_INET/INET6
        msg = self._neigh_msg(17, 1, 0x02, b"\0" * 4)
        self.assertIsNone(System._neigh_row(msg, {}))

    def test_arp_from_proc(self):
        data = ("IP address  HW type  Flags  HW address  Mask  Device\n"
                "192.168.1.1  0x1  0x2  aa:bb:cc:dd:ee:ff  *  eth0\n"
                "192.168.1.9  0x1  0x0  00:00:00:00:00:00  *  eth0\n")
        with mock.patch("builtins.open", mock.mock_open(read_data=data)):
            rows = System._arp_from_proc()
        self.assertEqual(rows, [
            {"ip": "192.168.1.1", "mac": "aa:bb:cc:dd:ee:ff",
             "dev": "eth0", "state": "REACHABLE"},
            {"ip": "192.168.1.9", "mac": "", "dev": "eth0",
             "state": "INCOMPLETE"}])

    def test_net_neighbours_render(self):
        rows = [
            {"ip": "192.168.1.50", "mac": "de:ad:be:ef:00:01",
             "dev": "eth0", "state": "STALE"},
            {"ip": "192.168.1.1", "mac": "aa:bb:cc:dd:ee:ff",
             "dev": "eth0", "state": "REACHABLE"},
            {"ip": "10.0.0.9", "mac": "", "dev": "eth0",
             "state": "INCOMPLETE"},
        ]
        self.assertEqual({r["state"] for r in rows},
                         {"STALE", "REACHABLE", "INCOMPLETE"})

    def test_icmp_checksum_and_reply_roundtrip(self):
        ident, seq = 4321, 7
        echo = System.icmp_echo(ident, seq)
        # minimal IPv4 header (ihl=5) wrapping an echo reply (type 0)
        reply = bytearray(echo)
        reply[0] = 0  # ICMP type: echo reply
        packet = b"\x45" + b"\x00" * 19 + bytes(reply)
        self.assertEqual(System.icmp_reply(packet, ident, seq), "reached")
        self.assertIsNone(System.icmp_reply(packet, ident, seq + 1))

    def test_scan_sizes_and_reports(self, ):
        import tempfile
        root = tempfile.mkdtemp(prefix="doi-scan-")
        with open(os.path.join(root, "big.bin"), "wb") as f:
            f.write(b"\0" * 200_000)
        os.mkdir(os.path.join(root, "sub"))
        with open(os.path.join(root, "sub", "small.bin"), "wb") as f:
            f.write(b"\0" * 4_000)

        scan = System.scan_sizes(root, limit=5)
        self.assertEqual(scan["path"], root)
        self.assertFalse(scan["truncated"])
        self.assertTrue(scan["files"][0][1].endswith("big.bin"))
        self.assertTrue(scan["dirs"])


@linux_only
class TestSystemProc(unittest.TestCase):

    def test_os_release(self):
        self.assertTrue(System.os_release())  # PRETTY_NAME from /etc/os-release

    def test_resolvconf(self):
        self.assertIn("nameserver", System.net_resolvconf())

    def test_host_uptime(self):
        self.assertIsInstance(System.host_uptime_seconds(), float)

    def test_memory(self):
        mem = System.memory()
        self.assertGreater(mem["total_b"], 0)
        self.assertLessEqual(mem["available_b"], mem["total_b"])

    def test_load_average(self):
        self.assertEqual(len(System.load_average()), 3)

    def test_process_list(self):
        procs = System.process_list()
        self.assertIn(os.getpid(), [p["pid"] for p in procs])
        self.assertTrue(all(p["cmdline"] for p in procs))
        self.assertEqual([p["pid"] for p in procs],
                         sorted(p["pid"] for p in procs))

    def test_process_cpu_times(self):
        procs = System.process_cpu_times()
        self.assertTrue(procs)
        self.assertEqual(set(procs[0]),
                         {"pid", "name", "cpu_s", "threads",
                          "starttime", "rss_b", "rss_peak_b"})
        me = next(p for p in procs if p["pid"] == os.getpid())
        self.assertGreaterEqual(me["rss_peak_b"], me["rss_b"])

    def test_net_addresses(self):
        rows = System.net_addresses()
        self.assertIsInstance(rows, list)
        for row in rows:
            self.assertEqual(set(row), {"dev", "ip"})
            self.assertNotEqual(row["dev"], "lo")

    def test_tcp_byte_counts(self):
        # needs netlink sock_diag, must not raise even where blocked
        self.assertIsInstance(System.tcp_byte_counts(), dict)

    def test_net_neighbour_list(self):
        # RTM_GETNEIGH over netlink, or the /proc/net/arp fallback; must
        # return a list of well-shaped rows and never raise
        rows = System.net_neighbour_list()
        self.assertIsInstance(rows, list)
        for r in rows:
            self.assertEqual(set(r), {"ip", "mac", "dev", "state"})

    def test_net_socket_list(self):
        socks = System.net_socket_list()
        self.assertIsInstance(socks, list)
        for s in socks:
            self.assertEqual(set(s),
                             {"proto", "local", "remote", "state",
                              "sent", "received"})

    def test_traceroute_without_capability(self):
        # raw sockets need CAP_NET_RAW, unprivileged it returns a message
        # not a raise
        out = System.traceroute("127.0.0.1", max_hops=1, cycles=1)
        self.assertEqual(out["dest"], "127.0.0.1")
        self.assertTrue(out["hops"] or out["error"])


class FakePopen:
    def __init__(self, stdout="", stderr="", returncode=0):
        self._out, self._err, self.returncode = stdout, stderr, returncode

    def communicate(self):
        return self._out, self._err


class TestPy3status(unittest.TestCase):
    '''
    py3status is an optional binary. run_module wraps it and parses its
    i3bar output, faked here so the wrapper is testable without it
    '''

    def test_missing_binary_returns_none(self):
        p = Py3status("mpd")
        config = p.config_path
        with mock.patch.object(main.subprocess, "Popen",
                               side_effect=FileNotFoundError):
            self.assertIsNone(p.run_module())
        # the temp config is still cleaned up on the failure path
        self.assertFalse(os.path.exists(config))

    def test_spawn_error_returns_none(self):
        with mock.patch.object(main.subprocess, "Popen",
                               side_effect=OSError("EAGAIN")):
            self.assertIsNone(Py3status("mpd").run_module())

    def test_parses_full_text(self):
        out = '[{"full_text": "OK Computer - Radiohead", "name": "mpd"}]\n'
        with mock.patch.object(main.subprocess, "Popen",
                               return_value=FakePopen(stdout=out)):
            self.assertEqual(Py3status("mpd").run_module(),
                             "OK Computer - Radiohead")

    def test_failed_module_returns_none(self):
        # py3status renders a broken module as its own name
        out = '[{"full_text": "mpd", "name": "mpd"}]\n'
        with mock.patch.object(main.subprocess, "Popen",
                               return_value=FakePopen(stdout=out)):
            self.assertIsNone(Py3status("mpd").run_module())

    def test_mpd_wrapper_survives_missing_binary(self):
        from doi.main import Music
        with mock.patch.object(main.subprocess, "Popen",
                               side_effect=FileNotFoundError):
            self.assertIsNone(Music().mpd())


if __name__ == "__main__":
    unittest.main()
