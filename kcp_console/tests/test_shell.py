import sys
import unittest

from kcp_console.shell import Shell


class StreamTest(unittest.TestCase):
    def setUp(self):
        self.lines = []
        self.sh = Shell(mode="live", log=self.lines.append)

    def test_lines_are_logged_as_printed_then_the_summary(self):
        r = self.sh.run([sys.executable, "-c", "import sys; print('one'); print('two', file=sys.stderr)"], stream=True)
        self.assertEqual(r.rc, 0)
        self.assertEqual(r.stdout, "one\n")
        self.assertEqual(r.stderr, "two\n")
        self.assertIn("one", self.lines)
        self.assertIn("two", self.lines)
        self.assertTrue(self.lines[-1].startswith("$ "))

    def test_sensitive_output_is_never_streamed(self):
        self.sh.run([sys.executable, "-c", "print('hid' + 'den-value')"], stream=True, sensitive=True)
        self.assertNotIn("hidden-value", "\n".join(self.lines))

    def test_timeout_kills_a_streaming_command(self):
        r = self.sh.run([sys.executable, "-c", "import time; print('start', flush=True); time.sleep(30)"],
                        timeout=1, stream=True)
        self.assertEqual(r.rc, 124)
        self.assertIn("start", self.lines)


if __name__ == "__main__":
    unittest.main()
