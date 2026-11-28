"""Inspect a real built ZIP without loading Linux binaries on Windows."""
import ast
from email.parser import BytesParser
from pathlib import PurePosixPath
import struct
import sys
import zipfile


def check(path):
    with zipfile.ZipFile(path) as archive:
        names = archive.namelist()
        assert "app.py" in names and "discord_bot/commands/ping.py" in names
        native = []
        versions = {}
        for name in names:
            parts = PurePosixPath(name).parts
            assert not name.startswith("/") and ".." not in parts and "\\" not in name, name
            assert not name.endswith((".pyd", ".dll", ".pyc")), name
            if name.endswith(".so"):
                header = archive.read(name)[:20]
                assert header[:5] == b"\x7fELF\x02" and header[5] == 1, name
                assert struct.unpack_from("<H", header, 18)[0] == 183, f"not aarch64: {name}"
                native.append(name)
            elif name.endswith(".py"):
                ast.parse(archive.read(name), filename=name)
            elif name.endswith(".dist-info/METADATA"):
                metadata = BytesParser().parsebytes(archive.read(name))
                versions[metadata["Name"].lower()] = metadata["Version"]
        assert native, "No native libraries found"
        assert versions == {"cryptography": "50.0.2", "cffi": "2.1.1", "pycparser": "3.12", "boto3": "1.44.12", "botocore": "1.44.12", "jmespath": "1.1.0", "s3transfer": "0.19.3", "python-dateutil": "2.9.0.post0", "six": "1.17.0", "urllib3": "2.8.0"}, versions
        print(f"Verified {len(names)} ZIP entries, {len(native)} Linux aarch64 libraries, pinned dependencies")


if __name__ == "__main__":
    check(sys.argv[1])
