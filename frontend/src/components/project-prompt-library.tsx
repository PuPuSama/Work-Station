"use client";

/* eslint-disable react-hooks/set-state-in-effect */

import {
  AlertCircle,
  CheckCircle2,
  Eye,
  FileUp,
  Loader2,
  Pencil,
  Plus,
  Power,
  PowerOff,
  Save,
  Trash2,
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
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { ScrollArea } from "@/components/ui/scroll-area";
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

type PromptDialogMode = "preview" | "create" | "edit";

export function ProjectPromptLibraryCard({ customer }: { customer: string }) {
  const [library, setLibrary] = useState<ProjectPromptLibrary>(EMPTY_LIBRARY);
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState("");
  const [feedback, setFeedback] = useState<{ kind: "success" | "error"; message: string } | null>(null);
  const [dialogOpen, setDialogOpen] = useState(false);
  const [dialogMode, setDialogMode] = useState<PromptDialogMode>("preview");
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
  const selectedPrompt = library.prompts.find((prompt) => prompt.id === editingId);
  const promptEditorDirty =
    dialogMode === "create"
      ? Boolean(name.trim() || content.trim() || kind !== "outline")
      : dialogMode === "edit" && selectedPrompt
        ? name !== selectedPrompt.name || content !== selectedPrompt.content
        : false;

  function resetEditor() {
    setEditingId("");
    setName("");
    setContent("");
    setKind("outline");
    if (fileRef.current) fileRef.current.value = "";
  }

  function populateEditor(prompt: PromptLibraryItem) {
    setEditingId(prompt.id);
    setName(prompt.name);
    setKind(prompt.kind);
    setContent(prompt.content);
    setFeedback(null);
  }

  function openCreateDialog() {
    resetEditor();
    setDialogMode("create");
    setDialogOpen(true);
    setFeedback(null);
  }

  function openPreviewDialog(prompt: PromptLibraryItem) {
    populateEditor(prompt);
    setDialogMode("preview");
    setDialogOpen(true);
  }

  function openEditDialog(prompt: PromptLibraryItem) {
    populateEditor(prompt);
    setDialogMode("edit");
    setDialogOpen(true);
  }

  function closeDialog() {
    if (busy === "prompt") return;
    if (
      promptEditorDirty &&
      !window.confirm("提示词还有未保存修改，确定关闭并放弃这些修改吗？")
    ) {
      return;
    }
    setDialogOpen(false);
    resetEditor();
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
      await load();
      const successMessage = editingId ? "提示词新版本已保存。" : "提示词已加入项目库。";
      setDialogOpen(false);
      resetEditor();
      setFeedback({ kind: "success", message: successMessage });
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
    <>
      <Card className="rounded-lg">
      <CardHeader className="border-b">
        <CardTitle>项目提示词库</CardTitle>
        <CardDescription>
          分别管理完整的大纲、正文和 SEO 质量复检提示词。系统会自动注入文章资料，并始终保留事实、结构和链接硬约束。
        </CardDescription>
        <CardAction>
          <div className="flex items-center gap-2">
            <Badge variant="outline">{library.prompts.length} 份</Badge>
            <Button
              size="sm"
              onClick={openCreateDialog}
              disabled={loading || Boolean(busy)}
            >
              <Plus />
              新增提示词
            </Button>
          </div>
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

        <div className="grid gap-3">
          <div className="flex flex-wrap items-center justify-between gap-2">
            <div className="font-medium">已保存提示词</div>
            <p className="text-xs text-muted-foreground">
              点击预览查看完整内容；编辑会保存为新版本。
            </p>
          </div>
          {!loading && library.prompts.length === 0 && (
            <div className="rounded-lg border border-dashed p-6 text-center">
              <p className="text-sm text-muted-foreground">
                还没有项目提示词。
              </p>
              <Button
                className="mt-3"
                variant="outline"
                size="sm"
                onClick={openCreateDialog}
              >
                <Plus />
                新增第一份提示词
              </Button>
            </div>
          )}
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
                <Button variant="outline" size="sm" onClick={() => openPreviewDialog(prompt)} disabled={Boolean(busy)}>
                  <Eye />
                  预览
                </Button>
                <Button variant="outline" size="sm" onClick={() => openEditDialog(prompt)} disabled={Boolean(busy)}>
                  <Pencil />
                  编辑
                </Button>
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

      <Dialog
        open={dialogOpen}
        onOpenChange={(open) => {
          if (open) {
            setDialogOpen(true);
          } else {
            closeDialog();
          }
        }}
      >
        <DialogContent className="max-h-[90vh] overflow-hidden p-0 sm:max-w-3xl">
          <DialogHeader className="border-b px-5 py-4 pr-12">
            <DialogTitle>
              {dialogMode === "preview"
                ? "预览提示词"
                : dialogMode === "edit"
                  ? "编辑提示词"
                  : "新增提示词"}
            </DialogTitle>
            <DialogDescription>
              {dialogMode === "preview"
                ? "完整查看项目中保存的提示词内容。"
                : "系统会在生成时注入文章资料和项目上下文，无需在提示词中填写变量。"}
            </DialogDescription>
          </DialogHeader>

          {feedback && (
            <Alert
              className="mx-5 w-auto"
              variant={feedback.kind === "error" ? "destructive" : "default"}
            >
              {feedback.kind === "error" ? <AlertCircle /> : <CheckCircle2 />}
              <AlertTitle>
                {feedback.kind === "error" ? "操作失败" : "操作成功"}
              </AlertTitle>
              <AlertDescription>{feedback.message}</AlertDescription>
            </Alert>
          )}

          {dialogMode === "preview" && selectedPrompt ? (
            <div className="grid min-h-0 gap-4 px-5">
              <div className="flex flex-wrap items-center gap-2">
                <span className="text-base font-medium">{selectedPrompt.name}</span>
                <Badge variant="outline">
                  {selectedPrompt.kind === "outline"
                    ? "大纲"
                    : selectedPrompt.kind === "article"
                      ? "正文"
                      : "复检"}
                </Badge>
                <Badge variant={selectedPrompt.active ? "secondary" : "outline"}>
                  {selectedPrompt.active ? "使用中" : "已停用"}
                </Badge>
                <span className="text-xs text-muted-foreground">
                  v{selectedPrompt.version} · 已生成 {selectedPrompt.use_count} 次
                </span>
              </div>
              <ScrollArea className="h-[52vh] rounded-lg border bg-muted/20">
                <pre className="whitespace-pre-wrap break-words p-4 font-mono text-sm leading-6">
                  {selectedPrompt.content}
                </pre>
              </ScrollArea>
            </div>
          ) : (
            <div className="grid min-h-0 gap-4 overflow-y-auto px-5">
              <div className="grid gap-3 md:grid-cols-[1fr_180px]">
                <div className="grid gap-2">
                  <Label htmlFor="prompt-dialog-name">名称</Label>
                  <Input
                    id="prompt-dialog-name"
                    value={name}
                    maxLength={120}
                    disabled={busy === "prompt"}
                    onChange={(event) => setName(event.target.value)}
                    placeholder="例如：产品对比型正文"
                  />
                </div>
                <div className="grid gap-2">
                  <Label htmlFor="prompt-dialog-kind">类型</Label>
                  <select
                    id="prompt-dialog-kind"
                    className="h-9 rounded-md border bg-background px-3 text-sm"
                    value={kind}
                    disabled={dialogMode === "edit" || busy === "prompt"}
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
                  <Label htmlFor="prompt-dialog-content">提示词内容</Label>
                  <div>
                    <input
                      ref={fileRef}
                      type="file"
                      accept=".txt,text/plain"
                      className="hidden"
                      disabled={busy === "prompt"}
                      onChange={(event) => {
                        const file = event.target.files?.[0];
                        if (file) void readFile(file);
                      }}
                    />
                    <Button
                      type="button"
                      variant="outline"
                      size="sm"
                      disabled={busy === "prompt"}
                      onClick={() => fileRef.current?.click()}
                    >
                      <FileUp />
                      上传 .txt
                    </Button>
                  </div>
                </div>
                <Textarea
                  id="prompt-dialog-content"
                  value={content}
                  maxLength={40000}
                  disabled={busy === "prompt"}
                  className="min-h-[42vh] resize-y font-mono text-sm leading-6"
                  onChange={(event) => setContent(event.target.value)}
                  placeholder="直接粘贴完整提示词，或上传 UTF-8 .txt 文件。无需填写变量。"
                />
                <p className="text-right text-xs text-muted-foreground">
                  {content.length.toLocaleString()} / 40,000
                </p>
              </div>
            </div>
          )}

          <DialogFooter className="mx-0 mb-0 px-5 py-4">
            <Button variant="outline" onClick={closeDialog} disabled={busy === "prompt"}>
              关闭
            </Button>
            {dialogMode === "preview" && selectedPrompt ? (
              <Button
                onClick={() => setDialogMode("edit")}
                disabled={Boolean(busy)}
              >
                <Pencil />
                编辑这份提示词
              </Button>
            ) : (
              <Button
                onClick={() => void savePrompt()}
                disabled={!name.trim() || !content.trim() || Boolean(busy)}
              >
                {busy === "prompt" ? <Loader2 className="animate-spin" /> : <Save />}
                {dialogMode === "edit" ? "保存为新版本" : "加入提示词库"}
              </Button>
            )}
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </>
  );
}
