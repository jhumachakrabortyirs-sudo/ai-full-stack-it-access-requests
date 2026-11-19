"""Offline installation preparation and spawn-safe T41/T42 helpers."""
import base64
import csv
import hashlib
from importlib import metadata
import io
import json
from pathlib import Path
import sys
import zipfile

from operation_support import FixtureProvider, offline


def prepare_wheelhouse(lockfile, destination):
    """Prepare local wheels from the already-installed pinned distributions.

    This verifies installation from prepared local package files on this same
    interpreter/platform. It does not verify downloading upstream artifacts or
    installing binary wheels on another platform.
    """
    destination = Path(destination)
    destination.mkdir()
    for line in Path(lockfile).read_text().splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        name, version = line.strip().split("==")
        distribution = metadata.distribution(name)
        if distribution.version != version:
            raise AssertionError(f"Prepared runtime differs from lockfile for {name}")
        files = distribution.files
        wheel_metadata = next(path for path in files if str(path).endswith('.dist-info/WHEEL'))
        record_name = str(wheel_metadata).rsplit('/', 1)[0] + '/RECORD'
        tags = [row[5:] for row in distribution.locate_file(wheel_metadata).read_text().splitlines() if row.startswith('Tag: ')]
        filename = f"{name.replace('-', '_')}-{version}-{tags[0]}.whl"
        records = []
        with zipfile.ZipFile(destination / filename, 'w', zipfile.ZIP_DEFLATED) as wheel:
            for path in files:
                relative = str(path)
                if '..' in Path(relative).parts or relative == record_name or relative.endswith('.pyc'):
                    continue
                installed = distribution.locate_file(path)
                if not installed.is_file():
                    continue
                raw = installed.read_bytes()
                wheel.writestr(relative, raw)
                digest = base64.urlsafe_b64encode(hashlib.sha256(raw).digest()).rstrip(b'=').decode('ascii')
                records.append([relative, 'sha256=' + digest, str(len(raw))])
            records.append([record_name, '', ''])
            output = io.StringIO(newline='')
            csv.writer(output).writerows(records)
            wheel.writestr(record_name, output.getvalue())


def cli_interrupt_process(database, inputs, ready, release):
    """Run the real parser/service with a deterministic delayed fixture."""
    with offline():
        from intake.cli import main
        provider = FixtureProvider(enter={'IT-002': ready}, release={'IT-002': release})
        code = main([
            '--db', str(database), 'run', str(inputs), '--provider', 'mock',
            '--operator', 'interrupted-cli', '--workers', '1', '--lease-seconds', '30',
        ], provider_factory=lambda: provider, clock=lambda: 1000.0)
        if code:
            raise SystemExit(code)


def export_crash_process(database, output, stage, ready, release):
    with offline():
        from intake.exports import publish

        def checkpoint(actual_stage, generation):
            if actual_stage == stage:
                ready.set()
                if not release.wait(timeout=15):
                    raise AssertionError('publication checkpoint was not released')

        publish(Path(database), Path(output), checkpoint=checkpoint)
