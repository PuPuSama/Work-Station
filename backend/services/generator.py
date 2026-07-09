from __future__ import annotations

import re

from config import AppConfig
from models import Product, TaskRecord
from services.knowledge import collect_customer_context
from services.llm import LLMClient


def parse_numbered_list(text: str, limit: int) -> list[str]:
    titles: list[str] = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        line = re.sub(r"^\s*[-*\d.)]+\s*", "", line).strip()
        line = line.strip('"')
        if line and line not in titles:
            titles.append(line)
        if len(titles) >= limit:
            break
    return titles


def generate_titles(config: AppConfig, task: TaskRecord) -> list[str]:
    llm = LLMClient(config)
    context = collect_customer_context(config, task.customer)
    keyword = primary_keyword(task)
    prompt = f"""
Create {config.title_candidates} English B2B blog titles for this article task.

Customer website: {task.customer}
Topic: {task.topic}
Primary keyword: {keyword}
Competitor website / keyword source: {task.competitor_keyword}
Competitor blog: {task.competitor_blog}

Customer knowledge:
{context}

Rules:
- English only.
- Make the titles specific, useful, and search-friendly.
- Use the topic and primary keyword naturally when it improves relevance.
- Titles can be question-style or guide-style.
- Do not separate the title with a colon unless it is clearly necessary.
- Avoid clickbait.
- Include commercial or product relevance where natural.
- Return only a numbered list of titles.
""".strip()
    result = llm.chat(
        [
            {"role": "system", "content": "You are a senior B2B SEO editor."},
            {"role": "user", "content": prompt},
        ],
        temperature=0.75,
        max_tokens=1200,
    )
    titles = parse_numbered_list(result, config.title_candidates) if result else []
    if len(titles) >= config.title_candidates:
        return titles[: config.title_candidates]
    return mock_titles(task, config.title_candidates)


def mock_titles(task: TaskRecord, count: int) -> list[str]:
    topic = task.topic.rstrip(".")
    base = [
        f"How to Choose the Right {topic} for Your Business",
        f"{topic}: A Practical Buying Guide for B2B Buyers",
        f"What to Know Before Sourcing {topic}",
        f"{topic} Explained: Key Features, Uses, and Selection Tips",
        f"How {task.customer} Helps Buyers Solve {topic} Challenges",
        f"Common Mistakes to Avoid When Comparing {topic} Options",
        f"{topic} vs. Alternative Solutions: Which Is Best for Your Project?",
        f"Why Product Quality Matters When Buying {topic}",
        f"A Complete Guide to {topic} for Procurement Teams",
        f"How to Evaluate Suppliers for {topic}",
    ]
    return base[:count]


def generate_outline(config: AppConfig, task: TaskRecord) -> str:
    title = task.selected_title or task.topic
    products = products_for_prompt(task.products)
    context = collect_customer_context(config, task.customer)
    llm = LLMClient(config)
    keyword = primary_keyword(task)
    prompt = f"""
Build a detailed English blog outline.

Title: {title}
Customer website: {task.customer}
Topic: {task.topic}
Primary keyword: {keyword}
Competitor website / keyword source: {task.competitor_keyword}
Competitor blog: {task.competitor_blog}
Recommended products:
{products}

Customer knowledge:
{context}

Rules:
- Use H2 and H3 headings only. Do not create H4 headings.
- H2 headings should usually be phrased as useful questions.
- H3 headings should answer or support the H2 using concise noun phrases or short statements.
- Put three parallel H3 headings under each H2 where it is useful.
- Use no more than five main H2 sections before FAQ/conclusion unless the topic truly needs more.
- Use title case for headings: capitalize important words, keep short function words lowercase where natural.
- Include a natural section recommending relevant products with URLs if available.
- Reflect product advantages and end with service, contact, or next-step intent.
- Make the outline suitable for a {config.default_word_count}-word article.
- Do not copy competitor wording.
- Do not mention competitor company names or competitor products in the outline.
- Return Markdown only.
""".strip()
    result = llm.chat(
        [
            {"role": "system", "content": "You are a B2B content strategist."},
            {"role": "user", "content": prompt},
        ],
        temperature=0.55,
        max_tokens=1800,
    )
    return result or mock_outline(title, task)


def generate_article(config: AppConfig, task: TaskRecord, word_count: int | None = None) -> str:
    title = task.selected_title or task.topic
    outline = task.outline or mock_outline(title, task)
    target_words = word_count or config.default_word_count
    products = products_for_prompt(task.products)
    context = collect_customer_context(config, task.customer)
    llm = LLMClient(config)
    keyword = primary_keyword(task)
    prompt = f"""
Write a polished English B2B blog article.

Title: {title}
Target length: about {target_words} words
Customer website: {task.customer}
Topic: {task.topic}
Primary keyword: {keyword}
Competitor website / keyword source: {task.competitor_keyword}
Competitor blog: {task.competitor_blog}
Recommended products:
{products}

Approved outline:
{outline}

Customer knowledge:
{context}

Rules:
- Use Markdown headings.
- Start with # {title}.
- Follow the approved outline and avoid unnecessary H4 headings.
- Keep the writing specific and practical.
- Use the primary keyword naturally and preserve its wording when it appears.
- Introduce the customer company naturally within the opening section, within 300 words, with a homepage hyperlink if available.
- After the opening company mention, avoid repeating the brand name unless it is necessary for clarity.
- Mention recommended products naturally and include their URLs when available.
- Add cohesive transition content between each H2 and its H3 subsections.
- Remove repeated H2/H3 ideas instead of writing filler.
- Prefer second-person wording where it sounds natural for a buyer guide.
- Avoid the words "understanding" and "exploring" in headings and body copy.
- Prefer clear sentence structures and avoid unnecessary preposition-heavy phrasing.
- Add 3 FAQs at the end in this format: Q: ... then A: ...
- Avoid generic AI-sounding filler.
- Do not mention that this was generated by AI.
- Do not copy competitor wording.
- Do not mention competitor company names or competitor products.
""".strip()
    result = llm.chat(
        [
            {"role": "system", "content": "You are an expert B2B industry copywriter."},
            {"role": "user", "content": prompt},
        ],
        temperature=0.65,
        max_tokens=3600,
    )
    return result or mock_article(title, task, outline)


def humanize_article(config: AppConfig, task: TaskRecord) -> str:
    if not task.article:
        return ""
    llm = LLMClient(config)
    keyword = primary_keyword(task)
    prompt = f"""
Revise this English B2B article to sound more natural, specific, and expert-written.

ZeroGPT notes or score:
{task.zero_gpt_report}

Primary keyword to preserve exactly where it appears:
{keyword}

Rules:
- Preserve facts, headings, product URLs, and structure.
- Do not change the article title, H2 headings, or H3 headings.
- Preserve product names, company names, model numbers, technical terms, and data.
- Preserve the primary keyword wording when it appears.
- Reduce repetitive phrasing and generic marketing language.
- Add concrete industry context where appropriate.
- Use real scenarios, numbers, and practical buyer details where they fit the content.
- Vary sentence patterns and split overly long sentences when readability improves.
- Use plain, direct vocabulary instead of inflated wording.
- Return the revised Markdown article only.

Article:
{task.article}
""".strip()
    result = llm.chat(
        [
            {"role": "system", "content": "You are a careful human editor for B2B content."},
            {"role": "user", "content": prompt},
        ],
        temperature=0.55,
        max_tokens=3800,
    )
    return result or task.article


def products_for_prompt(products: list[Product]) -> str:
    if not products:
        return "No confirmed products yet."
    lines = []
    for product in products:
        lines.append(
            f"- {product.name} | URL: {product.url or 'N/A'} | Image: {product.image_path or 'N/A'} | Notes: {product.description or 'N/A'}"
        )
    return "\n".join(lines)


def primary_keyword(task: TaskRecord) -> str:
    candidate = task.competitor_keyword.strip()
    if candidate and not re.match(r"^https?://", candidate, flags=re.IGNORECASE):
        return candidate
    return task.topic


def mock_outline(title: str, task: TaskRecord) -> str:
    return f"""# {title}

## Introduction
- Explain the buyer problem behind {task.topic}.
- Connect the topic to sourcing, quality, and project outcomes.

## What Buyers Should Know About {task.topic}
### Core Use Cases
### Important Specifications
### Quality and Compliance Factors

## How to Compare Supplier Options
### Product Fit
### Manufacturing Capability
### Lead Time and Support

## Recommended Product Options
- Add confirmed product names, URLs, and images before final export.

## Practical Buying Checklist
- Summarize the points procurement teams should verify.

## Conclusion
- Close with a useful recommendation and next step.
"""


def mock_article(title: str, task: TaskRecord, outline: str) -> str:
    product_lines = products_for_prompt(task.products)
    return f"""# {title}

## Introduction

For B2B buyers, {task.topic} is not only a product category. It is part of a broader decision that affects quality, lead time, operating cost, and long-term supplier reliability. A useful article should help buyers understand what to compare before they request a quote or commit to a supplier.

## What Buyers Should Know About {task.topic}

The first step is to define the real application. Buyers should review the working environment, required specifications, expected order volume, and any quality requirements that may affect production or performance. This helps avoid vague inquiries and makes supplier communication more efficient.

### Core Use Cases

Most sourcing mistakes happen when a buyer compares products without confirming the end use. A product that looks similar in a catalog may perform differently once material, tolerance, surface treatment, size, or production process changes.

### Important Specifications

Clear specifications make the buying process faster. Buyers should prepare details such as dimensions, material, color, finish, packaging, quantity, target market, and any certification or testing requirement. If customization is needed, drawings, samples, or reference photos should be shared early.

## How to Compare Supplier Options

When comparing suppliers, price should not be the only factor. A stable supplier should be able to explain production capability, quality control steps, delivery timing, and after-sales support. This is especially important when the product will be used in repeat orders or exported to markets with strict customer expectations.

### Product Fit

A good product fit means the supplier understands the buyer's application and can recommend suitable options instead of only quoting the cheapest item. This reduces rework and improves the chance of receiving products that match the project goal.

### Manufacturing Capability

Buyers should ask how the supplier controls raw materials, production process, inspection, and packaging. Photos, test reports, production videos, and previous project references can help confirm whether the supplier is suitable.

## Recommended Product Options

{product_lines}

If confirmed product URLs are available, they should be placed in this section so readers can move from education to product evaluation without searching the website manually.

## Practical Buying Checklist

- Confirm the application and target market.
- Prepare product specifications before requesting a quote.
- Compare suppliers by quality control, not price alone.
- Ask for product photos, samples, or technical documents.
- Keep product URLs and supplier contact information easy to access.

## Conclusion

Choosing {task.topic} becomes easier when the buyer has clear requirements and a reliable supplier comparison process. The best result usually comes from matching product details with real application needs, then confirming whether the supplier can support stable quality and delivery.
"""
