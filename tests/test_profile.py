from command_center import profile_example
from command_center.config import LANES


def test_profile_example_lane_labels_match_config_lanes() -> None:
    """profile.py/profile_example.py's LANE_LABELS keys must exactly match
    config.LANES — the dashboard/settings templates do a plain dict lookup
    per lane key, so a mismatch would KeyError at render time."""
    assert set(profile_example.LANE_LABELS.keys()) == set(LANES)


def test_profile_example_has_required_shape() -> None:
    assert isinstance(profile_example.PROFILE, dict)
    for field in ("name", "title", "tagline", "photo_url", "initials"):
        assert field in profile_example.PROFILE

    assert isinstance(profile_example.PROJECTS, list)
    assert len(profile_example.PROJECTS) >= 1
    for project in profile_example.PROJECTS:
        assert {"name", "sentence", "status_tag", "link"} <= project.keys()

    assert isinstance(profile_example.FOOTER_LINKS, dict)
    assert isinstance(profile_example.BUILD_LOG, list)
    assert isinstance(profile_example.MEDIUM_RANKING_CONTEXT, str)
    assert profile_example.MEDIUM_RANKING_CONTEXT
