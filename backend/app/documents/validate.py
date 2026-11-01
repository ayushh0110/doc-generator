import difflib
import re
from pathlib import Path
from typing import Any, Iterable

from app.documents import engine_for
from app.documents.base import ApplyResult, Check

CITATION_RE = re.compile(r"\[(S\d+)\]")
FILLER_RE = re.compile(r"lorem ipsum|\[insert[^\]]*\]|\bTBD\b|\bTODO\b|<placeholder>|xx+%", re.IGNORECASE)


def texts_in_ops(ops: Iterable[Any]) -> list[tuple[str, str]]:
    """Every (element, text) an edit plan writes, for citation and filler checks."""
    found: list[tuple[str, str]] = []

    def walk(node: Any, label: str) -> None:
        if isinstance(node, str):
            found.append((label, node))
        elif isinstance(node, list):
            for item in node:
                walk(item, label)
        elif isinstance(node, dict):
            for key, value in node.items():
                if key in {"text", "paragraphs", "rows", "values", "replace", "value", "blocks", "content", "edits", "headers", "notes"}:
                    walk(value, label)

    for op in ops:
        data = op.model_dump() if hasattr(op, "model_dump") else op
        if data.get("op") == "set_sources":
            continue
        label = str(data.get("target") or data.get("ref") or data.get("layout") or data.get("sheet") or data.get("op"))
        if data.get("sheet") and data.get("ref"):
            label = f"{data['sheet']}!{data['ref']}"
        walk(data, label)
    return found


def validate_plan(
    src: Path, dest: Path, result: ApplyResult, ops: list[Any], known_sources: set[str]
) -> list[Check]:
    engine = engine_for(dest)
    checks: list[Check] = []
    try:
        analysed = engine.analyse(dest)
        checks.append(Check("File opens cleanly", True, f"{analysed['format'].upper()} parsed after the edit"))
    except Exception as exc:  # A broken file must fail the check, whatever the parser raises.
        return [Check("File opens cleanly", False, str(exc))]

    checks.extend(engine.preservation_checks(src, dest, result))

    written = texts_in_ops(ops)
    cited = {key for _, text in written for key in CITATION_RE.findall(text)}
    unknown = sorted(cited - known_sources)
    checks.append(
        Check(
            "Citations point at real sources",
            not unknown,
            f"Unknown: {', '.join(unknown)}" if unknown else f"{len(cited)} source(s) cited",
        )
    )

    filler = [f'{label}: "{FILLER_RE.search(text)[0]}"' for label, text in written if FILLER_RE.search(text)]
    checks.append(Check("No placeholder text", not filler, "; ".join(filler[:5])))
    return checks


def blocking_failures(checks: list[Check]) -> list[Check]:
    return [c for c in checks if not c.ok and c.severity == "error"]


def changes_between(before: list[dict[str, Any]], after: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """A readable before/after list from two outlines, for the Changes tab."""
    old = _flatten(before)
    new = _flatten(after)
    old_texts = [text for _, text in old]
    new_texts = [text for _, text in new]

    changes: list[dict[str, Any]] = []
    matcher = difflib.SequenceMatcher(a=old_texts, b=new_texts, autojunk=False)
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "equal":
            continue
        removed = old[i1:i2]
        added = new[j1:j2]
        for index in range(max(len(removed), len(added))):
            was = removed[index] if index < len(removed) else None
            now = added[index] if index < len(added) else None
            changes.append(
                {
                    "element": (now or was)[0],
                    "kind": "changed" if was and now else "added" if now else "removed",
                    "before": was[1] if was else None,
                    "after": now[1] if now else None,
                }
            )
    return changes


def cited_elements(outline: list[dict[str, Any]]) -> list[tuple[str, str, str]]:
    """(source key, element, excerpt) for each citation in the document's own text.

    Read from the file rather than the plan, so an edit that deletes a cited
    sentence also drops that source. The Sources list itself doesn't count.
    """
    body: list[dict[str, Any]] = []
    for item in outline:
        if item.get("kind") == "paragraph" and item.get("text", "").strip() == "Sources":
            break
        if item.get("kind") == "sheet" and item.get("id") == "Sources":
            continue
        if item.get("kind") == "slide" and item.get("notes"):
            item = {**item, "notes": item["notes"].split("Sources:")[0].strip()}
        body.append(item)

    found = []
    for element, text in _flatten(body):
        for key in dict.fromkeys(CITATION_RE.findall(text)):
            found.append((key, element, text[:400]))
    return found


def _flatten(outline: list[dict[str, Any]]) -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    for item in outline:
        kind = item.get("kind")
        if kind == "paragraph":
            out.append((item["id"], item.get("text", "")))
        elif kind == "table":
            for row in item.get("rows", []):
                out.append((row[0]["id"].rsplit(".", 1)[0] if row else item["id"], " | ".join(c["text"] for c in row)))
        elif kind == "slide":
            for shape in item.get("shapes", []):
                if shape.get("paragraphs"):
                    out.append((shape["id"], "\n".join(shape["paragraphs"])))
                if shape.get("rows"):
                    for r, row in enumerate(shape["rows"]):
                        out.append((f"{shape['id']}.r{r}", " | ".join(row)))
                if shape.get("series"):
                    summary = "; ".join(f"{s['name']}: {s['values']}" for s in shape["series"])
                    out.append((shape["id"], f"Chart data. {summary}"))
            if item.get("notes"):
                out.append((f"{item['id']}.notes", item["notes"]))
        elif kind == "sheet":
            for row in item.get("rows", []):
                cells = row["cells"]
                label = f"{item['id']}!{cells[0]['ref']}" if cells else item["id"]
                out.append((label, " | ".join(str(c.get("formula") or c.get("value")) for c in cells)))
    return out
