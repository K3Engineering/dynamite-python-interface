"""Release-selection and catalog tests — a port of the app's
firmware_release_test.dart groups, plus the GithubReleaseCatalog fetch
paths driven through an injected fetch."""

import hashlib
import json

import pytest

import dynamite_sampler as dms
from dynamite_sampler.errors import FirmwareCatalogError
from dynamite_sampler.releases import (
    RELEASES_URL,
    FirmwareChannel,
    FirmwareVersion,
    GithubRelease,
    GithubReleaseCatalog,
    describe_matches_tag,
    firmware_image_name,
    parse_firmware_rev,
    select_target,
)

STABLE = FirmwareChannel.STABLE
BETA = FirmwareChannel.BETA


def v(tag):
    return FirmwareVersion.try_parse(tag)


class TestFirmwareVersion:
    def test_parses_plain_and_prerelease_tags_with_and_without_leading_v(self):
        assert v("v1.2.3") == FirmwareVersion(1, 2, 3)
        assert v("1.2.3") == FirmwareVersion(1, 2, 3)
        assert v("v0.4.0-beta.1") == FirmwareVersion(0, 4, 0, "beta.1")
        assert v("v1.2") is None
        assert v("va1.2.3") is None
        assert v("v1.2.3.4") is None

    def test_numeric_fields_order_numerically(self):
        assert v("v0.2.0") < v("v0.10.0")
        assert v("v1.2.3") > v("v0.99.99")

    def test_a_release_outranks_any_of_its_prereleases(self):
        assert v("v1.2.3-beta.4") < v("v1.2.3")
        assert v("v1.2.3-rc.1") < v("v1.2.3")

    def test_prerelease_segments_compare_numerically_then_lexically(self):
        assert v("v1.2.3-beta.2") < v("v1.2.3-beta.10")  # not string order
        assert v("v1.2.3-beta.1") < v("v1.2.3-rc.1")
        assert v("v1.2.3-beta.1") < v("v1.2.3-beta.1.1")

    def test_label_round_trips_with_a_leading_v(self):
        assert v("1.2.3-beta.4").label == "v1.2.3-beta.4"


class TestIdentityHelpers:
    def test_parse_firmware_rev_splits_board_and_describe(self):
        parsed = parse_firmware_rev("v700P|v0.2.0")
        assert (parsed.board, parsed.describe) == ("v700P", "v0.2.0")
        assert parse_firmware_rev("a|b|c").describe == "b|c"
        for garbage in ("", "v700P", "|v0.2.0", "v700P|"):
            assert parse_firmware_rev(garbage) is None

    def test_describe_matches_tag_is_the_direction_agnostic_identity_check(self):
        assert describe_matches_tag("v0.2.0", "v0.2.0")
        assert describe_matches_tag("0.2.0", "v0.2.0")
        assert not describe_matches_tag("v0.2.0-1-gabcdef", "v0.2.0")
        assert not describe_matches_tag("v0.2.0", "v0.3.0")
        # A leading v strips only ahead of a digit.
        assert not describe_matches_tag("vbeta", "beta")


def _release_json(tag, *, draft=False, prerelease=False, assets=None):
    return {
        "tag_name": tag,
        "draft": draft,
        "prerelease": prerelease,
        "assets": assets if assets is not None else _release_assets(tag),
    }


def _release_assets(tag):
    image = firmware_image_name(tag)
    return [
        {"name": image, "size": 100, "browser_download_url": f"https://x/{image}"},
        {
            "name": f"{image}.sha256",
            "size": 64,
            "browser_download_url": f"https://x/{image}.sha256",
        },
    ]


def _release(tag, **kwargs):
    return GithubRelease.from_json(_release_json(tag, **kwargs))


def _releases(*tags):
    return [_release(t, prerelease="-" in t) for t in tags]


class TestSelectTarget:
    def test_stable_picks_the_newest_non_prerelease_with_an_image(self):
        # Backports first: selection is semver-max, not list order.
        target = select_target(_releases("v0.2.1", "v0.3.0", "v0.1.0"), STABLE)
        assert target.tag == "v0.3.0"

    def test_beta_includes_prereleases_and_orders_them_below_their_release(self):
        releases = _releases("v0.3.0", "v0.4.0-beta.2", "v0.4.0-beta.1")
        assert select_target(releases, BETA).tag == "v0.4.0-beta.2"
        # The stable channel does not see them at all.
        assert select_target(releases, STABLE).tag == "v0.3.0"

    def test_drafts_unparseable_tags_and_asset_less_releases_are_skipped(self):
        releases = [
            _release("v9.9.9", draft=True),
            _release("not-a-version"),
            _release("v9.9.8", assets=[]),
            _release("v0.1.0"),
        ]
        assert select_target(releases, STABLE).tag == "v0.1.0"

    def test_returns_none_when_nothing_qualifies(self):
        assert select_target([], STABLE) is None
        assert select_target(_releases("v0.4.0-beta.1"), STABLE) is None

    def test_skips_releases_without_the_sha256_sidecar(self):
        no_sidecar = GithubRelease.from_json(
            _release_json("v9.9.9", assets=_release_assets("v9.9.9")[:1])
        )
        assert select_target([no_sidecar, _release("v0.1.0")], STABLE).tag == "v0.1.0"

    def test_release_carries_the_image_metadata(self):
        target = select_target(_releases("v0.3.0"), STABLE)
        image = firmware_image_name("v0.3.0")
        assert target.asset_name == image
        assert target.download_url.endswith(image)
        assert target.sha256_url.endswith(f"{image}.sha256")
        assert target.size == 100
        assert target.version == v("v0.3.0")


def _fetch_map(mapping):
    """fetch(url, headers) -> (status, body) over a url -> (status, body)
    mapping; anything else is a 404."""
    return lambda url, headers: mapping.get(url, (404, b""))


class TestGithubReleaseCatalog:
    def test_latest_for_parses_the_api_payload(self):
        payload = json.dumps([_release_json("v0.2.0")]).encode()
        catalog = GithubReleaseCatalog(fetch=_fetch_map({RELEASES_URL: (200, payload)}))
        assert catalog.latest_for(STABLE).tag == "v0.2.0"

    def test_latest_for_surfaces_a_non_200(self):
        catalog = GithubReleaseCatalog(fetch=_fetch_map({}))
        with pytest.raises(FirmwareCatalogError, match="HTTP 404"):
            catalog.latest_for(STABLE)

    def test_latest_for_surfaces_a_bad_payload(self):
        catalog = GithubReleaseCatalog(fetch=_fetch_map({RELEASES_URL: (200, b"[{")}))
        with pytest.raises(FirmwareCatalogError, match="payload"):
            catalog.latest_for(STABLE)

    def test_download_image_verifies_size_and_checksum(self):
        image = b"\xaa" * 100
        release = _target("v0.2.0")
        sha = hashlib.sha256(image).hexdigest().encode() + b"  image.bin\n"
        fetch = _fetch_map(
            {
                release.download_url: (200, image),
                release.sha256_url: (200, sha),
            }
        )
        assert GithubReleaseCatalog(fetch=fetch).download_image(release) == image

    def test_download_image_rejects_a_size_mismatch(self):
        release = _target("v0.2.0")
        fetch = _fetch_map({release.download_url: (200, b"\xaa" * 99)})
        with pytest.raises(FirmwareCatalogError, match="size mismatch"):
            GithubReleaseCatalog(fetch=fetch).download_image(release)

    def test_download_image_rejects_a_checksum_mismatch(self):
        image = b"\xaa" * 100
        release = _target("v0.2.0")
        fetch = _fetch_map(
            {
                release.download_url: (200, image),
                release.sha256_url: (200, ("0" * 64).encode()),
            }
        )
        with pytest.raises(FirmwareCatalogError, match="checksum mismatch"):
            GithubReleaseCatalog(fetch=fetch).download_image(release)

    def test_catalog_errors_join_the_hierarchy(self):
        assert issubclass(FirmwareCatalogError, dms.DynamiteError)


def _target(tag):
    return select_target([_release(tag)], STABLE)
