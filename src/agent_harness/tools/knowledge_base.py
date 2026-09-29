"""Deterministic keyword overlap search, with stable ordering and bounded excerpts."""

import re

from agent_harness.tools.context import ToolExecutionContext
from agent_harness.tools.data import KnowledgeDataset
from agent_harness.tools.schemas import (
    KnowledgeMatch,
    SearchKnowledgeBaseInput,
    SearchKnowledgeBaseOutput,
)


def tokens(text: str) -> set[str]:
    return set(re.findall(r"[a-z0-9]+", text.casefold()))


class KnowledgeBaseTool:
    def __init__(self, dataset: KnowledgeDataset) -> None:
        self._documents = tuple(
            (book, tokens(f"{book.title} {book.content} {' '.join(book.services)}"))
            for book in dataset.runbooks
        )

    async def __call__(
        self, arguments: SearchKnowledgeBaseInput, context: ToolExecutionContext | None = None
    ) -> SearchKnowledgeBaseOutput:
        query = tokens(arguments.query)
        if not query:
            return SearchKnowledgeBaseOutput()
        matches = []
        for book, words in self._documents:
            overlap = len(query & words)
            if overlap:
                matches.append(
                    KnowledgeMatch(
                        document_id=book.document_id,
                        title=book.title,
                        excerpt=book.content[:1000],
                        relevance=overlap / len(query),
                    )
                )
        matches.sort(key=lambda match: (-match.relevance, match.document_id))
        return SearchKnowledgeBaseOutput(matches=tuple(matches[:5]))
