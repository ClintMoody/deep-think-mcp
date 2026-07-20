"""Critique lens discovery for deep-think-mcp.

The lens library is `src/deep_think_mcp/lenses/` -- 8 bundled `.md` files,
one per critique lens, per `docs/build-plan.md` § "Critique lens library"
and the exact 8 names `stages.SERIAL_LENS_DEFAULTS` / config
`[serial].default_lenses` already commit to: `overconfidence, weak_evidence,
missing_perspective, unstated_assumption, scope_creep, alternative_framing,
steel_man, first_principles`.

This module is discovery only -- it has no opinion on what a lens is used
*for*. That's Task 7's serial engine (which will call `critique_current_
thought(lens)` and hand a template's raw text back to the model) and Task
11's subagent adapter (which prepends lens text as specialist prompt
scaffolding). All this module answers is "what lenses exist right now" and
"what's each one's raw template text".

Two sources, lowest to highest precedence:

    bundled lenses (this package's own lenses/ dir, always present)
        < user lenses (`<root>/lenses/`, if it exists)

A same-named user file replaces the bundled one entirely (whole-file
override, not a merge) -- the drop-in contract `docs/execution-plan.md`
Task 6 derives: "users can drop additional `.md` files into `<root>/
lenses/`... user dir wins on name collision".

Why `Path(__file__)`, not `importlib.resources`: unlike `config/
default.toml` (which lives at the *repo root*, outside the installed
package -- see `config.py`'s `PACKAGED_DEFAULT_CONFIG_PATH` comment, which
notes that only resolves in a dev/editable checkout), `lenses/` ships
*inside* `src/deep_think_mcp/` per the project layout. This module is a
sibling of `lenses/` in that same package directory, so
`Path(__file__).resolve().parent / "lenses"` is correct both in a dev
checkout and once installed as a wheel -- the directory travels with the
module, no import-machinery indirection needed.
"""

from __future__ import annotations

from pathlib import Path

PACKAGE_LENSES_DIR = Path(__file__).resolve().parent / "lenses"

_USER_LENSES_DIRNAME = "lenses"


def _read_lens_dir(directory: Path) -> dict[str, str]:
    """Every `*.md` file directly inside `directory`, mapped by filename
    stem to its text content. Returns `{}` if `directory` doesn't exist --
    a missing user `lenses/` dir (or a root that doesn't exist at all) is
    not an error, just "no drop-ins to merge in".
    """
    if not directory.is_dir():
        return {}
    # [task 13 hardening #7] Pin encoding="utf-8": lens templates are UTF-8
    # (em-dashes, curly quotes, arrows appear in the bundled `.md` files), but
    # `Path.read_text()` with no encoding follows the platform locale, so a
    # server started under a non-UTF-8 `LC_*`/`PYTHONUTF8=0` would raise
    # `UnicodeDecodeError` reading a perfectly valid lens. Reading is
    # deterministic regardless of the host's locale now.
    return {
        path.stem: path.read_text(encoding="utf-8")
        for path in sorted(directory.glob("*.md"))
    }


def discover_lenses(root: Path | str | None = None) -> dict[str, str]:
    """Discover every critique lens currently available.

    Returns `{lens_name: template_text}`. Always includes the 8 bundled
    lenses. If `root` is given and `<root>/lenses/` exists, its `.md`
    files are merged on top -- same-named entries there replace the
    bundled version (see module docstring for the precedence contract).

    `root` defaults to `None`, meaning "bundled lenses only" -- this
    function never reaches for a real home directory or any implicit
    default root on its own; callers that want a user overlay pass their
    resolved data root explicitly (same convention `store.py`/`index.py`
    use for their own `root` parameters).
    """
    lenses = _read_lens_dir(PACKAGE_LENSES_DIR)
    if root is not None:
        user_dir = Path(root).expanduser() / _USER_LENSES_DIRNAME
        lenses.update(_read_lens_dir(user_dir))
    return lenses
