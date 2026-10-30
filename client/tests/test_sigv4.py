"""SigV4 against the published AWS examples (the 'get-vanilla' suite case and
the IAM ListUsers example from the Signature Version 4 guide)."""

import calendar
import time
import unittest

from jfrog_oidc import sigv4

SECRET = "wJalrXUtnFEMI/K7MDENG+bPxRfiCYEXAMPLEKEY"
AKID = "AKIDEXAMPLE"


def at(stamp):
    return calendar.timegm(time.strptime(stamp, "%Y%m%dT%H%M%SZ"))


class SigV4Tests(unittest.TestCase):
    def test_signing_key_example(self):
        # "Examples of how to derive a signing key for Signature Version 4"
        key = sigv4.signing_key(SECRET, "20120215", "us-east-1", "iam")
        self.assertEqual(key.hex(), "f4780e2d9f65fa895f9c67b32ce1baf0b0d8a43505a000a1a9e090d414db404d")

    def test_get_vanilla(self):
        h = sigv4.sign("GET", "https://example.amazonaws.com/", {}, b"", access_key=AKID, secret_key=SECRET,
                       region="us-east-1", service="service", now=at("20150830T123600Z"))
        self.assertEqual(h["X-Amz-Date"], "20150830T123600Z")
        self.assertTrue(h["Authorization"].endswith(
            "SignedHeaders=host;x-amz-date, "
            "Signature=5fa00fa31553b73ebf1942676e86291e8372ff2a2260956d9b8aae1d763fbf31"))

    def test_iam_list_users(self):
        h = sigv4.sign("GET", "https://iam.amazonaws.com/?Action=ListUsers&Version=2010-05-08",
                       {"Content-Type": "application/x-www-form-urlencoded; charset=utf-8"}, b"",
                       access_key=AKID, secret_key=SECRET, region="us-east-1", service="iam",
                       now=at("20150830T123600Z"))
        self.assertEqual(h["Authorization"],
                         "AWS4-HMAC-SHA256 Credential=AKIDEXAMPLE/20150830/us-east-1/iam/aws4_request, "
                         "SignedHeaders=content-type;host;x-amz-date, "
                         "Signature=5d672d79c15b13162d9279b0855cfba6789a8edb4c82c400e06b5924a6f2b5d7")

    def test_session_token_is_signed(self):
        h = sigv4.sign("POST", "https://sts.us-east-1.amazonaws.com/", {}, b"x", access_key=AKID,
                       secret_key=SECRET, region="us-east-1", service="sts", session_token="tok",
                       now=at("20150830T123600Z"))
        self.assertEqual(h["X-Amz-Security-Token"], "tok")
        self.assertIn("SignedHeaders=host;x-amz-date;x-amz-security-token", h["Authorization"])
        self.assertNotIn(SECRET, repr(h))


if __name__ == "__main__":
    unittest.main()
