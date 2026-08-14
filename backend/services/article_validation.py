from __future__ import annotations

from collections import Counter
from hashlib import sha256
import re
from typing import Iterable


VISIBLE_WORD_PATTERN = re.compile(r"[A-Za-z0-9]+(?:[-'][A-Za-z0-9]+)*")
MARKDOWN_LINK_PATTERN = re.compile(
    r"(?<!!)\[(?P<anchor>[^\]\r\n]+)\]\((?P<url>https?://[^)\s]+)\)",
    flags=re.IGNORECASE,
)
MARKDOWN_IMAGE_PATTERN = re.compile(r"!\[[^\]\r\n]*\]\([^\r\n)]*\)")
BARE_URL_PATTERN = re.compile(r"https?://[^\s<>\])]+", flags=re.IGNORECASE)
IMG_MARKER_PATTERN = re.compile(r"\bimg\.[^\r\n]*?\.webp\b", flags=re.IGNORECASE)
HEADING_PATTERN = re.compile(r"^(#{1,6})\s+(.+?)\s*$")
NUMBER_TOKEN_PATTERN = re.compile(r"(?<![A-Za-z0-9])\d+(?:[.,]\d+)*(?:%|°[CF])?", re.IGNORECASE)
FAQ_HEADING_TEXT = "FAQ"
FAQ_QUESTION_PATTERN = re.compile(
    r"^\s*###\s+(?!(?:\*\*)?Q:\s+)(?!\*\*).+?(?<!\*\*)\s*$",
    re.MULTILINE,
)
FAQ_ANSWER_PATTERN = re.compile(
    r"^(?!\s*#{1,6}\s+)(?!\s*(?:[-+*]|\d+[.)])\s+)"
    r"(?!\s*(?:\*\*\s*)?(?:Q:|A:)\s+)\S.*$",
    re.MULTILINE,
)
FAQ_LEGACY_BOLD_QUESTION_PATTERN = re.compile(
    r"^\s*\*\*Q:\s+.+?\*\*\s*$",
    re.MULTILINE,
)
FAQ_LEGACY_QUESTION_PATTERN = re.compile(
    r"^\s*(?:\*\*)?Q:\s+.+?(?:\*\*)?\s*$",
    re.IGNORECASE | re.MULTILINE,
)
FAQ_LEGACY_ANSWER_PATTERN = re.compile(
    r"^\s*A:\s+\S.*$",
    re.IGNORECASE | re.MULTILINE,
)
FAQ_PAIR_COUNT = 3
LIST_ITEM_PATTERN = re.compile(r"^\s*(?:[-+*]|\d+[.)])\s+", re.MULTILINE)


class ArticleValidationError(ValueError):
    """Raised when an article cannot safely advance to the next workflow step."""


class ArticleStructureError(ArticleValidationError):
    """Raised when required Markdown structure is missing or changed."""


class LinkRestorationError(ArticleValidationError):
    """Raised when links cannot be restored without unauthorized copy changes."""


def strip_llm_code_fence(value: str) -> str:
    """Remove one outer Markdown fence commonly added by chat models."""

    text = value.strip()
    match = re.match(r"^```(?:markdown|md)?\s*\r?\n(?P<body>[\s\S]*?)\r?\n```$", text, flags=re.IGNORECASE)
    return match.group("body").strip() if match else text


def visible_markdown_text(markdown: str) -> str:
    """Return text used by the workflow's visible-English-word counter.

    Link anchors remain visible, while URL targets, bare URLs, Markdown images,
    and ``img.<filename>.webp`` publishing markers are excluded.
    """

    value = MARKDOWN_IMAGE_PATTERN.sub(" ", markdown)
    value = IMG_MARKER_PATTERN.sub(" ", value)
    value = MARKDOWN_LINK_PATTERN.sub(lambda match: match.group("anchor"), value)
    value = BARE_URL_PATTERN.sub(" ", value)
    return value


def visible_word_count(markdown: str) -> int:
    return len(VISIBLE_WORD_PATTERN.findall(visible_markdown_text(markdown)))


def _faq_block_lines(markdown: str) -> list[str]:
    """Return non-empty lines directly under the first ``## FAQ`` heading."""

    lines = (markdown or "").splitlines()
    faq_index = next(
        (
            index
            for index, raw_line in enumerate(lines)
            if re.match(r"^\s*##\s+FAQ\s*$", raw_line, re.IGNORECASE)
        ),
        None,
    )
    if faq_index is None:
        return []

    result: list[str] = []
    for raw_line in lines[faq_index + 1 :]:
        stripped = raw_line.strip()
        heading = HEADING_PATTERN.match(stripped)
        if heading and len(heading.group(1)) == 2:
            break
        if stripped and stripped not in {"---", "***", "___"}:
            result.append(stripped)
    return result


def _matches_faq_pairs(
    lines: list[str],
    question_pattern: re.Pattern[str],
    answer_pattern: re.Pattern[str],
) -> bool:
    if len(lines) != FAQ_PAIR_COUNT * 2:
        return False
    return all(
        question_pattern.fullmatch(lines[index])
        and answer_pattern.fullmatch(lines[index + 1])
        for index in range(0, len(lines), 2)
    )


def _faq_layout_kind(markdown: str) -> str | None:
    lines = _faq_block_lines(markdown)
    if _matches_faq_pairs(lines, FAQ_QUESTION_PATTERN, FAQ_ANSWER_PATTERN):
        return "h3"
    if _matches_faq_pairs(
        lines,
        FAQ_LEGACY_QUESTION_PATTERN,
        FAQ_LEGACY_ANSWER_PATTERN,
    ):
        return "legacy"
    return None


def validate_humanized_article(
    source: str,
    candidate: str,
    *,
    required_phrases: Iterable[str] = (),
) -> None:
    """Validate the hard, locally checkable parts of the humanization prompt."""

    source_faq_kind = _faq_layout_kind(source)
    validate_article_layout(
        candidate,
        allow_legacy_faq=source_faq_kind == "legacy",
    )
    candidate_faq_kind = _faq_layout_kind(candidate)
    if source_faq_kind == "h3" and candidate_faq_kind != "h3":
        raise ArticleStructureError(
            "Humanization changed the FAQ question heading structure."
        )
    if source_faq_kind == "legacy" and candidate_faq_kind not in {
        "legacy",
        "h3",
    }:
        raise ArticleStructureError(
            "Humanization changed the FAQ question and answer structure."
        )

    if canonical_heading_sequence(source) != canonical_heading_sequence(candidate):
        raise ArticleStructureError("Humanization changed the article heading hierarchy or heading text.")

    source_visible = visible_markdown_text(source)
    candidate_visible = visible_markdown_text(candidate)
    source_numbers = Counter(NUMBER_TOKEN_PATTERN.findall(source_visible))
    candidate_numbers = Counter(NUMBER_TOKEN_PATTERN.findall(candidate_visible))
    if source_numbers != candidate_numbers:
        raise ArticleStructureError(
            "Humanization changed, removed, or added numeric facts, units, or percentages."
        )

    source_tables = [line.strip() for line in source.splitlines() if line.strip().startswith("|")]
    candidate_tables = [
        line.strip() for line in candidate.splitlines() if line.strip().startswith("|")
    ]
    if source_tables != candidate_tables:
        raise ArticleStructureError("Humanization changed a Markdown table or its contents.")

    if len(LIST_ITEM_PATTERN.findall(source)) != len(LIST_ITEM_PATTERN.findall(candidate)):
        raise ArticleStructureError("Humanization changed the article list structure.")

    for raw_phrase in required_phrases:
        phrase = str(raw_phrase or "").strip()
        if not phrase:
            continue
        source_count = source_visible.count(phrase)
        if source_count and candidate_visible.count(phrase) != source_count:
            raise ArticleStructureError(
                f"Humanization changed the required exact phrase: {phrase}"
            )


def strip_markdown_link_markup(markdown: str) -> str:
    """Remove only Markdown hyperlink syntax, retaining its visible anchor."""

    return MARKDOWN_LINK_PATTERN.sub(lambda match: match.group("anchor"), markdown)


def canonical_visible_body(markdown: str) -> str:
    """Canonical form used to prove that link recovery changed no visible copy."""

    without_links = strip_markdown_link_markup(markdown).replace("\r\n", "\n").replace("\r", "\n")
    return "\n".join(line.rstrip() for line in without_links.split("\n")).strip()


def candidate_with_source_link_anchors(source: str, candidate: str) -> str:
    """Normalize retained candidate links to the first version's exact anchors.

    Exact anchor/URL pairs are consumed first. A candidate link which retained
    an approved URL but renamed its anchor is paired with the next unmatched
    first-version link for that URL. Text outside Markdown links is untouched.
    """

    source_pairs = markdown_link_pairs(source)
    remaining = Counter(source_pairs)

    def replace_link(match: re.Match[str]) -> str:
        anchor = match.group("anchor")
        url = _clean_url(match.group("url"))
        exact_pair = (anchor, url)
        if remaining[exact_pair] > 0:
            remaining[exact_pair] -= 1
            return match.group(0)

        replacement = next(
            (
                pair
                for pair in source_pairs
                if pair[1] == url and remaining[pair] > 0
            ),
            None,
        )
        if replacement is None:
            return match.group(0)

        remaining[replacement] -= 1
        source_anchor, source_url = replacement
        return f"[{source_anchor}]({source_url})"

    return MARKDOWN_LINK_PATTERN.sub(replace_link, candidate)


def visible_body_hash(markdown: str) -> str:
    return sha256(canonical_visible_body(markdown).encode("utf-8")).hexdigest()


def markdown_link_pairs(markdown: str) -> list[tuple[str, str]]:
    return [
        (match.group("anchor"), _clean_url(match.group("url")))
        for match in MARKDOWN_LINK_PATTERN.finditer(markdown)
    ]


def markdown_link_counter(markdown: str) -> Counter[tuple[str, str]]:
    return Counter(markdown_link_pairs(markdown))


def url_counter(markdown: str) -> Counter[str]:
    return Counter(_clean_url(match.group(0)) for match in BARE_URL_PATTERN.finditer(markdown))


def extract_link_inventory(markdown: str) -> list[dict[str, object]]:
    """Extract an ordered, aggregated link inventory for the first article."""

    inventory: dict[tuple[str, str], dict[str, object]] = {}
    current_heading = ""

    for raw_line in markdown.splitlines():
        line = raw_line.strip()
        heading_match = HEADING_PATTERN.match(line)
        if heading_match:
            current_heading = heading_match.group(2).strip()

        for link_match in MARKDOWN_LINK_PATTERN.finditer(raw_line):
            anchor = link_match.group("anchor")
            url = _clean_url(link_match.group("url"))
            key = (anchor, url)
            if key in inventory:
                inventory[key]["count"] = int(inventory[key]["count"]) + 1
                continue
            inventory[key] = {
                "anchor": anchor,
                "url": url,
                "count": 1,
                "heading": current_heading,
                "context": line,
            }

    return list(inventory.values())


def missing_link_inventory(source_markdown: str, candidate_markdown: str) -> list[dict[str, object]]:
    expected = markdown_link_counter(source_markdown)
    actual = markdown_link_counter(candidate_markdown)
    missing = expected - actual
    if not missing:
        return []

    source_items = {
        (str(item["anchor"]), str(item["url"])): item
        for item in extract_link_inventory(source_markdown)
    }
    result: list[dict[str, object]] = []
    for pair, count in missing.items():
        source_item = source_items[pair]
        item = dict(source_item)
        item["count"] = count
        result.append(item)
    return result


def heading_sequence(markdown: str) -> list[tuple[int, str]]:
    result: list[tuple[int, str]] = []
    for raw_line in markdown.splitlines():
        match = HEADING_PATTERN.match(raw_line.strip())
        if match:
            result.append((len(match.group(1)), match.group(2).strip()))
    return result


def canonical_heading_sequence(markdown: str) -> list[tuple[int, str]]:
    """Compare headings while treating a legacy FAQ block as the final section.

    Older saved articles may place ``## FAQ`` before a conclusion. Operators
    must be able to repair those articles through the humanized-article editor,
    so the comparison ignores the FAQ block's old position while still locking
    every non-FAQ heading and its order.
    """

    lines = (markdown or "").splitlines()
    faq_start: int | None = None
    faq_end = len(lines)
    for index, raw_line in enumerate(lines):
        match = HEADING_PATTERN.match(raw_line.strip())
        if not match:
            continue
        level = len(match.group(1))
        text = match.group(2).strip()
        if faq_start is None and text.casefold() == FAQ_HEADING_TEXT.casefold():
            faq_start = index
            continue
        if faq_start is not None and level <= 2:
            faq_end = index
            break

    if faq_start is None:
        return heading_sequence(markdown)

    outside_faq = lines[:faq_start] + lines[faq_end:]
    return heading_sequence("\n".join(outside_faq)) + [(2, FAQ_HEADING_TEXT)]


def validate_article_layout(
    markdown: str,
    *,
    allow_legacy_faq: bool = True,
) -> None:
    """Enforce the operator's final FAQ contract on a complete article.

    The FAQ is deliberately strict because it is consumed by both the manual
    humanization checkpoint and Word export. The current contract uses three H3
    question headings, each followed by one plain answer line. Legacy bold
    ``Q:``/``A:`` blocks can be read when explicitly allowed so existing saved
    articles remain editable, but new generation paths disable that fallback.
    """

    lines = (markdown or "").splitlines()
    faq_headings: list[tuple[int, int, str]] = []
    h2_indices: list[int] = []
    for index, raw_line in enumerate(lines):
        match = HEADING_PATTERN.match(raw_line.strip())
        if not match:
            continue
        level = len(match.group(1))
        heading_text = match.group(2).strip()
        if level == 2:
            h2_indices.append(index)
        if heading_text.casefold() == FAQ_HEADING_TEXT.casefold():
            faq_headings.append((index, level, heading_text))

    if len(faq_headings) != 1:
        raise ArticleStructureError(
            "Article must contain exactly one FAQ heading written as '## FAQ'."
        )

    faq_index, faq_level, faq_text = faq_headings[0]
    if faq_level != 2 or faq_text != FAQ_HEADING_TEXT:
        raise ArticleStructureError("The FAQ heading must be written exactly as '## FAQ'.")
    if not h2_indices or h2_indices[-1] != faq_index:
        raise ArticleStructureError("'## FAQ' must be the final H2 section in the article.")

    faq_lines = _faq_block_lines(markdown)
    expected_line_count = FAQ_PAIR_COUNT * 2
    if len(faq_lines) != expected_line_count:
        raise ArticleStructureError(
            "The final FAQ section must contain exactly three Q/A pairs "
            "(three questions with one answer each) and no content after them."
        )

    if _matches_faq_pairs(faq_lines, FAQ_QUESTION_PATTERN, FAQ_ANSWER_PATTERN):
        return

    legacy_shape = _matches_faq_pairs(
        faq_lines,
        FAQ_LEGACY_QUESTION_PATTERN,
        FAQ_LEGACY_ANSWER_PATTERN,
    )
    if allow_legacy_faq and legacy_shape:
        for pair_index in range(FAQ_PAIR_COUNT):
            question = faq_lines[pair_index * 2]
            answer = faq_lines[pair_index * 2 + 1]
            if not FAQ_LEGACY_BOLD_QUESTION_PATTERN.fullmatch(question):
                raise ArticleStructureError(
                    "Each legacy FAQ question must be a complete bold Markdown line such as "
                    "'**Q: What should a buyer check?**'."
                )
            if not FAQ_LEGACY_ANSWER_PATTERN.fullmatch(answer):
                raise ArticleStructureError(
                    "Each legacy FAQ question must be followed by one 'A: ...' answer line."
                )
        return

    for pair_index in range(FAQ_PAIR_COUNT):
        question = faq_lines[pair_index * 2]
        answer = faq_lines[pair_index * 2 + 1]
        if not FAQ_QUESTION_PATTERN.fullmatch(question):
            raise ArticleStructureError(
                "Each FAQ question must be a level-3 Markdown heading such as "
                "'### What should a buyer check?' without a Q: prefix."
            )
        if not FAQ_ANSWER_PATTERN.fullmatch(answer):
            raise ArticleStructureError(
                "Each FAQ question heading must be followed by one plain answer line without an A: prefix."
            )


def intro_structure_bounds(markdown: str) -> tuple[int, int]:
    lines = markdown.splitlines()
    h1_index: int | None = None
    for index, raw_line in enumerate(lines):
        match = HEADING_PATTERN.match(raw_line.strip())
        if not match:
            continue
        level = len(match.group(1))
        if level == 1 and h1_index is None:
            h1_index = index
            continue
        if level == 2 and h1_index is not None:
            return h1_index, index

    if h1_index is None:
        raise ArticleStructureError("Article must contain an H1 heading before transition validation.")
    raise ArticleStructureError("Article must contain an H2 heading after its H1.")


def has_intro_transition(markdown: str) -> bool:
    h1_index, h2_index = intro_structure_bounds(markdown)
    for raw_line in markdown.splitlines()[h1_index + 1 : h2_index]:
        line = raw_line.strip()
        if not line:
            continue
        if HEADING_PATTERN.match(line):
            continue
        if MARKDOWN_IMAGE_PATTERN.fullmatch(line) or IMG_MARKER_PATTERN.fullmatch(line):
            continue
        if re.match(r"^(?:[-*+]\s+|\d+[.)]\s+|>\s*|\|)", line):
            continue
        if visible_word_count(line) > 0:
            return True
    return False


def insert_transition_before_first_h2(markdown: str, transition: str) -> str:
    _, h2_index = intro_structure_bounds(markdown)
    lines = markdown.splitlines()
    before = "\n".join(lines[:h2_index]).rstrip()
    after = "\n".join(lines[h2_index:]).lstrip()
    return f"{before}\n\n{transition.strip()}\n\n{after}".strip()


def validate_restored_links(source: str, candidate: str, restored: str) -> None:
    expected_candidate = candidate_with_source_link_anchors(source, candidate)
    if visible_body_hash(expected_candidate) != visible_body_hash(restored):
        raise LinkRestorationError(
            "Link restoration changed visible article text outside the approved first-version link anchors."
        )

    expected_links = markdown_link_counter(source)
    restored_links = markdown_link_counter(restored)
    if restored_links != expected_links:
        missing = expected_links - restored_links
        unexpected = restored_links - expected_links
        details = _counter_details(missing, unexpected)
        raise LinkRestorationError(f"Link restoration did not reproduce the first-version link set{details}.")

    expected_urls = url_counter(source)
    restored_urls = url_counter(restored)
    if restored_urls != expected_urls:
        added_urls = restored_urls - expected_urls
        removed_urls = expected_urls - restored_urls
        details = _url_counter_details(removed_urls, added_urls)
        raise LinkRestorationError(f"Link restoration changed the URL set{details}.")


def assert_no_unexpected_candidate_links(source: str, candidate: str) -> None:
    new_urls = url_counter(candidate) - url_counter(source)
    if new_urls:
        url_details = _url_counter_details(Counter(), new_urls)
        raise LinkRestorationError(
            f"The candidate contains URLs not present in the first version{url_details}."
        )


def _clean_url(value: str) -> str:
    return value.rstrip(".,;:!?")


def _counter_details(
    missing: Counter[tuple[str, str]], unexpected: Counter[tuple[str, str]]
) -> str:
    parts: list[str] = []
    if missing:
        parts.append("missing=" + _format_pairs(missing.elements()))
    if unexpected:
        parts.append("unexpected=" + _format_pairs(unexpected.elements()))
    return f" ({'; '.join(parts)})" if parts else ""


def _format_pairs(pairs: Iterable[tuple[str, str]]) -> str:
    return ", ".join(f"[{anchor}]({url})" for anchor, url in pairs)


def _url_counter_details(missing: Counter[str], added: Counter[str]) -> str:
    parts: list[str] = []
    if missing:
        parts.append("missing URLs=" + ", ".join(missing.elements()))
    if added:
        parts.append("added URLs=" + ", ".join(added.elements()))
    return f" ({'; '.join(parts)})" if parts else ""
