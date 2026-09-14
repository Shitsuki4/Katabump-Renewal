"""Install the pinned sing-box binary; verify its release SHA-256 first."""

import hashlib
import io
from pathlib import Path
import platform
import tarfile
import time
import urllib.request
import zipfile


VERSION = "1.13.16"
RELEASES = {
    "Linux": ("linux-amd64.tar.gz", "e37c312859dfa84cba148f41072ff6369f08361ae91d622dc1fd3aab49611a8d"),
    "Windows": ("windows-amd64.zip", "6cbf90ec4ee87122ffce09b73928fb31e763bc1c75a119f79c61d24734c78807"),
}


def main():
    if platform.machine().lower() not in ("amd64", "x86_64"):
        raise RuntimeError("This installer supports x86-64 only")
    suffix, digest = RELEASES[platform.system()]
    asset = f"sing-box-{VERSION}-{suffix}"
    url = f"https://github.com/SagerNet/sing-box/releases/download/v{VERSION}/{asset}"
    for attempt in range(3):
        try:
            with urllib.request.urlopen(url, timeout=60) as response:
                data = response.read(100 * 1024 * 1024)
            break
        except OSError:
            if attempt == 2:
                raise
            time.sleep(2 ** attempt)
    if hashlib.sha256(data).hexdigest() != digest:
        raise RuntimeError("sing-box archive checksum mismatch")
    binary = "sing-box.exe" if platform.system() == "Windows" else "sing-box"
    # Extract just the expected member; never extract arbitrary archive paths.
    folder = f"sing-box-{VERSION}-" + suffix.split(".", 1)[0]
    member = f"{folder}/{binary}"
    if suffix.endswith(".zip"):
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            content = archive.read(member)
    else:
        with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as archive:
            with archive.extractfile(member) as source:
                content = source.read()
    path = Path(binary)
    path.write_bytes(content)
    path.chmod(0o755)
    print(f"Installed sing-box {VERSION}; SHA-256 verified.")


if __name__ == "__main__":
    main()
