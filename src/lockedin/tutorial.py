"""The Tutorial bubble every new workspace starts with.

A blank workspace teaches nothing: the first thing a new user sees should be the product
explaining itself in its own medium — a bubble whose premise, document and chalk talks are
*about* working with an agent, already carrying the marks, drawings and history the real
workflow produces. Everything in it is safe to edit, mark up, or delete; the Overview says so.

Seeded once, at workspace creation. Deleting the bubble is the ordinary bubble deletion —
nothing recreates it.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

from . import bubbles, talks

log = logging.getLogger(__name__)

BUBBLE_NAME = "Tutorial"

ABSTRACT = (
    "This bubble is a worked example of how lockedin wants you to talk to an agent: it gives "
    "chalk talks, you answer with marks and drawings, and the document keeps what survives. "
    "Everything here is a prop — edit it, mark it, delete it."
)
GOAL = ("Leave one mark on a slide, connect a repo with the 🤖 button, and delete this bubble "
        "once it has nothing left to teach you.")

OVERVIEW = """\
# Overview

Welcome. This bubble is the product explaining itself in its own medium — the premise above,
this multi-page document, and the chalk talks below are all **about interacting with agents**,
and they already carry the marks, drawings and history the real workflow produces.

Where to look:

- [[The five marks]] — the whole feedback vocabulary, and why it beats prose.
- [[Chalk talks]] — how a deck works: versions, resolving, drawing on slides.
- [[Connecting your repo]] — the one line that puts an agent inside this bubble.

When it has nothing left to teach you, delete the whole bubble from the Bubbles list — it will
not come back.
"""

MARKS_PAGE = """\
# The five marks

Prose feedback makes an agent guess your intent. A mark does not:

| mark | means | the agent will |
|---|---|---|
| ✗ | this is wrong | re-derive, not reword |
| ? | I don't follow | re-explain, not re-derive |
| → | go deeper | expand, usually into a page here |
| ✓ | good, keep this | lean on it |
| ✂ | cut this | remove it |

A mark alone is a complete comment — tapping ✗ on a sentence says everything it needs to.

The same marks work on this document: select any rendered text and pick one. This very page
carries an open comment as a demonstration — toggle the right pane (◨, top corner) and the
highlight appears in the text with its thread beside it.

There is also a sixth mark you cannot pick from the menu: **✍ drawn**. Hit *draw* on any slide
and sketch — cross a line out, arrow a paragraph elsewhere, circle the weak step. The drawing
itself is the feedback.
"""

TALKS_PAGE = """\
# Chalk talks

A chalk talk is how the agent asks for your judgement: a dated deck, one idea per slide, the
soft spots named. You read it slide by slide and mark the exact thing that is wrong, unclear,
or good.

What to try on the decks in this bubble:

1. Open *How to ask your agent for work* — it has open marks of every kind, including a drawing.
2. Open *Why marks beat paragraphs* and click the **v2 · ◷** stamp on slide one: the history
   records the mark that caused the revision, and what it asked for.
3. Hit **✎ edit** on any slide — the document's editor opens on it, with every open mark shown
   as a comment wrapper in the text, moving with your edit.
4. Use **＋** to add a slide of your own, or **✂** to remove one.

A resolved mark is deleted, not archived: what it asked for lives on in the slide's history,
and nothing accumulates.
"""

CONNECT_PAGE = """\
# Connecting your repo

The point of all this is an agent that works in your repository and reports here.

1. Hit the 🤖 button in this bubble's header — it hands you one command, pre-signed for your
   machine.
2. Paste it into a bash in your project. It installs the client, signs you in, binds the
   folder, and starts a sync worker.
3. Run your agent as usual — `claude`, `codex`, `agy`. A `.lockedin/` folder now carries this
   bubble to it: the premise, these pages, the papers, and a `feedback/OPEN.md` with every mark
   you leave.

From then on the loop is: it writes decks and pages here, you mark them up, it answers — and
the marks you leave on the Tutorial decks are what that feedback file looks like to a real
agent.
"""

DECK_ASK = """\
<!-- slide: kind=setup, date={d0}, v=1 -->
# A good ask beats a good prompt

*The claim this deck defends, in four slides.*

An agent does its best work when the ask names the deliverable, not the steps.

- Name what you want to exist afterwards.
- Say what you already believe, so it can argue.
- Let the bubble carry the context — that is what it is for.

---

<!-- slide: kind=comparison, date={d0}, v=1 -->
# Two asks, same wish

*Left is a prompt. Right is an ask.*

- "Look into whether the schedule matters" — no deliverable, no stakes, no way to be wrong.
- "Write me a chalk talk arguing the schedule is misallocated — I think slide 2 of the last
  deck already implies it" — a deliverable, a stake, and something to argue with.

The second one can *fail*, which is exactly what makes it answerable.

---

<!-- slide: kind=implementation, date={d0}, v=1 -->
# Where the ask lives

*Not in the chat scrollback.*

- The **goal** line above this bubble is the standing ask — every agent reads it first.
- A **chalk talk request** is a one-off ask: the + add chalk talk button writes it for you.
- A **mark** is the smallest ask there is: one glyph, on the exact sentence.

---

<!-- slide: kind=ask, date={d0}, v=1 -->
# What I need from you

*The last slide of a deck is always this one.*

Mark anything on this deck — the marks are already flowing to `feedback/OPEN.md`, and any
agent connected to this bubble will find them on its next sync.
"""

DECK_MARKS = """\
<!-- slide: kind=derivation, date={d2}, v=1 -->
# Marks survive; paragraphs dissolve

*Why the feedback vocabulary is five glyphs and not a text box.*

A paragraph of feedback has to be interpreted; a mark on a quote does not.

- The mark names the intent; the anchor names the place.
- The pair travels to the agent as text it can grep for.
- What you said stays attached to what you said it about.

---

<!-- slide: kind=evidence, date={d2}, v=1 -->
# The loop, on one slide

*You are looking at the start state of one.*

This slide proves the whole system works perfectly. Marks are always better than prose in
every situation, and no agent ever misreads one.

---

<!-- slide: kind=ask, date={d2}, v=1 -->
# Try the loop yourself

Leave a ? on anything above, then connect a repo and ask the agent to address your feedback on
this bubble. Watch the mark disappear when it genuinely answers.
"""

# What the seeded revision replaces the overclaiming slide with — the ✗ below is consumed by
# this exact rewrite, so the version history carries a true story on day one.
DECK_MARKS_SLIDE2_FINAL = """\
This slide was revised once: a ✗ said the old version overclaimed. The mark is gone — resolved
marks are deleted, not archived — but click the **v2 · ◷** stamp above and the history shows
what it asked for and why the slide changed."""


def _today() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _days_ago(n: int) -> str:
    from datetime import timedelta
    return (datetime.now(timezone.utc) - timedelta(days=n)).strftime("%Y-%m-%d")


def seed() -> str | None:
    """Create the Tutorial bubble in the current workspace root. Returns its slug.

    Best-effort by design: a workspace must never fail to create because its welcome
    content could not be written.
    """
    try:
        return _seed()
    except Exception:
        log.warning("Could not seed the Tutorial bubble.", exc_info=True)
        return None


def _seed() -> str:
    slug = bubbles.create_bubble(BUBBLE_NAME)
    bubbles.approve_bubble(slug)
    bubbles.ensure_pages(slug)
    bubbles.set_premise(slug, abstract=ABSTRACT, goal=GOAL)

    home = bubbles.list_pages(slug)[0]["page_slug"]
    bubbles.save_page(slug, home, OVERVIEW, None)
    for title, body in (("The five marks", MARKS_PAGE),
                        ("Chalk talks", TALKS_PAGE),
                        ("Connecting your repo", CONNECT_PAGE)):
        page_slug = bubbles.create_page(slug, title)
        bubbles.save_page(slug, page_slug, body, None)

    # A live comment thread on the marks page, with the agent's answer — page feedback, shown
    # rather than described.
    content = bubbles.get_page(slug, "the-five-marks")
    anchor = "A mark alone is a complete comment"
    start = content.find(anchor)
    if start >= 0:
        state = bubbles.create_comment_state(
            slug, "the-five-marks", "you",
            "Does that mean the text box is pointless?",
            content=content, base_mtime=None,
            selection_start=start, selection_end=start + len(anchor), kind="q")
        bubbles.reply_comment_state(slug, "the-five-marks", state["thread"]["id"], "agent",
                                    "Not pointless — optional. The mark carries the intent; "
                                    "prose is for whatever the glyph cannot say. This thread "
                                    "is itself the demo: resolve it with ✓ when you are done.")

    d0, d2 = _today(), _days_ago(2)

    # Deck A: open marks of every kind.
    ask = talks.create_talk(slug, "How to ask your agent for work",
                            intent="The claim this deck defends, in four slides.",
                            date=d0, body=DECK_ASK.format(d0=d0))
    wrong = talks.add_note(slug, ask, slide=1, kind="bad", author="you",
                           quote='"Look into whether the schedule matters"',
                           text="Too kind — this one is not even a wish, it is a deflection.")
    talks.reply_note(slug, ask, wrong["id"], "agent",
                     "Fair. v2 will call it what it is; the contrast lands harder.")
    talks.add_note(slug, ask, slide=1, kind="good", author="you",
                   quote="The second one can *fail*, which is exactly what makes it answerable.")
    talks.add_note(slug, ask, slide=2, kind="q", author="you",
                   rect={"x": 6.0, "y": 34.0, "w": 88.0, "h": 30.0},
                   text="Which of these three should a brand-new user reach for first?")
    talks.add_note(slug, ask, slide=0, kind="ink", author="you",
                   paths=[
                       # an underline beneath the first bullet, a ring on the claim, an arrow
                       [{"x": 12, "y": 56}, {"x": 30, "y": 57}, {"x": 46, "y": 56.5}],
                       [{"x": 72.5, "y": 34.5},
                        {"x": 71.6, "y": 37.8},
                        {"x": 68.9, "y": 40.4},
                        {"x": 65.1, "y": 41.8},
                        {"x": 60.9, "y": 41.8},
                        {"x": 57.1, "y": 40.4},
                        {"x": 54.4, "y": 37.8},
                        {"x": 53.5, "y": 34.5},
                        {"x": 54.4, "y": 31.2},
                        {"x": 57.1, "y": 28.6},
                        {"x": 60.9, "y": 27.2},
                        {"x": 65.1, "y": 27.2},
                        {"x": 68.9, "y": 28.6},
                        {"x": 71.6, "y": 31.2},
                        {"x": 72.5, "y": 34.5}],
                       [{"x": 40, "y": 74}, {"x": 56, "y": 44}],
                       [{"x": 56, "y": 44}, {"x": 50, "y": 46}],
                       [{"x": 56, "y": 44}, {"x": 55, "y": 51}],
                   ],
                   covers=["Name what you want to exist afterwards.",
                           "names the deliverable, not the steps."],
                   text="The ringed phrase is the whole deck — consider opening with it.")

    # Deck B: a real resolved-mark history — mark the overclaim, then revise it away with
    # resolves=, exactly as an agent would — plus one mark left open on purpose.
    marks_deck = talks.create_talk(slug, "Why marks beat paragraphs",
                                   intent="Why the feedback vocabulary is five glyphs.",
                                   date=d2, body=DECK_MARKS.format(d2=d2))
    overclaim = talks.add_note(slug, marks_deck, slide=1, kind="bad", author="you",
                               quote="Marks are always better than prose in",
                               text="Overclaimed. Say what the mechanism buys, "
                                    "not that it is perfect.")
    talks.revise_slide(slug, marks_deck, 1, body=DECK_MARKS_SLIDE2_FINAL,
                       sub="You are looking at the end state of one.",
                       why="dropped the overclaim; the loop is the evidence",
                       resolves=[overclaim["id"]])
    talks.add_note(slug, marks_deck, slide=2, kind="more", author="you",
                   quote="connect a repo and ask the agent",
                   text="Deep-link this to the Connecting your repo page?")
    return slug
