# Article Workflow Agent

Local article workflow app for weekly topic-library based writing.

## Run

```powershell
cd D:\article\article-agent
.\start.ps1
```

Open:

```text
http://127.0.0.1:3000
```

The task header includes an **Open Project Folder** button. It opens the selected task's validated `task_dir` in Windows File Explorer.

Backend API:

```text
http://127.0.0.1:8000
```

## Current Flow

1. Scan `D:\article\话题库`.
2. Use each Excel file name as the customer website.
3. Treat each topic row as one article task.
4. Create weekly task folders under `D:\article\7.6-7.10-谷瑞勋`.
5. Generate title candidates, select one title, save products, generate an outline, and create the first article version.
6. Article generation is instructed to stay within 1000–1200 English words (roughly 8,000 characters including spaces). The app does not mechanically truncate the article or run an automatic compression pass. Every article must end with one `## FAQ` section containing exactly three Q/A pairs; each question is written as `**Q: ...**` and nothing follows the FAQ.
7. Copy the first version to ZeroGPT manually and save the first score/report when that checkpoint is needed.
8. Either run the configured UTF-8 humanization prompt after the first check, or paste an externally humanized article directly; the manual paste path does not require the first check or an in-app model rewrite.
   A completed task can reopen and save this humanized article; doing so returns the workflow to the final AI recheck and invalidates links, images, Word, TDK, and delivery records derived from the old text.
9. Verify/restore the first version's Markdown links, prepare up to three distinct hero/product images as WebP, then export Word. The three-image limit includes the hero image.
10. Generate English Google SEO TDK from the final article and save it as `D.docx`: `T` is copied exactly from the article H1, `D` is capped at 150 characters including spaces, and `K` contains exactly six comma-separated keyword phrases.
11. Product data can be filled manually or auto-discovered from Tavily plus the website's WordPress REST API, sitemap, and product indexes. Tavily only discovers official-domain URLs: the backend still opens each URL and verifies that it is a real product detail page.
12. For every verified product, the backend archives the official H1, description, facts, specification tables, FAQ, and up to 16 high-confidence product-gallery/content images under `product_assets/<product-id>/`. Exact and visually near-duplicate assets are collapsed before selection. A vision-capable model selects an image by manifest asset ID, never by a model-invented filename or path. The selected asset must belong to the same product; weak evidence is skipped instead of asking an operator to guess.

ZeroGPT is intentionally a manual checkpoint. The app provides copy buttons and separate before/after records but does not automate the ZeroGPT website.

## LLM Setup

Without environment variables, the app uses mock generation so the workflow can be tested.

To connect a real model, copy `.env.example` to `.env` and fill:

```text
LLM_API_KEY=your_key
LLM_MODEL=your_model
LLM_BASE_URL=https://api.openai.com/v1
TAVILY_API_KEY=your_tavily_key
```

The backend uses the OpenAI Responses API endpoint: `/v1/responses`. Configure a model that accepts image input to enable visual product-asset selection; when vision is unavailable, the backend uses a conservative evidence-based fallback and records that method.
All keys stay in the backend `.env`; the browser only receives integration readiness flags.

## Word Format

- Font: Times New Roman
- Heading 1: 22 pt
- Heading 2: 18 pt
- Heading 3: 13.5 pt
- Body: 12 pt

Product URLs are kept as Word hyperlinks. Product images are inserted when the product image path points to an existing local file.

The hero image is converted to WebP, named from the article title, and placed immediately before the first H2. Product images are placed after the complete Markdown paragraph containing their matching product name/link; a manually selected H2/H3 anchor uses the end of that heading's first complete prose paragraph. An article can contain at most three distinct images in total, including the hero; duplicate image content is skipped or rejected at validation. Every image is followed by `img.<actual filename>.webp`.

After the article Word file is exported, the final workflow step creates a separate `D.docx` in the same task folder with exactly three entries: `T:`, `D:`, and `K:`.

## Validation

```powershell
cd D:\article\article-agent
$env:PYTHONPATH="D:\article\article-agent\backend"
backend\.venv\Scripts\python.exe -m unittest discover -s backend\tests -v

cd frontend
npm.cmd run build
```

There are separate production and test dependency files:

```powershell
backend\.venv\Scripts\python.exe -m pip install -r backend\requirements.txt
backend\.venv\Scripts\python.exe -m pip install -r backend\requirements-dev.txt
```
