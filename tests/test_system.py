'''
Tests for doi.main.System

Most of it reads Linux interfaces directly (/proc, SIOCGIFCONF, netlink
sock_diag, raw ICMP) so those tests skip off Linux and are meant to run in
CI or a container, e.g. `podman run --rm python:3.12-slim ...`. The pure
helpers run everywhere
'''

import os
import socket
import sys
import unittest
from unittest import mock

import doi.main as main
from doi.main import Py3status, System

linux_only = unittest.skipUnless(sys.platform.startswith("linux"),
                                 "reads Linux-only interfaces")


class TestSystemHelpers(unittest.TestCase):
    '''Pure functions, no platform dependency'''

    def test_human_bytes(self):
        self.assertEqual(System.human_bytes(512), "512B")
        self.assertEqual(System.human_bytes(1536), "1.5K")
        self.assertEqual(System.human_bytes(5 * 1024 ** 3), "5.0G")

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
        self.assertIn("big.bin", System.biggest_files(root))
        self.assertIn("SIZE", System.biggest_dirs(root))


@linux_only
class TestSystemProc(unittest.TestCase):

    def test_os_release(self):
        self.assertTrue(System.os_release())  # PRETTY_NAME from /etc/os-release

    def test_resolvconf(self):
        self.assertIn("nameserver", System.net_resolvconf())

    def test_host_uptime(self):
        self.assertIsInstance(System.host_uptime_seconds(), float)
        self.assertRegex(System.host_uptime(), r"\d+m")

    def test_mem_data(self):
        text = System.mem_data()
        self.assertTrue(text.startswith("Memory:"))
        self.assertIn("total", text)

    def test_uptime_falls_back_to_proc(self):
        # `uptime` is absent from a slim image, so this exercises the
        # /proc/uptime + /proc/loadavg fallback
        out = System.uptime(os.environ.copy())
        self.assertIn("load average:", out["load"])

    def test_processes_from_proc(self):
        text = System._processes_from_proc()
        self.assertIn("COMMAND", text.splitlines()[0])
        self.assertIn(str(os.getpid()), text)

    def test_list_processes_fallback(self):
        # no ps binary either, same /proc listing
        self.assertIn("COMMAND", System.list_processes())

    def test_process_cpu_times_and_top(self):
        procs = System.process_cpu_times()
        self.assertTrue(procs)
        self.assertEqual(set(procs[0]),
                         {"pid", "name", "cpu_s", "threads",
                          "starttime", "rss_b"})
        self.assertIn("CPU TIME", System.top(limit=5))

    def test_net_addresses(self):
        out = System.net_addresses()
        self.assertIsInstance(out, str)
        # loopback is always filtered out
        self.assertNotIn("lo 127.0.0.1", out)

    def test_tcp_byte_counts(self):
        # needs netlink sock_diag, must not raise even where blocked
        self.assertIsInstance(System.tcp_byte_counts(), dict)

    def test_net_socket_list(self):
        socks = System.net_socket_list()
        self.assertIsInstance(socks, list)
        for s in socks:
            self.assertEqual(set(s),
                             {"proto", "local", "remote", "state",
                              "sent", "received"})

    def test_net_sockets_render(self):
        # Open a listener so there is at least one row to format
        srv = socket.socket()
        srv.bind(("127.0.0.1", 0))
        srv.listen(1)
        try:
            out = System.net_sockets(limit=20)
            self.assertIn("PROTO", out)
        finally:
            srv.close()

    def test_traceroute_without_capability(self):
        # raw sockets need CAP_NET_RAW, unprivileged it returns a message
        # not a raise
        out = System.traceroute("127.0.0.1", max_hops=1, cycles=1)
        self.assertIsInstance(out, str)


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
