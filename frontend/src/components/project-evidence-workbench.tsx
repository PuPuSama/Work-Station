"use client";

import {
  AlertCircle,
  BookMarked,
  ExternalLink,
  Loader2,
  Search,
  ShieldCheck,
} from "lucide-react";
import { useMemo, useState } from "react";

import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { apiPost } from "@/lib/api";
import type {
  KnowledgeEvidencePack,
  KnowledgeRetrievalPlan,
} from "@/types";

type ProjectEvidenceWorkbenchProps = {
  customer: string;
};

type ScopeType = "h2_section" | "product_fact" | "faq";

const sufficiencyLabel = {
  sufficient: "证据充分",
  weak: "证据偏弱",
  missing: "缺少证据",
} as const;

function safeIdentity(value: string) {
  return (
    value
      .trim()
      .toLowerCase()
      .replace(/[^a-z0-9_-]+/g, "-")
      .replace(/^-+|-+$/g, "")
      .slice(0, 80) || "draft"
  );
}

function errorMessage(error: unknown) {
  return error instanceof Error ? error.message : "未知错误";
}

function scoreDetail(explanation: Record<string, unknown>) {
  const vectorRank = explanation.vector_rank;
  const lexicalRank = explanation.lexical_rank;
  const parts = [
    typeof vectorRank === "number" ? `向量 #${vectorRank}` : null,
    typeof lexicalRank === "number" ? `全文 #${lexicalRank}` : null,
  ].filter(Boolean);
  return parts.length ? parts.join(" · ") : "已保留完整排序解释";
}

export function ProjectEvidenceWorkbench({
  customer,
}: ProjectEvidenceWorkbenchProps) {
  const [articleId, setArticleId] = useState("topic_006");
  const [outlineVersion, setOutlineVersion] = useState(1);
  const [scopeType, setScopeType] = useState<ScopeType>("h2_section");
  const [scopeTitle, setScopeTitle] = useState("核心章节");
  const [query, setQuery] = useState("");
  const [requireHardFact, setRequireHardFact] = useState(false);
  const [running, setRunning] = useState(false);
  const [error, setError] = useState("");
  const [pack, setPack] = useState<KnowledgeEvidencePack | null>(null);

  const identities = useMemo(() => {
    const article = safeIdentity(articleId);
    const scope = safeIdentity(scopeTitle);
    return {
      planId: `plan-${article}-v${outlineVersion}`,
      scopeId: `scope-${scope}`,
    };
  }, [articleId, outlineVersion, scopeTitle]);

  async function retrieveEvidence() {
    if (!articleId.trim() || !scopeTitle.trim() || !query.trim()) {
      setError("请填写文章 ID、Scope 名称和检索问题。");
      return;
    }
    setRunning(true);
    setError("");
    setPack(null);
    try {
      const plan = await apiPost<KnowledgeRetrievalPlan>(
        `/api/knowledge/${encodeURIComponent(customer)}/retrieval-plans`,
        {
          retrieval_plan_id: identities.planId,
          article_id: articleId.trim(),
          outline_version: outlineVersion,
          max_gap_fill_rounds: 2,
          scopes: [
            {
              scope_id: identities.scopeId,
              ordinal: 0,
              scope_type: scopeType,
              scope_key: safeIdentity(scopeTitle),
              title: scopeTitle.trim(),
              query_variants: [query.trim()],
              minimum_hits: 2,
              minimum_distinct_sources: 1,
              require_hard_fact: requireHardFact,
            },
          ],
          metadata: { created_from: "knowledge_evidence_workbench" },
        },
      );
      setPack(
        await apiPost<KnowledgeEvidencePack>(
          `/api/knowledge/${encodeURIComponent(customer)}/retrieval-plans/${encodeURIComponent(plan.retrieval_plan_id)}/scopes/${encodeURIComponent(identities.scopeId)}/evidence-packs`,
          { limit: 8 },
        ),
      );
    } catch (runError) {
      setError(errorMessage(runError));
    } finally {
      setRunning(false);
    }
  }

  return (
    <section className="mx-auto grid max-w-[1480px] gap-5 px-5 pb-8">
      <Card className="overflow-hidden">
        <CardHeader className="border-b bg-muted/30">
          <div className="flex flex-wrap items-start justify-between gap-3">
            <div>
              <div className="mb-2 flex items-center gap-2 text-xs font-medium uppercase tracking-[0.16em] text-muted-foreground">
                <BookMarked className="size-4" />
                M3 evidence workspace
              </div>
              <CardTitle>章节证据试运行</CardTitle>
              <CardDescription className="mt-1 max-w-3xl">
                每次检索绑定文章、大纲版本和独立 Scope。结果只读取当前项目中已发布的当前快照，
                并保留来源、信任层级与向量/全文排序解释。
              </CardDescription>
            </div>
            <Badge variant="outline">不会直接改写正文</Badge>
          </div>
        </CardHeader>
        <CardContent className="grid gap-5 py-5">
          {error ? (
            <Alert variant="destructive">
              <AlertCircle />
              <AlertTitle>证据检索失败</AlertTitle>
              <AlertDescription>{error}</AlertDescription>
            </Alert>
          ) : null}

          <div className="grid gap-4 md:grid-cols-2 xl:grid-cols-5">
            <div className="grid gap-2">
              <Label htmlFor="evidence-article-id">文章 ID</Label>
              <Input
                id="evidence-article-id"
                value={articleId}
                onChange={(event) => setArticleId(event.target.value)}
              />
            </div>
            <div className="grid gap-2">
              <Label htmlFor="evidence-outline-version">大纲版本</Label>
              <Input
                id="evidence-outline-version"
                type="number"
                min={1}
                value={outlineVersion}
                onChange={(event) =>
                  setOutlineVersion(Math.max(1, Number(event.target.value) || 1))
                }
              />
            </div>
            <div className="grid gap-2">
              <Label htmlFor="evidence-scope-type">Scope 类型</Label>
              <select
                id="evidence-scope-type"
                value={scopeType}
                onChange={(event) =>
                  setScopeType(event.target.value as ScopeType)
                }
                className="h-9 rounded-md border border-input bg-transparent px-3 text-sm"
              >
                <option value="h2_section">H2 章节</option>
                <option value="product_fact">产品事实</option>
                <option value="faq">FAQ</option>
              </select>
            </div>
            <div className="grid gap-2">
              <Label htmlFor="evidence-scope-title">Scope 名称</Label>
              <Input
                id="evidence-scope-title"
                value={scopeTitle}
                onChange={(event) => setScopeTitle(event.target.value)}
              />
            </div>
            <label className="flex min-h-9 items-center gap-2 self-end rounded-md border px-3 text-sm">
              <input
                type="checkbox"
                checked={requireHardFact}
                onChange={(event) => setRequireHardFact(event.target.checked)}
              />
              必须有硬事实来源
            </label>
          </div>

          <div className="grid gap-3 md:grid-cols-[minmax(0,1fr)_auto] md:items-end">
            <div className="grid gap-2">
              <Label htmlFor="evidence-query">这个章节需要回答什么？</Label>
              <Input
                id="evidence-query"
                value={query}
                onChange={(event) => setQuery(event.target.value)}
                placeholder="例如：qewit fastener 的产品材质、尺寸和适用场景"
                onKeyDown={(event) => {
                  if (event.key === "Enter") void retrieveEvidence();
                }}
              />
            </div>
            <Button onClick={() => void retrieveEvidence()} disabled={running}>
              {running ? <Loader2 className="animate-spin" /> : <Search />}
              {running ? "检索并固化证据…" : "检索并生成 Evidence Pack"}
            </Button>
          </div>

          {pack ? (
            <div className="grid gap-4">
              <div className="flex flex-wrap items-center gap-2 rounded-lg border bg-card p-4">
                <Badge
                  variant={
                    pack.sufficiency === "sufficient"
                      ? "default"
                      : pack.sufficiency === "missing"
                        ? "destructive"
                        : "secondary"
                  }
                >
                  {sufficiencyLabel[pack.sufficiency]}
                </Badge>
                <span className="text-sm font-medium">{scopeTitle}</span>
                <span className="text-xs text-muted-foreground">
                  {pack.hits.length} 个 Chunk · {pack.public_citation_urls.length} 个公开引用
                </span>
                <span className="ml-auto font-mono text-[11px] text-muted-foreground">
                  outline v{pack.outline_version}
                </span>
              </div>

              {pack.gap_reasons.length ? (
                <Alert>
                  <ShieldCheck />
                  <AlertTitle>证据门禁说明</AlertTitle>
                  <AlertDescription>
                    {pack.gap_reasons.join("；")}
                  </AlertDescription>
                </Alert>
              ) : null}

              <div className="grid gap-3 lg:grid-cols-2">
                {pack.hits.map((hit, index) => (
                  <article
                    key={hit.chunk_id}
                    className="rounded-lg border bg-card p-4"
                  >
                    <div className="flex items-start justify-between gap-3">
                      <div className="min-w-0">
                        <div className="text-xs text-muted-foreground">
                          #{index + 1} · {scoreDetail(hit.explanation)}
                        </div>
                        <div className="mt-1 truncate text-sm font-medium">
                          {hit.provenance?.display_name ?? hit.chunk_id}
                        </div>
                      </div>
                      <Badge variant="outline">
                        {(hit.score * 100).toFixed(1)}
                      </Badge>
                    </div>
                    <p className="mt-3 line-clamp-5 whitespace-pre-wrap text-sm leading-6 text-muted-foreground">
                      {hit.text}
                    </p>
                    <div className="mt-3 flex flex-wrap items-center gap-2 text-xs text-muted-foreground">
                      {hit.provenance ? (
                        <>
                          <Badge variant="secondary">
                            {hit.provenance.trust_tier}
                          </Badge>
                          <span className="font-mono">
                            {hit.provenance.snapshot_id}
                          </span>
                        </>
                      ) : null}
                      {hit.provenance?.canonical_url ? (
                        <a
                          href={hit.provenance.canonical_url}
                          target="_blank"
                          rel="noreferrer"
                          className="ml-auto inline-flex items-center gap-1 hover:text-foreground"
                        >
                          公开来源
                          <ExternalLink className="size-3" />
                        </a>
                      ) : (
                        <span className="ml-auto">私有来源</span>
                      )}
                    </div>
                  </article>
                ))}
              </div>
            </div>
          ) : null}
        </CardContent>
      </Card>
    </section>
  );
}
