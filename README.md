# Article Workflow Agent

Local article workflow app for persistent topic-library based writing.

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

Task state is stored in `data/tasks.sqlite3`, with one article per row so a
single save does not rewrite every project task. On first startup, an existing
`data/tasks.json` is imported automatically and retained as
`data/tasks.monolith.backup.json`; it is no longer used as the active store.
Background jobs remain isolated in `data/job_queue.sqlite3`.

1. Scan `D:\article\话题库`.
2. Use each Excel file name as the customer website.
3. Treat each topic row as one persistent article task. Re-syncing the library updates that task instead of creating a new weekly copy, so historical completion states remain visible.
4. Store task folders directly under `D:\article\<customer website>\topic_NNN`. The first non-weekly sync backs up the old task records and copies legacy task files into these canonical folders without deleting the old dated folders.
5. Save the official brand name, project introduction, and project-wide notes on the home page for each project. These project fields are synchronized to every topic under the same website and inherited by newly scanned topics. Article generation and Word export attach the customer-homepage hyperlink to the exact brand name instead of a bare URL or generic text such as "official website".
6. Each task has a **写作要求** tab for topic-specific notes, custom outline instructions, custom article instructions, and independent switches controlling whether generation reads the project introduction, project notes, and topic notes. Custom instructions supplement the default factual, word-count, and Markdown safeguards rather than silently removing them.
7. Generate title candidates, select one title, save products, generate an outline, and create the first article version.
   A task that needs a completely different article can use **完全重写** at any later stage. After confirmation, the workflow data returns to `待生成标题` while the source topic, task number, competitor fields, and project directory are preserved. Existing files in the project directory are not deleted automatically.
8. Article generation is instructed to stay within 1000–1200 English words (roughly 8,000 characters including spaces). The app does not mechanically truncate the article or run an automatic compression pass. Every article must end with one `## FAQ` section containing exactly three Q/A pairs; each question is written as `**Q: ...**` and nothing follows the FAQ.
   **仅重写正文** is available from every downstream workflow stage. It keeps the selected title, confirmed products, approved outline, project context, and task writing requirements while replacing the first article and invalidating all AI checks, humanized copy, restored links, prepared images, Word, TDK, and delivery records derived from the previous version.
9. Copy the first version to ZeroGPT manually and save the first score/report when that checkpoint is needed.
10. Either run the configured UTF-8 humanization prompt after the first check, or paste an externally humanized article directly; the manual paste path does not require the first check or an in-app model rewrite.
   A completed task can reopen and save this humanized article; doing so returns the workflow to the final AI recheck and invalidates links, images, Word, TDK, and delivery records derived from the old text.
11. Verify/restore the first version's Markdown links, prepare up to three distinct hero/product images as WebP, then export Word. The three-image limit includes the hero image.
12. Generate English Google SEO TDK from the final article and save it as `D.docx`: `T` is copied exactly from the article H1, `D` is capped at 150 characters including spaces, and `K` contains exactly six comma-separated keyword phrases.
13. Product data can be filled manually or auto-discovered from Tavily plus the website's WordPress REST API, sitemap, and product indexes. Tavily only discovers official-domain URLs: the backend still opens each URL and verifies that it is a real product detail page.
14. For every verified product, the backend archives the official H1, description, facts, specification tables, FAQ, and up to 16 high-confidence product-gallery/content images under `product_assets/<product-id>/`. Exact and visually near-duplicate assets are collapsed before selection. A vision-capable model selects an image by manifest asset ID, never by a model-invented filename or path. The selected asset must belong to the same product; weak evidence is skipped instead of asking an operator to guess.

ZeroGPT is intentionally a manual checkpoint. The app provides copy buttons and separate before/after records but does not automate the ZeroGPT website.

## Parallel Generation

The task table supports multi-select batch operations for **生成标题**, **找产品**, **生成大纲**, **生成正文**, and **仅重写正文**. Batch title generation creates ten candidates per article but deliberately leaves final title selection to the operator. Batch product discovery runs the same verified official-page and product-asset workflow as the single-task button. Writing jobs run at most three at once, while website/Tavily/product-vision work is isolated to a maximum of two concurrent tasks. Batch requests are stored in `data/job_queue.sqlite3`, so refreshing or closing the browser does not lose queued work. The backend never runs two active jobs for the same article.

Transient model failures such as HTTP 429/502/503 and timeouts are retried after 5, 15, and 45 seconds. A task revision is captured when it enters the queue and checked again before the model call and final save. If an operator edits that task while it is waiting, the job stops with **内容已变化** instead of overwriting the newer version. Failed, cancelled, and conflicted rows can be retried from the UI using the task's current revision and writing settings. A backend restart returns interrupted jobs to the persistent queue.

Batch article generation stops at **待 ZeroGPT 初检** just like single-article generation. ZeroGPT checks, humanization review, link restoration, image preparation, and delivery remain explicit operator steps.

## LLM Setup

Without environment variables, the app uses mock generation so the workflow can be tested.

To connect a real model, copy `.env.example` to `.env` and fill:

```text
LLM_API_KEY=your_key
LLM_MODEL=gpt-5.6-sol
LLM_BASE_URL=https://api.openai.com/v1
TAVILY_API_KEY=your_tavily_key
```

The backend uses `gpt-5.6-sol` through the OpenAI Responses API endpoint `/v1/responses`. Model responses are requested as server-sent events and accumulated from typed text-delta events before the existing backend endpoint returns the complete result. Configure a model that accepts image input to enable visual product-asset selection; when vision is unavailable, the backend uses a conservative evidence-based fallback and records that method.
All keys stay in the backend `.env`; the browser only receives integration readiness flags.

## Word Format

- Font: Times New Roman
- Heading 1: 22 pt
- Heading 2: 18 pt
- Heading 3: 13.5 pt
- Body: 12 pt

Homepage URLs are attached to the saved brand name, and product URLs are kept as Word hyperlinks. Hyperlinks remain blue even when they appear in a heading. Product images are inserted when the product image path points to an existing local file.

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
