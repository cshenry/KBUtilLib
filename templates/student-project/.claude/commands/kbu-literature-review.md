<!--
kbu skill provenance
type: harvested
source_repo: BERIL-research-observatory
source_commit: 940c3b0ee7bbf63bc576bd6e8c25210ad692df8e
source_path: .claude/skills/literature-review/SKILL.md
last_reviewed: 2026-06-05
-->

---
name: kbu-literature-review
description: Search and review biological literature using MCP tools (PubMed, arXiv, bioRxiv, Google Scholar). Use when the student wants to find papers, review existing research, check what's known about an organism or pathway, or support a hypothesis with citations.
allowed-tools: Bash, Read, Write, WebSearch, Agent, ToolSearch
---

# kbu-literature-review

Search, read, and synthesize biological literature relevant to a KBU subproject.
Combines multi-source discovery (PubMed, arXiv, bioRxiv, Google Scholar) with
full-text analysis and citation network exploration.

## Usage

```
/kbu-literature-review <topic_or_subproject_name>
```

## MCP Setup

Two MCP servers provide literature access. Add the following to the project's
`.mcp.json` OR to `~/.claude.json` (under `mcpServers`) to enable them:

```json
{
  "mcpServers": {
    "pubmed": {
      "type": "http",
      "url": "https://pubmed.mcp.claude.com/mcp"
    },
    "paper-search": {
      "command": "uvx",
      "args": ["--from", "paper-search-mcp", "python", "-m", "paper_search_mcp.server"],
      "env": {
        "SEMANTIC_SCHOLAR_API_KEY": "${SEMANTIC_SCHOLAR_API_KEY:-}"
      }
    }
  }
}
```

### Checking Tool Availability

Before searching, check whether the MCP servers are active via tool discovery
(`ToolSearch`). If both `pubmed` and `paper-search` MCP servers are absent,
fall back to `WebSearch` (see **Fallback** section) and append a `[fallback path]`
note to `references.md`.

### Primary: PubMed MCP (HTTP)

`mcp__pubmed__` tools — richest PubMed access:
- **`search_articles`** — PubMed search with date filters, MeSH, pagination
- **`find_related_articles`** — citation network: similar papers, PMC links
- **`get_full_text_article`** — full text from PMC (~6M open-access articles)
- **`get_article_metadata`** — detailed metadata for specific PMIDs
- **`convert_article_ids`** — PMID ↔ PMCID ↔ DOI conversion

### Secondary: paper-search-mcp

`mcp__paper-search__` tools — cross-preprint search:
- **`search_arxiv`** — arXiv preprints
- **`search_biorxiv`** — bioRxiv preprints
- **`search_medrxiv`** — medRxiv preprints
- **`search_google_scholar`** — broad coverage
- **`read_arxiv_paper`**, **`read_biorxiv_paper`**, **`read_medrxiv_paper`** — full text from preprint PDFs

Requires Python 3.10+ and [uv](https://docs.astral.sh/uv/).
`SEMANTIC_SCHOLAR_API_KEY` is optional; set it for higher rate limits.

## Workflow

### Step 1: Understand the Research Question

Clarify what the student wants. Ask if needed:
- Specific organism, gene, pathway, or phenotype?
- Time frame (recent papers only, or comprehensive)?

**Select a review depth tier:**

| Tier | Papers | Full text? | Citation snowball? | When to use |
|---|---|---|---|---|
| **Quick scan** | 5–10 | No | No | Ad-hoc checks |
| **Standard review** | 20–30 | Top 10 | Yes | Default for subproject workflows |
| **Deep review** | 50+ | Top 20 | Yes | Explicitly requested |

Default to **quick scan** for ad-hoc questions and **standard review** for subproject-based
work or when invoked via `/kbu-synthesize`.

### Step 2: Construct Search Queries

Build queries using biology-aware strategies:

- Expand biological topics to MeSH terms for PubMed (e.g., "pangenome" → `"pangenome" OR "pan-genome" OR "core genome"`).
- Include both formal taxon names and common variants.
- Expand functional annotations to gene/pathway terminology.

### Step 3: Discover Papers

Search all available sources in priority order: PubMed → bioRxiv → arXiv → Google Scholar.

Deduplicate by DOI (primary) or PMID. Rank by relevance to the research question:
- **HIGH**: directly tests the same hypothesis, same or closely related organisms
- **MEDIUM**: methodology papers, reviews, related organisms or pathways
- **LOW**: tangential topics, distant organisms

For standard and deep tiers, perform citation snowballing: for the top 10 PMIDs call
`mcp__pubmed__find_related_articles`.

### Step 4: Read Key Papers (Standard + Deep)

For the top 10 (standard) or top 20 (deep) papers, retrieve full text via
`mcp__pubmed__get_full_text_article` (PMC papers) or the paper-search preprint readers.
For papers not in PMC, use the abstract from the search results.

Focus on Methods, Results, and Discussion sections. For each paper note:
- Study design, organisms, sample size
- Key results with specific numbers
- Limitations
- Relevance to the student's research question

### Step 5: Summarize Findings

Present results grouped by theme:

```markdown
## Literature Review: [Topic]

**Review depth**: [Quick scan | Standard review | Deep review]
**Papers found**: [N total] | **Full text read**: [N] | **Abstract only**: [N]
**Sources searched**: [list]

### Summary
[2-3 sentence overview]

### Key Findings by Theme

#### Theme 1: [e.g., "Pangenome methods"]
- **Author et al. (Year)** — [Key finding]. DOI: [doi]

#### Theme 2: ...

### Gaps in Current Knowledge
- [What hasn't been studied that this subproject could address]
```

### Step 6: Save References

Save to `subprojects/<name>/references.md` (or current directory if no subproject context):

```markdown
# References

## [Topic or Research Question]

Searched: [date], Sources: [list], Query: "[search terms]"
Review depth: [tier]
[fallback path] *(if WebSearch fallback was used)*

### Cited References

1. Author A, Author B. (Year). "Title." *Journal*, Vol(Issue), Pages. DOI: [doi]. PMID: [pmid]

### Additional References (not cited but relevant)

1. ...
```

### Step 7: Save Session

```python
from assistant.state import save_session
save_session({
    'project_id': '<subproject_name_or_topic>',
    'command': 'kbu-literature-review',
    'topics_discussed': ['literature search', '<topic>'],
    'decisions_made': ['references saved'],
    'next_steps': ['incorporate citations into RESEARCH_PLAN.md or REPORT.md'],
    'summary': 'Literature review on <topic> — <N> papers found',
})
```

## Fallback: WebSearch

If both `pubmed` and `paper-search` MCP servers are absent:

1. Use `WebSearch` with queries like `site:pubmed.ncbi.nlm.nih.gov [query]` and
   `[topic] biology filetype:pdf`.
2. Use `WebFetch` to retrieve paper details from DOIs: `https://doi.org/[doi]`.
3. Append `[fallback path]` to the `references.md` header to note that results
   may be less comprehensive than MCP-based search (no full-text retrieval, no
   citation snowballing).

To enable MCP tools, add the `.mcp.json` snippet above to the project root or
`~/.claude.json`, then restart Claude.

## Integration

- **Called by**: `/kbu-synthesize` (Step 5), or directly by the student
- **Produces**: `subprojects/<name>/references.md`
- **Reads from**: (none — discovery from external sources)
- **Used by**: `/kbu-synthesize` for literature context in REPORT.md
