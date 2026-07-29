"use client";

/* eslint-disable react-hooks/set-state-in-effect */

import {
  AlertCircle,
  CheckCircle2,
  FileUp,
  Loader2,
  Pencil,
  Power,
  PowerOff,
  Save,
  Trash2,
  X,
} from "lucide-react";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";

import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import {
  Card,
  CardAction,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Textarea } from "@/components/ui/textarea";
import { apiDelete, apiGet, apiPost, apiPut } from "@/lib/api";
import type {
  ApiMessage,
  ProjectPromptLibrary,
  PromptKind,
  PromptLibraryItem,
} from "@/types";

function errorMessage(error: unknown) {
  return error instanceof Error ? error.message : "未知错误";
}

const EMPTY_LIBRARY: ProjectPromptLibrary = {
  prompts: [],
  defaults: {
    customer: "",
    default_outline_prompt_id: "",
    default_article_prompt_id: "",
    default_review_prompt_id: "",
  },
};

export function ProjectPromptLibraryCard({ customer }: { customer: string }) {
  const [library, setLibrary] = useState<ProjectPromptLibrary>(EMPTY_LIBRARY);
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState("");
  const [feedback, setFeedback] = useState<{ kind: "success" | "error"; message: string } | null>(null);
  const [editingId, setEditingId] = useState("");
  const [name, setName] = useState("");
  const [kind, setKind] = useState<PromptKind>("outline");
  const [content, setContent] = useState("");
  const [outlineDefault, setOutlineDefault] = useState("");
  const [articleDefault, setArticleDefault] = useState("");
  const [reviewDefault, setReviewDefault] = useState("");
  const fileRef = useRef<HTMLInputElement>(null);

  const load = useCallback(async () => {
    setLoading(true);
    try {
      const next = await apiGet<ProjectPromptLibrary>(
        `/api/projects/${encodeURIComponent(customer)}/prompts`,
      );
      setLibrary(next);
      setOutlineDefault(next.defaults.default_outline_prompt_id);
      setArticleDefault(next.defaults.default_article_prompt_id);
      setReviewDefault(next.defaults.default_review_prompt_id);
    } catch (error) {
      setFeedback({ kind: "error", message: errorMessage(error) });
    } finally {
      setLoading(false);
    }
  }, [customer]);

  useEffect(() => {
    void load();
  }, [load]);

  const outlinePrompts = useMemo(
    () => library.prompts.filter((prompt) => prompt.kind === "outline" && prompt.active),
    [library.prompts],
  );
  const articlePrompts = useMemo(
    () => library.prompts.filter((prompt) => prompt.kind === "article" && prompt.active),
    [library.prompts],
  );
  const reviewPrompts = useMemo(
    () => library.prompts.filter((prompt) => prompt.kind === "review" && prompt.active),
    [library.prompts],
  );
  const defaultsDirty =
    outlineDefault !== library.defaults.default_outline_prompt_id ||
    articleDefault !== library.defaults.default_article_prompt_id ||
    reviewDefault !== library.defaults.default_review_prompt_id;

  function resetEditor() {
    setEditingId("");
    setName("");
    setContent("");
    setKind("outline");
    if (fileRef.current) fileRef.current.value = "";
  }

  function edit(prompt: PromptLibraryItem) {
    setEditingId(prompt.id);
    setName(prompt.name);
    setKind(prompt.kind);
    setContent(prompt.content);
    setFeedback(null);
  }

  async function savePrompt() {
    if (!name.trim() || !content.trim()) return;
    setBusy("prompt");
    setFeedback(null);
    try {
      if (editingId) {
        await apiPut<PromptLibraryItem>(
          `/api/projects/${encodeURIComponent(customer)}/prompts/${editingId}`,
          { name, content },
        );
      } else {
        await apiPost<PromptLibraryItem>(
          `/api/projects/${encodeURIComponent(customer)}/prompts`,
          { name, kind, content },
        );
      }
      resetEditor();
      await load();
      setFeedback({ kind: "success", message: editingId ? "提示词新版本已保存。" : "提示词已加入项目库。" });
    } catch (error) {
      setFeedback({ kind: "error", message: errorMessage(error) });
    } finally {
      setBusy("");
    }
  }

  async function saveDefaults() {
    setBusy("defaults");
    setFeedback(null);
    try {
      await apiPut(
        `/api/projects/${encodeURIComponent(customer)}/prompt-defaults`,
        {
          default_outline_prompt_id: outlineDefault,
          default_article_prompt_id: articleDefault,
          default_review_prompt_id: reviewDefault,
        },
      );
      await load();
      setFeedback({ kind: "success", message: "项目默认提示词已保存。" });
    } catch (error) {
      setFeedback({ kind: "error", message: errorMessage(error) });
    } finally {
      setBusy("");
    }
  }

  async function toggleActive(prompt: PromptLibraryItem) {
    setBusy(prompt.id);
    setFeedback(null);
    try {
      await apiPut<PromptLibraryItem>(
        `/api/projects/${encodeURIComponent(customer)}/prompts/${prompt.id}/active`,
        { active: !prompt.active },
      );
      await load();
      setFeedback({ kind: "success", message: prompt.active ? "提示词已停用。" : "提示词已恢复。" });
    } catch (error) {
      setFeedback({ kind: "error", message: errorMessage(error) });
    } finally {
      setBusy("");
    }
  }

  async function remove(prompt: PromptLibraryItem) {
    if (!window.confirm(`彻底删除“${prompt.name}”吗？`)) return;
    setBusy(prompt.id);
    setFeedback(null);
    try {
      const result = await apiDelete<ApiMessage>(
        `/api/projects/${encodeURIComponent(customer)}/prompts/${prompt.id}`,
      );
      if (editingId === prompt.id) resetEditor();
      await load();
      setFeedback({ kind: "success", message: result.message });
    } catch (error) {
      setFeedback({ kind: "error", message: errorMessage(error) });
    } finally {
      setBusy("");
    }
  }

  async function readFile(file: File) {
    setFeedback(null);
    if (!file.name.toLowerCase().endsWith(".txt")) {
      setFeedback({ kind: "error", message: "仅支持 UTF-8 的 .txt 文件。" });
      return;
    }
    if (file.size > 160_000) {
      setFeedback({ kind: "error", message: "提示词文件过大，请控制在 40,000 字符以内。" });
      return;
    }
    const text = await file.text();
    if (text.length > 40_000) {
      setFeedback({ kind: "error", message: "提示词最多 40,000 字符。" });
      return;
    }
    setContent(text.replaceAll("\r\n", "\n"));
    if (!name.trim()) setName(file.name.replace(/\.txt$/i, ""));
  }

  return (
    <Card className="rounded-lg">
      <CardHeader className="border-b">
        <CardTitle>项目提示词库</CardTitle>
        <CardDescription>
          分别管理完整的大纲、正文和 SEO 质量复检提示词。系统会自动注入文章资料，并始终保留事实、结构和链接硬约束。
        </CardDescription>
        <CardAction>
          <Badge variant="outline">{library.prompts.length} 份</Badge>
        </CardAction>
      </CardHeader>
      <CardContent className="grid gap-6">
        {feedback && (
          <Alert variant={feedback.kind === "error" ? "destructive" : "default"}>
            {feedback.kind === "error" ? <AlertCircle /> : <CheckCircle2 />}
            <AlertTitle>{feedback.kind === "error" ? "操作失败" : "操作成功"}</AlertTitle>
            <AlertDescription>{feedback.message}</AlertDescription>
          </Alert>
        )}

        <div className="grid gap-4 rounded-lg border p-4 md:grid-cols-3">
          <div className="grid gap-2">
            <Label htmlFor="default-outline-prompt">默认大纲提示词</Label>
            <select
              id="default-outline-prompt"
              className="h-9 rounded-md border bg-background px-3 text-sm"
              value={outlineDefault}
              disabled={loading || Boolean(busy)}
              onChange={(event) => setOutlineDefault(event.target.value)}
            >
              <option value="">系统默认</option>
              {outlinePrompts.map((prompt) => <option key={prompt.id} value={prompt.id}>{prompt.name}</option>)}
            </select>
          </div>
          <div className="grid gap-2">
            <Label htmlFor="default-article-prompt">默认正文提示词</Label>
            <select
              id="default-article-prompt"
              className="h-9 rounded-md border bg-background px-3 text-sm"
              value={articleDefault}
              disabled={loading || Boolean(busy)}
              onChange={(event) => setArticleDefault(event.target.value)}
            >
              <option value="">系统默认</option>
              {articlePrompts.map((prompt) => <option key={prompt.id} value={prompt.id}>{prompt.name}</option>)}
            </select>
          </div>
          <div className="grid gap-2">
            <Label htmlFor="default-review-prompt">默认复检提示词</Label>
            <select
              id="default-review-prompt"
              className="h-9 rounded-md border bg-background px-3 text-sm"
              value={reviewDefault}
              disabled={loading || Boolean(busy)}
              onChange={(event) => setReviewDefault(event.target.value)}
            >
              <option value="">系统默认 SEO 质量复检</option>
              {reviewPrompts.map((prompt) => <option key={prompt.id} value={prompt.id}>{prompt.name}</option>)}
            </select>
          </div>
          <div className="flex justify-end md:col-span-3">
            <Button onClick={() => void saveDefaults()} disabled={!defaultsDirty || Boolean(busy)}>
              {busy === "defaults" ? <Loader2 className="animate-spin" /> : <Save />}
              保存项目默认值
            </Button>
          </div>
        </div>

        <div className="grid gap-3 rounded-lg border p-4">
          <div className="flex flex-wrap items-center justify-between gap-2">
            <div className="font-medium">{editingId ? "编辑提示词" : "新增提示词"}</div>
            {editingId && <Button variant="ghost" size="sm" onClick={resetEditor}><X />取消编辑</Button>}
          </div>
          <div className="grid gap-3 md:grid-cols-[1fr_180px]">
            <div className="grid gap-2">
              <Label htmlFor="prompt-name">名称</Label>
              <Input id="prompt-name" value={name} maxLength={120} onChange={(event) => setName(event.target.value)} placeholder="例如：产品对比型正文" />
            </div>
            <div className="grid gap-2">
              <Label htmlFor="prompt-kind">类型</Label>
              <select
                id="prompt-kind"
                className="h-9 rounded-md border bg-background px-3 text-sm"
                value={kind}
                disabled={Boolean(editingId)}
                onChange={(event) => setKind(event.target.value as PromptKind)}
              >
                <option value="outline">大纲提示词</option>
                <option value="article">正文提示词</option>
                <option value="review">复检提示词</option>
              </select>
            </div>
          </div>
          <div className="grid gap-2">
            <div className="flex flex-wrap items-center justify-between gap-2">
              <Label htmlFor="prompt-content">提示词内容</Label>
              <div>
                <input ref={fileRef} type="file" accept=".txt,text/plain" className="hidden" onChange={(event) => { const file = event.target.files?.[0]; if (file) void readFile(file); }} />
                <Button type="button" variant="outline" size="sm" onClick={() => fileRef.current?.click()}>
                  <FileUp />上传 .txt
                </Button>
              </div>
            </div>
            <Textarea id="prompt-content" value={content} maxLength={40000} className="min-h-52 resize-y font-mono text-sm" onChange={(event) => setContent(event.target.value)} placeholder="直接粘贴完整提示词，或上传 UTF-8 .txt 文件。无需填写变量。" />
            <p className="text-right text-xs text-muted-foreground">{content.length.toLocaleString()} / 40,000</p>
          </div>
          <div className="flex justify-end">
            <Button onClick={() => void savePrompt()} disabled={!name.trim() || !content.trim() || Boolean(busy)}>
              {busy === "prompt" ? <Loader2 className="animate-spin" /> : <Save />}
              {editingId ? "保存为新版本" : "加入提示词库"}
            </Button>
          </div>
        </div>

        <div className="grid gap-3">
          <div className="font-medium">已保存提示词</div>
          {!loading && library.prompts.length === 0 && <p className="text-sm text-muted-foreground">还没有项目提示词，可在上方粘贴或上传第一份。</p>}
          {library.prompts.map((prompt) => (
            <div key={prompt.id} className="grid gap-3 rounded-lg border p-4 md:grid-cols-[1fr_auto] md:items-center">
              <div className="min-w-0">
                <div className="flex flex-wrap items-center gap-2">
                  <span className="font-medium">{prompt.name}</span>
                  <Badge variant="outline">
                    {prompt.kind === "outline" ? "大纲" : prompt.kind === "article" ? "正文" : "复检"}
                  </Badge>
                  <Badge variant={prompt.active ? "secondary" : "outline"}>{prompt.active ? "使用中" : "已停用"}</Badge>
                  <span className="text-xs text-muted-foreground">v{prompt.version} · 已生成 {prompt.use_count} 次</span>
                </div>
                <p className="mt-1 line-clamp-2 whitespace-pre-wrap text-sm text-muted-foreground">{prompt.content}</p>
              </div>
              <div className="flex flex-wrap gap-2">
                <Button variant="outline" size="sm" onClick={() => edit(prompt)} disabled={Boolean(busy)}><Pencil />编辑</Button>
                <Button variant="outline" size="sm" onClick={() => void toggleActive(prompt)} disabled={Boolean(busy)}>
                  {prompt.active ? <PowerOff /> : <Power />}{prompt.active ? "停用" : "恢复"}
                </Button>
                {prompt.use_count === 0 && (
                  <Button variant="outline" size="sm" onClick={() => void remove(prompt)} disabled={Boolean(busy)}><Trash2 />删除</Button>
                )}
              </div>
            </div>
          ))}
        </div>
      </CardContent>
    </Card>
  );
}
