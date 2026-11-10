"""Fail CI if any test failed/skipped or PostgreSQL concurrency checks disappeared."""
import sys
import xml.etree.ElementTree as ET


def check(path):
    cases = ET.parse(path).getroot().findall(".//testcase")
    if not cases:
        raise ValueError("No test results")
    incomplete = [c.get("name") for c in cases
                  if any(c.find(tag) is not None for tag in ("failure", "error", "skipped"))]
    required = {
        "test_two_client_decisions_only_one_succeeds",
        "test_two_expiration_reads_only_one_event",
        "test_two_new_versions_only_one_draft",
        "test_deadline_rechecked_after_waiting_for_lock",
    }
    executed = {c.get("name") for c in cases if c.get("classname", "").endswith("test_concurrency")}
    if incomplete or required - executed:
        raise ValueError(f"Incomplete tests: {incomplete}; missing concurrency checks: {sorted(required - executed)}")
    print(f"Verified {len(cases)} tests, no failures/errors/skips; all four PostgreSQL concurrency checks executed.")


if __name__ == "__main__":
    check(sys.argv[1] if len(sys.argv) > 1 else "test-results.xml")
