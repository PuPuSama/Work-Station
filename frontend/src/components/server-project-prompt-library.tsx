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
  CardContent,
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
import { ApiError, apiDelete, apiGet, apiPost, apiPut } from "@/lib/api";
import type {
  ServerPromptDirectory,
  ServerPromptItem,
  ServerPromptKind,
} from "@/types";

type ServerProjectPromptLibraryProps = {
  projectId: string;
};

type DialogMode = "preview" | "create" | "edit";
type Feedback = { kind: "success" | "error"; message: string } | null;

const KINDS: ServerPromptKind[] = ["outline", "article", "review", "humanize"];

const KIND_LABELS: Record<ServerPromptKind, string> = {
  outline: "大纲",
  article: "正文",
  review: "SEO 复检",
  humanize: "降 AI 改写",
};

const EMPTY_DIRECTORY: ServerPromptDirectory = {
  prompts: [],
  defaults: {},
};

function errorMessage(error: unknown) {
  if (error instanceof ApiError && error.status === 409) {
    return "提示词已被其他成员更新，请重新加载后再编辑。";
  }
  return error instanceof Error && error.message
    ? error.message
    : "项目提示词操作失败，请重试。";
}

function kindLabel(kind: ServerPromptKind) {
  return KIND_LABELS[kind];
}

export function ServerProjectPromptLibrary({
  projectId,
}: ServerProjectPromptLibraryProps) {
  const encodedProject = useMemo(
    () => encodeURIComponent(projectId),
    [projectId],
  );
  const promptApi = `/api/projects/${encodedProject}/prompt-snapshots`;
  const [directory, setDirectory] = useState<ServerPromptDirectory>(
    EMPTY_DIRECTORY,
  );
  const [defaultSelections, setDefaultSelections] = useState<
    Record<ServerPromptKind, string>
  >({ outline: "", article: "", review: "", humanize: "" });
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState("");
  const [feedback, setFeedback] = useState<Feedback>(null);
  const [dialogOpen, setDialogOpen] = useState(false);
  const [dialogMode, setDialogMode] = useState<DialogMode>("preview");
  const [editingId, setEditingId] = useState("");
  const [name, setName] = useState("");
  const [kind, setKind] = useState<ServerPromptKind | "">("");
  const [content, setContent] = useState("");
  const fileRef = useRef<HTMLInputElement>(null);

  const load = useCallback(async (showLoader = true) => {
    if (showLoader) setLoading(true);
    try {
      const next = await apiGet<ServerPromptDirectory>(promptApi);
      setDirectory(next);
      setDefaultSelections({
        outline: next.defaults.outline?.prompt_id ?? "",
        article: next.defaults.article?.prompt_id ?? "",
        review: next.defaults.review?.prompt_id ?? "",
        humanize: next.defaults.humanize?.prompt_id ?? "",
      });
      setFeedback(null);
    } catch (error) {
      setFeedback({ kind: "error", message: errorMessage(error) });
    } finally {
      if (showLoader) setLoading(false);
    }
  }, [promptApi]);

  useEffect(() => {
    void load();
  }, [load]);

  const activeByKind = useMemo(() => {
    const result = {} as Record<ServerPromptKind, ServerPromptItem[]>;
    for (const promptKind of KINDS) {
      result[promptKind] = directory.prompts.filter(
        (prompt) => prompt.kind === promptKind && prompt.status === "active",
      );
    }
    return result;
  }, [directory.prompts]);

  const selectedPrompt = directory.prompts.find(
    (prompt) => prompt.prompt_id === editingId,
  );
  const editorDirty =
    dialogMode === "create"
      ? Boolean(name.trim() || content.trim() || kind)
      : dialogMode === "edit" && selectedPrompt
        ? name !== selectedPrompt.name ||
          kind !== selectedPrompt.kind ||
          content !== selectedPrompt.content
        : false;

  function resetEditor() {
    setEditingId("");
    setName("");
    setKind("");
    setContent("");
    if (fileRef.current) fileRef.current.value = "";
  }

  function openCreate() {
    resetEditor();
    setDialogMode("create");
    setDialogOpen(true);
    setFeedback(null);
  }

  function openPrompt(prompt: ServerPromptItem, mode: DialogMode) {
    setEditingId(prompt.prompt_id);
    setName(prompt.name);
    setKind(prompt.kind);
    setContent(prompt.content);
    setDialogMode(mode);
    setDialogOpen(true);
    setFeedback(null);
  }

  function closeDialog() {
    if (busy === "prompt") return;
    if (
      editorDirty &&
      !window.confirm("提示词还有未保存修改，确定关闭并放弃吗？")
    ) {
      return;
    }
    setDialogOpen(false);
    resetEditor();
  }

  async function savePrompt() {
    const trimmedName = name.trim();
    const trimmedContent = content.trim();
    if (!trimmedName || !trimmedContent || !kind) return;
    setBusy("prompt");
    setFeedback(null);
    const typeChanged = Boolean(
      editingId && selectedPrompt && kind !== selectedPrompt.kind,
    );
    try {
      if (editingId && selectedPrompt) {
        await apiPut<ServerPromptItem>(
          `${promptApi}/${encodeURIComponent(editingId)}`,
          {
            expected_version: selectedPrompt.version,
            name: trimmedName,
            kind,
            content: trimmedContent,
          },
        );
      } else {
        await apiPost<ServerPromptItem>(promptApi, {
          name: trimmedName,
          kind,
          content: trimmedContent,
        });
      }
      await load(false);
      setDialogOpen(false);
      resetEditor();
      setFeedback({
        kind: "success",
        message: typeChanged
          ? "提示词类型已修改，原类型记录已停用。"
          : editingId
            ? "提示词已保存。"
            : "提示词已加入项目库。",
      });
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
      for (const promptKind of KINDS) {
        await apiPut(
          `${promptApi.replace(/\/prompt-snapshots$/, "")}/prompt-defaults/${promptKind}`,
          { prompt_id: defaultSelections[promptKind] || null },
        );
      }
      await load(false);
      setFeedback({ kind: "success", message: "项目默认提示词已保存。" });
    } catch (error) {
      setFeedback({ kind: "error", message: errorMessage(error) });
      await load(false);
    } finally {
      setBusy("");
    }
  }

  async function toggleActive(prompt: ServerPromptItem) {
    setBusy(prompt.prompt_id);
    setFeedback(null);
    try {
      await apiPut<ServerPromptItem>(
        `${promptApi}/${encodeURIComponent(prompt.prompt_id)}/active`,
        {
          expected_version: prompt.version,
          active: prompt.status !== "active",
        },
      );
      await load(false);
      setFeedback({
        kind: "success",
        message:
          prompt.status === "active" ? "提示词已停用。" : "提示词已恢复。",
      });
    } catch (error) {
      setFeedback({ kind: "error", message: errorMessage(error) });
    } finally {
      setBusy("");
    }
  }

  async function deletePrompt(prompt: ServerPromptItem) {
    if (!window.confirm(`确定删除“${prompt.name}”吗？删除后不可恢复。`)) {
      return;
    }
    setBusy(prompt.prompt_id);
    setFeedback(null);
    try {
      await apiDelete(
        `${promptApi}/${encodeURIComponent(prompt.prompt_id)}`,
      );
      if (editingId === prompt.prompt_id) {
        setDialogOpen(false);
        resetEditor();
      }
      await load(false);
      setFeedback({ kind: "success", message: "提示词已删除。" });
    } catch (error) {
      setFeedback({ kind: "error", message: errorMessage(error) });
    } finally {
      setBusy("");
    }
  }

  async function readFile(file: File) {
    setFeedback(null);
    if (!file.name.toLowerCase().endsWith(".txt")) {
      setFeedback({ kind: "error", message: "仅支持 UTF-8 .txt 提示词文件。" });
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
          <div className="flex flex-wrap items-start justify-between gap-3">
            <div className="min-w-0">
              <CardTitle>项目提示词库</CardTitle>
              <p className="mt-1 max-w-3xl text-sm leading-6 text-muted-foreground">
                管理大纲、正文、SEO 复检和降 AI 改写提示词。生成时会自动注入项目资料；提示词版本会固定到任务快照。
              </p>
            </div>
            <div className="flex items-center gap-2">
              <Badge variant="outline">{directory.prompts.length} 份</Badge>
              <Button
                type="button"
                size="sm"
                onClick={openCreate}
                disabled={loading || Boolean(busy)}
              >
                <Plus />
                新增提示词
              </Button>
            </div>
          </div>
        </CardHeader>
        <CardContent className="grid gap-6">
          {feedback ? (
            <Alert variant={feedback.kind === "error" ? "destructive" : "default"}>
              {feedback.kind === "error" ? <AlertCircle /> : <CheckCircle2 />}
              <AlertTitle>{feedback.kind === "error" ? "操作失败" : "操作成功"}</AlertTitle>
              <AlertDescription>{feedback.message}</AlertDescription>
            </Alert>
          ) : null}

          <div className="grid gap-4 rounded-lg border p-4 sm:grid-cols-2 xl:grid-cols-4">
            {KINDS.map((promptKind) => (
              <div className="grid gap-2" key={promptKind}>
                <Label htmlFor={`server-default-${promptKind}`}>
                  默认{kindLabel(promptKind)}提示词
                </Label>
                <select
                  id={`server-default-${promptKind}`}
                  className="min-h-11 rounded-md border bg-background px-3 text-sm"
                  value={defaultSelections[promptKind]}
                  disabled={loading || Boolean(busy)}
                  onChange={(event) =>
                    setDefaultSelections((current) => ({
                      ...current,
                      [promptKind]: event.target.value,
                    }))
                  }
                >
                  <option value="">
                    {promptKind === "humanize" ? "未配置" : "系统默认"}
                  </option>
                  {activeByKind[promptKind].map((prompt) => (
                    <option key={prompt.prompt_id} value={prompt.prompt_id}>
                      {prompt.name}（v{prompt.version}）
                    </option>
                  ))}
                </select>
              </div>
            ))}
            <div className="flex items-end sm:col-span-2 xl:col-span-4">
              <Button
                type="button"
                onClick={() => void saveDefaults()}
                disabled={loading || Boolean(busy)}
              >
                {busy === "defaults" ? <Loader2 className="animate-spin" /> : <Save />}
                保存项目默认值
              </Button>
            </div>
          </div>

          <div className="rounded-lg border border-amber-200 bg-amber-50 p-3 text-sm leading-6 text-amber-950">
            降 AI 改写提示词必须包含且只能包含一个 <code>{"{{ARTICLE}}"}</code> 占位符；其余三类提示词不需要填写变量。
          </div>

          <div className="grid gap-3">
            {loading ? (
              <div className="rounded-lg border border-dashed p-6 text-center text-sm text-muted-foreground">
                正在加载项目提示词…
              </div>
            ) : directory.prompts.length === 0 ? (
              <div className="rounded-lg border border-dashed p-6 text-center">
                <p className="text-sm text-muted-foreground">还没有项目提示词。</p>
                <Button className="mt-3" variant="outline" size="sm" onClick={openCreate}>
                  <Plus />
                  新增第一份提示词
                </Button>
              </div>
            ) : (
              directory.prompts.map((prompt) => (
                <div
                  key={prompt.prompt_id}
                  className="grid gap-3 rounded-lg border p-4 md:grid-cols-[1fr_auto] md:items-center"
                >
                  <div className="min-w-0">
                    <div className="flex flex-wrap items-center gap-2">
                      <span className="font-medium">{prompt.name}</span>
                      <Badge variant="outline">{kindLabel(prompt.kind)}</Badge>
                      <Badge variant={prompt.status === "active" ? "secondary" : "outline"}>
                        {prompt.status === "active" ? "使用中" : "已停用"}
                      </Badge>
                      <span className="text-xs text-muted-foreground">
                        v{prompt.version}
                      </span>
                    </div>
                    <p className="mt-1 line-clamp-2 whitespace-pre-wrap text-sm text-muted-foreground">
                      {prompt.content}
                    </p>
                  </div>
                  <div className="flex flex-wrap gap-2">
                    <Button
                      type="button"
                      variant="outline"
                      size="sm"
                      onClick={() => openPrompt(prompt, "preview")}
                      disabled={Boolean(busy)}
                    >
                      <Eye />
                      预览
                    </Button>
                    {prompt.status === "active" ? (
                      <Button
                        type="button"
                        variant="outline"
                        size="sm"
                        onClick={() => openPrompt(prompt, "edit")}
                        disabled={Boolean(busy)}
                      >
                        <Pencil />
                        编辑
                      </Button>
                    ) : null}
                    <Button
                      type="button"
                      variant="outline"
                      size="sm"
                      onClick={() => void toggleActive(prompt)}
                      disabled={Boolean(busy)}
                    >
                      {prompt.status === "active" ? <PowerOff /> : <Power />}
                      {prompt.status === "active" ? "停用" : "恢复"}
                    </Button>
                    <Button
                      type="button"
                      variant="outline"
                      size="sm"
                      onClick={() => void deletePrompt(prompt)}
                      disabled={Boolean(busy)}
                    >
                      <Trash2 />
                      删除
                    </Button>
                  </div>
                </div>
              ))
            )}
          </div>
        </CardContent>
      </Card>

      <Dialog
        open={dialogOpen}
        onOpenChange={(open) => {
          if (open) setDialogOpen(true);
          else closeDialog();
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
                ? "查看当前项目中保存的完整提示词。"
                : "编辑会直接修改当前版本；已排队任务会按创建时的提示词快照校验。"}
            </DialogDescription>
          </DialogHeader>

          {dialogMode === "preview" && selectedPrompt ? (
            <div className="grid min-h-0 gap-4 px-5">
              <div className="flex flex-wrap items-center gap-2">
                <span className="font-medium">{selectedPrompt.name}</span>
                <Badge variant="outline">{kindLabel(selectedPrompt.kind)}</Badge>
                <Badge variant={selectedPrompt.status === "active" ? "secondary" : "outline"}>
                  {selectedPrompt.status === "active" ? "使用中" : "已停用"}
                </Badge>
                <span className="text-xs text-muted-foreground">v{selectedPrompt.version}</span>
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
                  <Label htmlFor="server-prompt-name">名称</Label>
                  <Input
                    id="server-prompt-name"
                    value={name}
                    maxLength={120}
                    disabled={busy === "prompt"}
                    onChange={(event) => setName(event.target.value)}
                    placeholder="例如：B2B 产品型正文"
                  />
                </div>
                <div className="grid gap-2">
                  <Label htmlFor="server-prompt-kind">类型</Label>
                  <select
                    id="server-prompt-kind"
                    className="min-h-11 rounded-md border bg-background px-3 text-sm"
                    value={kind}
                    disabled={dialogMode === "edit" || busy === "prompt"}
                    onChange={(event) => setKind(event.target.value as ServerPromptKind)}
                  >
                    <option value="" disabled>
                      请选择类型
                    </option>
                    {KINDS.map((promptKind) => (
                      <option key={promptKind} value={promptKind}>
                        {kindLabel(promptKind)}提示词
                      </option>
                    ))}
                  </select>
                </div>
              </div>
              <div className="grid gap-2">
                <div className="flex flex-wrap items-center justify-between gap-2">
                  <Label htmlFor="server-prompt-content">提示词内容</Label>
                  <>
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
                  </>
                </div>
                <Textarea
                  id="server-prompt-content"
                  value={content}
                  maxLength={40_000}
                  disabled={busy === "prompt"}
                  className="min-h-[42vh] resize-y font-mono text-sm leading-6"
                  onChange={(event) => setContent(event.target.value)}
                  placeholder={
                    kind === "humanize"
                      ? "必须包含一次 {{ARTICLE}}，它会被替换为待改写正文。"
                      : "直接粘贴完整提示词，或上传 UTF-8 .txt 文件。"
                  }
                />
                <p className="text-right text-xs text-muted-foreground">
                  {content.length.toLocaleString()} / 40,000
                </p>
              </div>
            </div>
          )}

          <DialogFooter className="mx-0 mb-0 px-5 py-4">
            <Button type="button" variant="outline" onClick={closeDialog} disabled={busy === "prompt"}>
              关闭
            </Button>
            {dialogMode === "preview" && selectedPrompt ? (
              <Button type="button" onClick={() => setDialogMode("edit")} disabled={Boolean(busy) || selectedPrompt.status !== "active"}>
                <Pencil />
                编辑这份提示词
              </Button>
            ) : (
              <Button
                type="button"
                onClick={() => void savePrompt()}
                disabled={!name.trim() || !kind || !content.trim() || Boolean(busy)}
              >
                {busy === "prompt" ? <Loader2 className="animate-spin" /> : <Save />}
                {dialogMode === "edit" ? "保存修改" : "加入提示词库"}
              </Button>
            )}
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </>
  );
}
