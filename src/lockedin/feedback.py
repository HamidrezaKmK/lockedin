"""One place the user's marks land, whatever surface they were left on.

A mark on a report page and a mark on a chalk-talk slide are the same act — pointing at
something and saying one of five things — so they share a vocabulary and a destination. A
syncing Scientist worker publishes them as a single ``feedback/OPEN.md``, and when nothing is
open the file is not published at all, so an agent opening a clean bubble sees no feedback
machinery whatsoever.

The raw stores (the review sidecar, the talk notes YAML, the snapshots) are never published.
They are working state; carrying them in an agent's context every session is the clutter this
file exists to avoid.
"""

from __future__ import annotations

from . import bubbles, talks

KINDS = talks.KINDS


def _page_blocks(slug: str) -> list[str]:
    """The report-page half of the file.

    A page mark is anchored by the `\\comment{id}{…}` wrapper in the page source rather than by a
    remembered quote. That is the one place pages must differ from slides: a report page is
    hand-edited constantly, and a wrapper moves with the text it surrounds while a quote quietly
    stops matching. The id below is the id in the wrapper — the agent can grep for it.
    """
    blocks = []
    for page in bubbles.list_pages(slug):
        page_slug = page["page_slug"]
        threads = [t for t in bubbles.list_comments(slug, page_slug).get("threads", [])
                   if t.get("status") == "open"]
        if not threads:
            continue
        blocks.append(f"## {page.get('title', page_slug)}  *(report page)*\n\n"
                      f"`reports/pages/{page_slug}.md`\n")
        for t in threads:
            k = KINDS.get(str(t.get("kind") or ""))
            head = f"{k['glyph']} {k['means']}" if k else "comment"
            anchor = t.get("anchor") or {}
            attached = t.get("anchor_state") == "attached"
            lines = [f"### {head} — {page.get('title', page_slug)}",
                     "",
                     f"- **id**: `{t.get('id','')}` — find `\\comment{{{t.get('id','')}}}{{…}}` "
                     f"in the page source; that wrapper *is* the anchor",
                     "- **on**: " + (f"the text `{anchor.get('quote','')}`" if anchor.get("quote")
                                     else "the page as a whole")]
            if not attached:
                lines.append("- **anchor**: ⚠ unanchored — its wrapper was deleted; ask before "
                             "guessing where it belonged")
            for msg in t.get("messages", []):
                if msg.get("body"):
                    lines += ["", f"> {msg['body']}"]
            blocks.append("\n".join(lines) + "\n")
    return blocks


def open_markdown(slug: str) -> bytes | None:
    """Every open mark on this bubble, pages and talks together. None when there are none."""
    blocks = _page_blocks(slug) + talks.feedback_blocks(slug)
    if not blocks:
        return None
    head = ("# Open feedback on this bubble\n\n"
            "Marks the user left while reading — on report pages and on chalk-talk slides. Each\n"
            "one is a pointed finger plus one of these:\n\n"
            "| mark | means |\n|---|---|\n"
            + "".join(f"| {KINDS[k]['glyph']} | {KINDS[k]['means']} |\n" for k in talks.KIND_ORDER)
            + "\n"
            "Work through them **with the user, not for them**: read the mark, look at the picture\n"
            "if there is one, propose a fix, and agree it before you change anything. A `?` means\n"
            "re-explain, not re-derive. A `✗` means the argument is wrong, so re-derive rather\n"
            "than reword. A `→` usually means the expansion belongs in a report page. If you\n"
            "think a mark is mistaken, say so and argue it — do not comply silently. A `✍` is a\n"
            "freehand drawing: its picture carries the whole message, so open it first.\n\n"
            "A mark disappears from this file once it is genuinely resolved, and the file\n"
            "disappears once none are left. Never edit this file or anything under `feedback/`:\n"
            "it is generated, and your edits are overwritten on the next sync.\n\n")
    return (head + "\n".join(blocks)).encode()


def images(slug: str) -> dict:
    """Snapshots for open marks — `feedback/shots/<id>.png` -> stored file."""
    return talks.open_note_images(slug)
