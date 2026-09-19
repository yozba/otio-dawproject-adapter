"""Create self-contained OTIOZ bundles from DAWproject archives."""

from __future__ import annotations

import argparse
import os
from pathlib import Path
import tempfile

import opentimelineio as otio

from . import dawproject


def write_otioz(input_path, output_path):
    """Convert a DAWproject archive to an OTIOZ bundle with embedded media."""
    source = Path(input_path).resolve()
    destination = Path(output_path).resolve()
    if source.suffix.lower() != ".dawproject":
        raise ValueError("OTIOZ input must have a .dawproject extension")
    if destination.suffix.lower() != ".otioz":
        raise ValueError("OTIOZ output must have a .otioz extension")
    destination.parent.mkdir(parents=True, exist_ok=True)

    # The OTIOZ writer requires file: URLs as input and rewrites them to paths
    # inside the bundle. Keep extraction temporary so no sidecar media folder
    # remains after the self-contained bundle has been written.
    with tempfile.TemporaryDirectory(
        prefix=".otio-dawproject-", dir=destination.parent
    ) as temporary:
        temporary_path = Path(temporary)
        timeline = dawproject.read_from_file(
            source,
            extract_media_to=temporary_path / "source-media",
            media_reference_style="url",
        )
        bundled = temporary_path / destination.name
        otio.adapters.write_to_file(timeline, str(bundled))
        os.replace(bundled, destination)
    return str(destination)


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Convert a DAWproject archive to a self-contained OTIOZ bundle."
    )
    parser.add_argument("-i", "--input", required=True, help="Input .dawproject file")
    parser.add_argument("-o", "--output", required=True, help="Output .otioz file")
    args = parser.parse_args(argv)
    write_otioz(args.input, args.output)


if __name__ == "__main__":
    main()
