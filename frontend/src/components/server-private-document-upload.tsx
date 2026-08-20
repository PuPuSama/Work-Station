"use client";

import {
  AlertCircle,
  CheckCircle2,
  FileText,
  Loader2,
  Upload,
  X,
} from "lucide-react";
import { FormEvent, useRef, useState } from "react";

import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";
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
import { apiUpload } from "@/lib/api";
import type { KnowledgeUploadResult } from "@/types";

type TrustTier =
  | "hard_fact"
  | "reference_material"
  | "writing_instruction";

type ServerPrivateDocumentUploadProps = {
  editable: boolean;
  projectPath: string;
  onUploaded: (result: KnowledgeUploadResult) => Promise<void>;
};

const MAX_UPLOAD_BYTES = 100 * 1024 * 1024;

type UploadOutcome = {
  key: string;
  name: string;
  status: "pending" | "uploading" | "success" | "error";
  message?: string;
};

function fileKey(file: File) {
  return `${file.name}:${file.size}:${file.lastModified}`;
}

function errorMessage(error: unknown) {
  return error instanceof Error
    ? error.message
    : "私有资料上传失败，请重试。";
}

function fileSizeLabel(bytes: number) {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${Math.ceil(bytes / 1024)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}

export function ServerPrivateDocumentUpload({
  editable,
  projectPath,
  onUploaded,
}: ServerPrivateDocumentUploadProps) {
  const fileInputRef = useRef<HTMLInputElement>(null);
  const [selectedFiles, setSelectedFiles] = useState<File[]>([]);
  const [displayName, setDisplayName] = useState("");
  const [trustTier, setTrustTier] =
    useState<TrustTier>("reference_material");
  const [uploading, setUploading] = useState(false);
  const [error, setError] = useState("");
  const [outcomes, setOutcomes] = useState<UploadOutcome[]>([]);

  function selectFiles(fileList: FileList | null) {
    setError("");
    setOutcomes([]);
    const files = Array.from(fileList ?? []);
    const oversized = files.filter((file) => file.size > MAX_UPLOAD_BYTES);
    const accepted = files.filter((file) => file.size <= MAX_UPLOAD_BYTES);
    const uniqueFiles = Array.from(
      new Map(accepted.map((file) => [fileKey(file), file])).values(),
    );
    setSelectedFiles(uniqueFiles);
    if (oversized.length) {
      setError(
        `以下文件超过 100 MB，未加入上传队列：${oversized.map((file) => file.name).join("、")}`,
      );
    }
    if (uniqueFiles.length === 1) {
      setDisplayName(uniqueFiles[0].name.replace(/\.[^.]+$/, ""));
    } else {
      setDisplayName("");
    }
  }

  function removeFile(key: string) {
    setSelectedFiles((files) => files.filter((file) => fileKey(file) !== key));
    setOutcomes([]);
  }

  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!selectedFiles.length) {
      setError("请先选择 DOCX、PDF、XLSX 或 XLSM 文件。");
      return;
    }
    setUploading(true);
    setError("");
    const files = [...selectedFiles];
    setOutcomes(
      files.map((file) => ({
        key: fileKey(file),
        name: file.name,
        status: "pending",
      })),
    );
    const successfulResults: KnowledgeUploadResult[] = [];
    const failedKeys = new Set<string>();

    for (const file of files) {
      const key = fileKey(file);
      setOutcomes((items) =>
        items.map((item) =>
          item.key === key ? { ...item, status: "uploading" } : item,
        ),
      );
      try {
        const body = new FormData();
        body.append("file", file);
        body.append(
          "display_name",
          files.length === 1 && displayName.trim()
            ? displayName.trim()
            : file.name.replace(/\.[^.]+$/, ""),
        );
        body.append("trust_tier", trustTier);
        body.append("review_mode", "automatic");
        const result = await apiUpload<KnowledgeUploadResult>(
          `${projectPath}/sources/upload`,
          body,
        );
        successfulResults.push(result);
        setOutcomes((items) =>
          items.map((item) =>
            item.key === key
              ? { ...item, status: "success", message: result.message }
              : item,
          ),
        );
      } catch (reason) {
        failedKeys.add(key);
        setOutcomes((items) =>
          items.map((item) =>
            item.key === key
              ? { ...item, status: "error", message: errorMessage(reason) }
              : item,
          ),
        );
      }
    }

    try {
      const lastResult = successfulResults.at(-1);
      if (lastResult) await onUploaded(lastResult);
      if (failedKeys.size) {
        setError(
          `${files.length - failedKeys.size} 个文件上传成功，${failedKeys.size} 个失败；失败文件已保留，可直接重试。`,
        );
        setSelectedFiles(files.filter((file) => failedKeys.has(fileKey(file))));
      } else {
        setSelectedFiles([]);
        setDisplayName("");
        if (fileInputRef.current) fileInputRef.current.value = "";
      }
    } catch (reason) {
      setError(`文件已上传，但刷新知识库列表失败：${errorMessage(reason)}`);
    }
    setUploading(false);
  }

  const locked = !editable || uploading;

  return (
    <Card>
      <CardHeader>
        <CardTitle className="flex items-center gap-2 text-base">
          <Upload className="size-4" />
          上传私有资料
        </CardTitle>
        <CardDescription className="leading-5">
          原始文件、标准化内容与内嵌图片会保存为项目私有对象。你可以让系统
          解析完成后自动发布；发现异常时再从知识库手动撤下。
        </CardDescription>
      </CardHeader>
      <CardContent>
        <form className="grid gap-4" onSubmit={submit}>
          <div className="grid gap-2">
            <Label htmlFor="server-knowledge-file">资料文件</Label>
            <Input
              ref={fileInputRef}
              id="server-knowledge-file"
              type="file"
              multiple
              accept=".docx,.pdf,.xlsx,.xlsm"
              aria-describedby="server-knowledge-file-help"
              disabled={locked}
              onClick={(event) => {
                event.currentTarget.value = "";
              }}
              onChange={(event) => selectFiles(event.target.files)}
            />
            <p
              id="server-knowledge-file-help"
              className="text-xs leading-5 text-muted-foreground"
            >
              支持一次多选 DOCX、PDF、XLSX、XLSM，单文件最大 100 MB。
            </p>
          </div>

          {selectedFiles.length ? (
            <div className="grid max-h-64 gap-2 overflow-y-auto rounded-xl border bg-muted/20 p-2">
              {selectedFiles.map((file) => (
                <div
                  key={fileKey(file)}
                  className="flex items-center gap-3 rounded-lg bg-background px-3 py-2"
                >
                  <FileText className="size-4 shrink-0 text-muted-foreground" />
                  <div className="min-w-0 flex-1">
                    <div className="truncate text-sm font-medium">{file.name}</div>
                    <div className="text-xs text-muted-foreground">
                      {fileSizeLabel(file.size)}
                    </div>
                  </div>
                  <Button
                    type="button"
                    variant="ghost"
                    size="icon"
                    aria-label={`移除 ${file.name}`}
                    disabled={locked}
                    onClick={() => removeFile(fileKey(file))}
                  >
                    <X />
                  </Button>
                </div>
              ))}
            </div>
          ) : null}

          <div className="grid gap-2">
            <Label htmlFor="server-knowledge-display-name">显示名称</Label>
            <Input
              id="server-knowledge-display-name"
              value={displayName}
              maxLength={255}
              disabled={locked || selectedFiles.length > 1}
              placeholder="例如：2026 产品规格表"
              onChange={(event) => setDisplayName(event.target.value)}
            />
            {selectedFiles.length > 1 ? (
              <p className="text-xs text-muted-foreground">
                批量上传时，每份资料默认使用各自的文件名作为显示名称。
              </p>
            ) : null}
          </div>

          <div className="grid gap-2">
            <Label htmlFor="server-knowledge-trust-tier">
              建议信任层级
            </Label>
            <select
              id="server-knowledge-trust-tier"
              value={trustTier}
              disabled={locked}
              className="min-h-11 rounded-lg border bg-background px-3 text-sm outline-none focus-visible:border-ring focus-visible:ring-[3px] focus-visible:ring-ring/50 disabled:cursor-not-allowed disabled:opacity-50"
              onChange={(event) =>
                setTrustTier(event.target.value as TrustTier)
              }
            >
              <option value="reference_material">参考资料</option>
              <option value="hard_fact">硬事实</option>
              <option value="writing_instruction">写作指令</option>
            </select>
            <p className="text-xs leading-5 text-muted-foreground">
              资料解析完成后自动发布；发现异常时可在知识库中手动撤下。
            </p>
          </div>

          {error ? (
            <Alert variant="destructive" aria-live="polite">
              <AlertCircle />
              <AlertTitle>资料未上传</AlertTitle>
              <AlertDescription>{error}</AlertDescription>
            </Alert>
          ) : null}

          {outcomes.length ? (
            <div className="grid gap-2 rounded-xl border p-3" aria-live="polite">
              {outcomes.map((item) => (
                <div key={item.key} className="flex items-start gap-2 text-sm">
                  {item.status === "success" ? (
                    <CheckCircle2 className="mt-0.5 size-4 shrink-0 text-emerald-600" />
                  ) : item.status === "error" ? (
                    <AlertCircle className="mt-0.5 size-4 shrink-0 text-destructive" />
                  ) : item.status === "uploading" ? (
                    <Loader2 className="mt-0.5 size-4 shrink-0 animate-spin" />
                  ) : (
                    <FileText className="mt-0.5 size-4 shrink-0 text-muted-foreground" />
                  )}
                  <span className="min-w-0">
                    <span className="break-all font-medium">{item.name}</span>
                    {item.message ? (
                      <span className="ml-2 text-xs text-muted-foreground">
                        {item.message}
                      </span>
                    ) : null}
                  </span>
                </div>
              ))}
            </div>
          ) : null}

          <Button
            type="submit"
            className="min-h-11"
            disabled={locked || !selectedFiles.length}
          >
            {uploading ? (
              <Loader2 className="animate-spin" />
            ) : (
              <Upload />
            )}
            {uploading
              ? `正在处理 ${selectedFiles.length} 个文件…`
              : `上传并发布 ${selectedFiles.length || ""} 个文件`}
          </Button>

          {!editable ? (
            <p className="text-xs leading-5 text-muted-foreground">
              当前角色可以查看 Inbox，但没有 knowledge.edit
              权限，不能上传私有资料。
            </p>
          ) : null}
        </form>
      </CardContent>
    </Card>
  );
}
