"use client";

/* eslint-disable react-hooks/set-state-in-effect */

import {
  AlertCircle,
  CheckCircle2,
  Eye,
  Loader2,
  RefreshCw,
  Save,
} from "lucide-react";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";

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
import { Label } from "@/components/ui/label";
import { Textarea } from "@/components/ui/textarea";
import { ApiError, apiGet, apiPost, apiPut } from "@/lib/api";
import type {
  EffectivePromptPreview,
  PromptKind,
  ServerPromptDirectory,
  ServerTaskWritingSettings,
  TaskRecord,
} from "@/types";

type PreviewKind = Extract<PromptKind, "outline" | "article">;

type ServerWritingRequirementsPanelProps = {
  task: TaskRecord;
  projectApi: string;
  taskApi: string;
  canEdit: boolean;
  onTaskUpdated: (task: TaskRecord) => void;
  onDirtyChange: (dirty: boolean) => void;
  onGenerationBlockedChange: (blocked: boolean) => void;
};

function errorMessage(error: unknown, fallback: string) {
  return error instanceof Error && error.message ? error.message : fallback;
}

function settingsFromTask(task: TaskRecord): ServerTaskWritingSettings {
  return {
    topic_notes: task.topic_notes || "",
    outline_custom_prompt: task.outline_custom_prompt || "",
    article_custom_prompt: task.article_custom_prompt || "",
    use_outline_custom_prompt: task.use_outline_custom_prompt ?? false,
    use_article_custom_prompt: task.use_article_custom_prompt ?? false,
    outline_prompt_selection:
      task.outline_prompt_selection || "project_default",
    article_prompt_selection:
      task.article_prompt_selection || "project_default",
    include_project_introduction:
      task.include_project_introduction ?? true,
    include_project_notes: task.include_project_notes ?? true,
    include_topic_notes: task.include_topic_notes ?? true,
  };
}

function settingsEqual(
  left: ServerTaskWritingSettings,
  right: ServerTaskWritingSettings,
) {
  return JSON.stringify(left) === JSON.stringify(right);
}

function kindLabel(kind: PreviewKind) {
  return kind === "outline" ? "大纲" : "正文";
}

function selectedPrompt(
  directory: ServerPromptDirectory | null,
  selection: string,
) {
  if (!directory || selection === "project_default" || selection === "system") {
    return null;
  }
  return (
    directory.prompts.find((prompt) => prompt.prompt_id === selection) || null
  );
}

function selectionInvalid(
  directory: ServerPromptDirectory | null,
  selection: string,
) {
  if (
    !directory ||
    selection === "project_default" ||
    selection === "system"
  ) {
    return false;
  }
  return selectedPrompt(directory, selection)?.status !== "active";
}

function currentRevisionFrom(error: unknown) {
  if (!(error instanceof ApiError) || !error.detail || typeof error.detail !== "object") {
    return null;
  }
  const detail = error.detail as Record<string, unknown>;
  const value = detail.current_revision ?? detail.revision;
  return typeof value === "number" && Number.isInteger(value) ? value : null;
}

export function ServerWritingRequirementsPanel({
  task,
  projectApi,
  taskApi,
  canEdit,
  onTaskUpdated,
  onDirtyChange,
  onGenerationBlockedChange,
}: ServerWritingRequirementsPanelProps) {
  const initialSettings = useMemo(() => settingsFromTask(task), [task]);
  const [settings, setSettings] =
    useState<ServerTaskWritingSettings>(initialSettings);
  const [baseline, setBaseline] =
    useState<ServerTaskWritingSettings>(initialSettings);
  const [baseRevision, setBaseRevision] = useState(task.revision ?? 0);
  const [directory, setDirectory] = useState<ServerPromptDirectory | null>(null);
  const [directoryLoading, setDirectoryLoading] = useState(true);
  const [directoryError, setDirectoryError] = useState("");
  const [saving, setSaving] = useState(false);
  const [saveError, setSaveError] = useState("");
  const [savedMessage, setSavedMessage] = useState("");
  const [saveConflict, setSaveConflict] = useState(false);
  const [conflictRevision, setConflictRevision] = useState<number | null>(null);
  const [reloading, setReloading] = useState(false);
  const [preview, setPreview] = useState<EffectivePromptPreview | null>(null);
  const [previewKind, setPreviewKind] = useState<PreviewKind | "">("");
  const [previewError, setPreviewError] = useState("");
  const [lastPreviewKind, setLastPreviewKind] = useState<PreviewKind>("outline");
  const hydratedTaskId = useRef(task.id);
  const scopeKey = `${projectApi}\n${taskApi}\n${task.id}`;
  const activeScopeRef = useRef("");
  const dirty = !settingsEqual(settings, baseline);

  useEffect(() => {
    activeScopeRef.current = scopeKey;
    return () => {
      if (activeScopeRef.current === scopeKey) activeScopeRef.current = "";
    };
  }, [scopeKey]);

  const loadDirectory = useCallback(async () => {
    const requestScope = scopeKey;
    setDirectoryLoading(true);
    setDirectoryError("");
    setDirectory(null);
    try {
      const nextDirectory = await apiGet<ServerPromptDirectory>(
        `${projectApi}/prompt-snapshots`,
      );
      if (activeScopeRef.current !== requestScope) return;
      setDirectory(nextDirectory);
    } catch (error) {
      if (activeScopeRef.current !== requestScope) return;
      setDirectoryError(errorMessage(error, "Project Prompt Library 加载失败。"));
    } finally {
      if (activeScopeRef.current === requestScope) {
        setDirectoryLoading(false);
      }
    }
  }, [projectApi, scopeKey]);

  useEffect(() => {
    void loadDirectory();
  }, [loadDirectory]);

  useEffect(() => {
    const taskChanged = hydratedTaskId.current !== task.id;
    if (taskChanged) {
      hydratedTaskId.current = task.id;
      setSettings(initialSettings);
      setBaseline(initialSettings);
      setBaseRevision(task.revision ?? 0);
      setSaveConflict(false);
      setConflictRevision(null);
      setSaveError("");
      setSavedMessage("");
      setPreview(null);
      setPreviewError("");
      return;
    }
    if (dirty) {
      if (task.revision !== baseRevision) {
        if (settingsEqual(initialSettings, baseline)) {
          setBaseRevision(task.revision ?? 0);
        } else {
          setSaveConflict(true);
          setConflictRevision(task.revision ?? 0);
          setSaveError(
            "服务器上的写作要求已变化。当前草稿仍保留；请手动重新载入后再保存。",
          );
        }
      }
      return;
    }
    if (
      task.revision !== baseRevision ||
      !settingsEqual(initialSettings, baseline)
    ) {
      setSettings(initialSettings);
      setBaseline(initialSettings);
      setBaseRevision(task.revision ?? 0);
      setSaveConflict(false);
      setConflictRevision(null);
      setSaveError("");
      setSavedMessage("");
      setPreview(null);
      setPreviewError("");
    }
  }, [baseRevision, baseline, dirty, initialSettings, task.id, task.revision]);

  useEffect(() => {
    onDirtyChange(dirty);
  }, [dirty, onDirtyChange]);

  const outlineSelectionInvalid = selectionInvalid(
    directory,
    settings.outline_prompt_selection,
  );
  const articleSelectionInvalid = selectionInvalid(
    directory,
    settings.article_prompt_selection,
  );
  const invalidSelection = outlineSelectionInvalid || articleSelectionInvalid;
  const generationBlocked =
    directoryLoading ||
    Boolean(directoryError) ||
    invalidSelection ||
    saveConflict;
  const inputInvalid =
    settings.topic_notes.length > 30000 ||
    settings.outline_custom_prompt.length > 40000 ||
    settings.article_custom_prompt.length > 40000;

  useEffect(() => {
    onGenerationBlockedChange(generationBlocked);
  }, [generationBlocked, onGenerationBlockedChange]);

  function updateSetting<Key extends keyof ServerTaskWritingSettings>(
    key: Key,
    value: ServerTaskWritingSettings[Key],
  ) {
    setSettings((current) => ({ ...current, [key]: value }));
    if (!saveConflict) setSaveError("");
    setSavedMessage("");
    setPreview(null);
    setPreviewError("");
  }

  function promptOptions(kind: PreviewKind, selection: string) {
    const active = (directory?.prompts || []).filter(
      (prompt) => prompt.kind === kind && prompt.status === "active",
    );
    const selected = selectedPrompt(directory, selection);
    const defaultPrompt = directory?.defaults[kind];
    return (
      <>
        <option value="project_default">
          项目默认（
          {defaultPrompt
            ? `${defaultPrompt.name} · v${defaultPrompt.version}`
            : "当前为系统默认"}
          ）
        </option>
        <option value="system">系统默认</option>
        {selected && selected.status !== "active" ? (
          <option value={selected.prompt_id} disabled>
            {selected.name} · v{selected.version}（已归档，请重新选择）
          </option>
        ) : null}
        {!selected &&
        selection !== "project_default" &&
        selection !== "system" ? (
          <option value={selection} disabled>
            当前选择不可用（请重新选择）
          </option>
        ) : null}
        {active.map((prompt) => (
          <option key={prompt.prompt_id} value={prompt.prompt_id}>
            {prompt.name} · v{prompt.version}
          </option>
        ))}
      </>
    );
  }

  function selectionWarning(kind: PreviewKind, selection: string) {
    const prompt = selectedPrompt(directory, selection);
    if (prompt?.status === "archived") {
      return `${prompt.name} 已归档。当前值会保留，但保存前必须重新选择。`;
    }
    if (
      directory &&
      selection !== "project_default" &&
      selection !== "system" &&
      !prompt
    ) {
      return "当前 Prompt 已不可用。当前值会保留，但保存前必须重新选择。";
    }
    if (selection === "project_default") {
      return "生成时读取项目最新默认；Job 入队时会固定精确 Prompt 版本。";
    }
    return "Job 入队时会固定当前精确 Prompt 版本。";
  }

  function selectionLabel(kind: PreviewKind, selection: string) {
    if (selection === "system") return "系统默认";
    if (selection === "project_default") {
      const defaultPrompt = directory?.defaults[kind];
      return defaultPrompt
        ? `项目默认：${defaultPrompt.name} · v${defaultPrompt.version}`
        : "项目默认：当前为系统默认";
    }
    const prompt = selectedPrompt(directory, selection);
    if (!prompt) return `不可用 Prompt：${selection}`;
    return `${prompt.name} · v${prompt.version}${
      prompt.status === "archived" ? "（已归档）" : ""
    }`;
  }

  async function save() {
    if (
      !canEdit ||
      !dirty ||
      inputInvalid ||
      invalidSelection ||
      saveConflict
    ) {
      return;
    }
    const requestScope = scopeKey;
    setSaving(true);
    setSaveError("");
    setSavedMessage("");
    try {
      const updated = await apiPut<TaskRecord>(`${taskApi}/writing-settings`, {
        revision: baseRevision,
        ...settings,
      });
      if (activeScopeRef.current !== requestScope) return;
      const normalized = settingsFromTask(updated);
      setSettings(normalized);
      setBaseline(normalized);
      setBaseRevision(updated.revision ?? baseRevision);
      setSavedMessage(`写作要求已保存为 Revision ${updated.revision ?? baseRevision}。`);
      onTaskUpdated(updated);
    } catch (error) {
      if (activeScopeRef.current !== requestScope) return;
      if (error instanceof ApiError && error.status === 409) {
        setSaveConflict(true);
        let latestRevision = currentRevisionFrom(error);
        if (latestRevision === null) {
          try {
            const latest = await apiGet<TaskRecord>(taskApi);
            if (activeScopeRef.current !== requestScope) return;
            latestRevision = latest.revision ?? null;
          } catch {
            // Keep the local draft even if the read-only conflict probe fails.
          }
        }
        setConflictRevision(latestRevision);
        setSaveError(
          "服务器 Task 已被其他成员更新。本地草稿仍保留；请手动重新载入后再保存。",
        );
      } else {
        setSaveError(errorMessage(error, "写作要求保存失败，请重试。"));
      }
    } finally {
      if (activeScopeRef.current === requestScope) setSaving(false);
    }
  }

  async function reloadLatest() {
    if (
      dirty &&
      !window.confirm("重新载入会丢弃当前未保存的写作要求草稿。确定继续吗？")
    ) {
      return;
    }
    const requestScope = scopeKey;
    setReloading(true);
    setSaveError("");
    try {
      const latest = await apiGet<TaskRecord>(taskApi);
      if (activeScopeRef.current !== requestScope) return;
      const nextSettings = settingsFromTask(latest);
      hydratedTaskId.current = latest.id;
      setSettings(nextSettings);
      setBaseline(nextSettings);
      setBaseRevision(latest.revision ?? 0);
      setSaveConflict(false);
      setConflictRevision(null);
      setSavedMessage(`已重新载入 Revision ${latest.revision ?? 0}。`);
      setPreview(null);
      setPreviewError("");
      onTaskUpdated(latest);
    } catch (error) {
      if (activeScopeRef.current !== requestScope) return;
      setSaveError(errorMessage(error, "最新 Task Revision 读取失败。"));
    } finally {
      if (activeScopeRef.current === requestScope) setReloading(false);
    }
  }

  async function loadPreview(kind: PreviewKind) {
    const requestScope = scopeKey;
    setLastPreviewKind(kind);
    setPreviewKind(kind);
    setPreviewError("");
    try {
      const result = await apiPost<EffectivePromptPreview>(
        `${taskApi}/writing-settings/preview`,
        {
          revision: baseRevision,
          kind,
          ...settings,
        },
      );
      if (activeScopeRef.current !== requestScope) return;
      if (
        result.task_id !== task.id ||
        result.task_revision !== baseRevision ||
        result.kind !== kind ||
        result.prompt_snapshot.kind !== kind
      ) {
        throw new Error("Prompt Preview 与当前 Task 或生成环节不匹配。");
      }
      setPreview(result);
    } catch (error) {
      if (activeScopeRef.current !== requestScope) return;
      if (error instanceof ApiError && error.status === 409) {
        setSaveConflict(true);
        let latestRevision = currentRevisionFrom(error);
        if (latestRevision === null) {
          try {
            const latest = await apiGet<TaskRecord>(taskApi);
            if (activeScopeRef.current !== requestScope) return;
            latestRevision = latest.revision ?? null;
          } catch {
            // The stale draft remains usable even if the revision probe fails.
          }
        }
        setConflictRevision(latestRevision);
        setSaveError(
          "服务器 Task 已更新。当前表单仍保留；请手动重新载入后再预览或保存。",
        );
      }
      setPreviewError(errorMessage(error, "Prompt Preview 加载失败。"));
    } finally {
      if (activeScopeRef.current === requestScope) setPreviewKind("");
    }
  }

  function contextToggle(
    key:
      | "include_project_introduction"
      | "include_project_notes"
      | "include_topic_notes",
    label: string,
    description: string,
  ) {
    if (!canEdit) {
      return (
        <div
          className="flex min-h-11 items-start gap-3 rounded-lg border p-3 text-sm"
          role="checkbox"
          aria-checked={settings[key]}
          aria-readonly="true"
          tabIndex={0}
        >
          <span className="mt-0.5 shrink-0 font-medium" aria-hidden="true">
            {settings[key] ? "已启用" : "未启用"}
          </span>
          <span>
            <span className="block font-medium">{label}</span>
            <span className="mt-1 block text-xs leading-5 text-muted-foreground">
              {description}
            </span>
          </span>
        </div>
      );
    }
    return (
      <label className="flex min-h-11 items-start gap-3 rounded-lg border p-3 text-sm">
        <input
          type="checkbox"
          className="mt-1 size-4 shrink-0"
          checked={settings[key]}
          disabled={!canEdit || saving || reloading}
          onChange={(event) => updateSetting(key, event.target.checked)}
        />
        <span>
          <span className="block font-medium">{label}</span>
          <span className="mt-1 block text-xs leading-5 text-muted-foreground">
            {description}
          </span>
        </span>
      </label>
    );
  }

  function promptSelector(kind: PreviewKind) {
    const key =
      kind === "outline"
        ? "outline_prompt_selection"
        : "article_prompt_selection";
    const selection = settings[key];
    const warning = selectionWarning(kind, selection);
    const invalid =
      (kind === "outline" && outlineSelectionInvalid) ||
      (kind === "article" && articleSelectionInvalid) ||
      Boolean(
        directory &&
          selection !== "project_default" &&
          selection !== "system" &&
          !selectedPrompt(directory, selection),
      );
    const warningId = `server-${kind}-prompt-warning`;
    const labelId = `server-${kind}-prompt-label`;
    return (
      <div className="grid min-w-0 gap-2">
        <Label
          id={labelId}
          htmlFor={canEdit ? `server-${kind}-prompt` : undefined}
        >
          {kindLabel(kind)}完整提示词
        </Label>
        {canEdit ? (
          <select
            id={`server-${kind}-prompt`}
            className="min-h-11 w-full rounded-lg border bg-background px-3 text-sm"
            value={selection}
            disabled={saving || reloading || directoryLoading || !directory}
            aria-invalid={invalid}
            aria-describedby={warningId}
            onChange={(event) => updateSetting(key, event.target.value)}
          >
            {promptOptions(kind, selection)}
          </select>
        ) : (
          <div
            id={`server-${kind}-prompt`}
            className="flex min-h-11 items-center rounded-lg border bg-muted/20 px-3 text-sm"
            role="textbox"
            aria-readonly="true"
            aria-invalid={invalid}
            aria-labelledby={labelId}
            aria-describedby={warningId}
            tabIndex={0}
          >
            {selectionLabel(kind, selection)}
          </div>
        )}
        <p
          id={warningId}
          className={
            invalid ? "text-xs text-destructive" : "text-xs text-muted-foreground"
          }
        >
          {warning}
        </p>
      </div>
    );
  }

  return (
    <Card className="min-w-0 xl:col-span-2">
      <CardHeader className="gap-3 border-b">
        <div className="flex flex-wrap items-start justify-between gap-3">
          <div className="min-w-0">
            <CardTitle>本篇写作要求</CardTitle>
            <CardDescription className="mt-1 max-w-3xl leading-6">
              只保存当前 Task 的选择与补充要求。项目 Prompt 继续由不可变 Snapshot 管理，预览不会调用模型或触发生成。
            </CardDescription>
          </div>
          <div className="flex flex-wrap gap-2">
            <Badge variant="outline">基于 Revision {baseRevision}</Badge>
            {dirty ? <Badge variant="secondary">有未保存修改</Badge> : null}
            {!canEdit ? <Badge variant="outline">只读</Badge> : null}
          </div>
        </div>
      </CardHeader>
      <CardContent className="grid gap-5">
        {directoryError ? (
          <Alert variant="destructive">
            <AlertCircle />
            <AlertTitle>Prompt Library 未载入</AlertTitle>
            <AlertDescription>{directoryError}</AlertDescription>
            <div className="col-start-2 mt-2">
              <Button
                type="button"
                variant="outline"
                className="min-h-11"
                disabled={directoryLoading}
                onClick={() => void loadDirectory()}
              >
                {directoryLoading ? <Loader2 className="animate-spin" /> : <RefreshCw />}
                重试
              </Button>
            </div>
          </Alert>
        ) : null}

        {saveError ? (
          <Alert variant="destructive">
            <AlertCircle />
            <AlertTitle>
              {saveConflict ? "Task Revision 已变化" : "写作要求未保存"}
            </AlertTitle>
            <AlertDescription>
              {saveError}
              {conflictRevision !== null
                ? ` 服务器当前为 Revision ${conflictRevision}。`
                : ""}
            </AlertDescription>
            {saveConflict ? (
              <div className="col-start-2 mt-2">
                <Button
                  type="button"
                  variant="outline"
                  className="min-h-11"
                  disabled={reloading || saving}
                  onClick={() => void reloadLatest()}
                >
                  {reloading ? <Loader2 className="animate-spin" /> : <RefreshCw />}
                  手动重新载入
                </Button>
              </div>
            ) : null}
          </Alert>
        ) : null}

        {savedMessage ? (
          <Alert>
            <CheckCircle2 />
            <AlertTitle>写作要求已更新</AlertTitle>
            <AlertDescription>{savedMessage}</AlertDescription>
          </Alert>
        ) : null}

        <div className="grid gap-3 md:grid-cols-2">
          <div className="rounded-lg border bg-muted/20 p-4">
            <p className="font-medium">项目介绍（只读）</p>
            <p className="mt-2 max-h-32 overflow-auto whitespace-pre-wrap text-sm leading-6 text-muted-foreground">
              {task.project_introduction || "当前 Task 没有项目介绍。"}
            </p>
          </div>
          <div className="rounded-lg border bg-muted/20 p-4">
            <p className="font-medium">项目注意事项（只读）</p>
            <p className="mt-2 max-h-32 overflow-auto whitespace-pre-wrap text-sm leading-6 text-muted-foreground">
              {task.project_notes || "当前 Task 没有项目注意事项。"}
            </p>
          </div>
        </div>

        <div className="grid gap-2 rounded-lg border p-4">
          <Label htmlFor={`server-topic-notes-${task.id}`}>本话题专属注意事项</Label>
          <Textarea
            id={`server-topic-notes-${task.id}`}
            value={settings.topic_notes}
            maxLength={30000}
            readOnly={!canEdit}
            disabled={saving || reloading}
            className="min-h-28 resize-y"
            placeholder="仅这篇文章需要遵守的侧重点、客户反馈或禁用表达"
            onChange={(event) => updateSetting("topic_notes", event.target.value)}
          />
        </div>

        <div className="grid gap-3 rounded-lg border p-4">
          <div>
            <p className="font-medium">生成时读取哪些 Task 资料</p>
            <p className="mt-1 text-xs leading-5 text-muted-foreground">
              Published Current Knowledge 仍由服务端控制；这些开关只影响 Task 已保存的项目与话题字段。
            </p>
          </div>
          <div className="grid gap-3 md:grid-cols-3">
            {contextToggle(
              "include_project_introduction",
              "项目介绍",
              "读取此 Task 捕获的项目介绍。",
            )}
            {contextToggle(
              "include_project_notes",
              "项目注意事项",
              "读取此 Task 捕获的项目级要求。",
            )}
            {contextToggle(
              "include_topic_notes",
              "本话题注意事项",
              "读取上方当前文章专属要求。",
            )}
          </div>
        </div>

        <div className="grid gap-4 rounded-lg border p-4 md:grid-cols-2">
          {promptSelector("outline")}
          {promptSelector("article")}
        </div>

        <div className="grid gap-4 md:grid-cols-2">
          <div className="grid gap-3 rounded-lg border p-4">
            {canEdit ? (
              <label className="flex min-h-11 items-center gap-3 font-medium">
                <input
                  type="checkbox"
                  className="size-4"
                  checked={settings.use_outline_custom_prompt}
                  disabled={saving || reloading}
                  onChange={(event) =>
                    updateSetting("use_outline_custom_prompt", event.target.checked)
                  }
                />
                大纲使用单篇补充提示词
              </label>
            ) : (
              <div
                className="flex min-h-11 items-center gap-3 font-medium"
                role="checkbox"
                aria-checked={settings.use_outline_custom_prompt}
                aria-readonly="true"
                tabIndex={0}
              >
                <span aria-hidden="true">
                  {settings.use_outline_custom_prompt ? "已启用" : "未启用"}
                </span>
                大纲使用单篇补充提示词
              </div>
            )}
            <Textarea
              value={settings.outline_custom_prompt}
              maxLength={40000}
              readOnly={!canEdit}
              disabled={saving || reloading}
              className="min-h-36 resize-y font-mono text-sm"
              placeholder="只针对本篇大纲的额外结构、角度或表达要求"
              aria-label="大纲单篇补充提示词"
              onChange={(event) =>
                updateSetting("outline_custom_prompt", event.target.value)
              }
            />
          </div>
          <div className="grid gap-3 rounded-lg border p-4">
            {canEdit ? (
              <label className="flex min-h-11 items-center gap-3 font-medium">
                <input
                  type="checkbox"
                  className="size-4"
                  checked={settings.use_article_custom_prompt}
                  disabled={saving || reloading}
                  onChange={(event) =>
                    updateSetting("use_article_custom_prompt", event.target.checked)
                  }
                />
                正文使用单篇补充提示词
              </label>
            ) : (
              <div
                className="flex min-h-11 items-center gap-3 font-medium"
                role="checkbox"
                aria-checked={settings.use_article_custom_prompt}
                aria-readonly="true"
                tabIndex={0}
              >
                <span aria-hidden="true">
                  {settings.use_article_custom_prompt ? "已启用" : "未启用"}
                </span>
                正文使用单篇补充提示词
              </div>
            )}
            <Textarea
              value={settings.article_custom_prompt}
              maxLength={40000}
              readOnly={!canEdit}
              disabled={saving || reloading}
              className="min-h-36 resize-y font-mono text-sm"
              placeholder="只针对本篇正文的语气、角度或修改要求"
              aria-label="正文单篇补充提示词"
              onChange={(event) =>
                updateSetting("article_custom_prompt", event.target.value)
              }
            />
          </div>
        </div>

        <details className="rounded-lg border p-4">
          <summary className="min-h-11 cursor-pointer py-2 font-medium">
            查看本次实际生效 Prompt
          </summary>
          <div className="mt-3 grid gap-3">
            <p className="text-sm leading-6 text-muted-foreground">
              Preview 使用当前未保存草稿，但不会保存 Task、调用 LLM 或触发 Job。生成时仍以入队 Revision 固定精确 Prompt 与 Published Context。
            </p>
            <div className="grid gap-2 sm:grid-cols-2">
              {(["outline", "article"] as const).map((kind) => (
                <Button
                  key={kind}
                  type="button"
                  variant="outline"
                  className="min-h-11 w-full"
                  disabled={Boolean(previewKind) || saveConflict}
                  onClick={() => void loadPreview(kind)}
                >
                  {previewKind === kind ? <Loader2 className="animate-spin" /> : <Eye />}
                  预览{kindLabel(kind)} Prompt
                </Button>
              ))}
            </div>
            {previewError ? (
              <Alert variant="destructive">
                <AlertCircle />
                <AlertTitle>Prompt Preview 失败</AlertTitle>
                <AlertDescription>{previewError}</AlertDescription>
                <div className="col-start-2 mt-2">
                  <Button
                    type="button"
                    variant="outline"
                    className="min-h-11"
                    disabled={Boolean(previewKind) || saveConflict}
                    onClick={() => void loadPreview(lastPreviewKind)}
                  >
                    <RefreshCw />
                    重试{kindLabel(lastPreviewKind)} Preview
                  </Button>
                </div>
              </Alert>
            ) : null}
            {preview ? (
              <div className="grid gap-3 rounded-lg bg-muted/35 p-3">
                <div className="flex flex-wrap gap-2">
                  <Badge>{kindLabel(preview.kind)}</Badge>
                  <Badge variant="outline">Task Revision {preview.task_revision}</Badge>
                  <Badge variant="outline">
                    {preview.prompt_snapshot.name} · v{preview.prompt_snapshot.version}
                  </Badge>
                  <Badge variant="outline">{preview.prompt_snapshot.source}</Badge>
                  <Badge variant="outline">
                    Context {preview.context_chunk_count} 段
                  </Badge>
                  <Badge variant="outline">目标 {preview.target_words} 词</Badge>
                  {dirty ? <Badge variant="secondary">未保存草稿</Badge> : null}
                </div>
                <p className="break-all font-mono text-[11px] text-muted-foreground">
                  Prompt ID：{preview.prompt_snapshot.prompt_id || "system"} · captured_at：
                  {preview.prompt_snapshot.captured_at || "—"}
                </p>
                {preview.warnings.length ? (
                  <ul className="list-disc space-y-1 pl-5 text-xs leading-5 text-amber-700 dark:text-amber-300">
                    {preview.warnings.map((warning, index) => (
                      <li key={`${index}-${warning}`}>{warning}</li>
                    ))}
                  </ul>
                ) : null}
                <pre
                  className="max-h-[60dvh] overflow-auto whitespace-pre-wrap break-words rounded-lg border bg-background p-3 font-mono text-xs leading-5"
                  tabIndex={0}
                  aria-label="Effective Prompt Preview"
                >
                  {preview.effective_prompt}
                </pre>
              </div>
            ) : null}
          </div>
        </details>

        <div className="flex flex-col gap-3 border-t pt-4 sm:flex-row sm:items-center sm:justify-between">
          <p className="text-xs leading-5 text-muted-foreground">
            {canEdit
              ? "修改只会在显式保存后生效；有未保存修改时，父工作台应阻止生成。"
              : "当前角色可查看和预览，但不能修改或保存写作要求。"}
          </p>
          <Button
            type="button"
            className="min-h-11 w-full sm:w-auto"
            disabled={
              !canEdit ||
              !dirty ||
              saving ||
              reloading ||
              inputInvalid ||
              invalidSelection ||
              saveConflict
            }
            onClick={() => void save()}
          >
            {saving ? <Loader2 className="animate-spin" /> : <Save />}
            保存写作要求
          </Button>
        </div>
      </CardContent>
    </Card>
  );
}
