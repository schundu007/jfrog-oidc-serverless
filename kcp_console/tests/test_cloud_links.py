import unittest

from kcp_console import cloud_links

SUB = "00000000-0000-0000-0000-0000000000a1"


class CloudLinksTest(unittest.TestCase):
    def test_azure_needs_a_subscription_guid(self):
        got = cloud_links.links({"subscription": SUB, "resource_group": "rg apps"})
        self.assertTrue(got["scope"].startswith("https://portal.azure.com/"))
        self.assertIn("/resourceGroups/rg%20apps/", got["resource_group"])
        self.assertEqual(cloud_links.links({"subscription": "sandbox", "resource_group": "rg"}),
                         {"resource_group": None, "scope": None})

    def test_gcp_project(self):
        got = cloud_links.links({"project": "p-1"})
        self.assertIn("project=p-1", got["scope"])
        self.assertIsNone(got["resource_group"])

    def test_nothing_without_ids(self):
        self.assertEqual(cloud_links.links({}), {"resource_group": None, "scope": None})


if __name__ == "__main__":
    unittest.main()
