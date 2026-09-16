"""Firmware-release metadata and the GitHub releases catalog.

Port of the app's `firmware_release.dart` (pure: version and channel
rules, the device-identity parse, release selection) and
`firmware_catalog.dart` (fetching) in one module — the rules agree with
the app, a second consumer rather than a second opinion. The pure names
are hardware-free and unit-tested; `GithubReleaseCatalog` is the only
network code.
"""

import dataclasses
import enum
import functools
import hashlib
import json
import re
import urllib.error
import urllib.request

from .errors import FirmwareCatalogError

__all__ = [
    "FirmwareChannel",
    "FirmwareVersion",
    "FirmwareRelease",
    "InstalledFirmware",
    "GithubAsset",
    "GithubRelease",
    "GithubReleaseCatalog",
    "parse_firmware_rev",
    "describe_matches_tag",
    "firmware_image_name",
    "select_target",
    "RELEASES_URL",
]

# The firmware repo's releases are the single source of truth.
_OWNER = "K3Engineering"
_REPO = "dynamite-sampler-firmware"
RELEASES_URL = f"https://api.github.com/repos/{_OWNER}/{_REPO}/releases"

_API_HEADERS = {"Accept": "application/vnd.github+json"}

_HTTP_TIMEOUT_S = 30.0


class FirmwareChannel(enum.Enum):
    """Which release stream a device tracks. There is deliberately no
    "nightly": nightlies are covered by the from-file flash path."""

    STABLE = "stable"  # published releases only
    BETA = "beta"  # everything stable sees, plus GitHub prereleases


def _ordered(a, b) -> int:
    return (a > b) - (a < b)


def _maybe_int(s: str) -> int | None:
    try:
        return int(s)
    except ValueError:
        return None


_VERSION_RE = re.compile(r"v?(\d+)\.(\d+)\.(\d+)(?:-([0-9A-Za-z.-]+))?")


@functools.total_ordering
@dataclasses.dataclass(frozen=True)
class FirmwareVersion:
    """A semver-ish version parsed from a release tag (`v1.2.3`,
    `v1.2.3-beta.4`). The leading `v` is optional on parse and always
    rendered."""

    major: int
    minor: int
    patch: int
    prerelease: str | None = None

    @classmethod
    def try_parse(cls, tag: str) -> "FirmwareVersion | None":
        m = _VERSION_RE.fullmatch(tag.strip())
        if m is None:
            return None
        return cls(int(m[1]), int(m[2]), int(m[3]), m[4])

    @property
    def label(self) -> str:
        base = f"v{self.major}.{self.minor}.{self.patch}"
        return base if self.prerelease is None else f"{base}-{self.prerelease}"

    def _cmp(self, other: "FirmwareVersion") -> int:
        for a, b in (
            (self.major, other.major),
            (self.minor, other.minor),
            (self.patch, other.patch),
        ):
            if a != b:
                return _ordered(a, b)
        # Standard semver: a release outranks any of its prereleases.
        pre, other_pre = self.prerelease, other.prerelease
        if pre is None:
            return 0 if other_pre is None else 1
        if other_pre is None:
            return -1
        segs, other_segs = pre.split("."), other_pre.split(".")
        for a, b in zip(segs, other_segs):
            ai, bi = _maybe_int(a), _maybe_int(b)
            if ai is not None and bi is not None:
                if ai != bi:
                    return _ordered(ai, bi)
            elif a != b:
                return _ordered(a, b)
        return _ordered(len(segs), len(other_segs))

    def __lt__(self, other) -> bool:
        if not isinstance(other, FirmwareVersion):
            return NotImplemented
        return self._cmp(other) < 0

    def __str__(self) -> str:
        return self.label


@dataclasses.dataclass(frozen=True)
class FirmwareRelease:
    """The release a device should be running for its channel: one image
    asset of one GitHub release."""

    tag: str  # as published, e.g. "0.4.0-beta.1"
    version: FirmwareVersion
    asset_name: str
    size: int
    download_url: str
    # The `.sha256` sidecar asset. Release CI always publishes one; a
    # release without it is not a candidate (see select_target).
    sha256_url: str


@dataclasses.dataclass(frozen=True)
class InstalledFirmware:
    """The firmware's `<board>|<git describe>` firmware-revision string."""

    board: str
    describe: str


@dataclasses.dataclass(frozen=True)
class GithubAsset:
    name: str
    size: int
    url: str

    @classmethod
    def from_json(cls, j) -> "GithubAsset":
        return cls(name=j["name"], size=j["size"], url=j["browser_download_url"])


@dataclasses.dataclass(frozen=True)
class GithubRelease:
    """Minimal view of one GitHub release for selection — parsed from the
    API by the catalog, constructed directly in tests."""

    tag: str
    draft: bool
    prerelease: bool
    assets: list[GithubAsset]

    @classmethod
    def from_json(cls, j) -> "GithubRelease":
        return cls(
            tag=j["tag_name"],
            draft=j["draft"],
            prerelease=j["prerelease"],
            assets=[GithubAsset.from_json(a) for a in j["assets"]],
        )


def parse_firmware_rev(rev: str) -> InstalledFirmware | None:
    """Split the device's `<board>|<git describe>` string; None on garbage."""
    i = rev.find("|")
    if i <= 0 or i >= len(rev) - 1:
        return None
    return InstalledFirmware(rev[:i], rev[i + 1 :])


def _strip_v(s: str) -> str:
    if len(s) < 2 or s[0] != "v" or s[1] not in "0123456789":
        return s
    return s[1:]


def describe_matches_tag(describe: str, tag: str) -> bool:
    """Whether the device is already running the bits of release `tag`. The
    identity key is the git-describe string — CI builds check out the tag
    with a clean tree, so a release flashed properly describes exactly as
    the tag (`v0.2.0`). This comparison is direction-agnostic by design:
    "matches the channel target" is the only thing the update flow offers
    or doesn't."""
    return _strip_v(describe.strip()) == _strip_v(tag.strip())


def firmware_image_name(tag: str) -> str:
    """The image asset a release publishes, per the firmware repo's release
    workflow. One build covers every board; a wrong-chip image is the
    device's own OTA validation's job to reject (esp_ota_end), not this
    name's."""
    return f"dynamite-sampler-firmware-release-{tag}.bin"


def select_target(
    releases: list[GithubRelease], channel: FirmwareChannel
) -> FirmwareRelease | None:
    """Pick the release a device on `channel` should run: the newest
    (semver-max) non-draft release — stable excludes prereleases — that
    carries its image and `.sha256` checksum assets. None when nothing
    qualifies (no releases yet, or none with both assets attached yet).

    TODO(runbook): tags are the version contract — never publish a
    backport for an older line once a newer release exists; under the
    direction-agnostic offer rule that would downgrade-offer the fleet."""
    best = None
    for release in releases:
        if release.draft:
            continue
        if channel is FirmwareChannel.STABLE and release.prerelease:
            continue
        version = FirmwareVersion.try_parse(release.tag)
        if version is None:
            continue
        if best is not None and best.version >= version:
            continue
        image_name = firmware_image_name(release.tag)
        image = next((a for a in release.assets if a.name == image_name), None)
        if image is None:
            continue
        sha256 = next(
            (a for a in release.assets if a.name == f"{image.name}.sha256"), None
        )
        if sha256 is None:
            continue
        best = FirmwareRelease(
            tag=release.tag,
            version=version,
            asset_name=image.name,
            size=image.size,
            download_url=image.url,
            sha256_url=sha256.url,
        )
    return best


def _http_get(url: str, headers: dict) -> tuple[int, bytes]:
    """(status, body). Network failures are FirmwareCatalogError; non-2xx
    comes back as its status for the caller's message."""
    request = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=_HTTP_TIMEOUT_S) as res:
            return res.status, res.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()
    except urllib.error.URLError as exc:
        raise FirmwareCatalogError(f"Request to {url} failed: {exc.reason}") from exc


class GithubReleaseCatalog:
    """GitHub Releases of the public firmware repo as the catalog.

    The raw releases list is used rather than the `/latest` endpoint:
    `/latest` is "most recently published", not "newest version", and the
    offer rule is direction-agnostic, so a backport flipping `/latest`
    backward would downgrade-offer every newer unit. List + semver-max has
    no such window.

    Unlike the app there is no download proxy: that hop exists because
    browsers can't fetch `browser_download_url` (GitHub serves it without
    CORS headers); urllib has no CORS.
    """

    def __init__(self, fetch=None):
        # fetch(url, headers) -> (status, body bytes); injectable in tests.
        self._fetch = fetch if fetch is not None else _http_get

    def latest_for(self, channel: FirmwareChannel) -> FirmwareRelease | None:
        """The channel's target release, or None when nothing qualifies.
        Raises FirmwareCatalogError on fetch/parse failures — "couldn't
        check" is surfaced, never silently treated as "up to date"."""
        status, body = self._fetch(RELEASES_URL, _API_HEADERS)
        if status != 200:
            raise FirmwareCatalogError(f"Release check failed (HTTP {status})")
        try:
            releases = [GithubRelease.from_json(r) for r in json.loads(body)]
        except (ValueError, KeyError, TypeError) as exc:
            raise FirmwareCatalogError(f"Unexpected releases payload: {exc}") from exc
        return select_target(releases, channel)

    def download_image(self, release: FirmwareRelease) -> bytes:
        """The release's image bytes, size-checked against the asset
        metadata and SHA-256-verified against its sidecar. Raises
        FirmwareCatalogError on any mismatch — never flash bytes that
        failed the checks."""
        status, data = self._fetch(release.download_url, {})
        if status != 200:
            raise FirmwareCatalogError(
                f"Image download failed (HTTP {status}) for {release.asset_name}"
            )
        if len(data) != release.size:
            raise FirmwareCatalogError(
                f"Image size mismatch for {release.asset_name}: "
                f"{len(data)} bytes, expected {release.size}"
            )
        status, sha = self._fetch(release.sha256_url, {})
        if status != 200:
            raise FirmwareCatalogError(f"Checksum download failed (HTTP {status})")
        expected = sha.decode().split()[0]
        if expected != hashlib.sha256(data).hexdigest():
            raise FirmwareCatalogError(
                f"Image checksum mismatch for {release.asset_name}"
            )
        return data
