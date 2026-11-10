"""Offline tests for the RAG track: no Ollama or Postgres needed."""

import pytest

from docsrag.chunking import Chunk, chunk, fixed, heading
from docsrag.corpus import ROOT, Doc, load_corpus, render_commands
from docsrag.evaluate import (Question, Gold, best_threshold, first_relevant_rank,
                              load_questions, loo_threshold)
from docsrag.retrieve import Hit, Result, rrf
from docsrag.tool import format_result


@pytest.fixture(scope="module")
def corpus():
    return load_corpus()


def test_every_gold_phrase_is_in_its_source(corpus):
    # load_questions raises on a missing phrase, so this guards the question file.
    qs = load_questions(ROOT / "rag" / "questions.yaml", corpus)
    assert sum(q.answerable for q in qs) >= 20
    assert sum(not q.answerable for q in qs) >= 5


def test_a_typo_in_a_gold_phrase_is_rejected(tmp_path, corpus):
    p = tmp_path / "q.yaml"
    p.write_text("questions:\n  - {id: x, style: direct, q: '?', "
                 "answer: [{source: mcp-server.md, contains: 'MCP_READ_ONLY=yes'}]}\n")
    with pytest.raises(ValueError, match="not in mcp-server.md"):
        load_questions(p, corpus)


def test_corpus_excludes_private_notes(corpus):
    # The deployment repo also holds personal lab and setup notes; only
    # product documentation belongs in the public corpus.
    text = " ".join(d.text for d in corpus).lower()
    for marker in ("gpu lab:", "bw_session", "@live.com"):
        assert marker not in text


def test_cli_reference_follows_the_allowlist():
    ref = render_commands()
    for cmd in ("vinfo", "vreport", "vkill", "vset"):
        assert f"## {cmd}" in ref
    assert "Allowed values: up, down, maint, dev, allocated, reserved" in ref
    assert "Highest blast radius" in ref  # the not-exposed rationale is kept


def test_fixed_windows_overlap_and_cover_everything():
    doc = Doc("d.md", " ".join(f"w{i}" for i in range(450)))
    cs = fixed(doc, size=200, overlap=40)
    assert [len(c.text.split()) for c in cs] == [200, 200, 130]
    assert cs[1].text.split()[0] == "w160"
    assert cs[-1].text.split()[-1] == "w449"


def test_heading_chunks_carry_their_path_and_ignore_code_comments():
    doc = Doc("d.md", "# Top\nintro\n## Install\n```bash\n# not a heading\nmake\n```\n## Use\nrun it\n")
    cs = heading(doc)
    assert [c.heading for c in cs] == ["d.md > Top", "d.md > Top > Install", "d.md > Top > Use"]
    assert "# not a heading" in cs[1].text
    assert cs[1].embed_text.startswith("d.md > Top > Install")


def test_long_sections_split_at_paragraphs():
    body = "\n\n".join(" ".join(["word"] * 100) for _ in range(5))
    cs = heading(Doc("d.md", "# S\n" + body), max_words=250)
    assert [len(c.text.split()) for c in cs] == [200, 200, 100]


def test_chunk_ids_are_unique(corpus):
    for s in ("fixed", "heading"):
        ids = [c.id for c in chunk(corpus, s)]
        assert len(ids) == len(set(ids))


def test_rrf_rewards_agreement():
    fused = rrf(["a", "b", "c"], ["c", "a", "d"])
    assert [c for c, _ in fused][:2] == ["a", "c"]
    assert {c for c, _ in fused} == {"a", "b", "c", "d"}


def test_rank_uses_source_and_phrase():
    q = Question("q", "direct", "?", (Gold("x.md", "needle here"),))
    hits = [Chunk("y.md#0", "y.md", "", "needle here"),     # right text, wrong source
            Chunk("x.md#0", "x.md", "", "hay"),
            Chunk("x.md#1", "x.md", "", "the Needle\n  here")]  # case/whitespace ignored
    assert first_relevant_rank(q, hits) == 3


def test_threshold_separates_and_loo_never_sees_itself():
    xs = [(0.8, True), (0.75, True), (0.6, False), (0.55, False)]
    assert 0.6 < best_threshold(xs) < 0.75
    # Held out, the lone low answerable point is predicted as "abstain".
    sims, ans = [0.8, 0.75, 0.5, 0.6, 0.55], [True, True, True, False, False]
    assert loo_threshold(sims, ans) == [False, False, True, True, True]


def test_weak_matches_are_flagged_not_hidden():
    c = Chunk("x.md#0", "x.md", "x.md > S", "text")
    weak = format_result("q?", Result([Hit(c, 0.6)], 0.6), 0.69)
    strong = format_result("q?", Result([Hit(c, 0.8)], 0.8), 0.69)
    assert weak.startswith("WEAK MATCH") and "[1] x.md > S" in weak
    assert not strong.startswith("WEAK") and "cite the source" in strong
