"use client";

import {
  AlertCircle,
  Download,
  FileText,
  Loader2,
  Paperclip,
  RefreshCw,
  Trash2,
} from "lucide-react";
import { ChangeEvent, useEffect, useRef, useState } from "react";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Progress } from "@/components/ui/progress";
import { ApiError, apiGet, apiPost, apiUploadWithProgress } from "@/lib/api";
import type {
  WorkflowAssistantAttachment,
  WorkflowAssistantAttachmentList,
  WorkflowAssistantAttachmentStatus,
} from "@/types";

const MAX_ATTACHMENT_BYTES = 25 * 1024 * 1024;
const ALLOWED_EXTENSIONS = new Set(["pdf", "docx", "xlsx", "xlsm", "txt", "md"]);
const MIME_BY_EXTENSION: Record<string, string> = {
  pdf: "application/pdf",
  docx: "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
  xlsx: "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
  xlsm: "application/vnd.ms-excel.sheet.macroenabled.12",
  txt: "text/plain",
  md: "text/markdown",
};

const statusLabels: Record<WorkflowAssistantAttachmentStatus, string> = {
  uploading: "写入临时存储中",
  uploaded: "临时上传完成",
  classifying: "分类中",
  needs_user_choice: "需要选择",
  proposal_ready: "导入提案待确认",
  importing: "导入中",
  imported: "已导入",
  rejecting: "正在拒绝并清理",
  rejected: "已拒绝",
  expiring: "到期清理中",
  expired: "已过期",
  failed: "处理失败",
};

type UploadItem = {
  clientId: string;
  file: File;
  idempotencyKey: string;
  progress: number;
  status: "uploading" | "failed";
  error: string;
};

type WorkflowAssistantAttachmentsProps = {
  conversationId: string | null;
  selectedProjectIds: string[];
};

function extension(file: File) {
  return file.name.split(".").pop()?.toLowerCase() || "";
}

function normalizedFile(file: File) {
  const suffix = extension(file);
  const expectedMime = MIME_BY_EXTENSION[suffix];
  if (!expectedMime || file.type.toLowerCase() === expectedMime) return file;
  if (suffix === "md" && file.type.toLowerCase() === "text/plain") return file;
  return new File([file], file.name, {
    type: expectedMime,
    lastModified: file.lastModified,
  });
}

function localId(prefix: string) {
  const random = globalThis.crypto?.randomUUID?.() || `${Date.now()}-${Math.random()}`;
  return `${prefix}-${random}`.replace(/[^A-Za-z0-9._:-]/g, "-");
}

function errorMessage(error: unknown) {
  if (error instanceof ApiError && error.detail && typeof error.detail === "object") {
    const detail = error.detail as { message?: unknown };
    if (typeof detail.message === "string") return detail.message;
  }
  return error instanceof Error ? error.message : "附件上传失败，请重试。";
}

function sizeLabel(bytes: number) {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${Math.ceil(bytes / 1024)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}

function expiryLabel(value: string) {
  const parsed = new Date(value);
  if (Number.isNaN(parsed.valueOf())) return value;
  return parsed.toLocaleString("zh-CN", {
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
  });
}

export function WorkflowAssistantAttachments({
  conversationId,
  selectedProjectIds,
}: WorkflowAssistantAttachmentsProps) {
  const inputRef = useRef<HTMLInputElement>(null);
  const [attachments, setAttachments] = useState<WorkflowAssistantAttachment[]>([]);
  const [uploads, setUploads] = useState<UploadItem[]>([]);
  const [loading, setLoading] = useState(false);
  const [actionPending, setActionPending] = useState("");
  const [error, setError] = useState("");

  useEffect(() => {
    let disposed = false;
    setUploads([]);
    setAttachments([]);
    setError("");
    if (!conversationId) {
      setLoading(false);
      return () => {
        disposed = true;
      };
    }
    setLoading(true);
    void apiGet<WorkflowAssistantAttachmentList>(
        `/api/workflow-assistant/conversations/${encodeURIComponent(conversationId)}/attachments`,
      )
      .then((response) => {
        if (!disposed) setAttachments(response.attachments);
      })
      .catch((reason) => {
        if (!disposed) setError(errorMessage(reason));
      })
      .finally(() => {
        if (!disposed) setLoading(false);
      });
    return () => {
      disposed = true;
    };
  }, [conversationId]);

  async function upload(item: UploadItem) {
    if (!conversationId) return;
    setUploads((current) => current.map((candidate) => (
      candidate.clientId === item.clientId
        ? { ...candidate, status: "uploading", progress: 0, error: "" }
        : candidate
    )));
    const body = new FormData();
    body.append("file", normalizedFile(item.file));
    body.append("idempotency_key", item.idempotencyKey);
    if (selectedProjectIds.length === 1) {
      body.append("proposed_project_id", selectedProjectIds[0]);
    }
    try {
      const created = await apiUploadWithProgress<WorkflowAssistantAttachment>(
        `/api/workflow-assistant/conversations/${encodeURIComponent(conversationId)}/attachments`,
        body,
        (progress) => setUploads((current) => current.map((candidate) => (
          candidate.clientId === item.clientId ? { ...candidate, progress } : candidate
        ))),
      );
      setAttachments((current) => [
        created,
        ...current.filter((candidate) => candidate.attachment_id !== created.attachment_id),
      ]);
      setUploads((current) => current.filter((candidate) => candidate.clientId !== item.clientId));
    } catch (reason) {
      setUploads((current) => current.map((candidate) => (
        candidate.clientId === item.clientId
          ? { ...candidate, status: "failed", error: errorMessage(reason) }
          : candidate
      )));
    }
  }

  function chooseFiles(event: ChangeEvent<HTMLInputElement>) {
    const files = Array.from(event.target.files ?? []);
    event.target.value = "";
    setError("");
    if (!conversationId) {
      setError("请先新建或选择一个会话，再上传临时附件。");
      return;
    }
    const accepted: UploadItem[] = [];
    const rejected: string[] = [];
    for (const file of files) {
      const suffix = extension(file);
      if (!ALLOWED_EXTENSIONS.has(suffix)) {
        rejected.push(`${file.name}（不支持的类型）`);
        continue;
      }
      if (!file.size || file.size > MAX_ATTACHMENT_BYTES) {
        rejected.push(`${file.name}（必须大于 0 且不超过 25 MB）`);
        continue;
      }
      accepted.push({
        clientId: localId("attachment-selection"),
        file,
        idempotencyKey: localId("attachment-upload"),
        progress: 0,
        status: "uploading",
        error: "",
      });
    }
    if (rejected.length) setError(`未加入上传队列：${rejected.join("、")}`);
    if (!accepted.length) return;
    setUploads((current) => [...current, ...accepted]);
    for (const item of accepted) void upload(item);
  }

  async function download(attachment: WorkflowAssistantAttachment) {
    if (!conversationId) return;
    setActionPending(`download:${attachment.attachment_id}`);
    setError("");
    try {
      const response = await apiGet<WorkflowAssistantAttachment>(
        `/api/workflow-assistant/attachments/${encodeURIComponent(attachment.attachment_id)}/download?conversation_id=${encodeURIComponent(conversationId)}`,
      );
      if (!response.download_url) throw new Error("服务器没有返回可用的短期下载地址。");
      window.location.assign(response.download_url);
    } catch (reason) {
      setError(errorMessage(reason));
    } finally {
      setActionPending("");
    }
  }

  async function reject(attachment: WorkflowAssistantAttachment) {
    if (!conversationId) return;
    setActionPending(`reject:${attachment.attachment_id}`);
    setError("");
    try {
      await apiPost<WorkflowAssistantAttachment>(
        `/api/workflow-assistant/attachments/${encodeURIComponent(attachment.attachment_id)}/reject?conversation_id=${encodeURIComponent(conversationId)}`,
      );
      setAttachments((current) => current.filter(
        (candidate) => candidate.attachment_id !== attachment.attachment_id,
      ));
    } catch (reason) {
      setError(errorMessage(reason));
    } finally {
      setActionPending("");
    }
  }

  return (
    <section className="grid gap-2 rounded-xl border border-dashed bg-muted/20 p-3" aria-label="临时附件">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <div>
          <p className="text-sm font-medium">临时附件</p>
          <p className="text-xs leading-5 text-muted-foreground">
            临时上传，未导入、未发布；最多保留 7 天。
          </p>
        </div>
        <input
          ref={inputRef}
          type="file"
          multiple
          className="sr-only"
          accept=".pdf,.docx,.xlsx,.xlsm,.txt,.md"
          onChange={chooseFiles}
        />
        <Button
          type="button"
          size="sm"
          variant="outline"
          disabled={!conversationId}
          onClick={() => inputRef.current?.click()}
        >
          <Paperclip />
          添加附件
        </Button>
      </div>
      <p className="text-xs text-muted-foreground">
        支持 PDF、DOCX、XLSX、XLSM、TXT、MD，单文件最大 25 MB。文件内容不会被执行。
      </p>

      {uploads.map((item) => (
        <div key={item.clientId} className="grid gap-2 rounded-lg border bg-background px-3 py-2">
          <div className="flex items-start justify-between gap-3 text-sm">
            <span className="min-w-0">
              <span className="flex items-center gap-2 font-medium"><FileText className="size-4 shrink-0" /><span className="truncate">{item.file.name}</span></span>
              <span className="text-xs text-muted-foreground">{sizeLabel(item.file.size)}</span>
            </span>
            {item.status === "uploading" ? <Badge variant="outline">上传 {item.progress}%</Badge> : <Badge variant="destructive">上传失败</Badge>}
          </div>
          {item.status === "uploading" ? <Progress value={item.progress} aria-label={`${item.file.name} 上传进度`} /> : (
            <div className="flex items-center justify-between gap-2">
              <span className="flex items-center gap-1 text-xs text-destructive"><AlertCircle className="size-3" />{item.error}</span>
              <Button type="button" size="sm" variant="outline" onClick={() => void upload(item)}><RefreshCw />重试</Button>
            </div>
          )}
        </div>
      ))}

      {loading && <div className="flex items-center gap-2 text-xs text-muted-foreground"><Loader2 className="size-3 animate-spin" />读取附件中…</div>}
      {attachments.map((attachment) => {
        const downloadPending = actionPending === `download:${attachment.attachment_id}`;
        const rejectPending = actionPending === `reject:${attachment.attachment_id}`;
        return (
          <article key={attachment.attachment_id} className="rounded-lg border bg-background px-3 py-2">
            <div className="flex items-start justify-between gap-3">
              <div className="min-w-0">
                <p className="flex items-center gap-2 text-sm font-medium"><FileText className="size-4 shrink-0" /><span className="truncate">{attachment.original_filename}</span></p>
                <p className="mt-1 text-xs text-muted-foreground">
                  {sizeLabel(attachment.byte_size)} · 到期 {expiryLabel(attachment.expires_at)}
                </p>
              </div>
              <Badge variant={attachment.status === "failed" ? "destructive" : "outline"}>
                {statusLabels[attachment.status] || attachment.status}
              </Badge>
            </div>
            <p className="mt-2 text-xs font-medium text-amber-700 dark:text-amber-300">
              当前仅为临时上传，尚未导入业务系统，也未发布为可作证知识。
            </p>
            <div className="mt-2 flex flex-wrap gap-2">
              <Button type="button" size="sm" variant="outline" disabled={Boolean(actionPending)} onClick={() => void download(attachment)}>
                {downloadPending ? <Loader2 className="animate-spin" /> : <Download />}下载
              </Button>
              <Button type="button" size="sm" variant="outline" disabled={Boolean(actionPending)} onClick={() => void reject(attachment)}>
                {rejectPending ? <Loader2 className="animate-spin" /> : <Trash2 />}拒绝并删除临时文件
              </Button>
            </div>
          </article>
        );
      })}
      {conversationId && !loading && !uploads.length && !attachments.length && (
        <p className="text-xs text-muted-foreground">这个会话还没有临时附件。</p>
      )}
      {!conversationId && <p className="text-xs text-muted-foreground">先新建或选择一个会话后即可添加附件。</p>}
      {error && <p className="flex items-start gap-1 text-xs text-destructive"><AlertCircle className="mt-0.5 size-3 shrink-0" />{error}</p>}
    </section>
  );
}
