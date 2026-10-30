"""CI checks with bounded, metadata-only reports; scanner failure is a failure."""
from __future__ import annotations
import argparse
import json
from pathlib import Path
from backend import security_audit


def run(dependencies=False):
    reports = [security_audit.scan_secrets(), security_audit.audit_ci()]
    if dependencies:
        reports.append(security_audit.audit_lockfiles())
    failed = False
    for report in reports:
        # Any dependency advisory blocks release. An incomplete scan cannot
        # certify a release, even if the returned finding list looks empty.
        bad = (report.get('status') == 'error' or bool(report.get('errors')) or report.get('truncated') or
               any(f.get('severity') in ('critical', 'high', 'medium') for f in report['findings']))
        failed |= bool(bad)
        print(json.dumps(report, ensure_ascii=False))
    return int(failed)


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--dependencies', action='store_true')
    raise SystemExit(run(parser.parse_args().dependencies))
