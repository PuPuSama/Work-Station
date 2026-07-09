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

Backend API:

```text
http://127.0.0.1:8000
```

## Current Flow

1. Scan `D:\article\话题库`.
2. Use each Excel file name as the customer website.
3. Treat each topic row as one article task.
4. Create weekly task folders under `D:\article\7.6-7.10-谷瑞勋`.
5. Generate title candidates, select one title, save products, generate outline, generate article, paste ZeroGPT report, optimize article, export Word.
6. Product data can be filled manually or auto-recommended from WordPress REST API, sitemap, homepage links, and product-page images.

## LLM Setup

Without environment variables, the app uses mock generation so the workflow can be tested.

To connect a real model, copy `.env.example` to `.env` and fill:

```text
LLM_API_KEY=your_key
LLM_MODEL=your_model
LLM_BASE_URL=https://api.openai.com/v1
```

The backend uses the OpenAI Responses API endpoint: `/v1/responses`.

## Word Format

- Font: Times New Roman
- Heading 1: 22 pt
- Heading 2: 18 pt
- Heading 3: 13.5 pt
- Body: 12 pt

Product URLs are kept as Word hyperlinks. Product images are inserted when the product image path points to an existing local file.
