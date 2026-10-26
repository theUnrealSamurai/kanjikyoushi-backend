"""Download Debian's official trixie-slim arm64 image from Docker Hub as a plain rootfs tarball.

No Docker needed: the image is a single gzipped tar layer, fetched from the registry and checked against
its SHA-256 digest. Usage: uv run --no-project python fetch_debian.py OUTPUT.tar.gz
"""
import hashlib
import json
import sys
import urllib.request

REPO, TAG = "library/debian", "trixie-slim"
INDEX = "application/vnd.oci.image.index.v1+json, application/vnd.docker.distribution.manifest.list.v2+json"
MANIFEST = "application/vnd.oci.image.manifest.v1+json, application/vnd.docker.distribution.manifest.v2+json"


def get(url, token, accept=None):
    request = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
    if accept:
        request.add_header("Accept", accept)
    return urllib.request.urlopen(request)


def main(output):
    auth = f"https://auth.docker.io/token?service=registry.docker.io&scope=repository:{REPO}:pull"
    token = json.load(urllib.request.urlopen(auth))["token"]
    registry = f"https://registry-1.docker.io/v2/{REPO}"

    index = json.load(get(f"{registry}/manifests/{TAG}", token, INDEX))
    arm64 = next(m for m in index["manifests"]
                 if m["platform"].get("architecture") == "arm64" and m["platform"].get("os") == "linux")
    manifest = json.load(get(f"{registry}/manifests/{arm64['digest']}", token, MANIFEST))
    if len(manifest["layers"]) != 1:
        sys.exit(f"expected a single-layer image, got {len(manifest['layers'])} layers")

    layer = manifest["layers"][0]
    data = get(f"{registry}/blobs/{layer['digest']}", token).read()
    if "sha256:" + hashlib.sha256(data).hexdigest() != layer["digest"]:
        sys.exit("downloaded layer doesn't match its digest")
    with open(output, "wb") as file:
        file.write(data)
    print(f"Debian {TAG} arm64 saved to {output} ({len(data) // 1_000_000} MB, {layer['digest'][:19]})")


if __name__ == "__main__":
    main(sys.argv[1])
