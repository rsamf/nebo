"""Example 1: Basic Pipeline with DAG Inference and Logging

Demonstrates:
- @nb.fn() decorator with automatic DAG edge inference
- Automatic source node detection (in-degree 0)
- nb.log_text() for named text streams
- nb.track() for progress tracking
- nb.md() for workflow-level documentation
Key concept: DAG edges are inferred when one @fn function calls another
@fn function. The caller becomes the parent, and the callee becomes the child.
Source nodes are those with in-degree 0 (nothing calls them).
"""

import time
import nebo as nb


@nb.fn()
def clean_text(documents: list[dict]) -> list[dict]:
    """Clean and normalize document text by lowering case and stripping whitespace."""
    cleaned = []
    for doc in nb.track(documents, name="cleaning"):
        cleaned_doc = {
            **doc,
            "content": doc["content"].lower().strip(),
            "cleaned": True,
        }
        cleaned.append(cleaned_doc)
        time.sleep(0.3)
    nb.log_text("status", f"Cleaned {len(cleaned)} documents")
    return cleaned


@nb.fn()
def extract_keywords(documents: list[dict]) -> list[dict]:
    """Extract keywords from cleaned documents using simple word frequency."""
    keywords_list = ["python", "ai", "machine", "learning", "data", "science"]
    results = []
    for doc in nb.track(documents, name="extracting"):
        words = doc["content"].split()
        found = [w for w in words if w in keywords_list]
        result = {
            "path": doc["path"],
            "keywords": found,
            "keyword_count": len(found),
        }
        results.append(result)
        nb.log_text("keywords", f"Found {len(found)} keywords in {doc['path']}")
        time.sleep(0.3)
    return results


@nb.fn()
def summarize(results: list[dict]) -> str:
    """Generate a summary report of keyword extraction results."""
    total_keywords = sum(r["keyword_count"] for r in results)
    summary = (
        f"Processed {len(results)} documents, found {total_keywords} total keywords."
    )
    nb.log_text("summary", summary)
    return summary


@nb.fn()
def run_pipeline(file_paths: list[str]) -> str:
    """Run the full text processing pipeline end-to-end.

    This is the source node — it calls clean_text → extract_keywords → summarize,
    and nebo infers the DAG edges automatically from the call chain.
    """
    # Simulate loading documents
    documents = []
    for path in nb.track(file_paths, name="loading"):
        doc = {
            "path": path,
            "content": f"This is the content of {path}. It has Python, AI, and machine learning keywords.",
            "size": len(path) * 100,
        }
        documents.append(doc)
        nb.log_text("status", f"Loaded document: {path} ({doc['size']} bytes)")
        time.sleep(0.3)

    # Pipeline: each call creates a DAG edge from run_pipeline → callee
    cleaned = clean_text(documents)
    keywords = extract_keywords(cleaned)
    result = summarize(keywords)

    return result


def main():
    """Run the text processing pipeline."""

    # Emitted outside any @nb.fn() — lands on the Global loggable and
    # appears inline with node rows in the terminal display.
    nb.log_text("status", "pipeline starting")

    # Describe the workflow for AI agents
    nb.md(
        "A simple text processing pipeline that loads documents, cleans them, and extracts keywords."
    )

    file_paths = [
        "docs/intro.txt",
        "docs/chapter1.txt",
        "docs/chapter2.txt",
        "docs/conclusion.txt",
        "docs/appendix.txt",
    ]

    result = run_pipeline(file_paths)
    print(f"\nSummary: {result}")


if __name__ == "__main__":
    main()
