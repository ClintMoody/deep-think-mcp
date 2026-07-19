"""Tests for deep_think_mcp.lens_loader: critique lens discovery.

Per docs/build-plan.md § "Critique lens library" and
docs/execution-plan.md Task 6: the server bundles 8 critique-lens `.md`
templates inside the package's `lenses/` dir, and users can drop additional
`.md` files into `<root>/lenses/` -- discovered on top of the bundled set,
winning on name collision.

This module is discovery only (no engine logic yet -- that's Task 7), so
these tests only cover: the 8 bundled lenses are found, a custom drop-in is
found, and collision precedence. Per Global Constraints, tests never touch
the real home directory -- every `root` used here is an explicit tmp_path.
"""

from __future__ import annotations

from deep_think_mcp import config, lens_loader

BUNDLED_LENS_NAMES = {
    "overconfidence",
    "weak_evidence",
    "missing_perspective",
    "unstated_assumption",
    "scope_creep",
    "alternative_framing",
    "steel_man",
    "first_principles",
}


# ---------------------------------------------------------------------------
# Bundled discovery (no user root)
# ---------------------------------------------------------------------------


def test_discover_lenses_finds_all_eight_bundled_lenses():
    lenses = lens_loader.discover_lenses()
    assert set(lenses) == BUNDLED_LENS_NAMES


def test_discover_lenses_bundled_content_is_nonempty_text():
    lenses = lens_loader.discover_lenses()
    for name, content in lenses.items():
        assert isinstance(content, str)
        assert len(content.strip()) > 0, f"{name} template is empty"


def test_discover_lenses_names_match_config_default_lenses():
    # The loader's bundled set must exactly match [serial].default_lenses
    # in config/default.toml, and stages.SERIAL_LENS_DEFAULTS -- all three
    # are independent sources of truth for the same 8 lens names and must
    # never drift apart.
    defaults = config.load_defaults()
    assert set(defaults["serial"]["default_lenses"]) == BUNDLED_LENS_NAMES


def test_discover_lenses_with_no_root_ignores_any_user_dir():
    # root defaults to None -- discovery must not reach for a real home
    # directory or any implicit root; it just returns the bundled set.
    lenses = lens_loader.discover_lenses(root=None)
    assert set(lenses) == BUNDLED_LENS_NAMES


# ---------------------------------------------------------------------------
# User drop-ins under <root>/lenses/
# ---------------------------------------------------------------------------


def test_discover_lenses_finds_custom_drop_in_alongside_bundled(tmp_path):
    user_lenses_dir = tmp_path / "lenses"
    user_lenses_dir.mkdir()
    (user_lenses_dir / "my_custom_lens.md").write_text("# Custom\nDo the custom thing.\n")

    lenses = lens_loader.discover_lenses(root=tmp_path)

    assert set(lenses) == BUNDLED_LENS_NAMES | {"my_custom_lens"}
    assert lenses["my_custom_lens"] == "# Custom\nDo the custom thing.\n"


def test_discover_lenses_root_with_no_lenses_subdir_falls_back_to_bundled_only(tmp_path):
    # root exists but has no lenses/ subdir at all.
    lenses = lens_loader.discover_lenses(root=tmp_path)
    assert set(lenses) == BUNDLED_LENS_NAMES


def test_discover_lenses_nonexistent_root_falls_back_to_bundled_only(tmp_path):
    missing_root = tmp_path / "does-not-exist"
    lenses = lens_loader.discover_lenses(root=missing_root)
    assert set(lenses) == BUNDLED_LENS_NAMES


def test_discover_lenses_ignores_non_markdown_files_in_user_dir(tmp_path):
    user_lenses_dir = tmp_path / "lenses"
    user_lenses_dir.mkdir()
    (user_lenses_dir / "notes.txt").write_text("not a lens")
    (user_lenses_dir / "real_lens.md").write_text("a real lens")

    lenses = lens_loader.discover_lenses(root=tmp_path)

    assert "notes" not in lenses
    assert lenses["real_lens"] == "a real lens"


# ---------------------------------------------------------------------------
# Collision precedence: user dir wins over bundled
# ---------------------------------------------------------------------------


def test_discover_lenses_user_dropin_wins_on_name_collision(tmp_path):
    user_lenses_dir = tmp_path / "lenses"
    user_lenses_dir.mkdir()
    override_text = "# Overridden overconfidence lens\nUser-authored replacement.\n"
    (user_lenses_dir / "overconfidence.md").write_text(override_text)

    lenses = lens_loader.discover_lenses(root=tmp_path)

    assert lenses["overconfidence"] == override_text
    # every other bundled lens is untouched
    assert set(lenses) == BUNDLED_LENS_NAMES


def test_discover_lenses_bundled_set_unaffected_by_prior_user_root_call(tmp_path):
    # Regression guard: discover_lenses must never mutate module-level
    # state, so a call with a user root doesn't leak into a later
    # bundled-only call.
    user_lenses_dir = tmp_path / "lenses"
    user_lenses_dir.mkdir()
    (user_lenses_dir / "overconfidence.md").write_text("overridden")

    lens_loader.discover_lenses(root=tmp_path)
    lenses_again = lens_loader.discover_lenses()

    assert lenses_again["overconfidence"] != "overridden"
    assert set(lenses_again) == BUNDLED_LENS_NAMES
