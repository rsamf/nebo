"""Summarize-and-judge chain over real articles with a local flan-t5-small.

Run:  python chain.py             # 12 docs, ~3-6 min CPU (first run downloads ~300 MB)
      python chain.py --smoke     # 2 docs
      python chain.py --template one-sentence
"""
from __future__ import annotations

import argparse
import re
import time
from collections import Counter, defaultdict
from pathlib import Path

import nebo as nb

CORPUS_DIR = Path(__file__).parent / "corpus"
DEFAULT_MODEL = "google/flan-t5-small"

TEMPLATES = {
    "plain": "Summarize: {text}",
    "one-sentence": "Summarize the following article in one sentence: {text}",
    "explain-simple": "Explain the following article in simple words for a student: {text}",
}

STOPWORDS = set(
    "the a an of and or to in is are was were for on with as by at it its "
    "this that from be been has have had which their other more most can "
    "may also into such used use between over about".split()
)

nb.md("""
# LLM lab — summarize-and-judge chain

flan-t5-small (~77M parameters, running locally on CPU) reads twelve
real Wikipedia articles — four each in science, history, technology —
and every document takes the same trip: **summarize**, then **judge**.

**Follow one document** (steps are the document index — scrub to
switch):

- `summarize/prompt` and `summarize/output` show exactly what the model
  was asked and what it wrote. Small models are erratic summarizers:
  some outputs are sharp one-liners, others loop ("Learn about
  lithium-ion batteries." ten times over) — that variance is what the
  judge exists to measure.
- The judge asks the same model whether the summary matches the article
  (`judge/verdict`), then blends that yes/no with content-word
  precision (how much of the summary's substance is really in the
  article) and a brevity band into `judge/score` (0-100).
  `judge/rationale` shows the arithmetic behind every verdict.
- `aggregate` rolls the run up: the verdict mix (pie), mean score per
  category (bar), and article-vs-summary length histograms — the
  compression the chain actually achieved.

`experiments.py` runs three prompt templates over the same corpus into
[llm-lab/prompt-experiments](nebo://group/llm-lab/prompt-experiments);
AGENT.md then walks a coding agent through comparing them, logging a
derived chart, setting an alert rule, and publishing its findings as a
group doc.
""")
nb.ui(view="dag", layout="horizontal", tracker="step")

_MODEL_CACHE: dict = {}


def generate(model_name: str, prompt: str, max_new_tokens: int = 80) -> str:
    import torch
    if model_name not in _MODEL_CACHE:
        from transformers import AutoModelForSeq2SeqLM, AutoTokenizer
        tok = AutoTokenizer.from_pretrained(model_name)
        model = AutoModelForSeq2SeqLM.from_pretrained(model_name)
        model.eval()
        _MODEL_CACHE[model_name] = (tok, model)
    tok, model = _MODEL_CACHE[model_name]
    ids = tok(prompt, return_tensors="pt", truncation=True, max_length=512)
    with torch.no_grad():
        out = model.generate(**ids, max_new_tokens=max_new_tokens)
    return tok.decode(out[0], skip_special_tokens=True).strip()


def content_words(text: str) -> set[str]:
    return {w for w in re.findall(r"[a-z']+", text.lower())
            if len(w) > 2 and w not in STOPWORDS}


@nb.fn(ui={"default_tab": "text"})
def load_corpus(limit: int | None) -> list[dict]:
    """Load the committed Wikipedia-extract corpus."""
    docs = []
    for path in sorted(CORPUS_DIR.glob("*__*.txt")):
        category, _, title = path.stem.partition("__")
        docs.append({"title": title.replace("_", " "), "category": category,
                     "text": path.read_text().strip()})
    if limit:
        docs = docs[:limit]
    nb.log_text("corpus/status", f"loaded {len(docs)} documents")
    return docs


@nb.fn(ui={"default_tab": "text"})
def summarize(doc: dict, step: int, template: str, model_name: str) -> str:
    """Generate a summary with the local model."""
    prompt = template.format(text=doc["text"][:3000])
    nb.log_text("summarize/prompt", prompt[:400], step=step)
    t0 = time.perf_counter()
    summary = generate(model_name, prompt, max_new_tokens=80)
    nb.log_line("latency/summarize", round(time.perf_counter() - t0, 3), step=step)
    nb.log_text("summarize/output", summary, step=step)
    return summary


@nb.fn(ui={"default_tab": "text"})
def judge(doc: dict, summary: str, step: int, model_name: str) -> dict:
    """Score the summary: model yes/no + content-word precision + brevity."""
    t0 = time.perf_counter()
    question = (
        f"Article: {doc['text'][:1500]}\n\nSummary: {summary}\n\n"
        "Does the summary accurately describe the article? Answer yes or no."
    )
    answer = generate(model_name, question, max_new_tokens=4)
    yes = answer.lower().startswith("yes")
    sw, aw = content_words(summary), content_words(doc["text"])
    precision = len(sw & aw) / len(sw) if sw else 0.0
    ratio = len(summary.split()) / max(1, len(doc["text"].split()))
    brevity = 1.0 if 0.03 <= ratio <= 0.5 else 0.5
    score = round(100 * (0.75 * precision + 0.25 * brevity) * (1.0 if yes else 0.6), 1)
    if yes and score >= 50:
        verdict = "pass"
    elif score >= 50 or yes:
        verdict = "borderline"
    else:
        verdict = "fail"
    nb.log_line("latency/judge", round(time.perf_counter() - t0, 3), step=step)
    nb.log_line("judge/score", score, step=step)
    nb.log_text("judge/verdict", f"{verdict} ({doc['title']})", step=step)
    nb.log_text(
        "judge/rationale",
        f"model said {answer!r}; content-word precision {precision:.2f}; "
        f"compression ratio {ratio:.2f}",
        step=step,
    )
    return {"title": doc["title"], "category": doc["category"], "score": score,
            "verdict": verdict, "article_words": len(doc["text"].split()),
            "summary_words": len(summary.split())}


@nb.fn(ui={"default_tab": "metrics"})
def aggregate(results: list[dict]) -> dict:
    """Corpus-level rollups: verdict pie, per-category bar, length histograms."""
    verdicts = Counter(r["verdict"] for r in results)
    nb.log_pie("eval/verdicts", dict(verdicts))
    by_cat = defaultdict(list)
    for r in results:
        by_cat[r["category"]].append(r["score"])
    nb.log_bar("eval/avg_score_by_category",
               {c: round(sum(v) / len(v), 1) for c, v in by_cat.items()})
    nb.log_histogram(
        "eval/word_lengths",
        {"article": [r["article_words"] for r in results],
         "summary": [r["summary_words"] for r in results]},
        colors=True,
    )
    avg = round(sum(r["score"] for r in results) / len(results), 1)
    nb.log_text("eval/summary", f"{len(results)} docs, mean score {avg}")
    return {"mean_score": avg, "verdicts": dict(verdicts)}


@nb.fn()
def run_chain(cfg: dict) -> dict:
    nb.log_cfg(cfg)
    docs = load_corpus(cfg["limit"])
    results = []
    for step, doc in enumerate(nb.track(docs, name="documents")):
        summary = summarize(doc, step, cfg["template"], cfg["model"])
        results.append(judge(doc, summary, step, cfg["model"]))
    return aggregate(results)


def main(argv=None) -> dict:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--template", choices=sorted(TEMPLATES), default="plain")
    ap.add_argument("--model", default=DEFAULT_MODEL)
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--smoke", action="store_true", help="2 documents only")
    args = ap.parse_args(argv)
    limit = 2 if args.smoke else args.limit
    cfg = {"model": args.model, "template": TEMPLATES[args.template],
           "template_name": args.template, "limit": limit}
    with nb.start_run(name=f"prompt={args.template}", config=cfg, group="llm-lab"):
        return run_chain(cfg)


if __name__ == "__main__":
    main()
