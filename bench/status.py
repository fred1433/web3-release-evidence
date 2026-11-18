"""Release status, computed from the required-checks manifest and the check results.

Blocked                     at least one required check failed on a named assertion
Incomplete                  no failure, but a required check was not executed, was skipped,
                            timed out or crashed (the denominator never shrinks)
Demonstrated checks passed  every required check executed and passed

The status never speaks for the client's own product: "Client MVP: not assessed".
"""

BLOCKED = 'Blocked'
INCOMPLETE = 'Incomplete'
PASSED = 'Demonstrated checks passed'


def decide(required_ids, results):
    """required_ids: list of check ids from the manifest. results: list of dicts with id and outcome."""
    by_id = {}
    for r in results:
        if r['id'] in required_ids:
            by_id[r['id']] = r  # results for ids outside the manifest never fill a required slot
    failed = [i for i in required_ids if i in by_id and by_id[i]['outcome'] == 'failed']
    errored = [i for i in required_ids if i in by_id and by_id[i]['outcome'] == 'error']
    passed = [i for i in required_ids if i in by_id and by_id[i]['outcome'] == 'passed']
    missing = [i for i in required_ids if i not in by_id or by_id[i]['outcome'] not in ('passed', 'failed', 'error')]
    if failed:
        status = BLOCKED
    elif not required_ids or missing or errored:
        status = INCOMPLETE
    else:
        status = PASSED
    return {
        'status': status,
        'required': len(required_ids),
        'executed': len(passed) + len(failed),
        'passed': len(passed),
        'failed': failed,
        'errored': errored,
        'not_executed': missing,
    }
