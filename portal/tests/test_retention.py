"""Only the newest 7 runs are kept across the portal, live or archived; running ones are never deleted."""

import unittest

from portal import retention


class FakeRuns:
    def __init__(self, rows):
        self.rows = rows

    def list(self):
        return list(self.rows)

    def delete(self, i):
        self.rows = [r for r in self.rows if r["id"] != i]


class FakeArchive:
    def __init__(self, rows):
        self.rows = rows

    def drop_run(self, i):
        self.rows = [r for r in self.rows if r["id"] != i]
        return True


class FakeConsole:
    def __init__(self, live, gone):
        self.runs, self.archive = FakeRuns(live), FakeArchive(gone)

    def archived(self):
        return {"entries": [{"run_rows": list(self.archive.rows)}]}


class FakeJobs:
    def __init__(self, hist, arch):
        self.hist, self.arch = hist, arch

    def history(self):
        return list(self.hist)

    def archived(self):
        return list(self.arch)

    def remove(self, i):
        self.hist = [r for r in self.hist if r["id"] != i]

    def remove_archived(self, i):
        self.arch = [r for r in self.arch if r["id"] != i]
        return True


def iso(day):
    return f"2026-10-{day:02d}T00:00:00Z"


def epoch(day):
    return retention._epoch(iso(day))


class RetentionTest(unittest.TestCase):
    def test_keeps_the_newest_seven_everywhere(self):
        sv = FakeConsole([{"id": f"s{d}", "created": iso(d), "status": "succeeded"} for d in (1, 5, 9, 13)],
                         [{"id": "sa2", "created": iso(2)}])
        k8 = FakeConsole([{"id": "k3", "created": iso(3), "status": "running"}, {"id": "k11", "created": iso(11), "status": "failed"}],
                         [{"id": "ka12", "created": iso(12)}])
        jobs = FakeJobs([{"id": f"j{d}", "started": epoch(d), "state": "succeeded"} for d in (4, 10, 14)],
                        [{"id": "ja8", "started": epoch(8)}])
        retention.trim(sv, k8, jobs)
        kept = {r["id"] for r in sv.runs.rows + sv.archive.rows + k8.runs.rows + k8.archive.rows + jobs.hist + jobs.arch}
        # Newest seven: 14, 13, 12, 11, 10, 9, 8; k3 is running, so it stays too.
        self.assertEqual(kept, {"j14", "s13", "ka12", "k11", "j10", "s9", "ja8", "k3"})

    def test_nothing_to_do_under_seven(self):
        sv = FakeConsole([{"id": "a", "created": iso(1), "status": "succeeded"}], [])
        self.assertEqual(retention.trim(sv), 0)


if __name__ == "__main__":
    unittest.main()
