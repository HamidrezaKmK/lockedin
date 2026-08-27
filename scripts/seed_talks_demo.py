"""Seed a throwaway account with a bubble that already has chalk talks on it.

    LOCKEDIN_HOME=/tmp/li_talks uv run python scripts/seed_talks_demo.py

Creates ``talks`` / ``talks``, a diffusion-schedules bubble with a few report pages, and three
dated decks: one carrying open marks, one whose marks the agent has already answered, and one
that was revised twice in response to marks so the history has something in it.
"""
from __future__ import annotations

import sys

from lockedin import auth, bubbles, paths, talks, workspaces

USER, PASSWORD = "talks", "talks"
BUBBLE = "Diffusion noise schedules"

OVERVIEW = """\
# Overview

We think the standard linear $\\beta$-schedule spends most of its training steps in a regime
where the score is nearly trivial to predict, so the model's capacity is misallocated.

The argument is built up in the chalk talks and lands here once it survives review. See
[[SNR reparameterisation]] for the settled derivation.
"""

SNR_PAGE = """\
# SNR reparameterisation

Settled on Aug 24 after two rounds of marks on the Aug 22 talk.

The claim, in the form that survived: uniform-in-log-SNR training reaches equal FID in fewer
gradient steps than uniform-in-$t$, on CIFAR-10 at matched architecture and matched FLOPs.
"""

DECK_VARIANCE = """\
<!-- slide: kind=setup, date=2026-08-27, v=1 -->
# What we're changing, in one line

*So the rest of the talk has somewhere to stand.*

Training samples $t$ uniformly and then maps it to a noise level. I want to sample the **noise
level** uniformly instead, in log-SNR space, and let $t$ follow.

> The claim: this changes which regions of the diffusion the model spends its capacity on, and
> the linear schedule spends it badly.

Everything downstream is about whether the loss is still the same objective after that change of
variables. My answer is *almost*.

---

<!-- slide: kind=derivation, date=2026-08-27, v=1 -->
# The ELBO, before we touch anything

*Standard, stated only so the next slide has a baseline.*

1. $L = \\mathbb{E}_q[\\sum_t D_{KL}(q(x_{t-1}|x_t,x_0) \\| p_\\theta(x_{t-1}|x_t))]$
2. $= \\mathbb{E}_{t,\\epsilon}[w(t)\\,\\|\\epsilon_\\theta(x_t,t) - \\epsilon\\|^2] + C$
3. $t \\sim U\\{1..T\\}$, so every noise level is weighted by how much $t$-mass sits near it.

> That last line is the whole problem: $t$-mass and difficulty are not the same thing.

---

<!-- slide: kind=derivation, date=2026-08-27, v=1 -->
# The residual term survives the change of variables

*Every step is exact except one, and I've marked which.*

1. $\\lambda(t) = \\log(\\bar\\alpha_t / (1-\\bar\\alpha_t))$, and $d\\lambda/dt < 0$ strictly, so
   $\\lambda$ is a valid reparameterisation.
2. $L = \\mathbb{E}_{\\lambda \\sim p(\\lambda)}[w(\\lambda)\\|\\epsilon_\\theta - \\epsilon\\|^2]\\cdot|dt/d\\lambda|$
3. Here I assume $w(\\lambda)\\cdot|dt/d\\lambda| \\to \\text{const}$, which kills the variance term.
4. $L \\approx \\mathbb{E}_{\\lambda \\sim U[\\lambda_{min},\\lambda_{max}]}[\\|\\epsilon_\\theta - \\epsilon\\|^2] + R$
5. $R = \\tfrac{1}{2}\\mathrm{Var}_\\lambda[w(\\lambda)] + O(\\Delta\\lambda^2)$ — which is not
   obviously negligible, and I can't bound it.

---

<!-- slide: kind=comparison, date=2026-08-26, v=1 -->
# Two ways to fix it, and what each costs

*I have a preference but it's weakly held.*

**A — keep the residual, bound it.** Carry $R$ through and show it is $O(\\Delta\\lambda^2)$ under a
Lipschitz condition on $w$. Honest, but the condition may not hold for the linear schedule, which
is the case we care about.

**B — choose $w$ to kill it.** Pick the weighting so $w(\\lambda)|dt/d\\lambda|$ is constant by
construction. Then the assumption is a definition rather than a claim — but it is a different
objective from the one everyone reports.

> My call: **B**, with A in an appendix. B is defensible and it is what we would actually train.
> The risk is a reviewer calling it a different loss.

---

<!-- slide: kind=ask, date=2026-08-27, v=1 -->
# What I need from you

*Three things, in order of how stuck I am.*

1. Is the step-3 assumption salvageable, or do I abandon it? I have been circling this for two days.
2. A or B on the previous slide — do you agree B is worth the "different objective" objection?
3. If B: should the CIFAR runs use the new weighting, or both, for the comparison to be fair?
"""

DECK_SAMPLER = """\
<!-- slide: kind=comparison, date=2026-08-25, v=1 -->
# Ancestral vs. DDIM under the new schedule

*They agree above ~50 steps and diverge sharply below.*

- **A — Ancestral:** $x_{t-1} = \\mu_\\theta(x_t, \\lambda_t) + \\sigma_t z$ — keeps the injected noise.
- **B — DDIM:** deterministic, $\\sigma_t = 0$ — fewer steps, but assumes the marginals still match
  after reparameterisation.

> My call: ancestral. The DDIM assumption is the one I am least sure of.

---

<!-- slide: kind=ask, date=2026-08-25, v=1 -->
# The one thing I'd want checked

*Cheap to run if you think it is worth it.*

A step-count sweep from 10 to 250 on both samplers, same seeds, FID at each point. Half a day of
compute. If they agree everywhere above 50 the choice does not matter and I will stop thinking
about it.
"""

DECK_CLAIM_V1 = """\
<!-- slide: kind=setup, date=2026-08-22, v=1 -->
# The claim, stated so it can be wrong

*The core of the project in one sentence.*

Uniform-in-log-SNR training beats uniform-in-$t$ training.

---

<!-- slide: kind=evidence, date=2026-08-24, v=1 -->
# Where the capacity goes

*Landed in the document as its own section.*

The Jacobian-weighted loss weight is flat on the right and blows up on the left for the linear
schedule — which is exactly the regime the whole idea is about.
"""


def main() -> int:
    accounts = auth.load_accounts()
    if USER not in accounts:
        auth.create_user(USER, PASSWORD)
        accounts = auth.load_accounts()
    ws = workspaces.ensure_personal(USER, accounts.get(USER, {}))
    home = workspaces.workspace_home(ws["id"])

    with paths.use_root(home):
        slug = bubbles.create_bubble(BUBBLE) if not bubbles.load_registry().get(
            "bubbles", {}).get("diffusion-noise-schedules") else "diffusion-noise-schedules"
        bubbles.approve_bubble(slug)
        bubbles.set_premise(
            slug,
            abstract="We think the standard linear β-schedule spends most of its training steps "
                     "in a regime where the score is nearly trivial to predict, so the model's "
                     "capacity is misallocated. If we reparameterise the schedule by log-SNR and "
                     "sample uniformly there, the effective task difficulty is roughly flat "
                     "across t, and we should get better samples at equal compute.",
            goal="Establish — analytically, then on CIFAR-10 — that uniform-in-log-SNR training "
                 "dominates linear at matched FLOPs, and characterise where it doesn't.")
        bubbles.ensure_pages(slug)
        bubbles.save_page(slug, "overview", OVERVIEW)
        pg = bubbles.create_page(slug, "SNR reparameterisation")
        bubbles.save_page(slug, pg, SNR_PAGE)
        # Enough pages to see what the band does at scale, not just at four.
        for extra in ("Experiments", "Open questions", "Related work", "Sampler ablations",
                      "Cosine schedule notes", "Failure cases", "Compute budget",
                      "Reviewer objections", "Figures and plots", "Meeting notes"):
            bubbles.create_page(slug, extra)

        # --- talk 1: open marks waiting on you -------------------------------
        t1 = talks.create_talk(
            slug, "Why the variance term doesn't vanish", date="2026-08-27", kicker="derivation",
            intent="I derived the ELBO under the reparameterised schedule and there's a residual "
                   "term I can't argue away. I need you on slide 3.",
            body=DECK_VARIANCE)
        talks.add_note(slug, t1, slide=2, kind="bad", author=USER,
                       quote="Here I assume $w(\\lambda)\\cdot|dt/d\\lambda| \\to \\text{const}$",
                       text="Only true in the high-SNR tail. At λ < −6 the Jacobian blows up — "
                            "that's exactly the regime the whole idea is about. You can't assume "
                            "away the thing you're studying.")
        talks.add_note(slug, t1, slide=2, kind="q", author=USER, quote="+ R",
                       text="Where did R come from? It appears without introduction.")
        talks.add_note(slug, t1, slide=1, kind="more", author=USER,
                       quote="$t$-mass and difficulty are not the same thing",
                       text="Show me this for the cosine schedule too, then put the result in "
                            "the document.")

        # --- talk 2: marks the agent has already answered ---------------------
        t2 = talks.create_talk(
            slug, "Two ways to implement the sampler", date="2026-08-25", kicker="implementation",
            intent="Ancestral vs. DDIM-style under the new schedule; they disagree below ~50 "
                   "steps. I recommend ancestral and want a sanity check.",
            body=DECK_SAMPLER)
        n = talks.add_note(slug, t2, slide=0, kind="more", author=USER,
                           quote="assumes the marginals still match",
                           text="Match in what sense — in distribution, or pathwise? Those give "
                                "different answers here.")
        talks.reply_note(slug, t2, n["id"], "agent",
                         "In distribution only. Pathwise fails as soon as σ_t = 0; the document "
                         "now says so explicitly.")

        # --- talk 3: revised twice, so the history has something in it --------
        t3 = talks.create_talk(
            slug, "What the log-SNR reparameterisation actually buys", date="2026-08-22",
            kicker="derivation",
            intent="The core claim, from scratch. You said “go deeper” on slide 1 — I expanded it "
                   "and wrote the result up in the document.",
            body=DECK_CLAIM_V1)
        n1 = talks.add_note(slug, t3, slide=0, kind="bad", author=USER,
                            quote="beats uniform-in-$t$ training",
                            text="“Beats” how, and measured with what? Not falsifiable as written.")
        talks.revise_slide(
            slug, t3, 0,
            why="you marked it ✗ this is wrong — “beats” isn't a claim, it's a hope",
            body="Uniform-in-log-SNR training reaches equal FID in fewer gradient steps than\n"
                 "uniform-in-$t$, on CIFAR-10 at matched architecture.")
        talks.reply_note(slug, t3, n1["id"], "agent",
                         "Rewritten in v2 — equal FID in fewer gradient steps, on CIFAR-10.")
        n2 = talks.add_note(slug, t3, slide=0, kind="more", author=USER, version=2,
                            quote="at matched architecture",
                            text="Add what would falsify it, otherwise it's still not a real claim.")
        talks.revise_slide(
            slug, t3, 0,
            why="you marked it → go deeper — say what would falsify it",
            body="Uniform-in-log-SNR training reaches equal FID in fewer gradient steps than\n"
                 "uniform-in-$t$, on CIFAR-10 at matched architecture and matched FLOPs.\n\n"
                 "> Falsified if: the gap closes at long training horizons, or if it doesn't\n"
                 "> survive a change of dataset. Both are cheap to check and both are in\n"
                 "> Experiments.")
        talks.reply_note(slug, t3, n2["id"], "agent",
                         "v3 adds the falsification conditions and points at Experiments.")

        # a mark on a report page, so feedback/OPEN.md shows both surfaces sharing one vocabulary
        overview = bubbles.get_page(slug, "overview")
        needle = "the score is nearly trivial to predict"
        at = overview.find(needle)
        if at >= 0:
            bubbles.create_comment_state(
                slug, "overview", USER,
                "Trivial by what measure? Give the number, or this is hand-waving.",
                content=overview, base_mtime=None,
                selection_start=at, selection_end=at + len(needle), kind="q")

        idx = talks.load_index(slug)
        for rec in idx["talks"]:
            if rec["id"] == t3:
                rec["landed"] = "SNR reparameterisation"
        talks.save_index(slug, idx)

    print(f"seeded {USER}/{PASSWORD} · bubble '{slug}' · workspace {ws['id']}")
    print(f"  home: {home}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
