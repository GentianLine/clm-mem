"""Layout contract for the web studio: three independent scroll regions.

A headless browser is not available in the sandbox, so the invariants that keep
the side panels from scrolling along with the chat column are asserted against
the shipped assets themselves. They are cheap, and they encode the rules the
CSS/JS actually rely on:

  * the page itself never scrolls (`html, body { overflow: hidden }`);
  * `main` fills the frame instead of borrowing a magic header height;
  * every scroll region opts out of the automatic minimum size (`min-height: 0`)
    and stops scroll chaining (`overscroll-behavior: contain`);
  * nothing uses `scrollIntoView`, which scrolls *every* scrollable ancestor.
"""

import os
import re

import clm_mem

STATIC = os.path.join(os.path.dirname(clm_mem.__file__), "static")


def read(name: str) -> str:
    with open(os.path.join(STATIC, name), encoding="utf-8") as fh:
        return fh.read()


def declarations(css: str, selector: str) -> dict:
    """Declarations of the first top-level rule whose selector matches."""
    pat = re.compile(r"(?:^|\n)" + re.escape(selector) + r"[^{}]*\{([^{}]*)\}", re.M)
    m = pat.search(css)
    assert m, f"no rule found for {selector!r}"
    out = {}
    for decl in m.group(1).split(";"):
        d = decl.strip()
        if not d or d.startswith("/*"):
            continue
        key, _, val = d.partition(":")
        out[key.strip()] = " ".join(val.strip().split())
    return out


CSS = read("style.css")
JS = read("app.js")
HTML = read("index.html")


# -- the frame ---------------------------------------------------------------
def test_page_itself_never_scrolls():
    assert declarations(CSS, "html, body")["overflow"] == "hidden"


def test_main_never_borrows_a_magic_header_height():
    decls = declarations(CSS, "main")
    assert "height" not in decls, "main must not hardcode its height"
    assert decls.get("flex", "").startswith("1")
    assert decls.get("min-height") == "0"
    # no viewport arithmetic anywhere near the layout frame
    assert "calc(100vh" not in CSS


def test_columns_do_not_share_a_scroll_region():
    decls = declarations(CSS, "aside, section")
    assert decls["overflow"] == "hidden"
    assert decls["min-height"] == "0"


def test_hidden_attribute_beats_display_rules():
    # `.tab` sets display:flex, which would otherwise override the UA sheet
    assert declarations(CSS, "[hidden]")["display"] == "none !important"


# -- each scroll region owns its own scroll ----------------------------------
def test_scroll_regions_are_self_contained():
    for selector in ("#wsList", "#messages"):
        decls = declarations(CSS, selector)
        assert decls["overflow-y"] == "auto", selector
        assert decls["min-height"] == "0", selector
        assert decls["overscroll-behavior"] == "contain", selector


def test_inspector_tabs_scroll_without_squishing_their_children():
    decls = declarations(CSS, ".tab")
    assert decls["overflow-y"] == "auto"
    assert decls["min-height"] == "0"
    assert declarations(CSS, ".tab > *")["flex-shrink"] == "0"
    listing = declarations(CSS, ".tab-list")
    assert listing["overflow-y"] == "auto"
    assert listing["min-height"] == "0"


def test_lists_pin_their_heads():
    # context + archive keep their meter / search field out of the scroll flow
    assert HTML.count('class="tab-head"') == 2
    for list_id in ("blockList", "archiveList", "eventList"):
        assert re.search(
            r'id="%s" class="tab-list"' % list_id, HTML
        ), f"#{list_id} should scroll on its own"
    # narrow screens switch one panel per view instead of sharing a column
    assert 'class="view-btns"' in HTML
    assert re.search(r'<body class="view-chat">', HTML)


# -- scripting ---------------------------------------------------------------
def test_nothing_chains_an_ancestor_scroll():
    assert "scrollIntoView" not in JS, (
        "scrollIntoView scrolls every scrollable ancestor, which is exactly "
        "what pins the side panels to the chat column; use scrollToBottom()/revealIn()"
    )


def test_scrolling_helpers_are_offset_scoped():
    assert "function scrollToBottom" in JS
    assert "function revealIn" in JS
    assert "const atBottom" in JS or "atBottom = (" in JS


def test_background_rebuilds_keep_their_readers():
    # every list rebuild must run inside keepScroll, otherwise the 4-second
    # poll sends anyone scrolling a side panel back to its top
    assert "function keepScroll" in JS
    assert JS.count("keepScroll(ul") == 5, (
        "wsList / blockList / archiveList / eventList / fileList all "
        "rebuild under keepScroll"
    )


def test_events_timeline_anchors_to_bottom():
    body = JS[JS.index("function renderEvents") : JS.index("function primeTuning")]
    assert "keepScroll(ul" in body
    assert ", true);" in body, "head-growing lists anchor to the bottom edge"


def test_panels_announce_their_state():
    assert 'role="tablist"' in HTML
    assert HTML.count('role="tab"') == 5
    assert HTML.count('role="tabpanel"') == 5
    assert "aria-pressed" in HTML and 'setAttribute("aria-pressed"' in JS
    assert 'setAttribute("aria-selected", "true")' in JS
