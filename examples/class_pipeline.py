"""Example: Class Decoration with @nb.fn()

Demonstrates:
- @nb.fn() on a class wraps all methods with scope tracking
- Methods are grouped under the class name in the DAG
- nb.ui() for run-level UI configuration
- Standalone @nb.fn() functions called within a class inherit the group
"""

import time
import nebo as nb


@nb.fn()
def fetch_context(query: str) -> list[str]:
    """Retrieve relevant context documents for the query."""
    nb.log_text("status", f"Fetching context for: {query}")
    time.sleep(0.2)
    return [f"Document about {query}", f"Reference for {query}"]


@nb.fn()
class Agent:
    """An AI agent that processes queries through think-act-reflect steps."""

    def think(self, query: str, context: list[str]):
        """Analyze the query and context to form a plan."""
        nb.log_text("thoughts", f"Thinking about: {query}")
        nb.log_text("thoughts", f"Using {len(context)} context documents")
        nb.log_line("context_docs", float(len(context)))
        time.sleep(0.3)
        return {"plan": f"Respond to '{query}' using context"}

    def act(self, plan: dict) -> str:
        """Execute the plan to generate a response."""
        nb.log_text("actions", f"Acting on plan: {plan['plan']}")
        time.sleep(0.3)
        result = f"Response based on: {plan['plan']}"
        nb.log_text("actions", f"Generated response: {result[:50]}...")
        return result

    def reflect(self, query: str, response: str) -> dict:
        """Evaluate the quality of the response."""
        nb.log_text("reflections", "Reflecting on response quality")
        score = 0.85
        nb.log_line("quality_score", score)
        time.sleep(0.2)
        return {"score": score, "response": response}


def main():
    """Run the agent pipeline."""

    # Configure UI defaults
    nb.ui(layout="horizontal", theme="dark")

    nb.md("An agent-style pipeline using class decoration for grouped nodes.")

    query = "What is nebo?"
    context = fetch_context(query)

    agent = Agent()
    plan = agent.think(query, context)
    response = agent.act(plan)
    result = agent.reflect(query, response)

    print(f"\nResult: score={result['score']}, response={result['response'][:60]}...")


if __name__ == "__main__":
    main()
