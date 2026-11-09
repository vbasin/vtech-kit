---
type: llm
focus: last_message
weight: 1
---

PASS if the reply reports at least one of these about `added_dependencies`:
- a dependency declared inline, such as `dependencies = ["httpx>=0.27", "rich"]` on one
  line, is missed;
- any quoted string alone on a line anywhere in the file (a `testpaths` or `keywords`
  entry, a `[build-system] requires` item) is read as a dependency, because nothing checks
  that the line sits inside a dependency array.

FAIL if `deps.py` is not discussed, or only its regex style is.
