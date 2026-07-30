"use client";

import Image from "next/image";
import { useRef, useState } from "react";
import {
  ClipboardPaste,
  ExternalLink,
  ImagePlus,
  Loader2,
  Plus,
  RefreshCw,
  Save,
  Upload,
  WandSparkles,
  X,
} from "lucide-react";

import { WorkbenchField } from "@/components/article-workbench-ui";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Label } from "@/components/ui/label";
import { ScrollArea } from "@/components/ui/scroll-area";
import { Textarea } from "@/components/ui/textarea";
import { apiFileUrl, apiUpload } from "@/lib/api";
import type { Product } from "@/types";

type EditableProductField = "name" | "url" | "image_path" | "description";

type ArticleProductsStepProps = {
  taskId: string;
  products: Product[];
  productsDirty: boolean;
  busy: boolean;
  hasActiveJob: boolean;
  canUpdate: boolean;
  canRevalidateAssets: boolean;
  onAdd: () => void;
  onRemove: (index: number) => void;
  onUpdate: (index: number, field: EditableProductField, value: string) => void;
  onAutoFetch: () => void;
  onRevalidateAssets: () => void;
  onSave: () => void;
};

type ProductImageUploadResponse = {
  message: string;
  data?: {
    image_path?: string;
    filename?: string;
  };
};

const PRODUCT_IMAGE_TYPES = new Set(["image/jpeg", "image/png", "image/webp"]);
const MAX_PRODUCT_IMAGE_BYTES = 25 * 1024 * 1024;

function ProductImageUploader({
  taskId,
  imagePath,
  productName,
  disabled,
  onUploaded,
}: {
  taskId: string;
  imagePath: string;
  productName: string;
  disabled: boolean;
  onUploaded: (imagePath: string) => void;
}) {
  const inputRef = useRef<HTMLInputElement>(null);
  const [uploading, setUploading] = useState(false);
  const [feedback, setFeedback] = useState("");
  const [uploadError, setUploadError] = useState("");
  const [previewFailed, setPreviewFailed] = useState(false);

  async function uploadFile(file: File) {
    if (!PRODUCT_IMAGE_TYPES.has(file.type)) {
      setUploadError("仅支持 JPEG、PNG 或 WebP 图片。");
      setFeedback("");
      return;
    }
    if (file.size > MAX_PRODUCT_IMAGE_BYTES) {
      setUploadError("图片不能超过 25 MB。");
      setFeedback("");
      return;
    }

    setUploading(true);
    setUploadError("");
    setFeedback("");
    try {
      const body = new FormData();
      body.append("file", file, file.name || "clipboard-image.png");
      const result = await apiUpload<ProductImageUploadResponse>(
        `/api/tasks/${encodeURIComponent(taskId)}/products/image-upload`,
        body,
      );
      const uploadedPath = result.data?.image_path?.trim() || "";
      if (!uploadedPath) {
        throw new Error("服务器没有返回已保存的图片路径。");
      }
      setPreviewFailed(false);
      onUploaded(uploadedPath);
      setFeedback("图片已上传；点击“保存产品”后正式生效。");
    } catch (error) {
      setUploadError(error instanceof Error ? error.message : "图片上传失败。");
    } finally {
      setUploading(false);
      if (inputRef.current) inputRef.current.value = "";
    }
  }

  function handlePaste(event: React.ClipboardEvent<HTMLDivElement>) {
    if (disabled || uploading) return;
    const imageFile = Array.from(event.clipboardData.items)
      .find((item) => item.kind === "file" && item.type.startsWith("image/"))
      ?.getAsFile();
    if (!imageFile) {
      setUploadError("剪贴板中没有可用的图片。");
      setFeedback("");
      return;
    }
    event.preventDefault();
    void uploadFile(imageFile);
  }

  const previewUrl =
    imagePath && !previewFailed
      ? apiFileUrl(
          `/api/tasks/${encodeURIComponent(taskId)}/images/preview?path=${encodeURIComponent(imagePath)}`,
        )
      : "";

  return (
    <div className="grid gap-2 md:col-span-2">
      <Label>产品图片</Label>
      <div
        tabIndex={disabled || uploading ? -1 : 0}
        onPaste={handlePaste}
        aria-label="产品图片上传区，可从本地选择图片或粘贴剪贴板图片"
        className="grid gap-3 rounded-lg border border-dashed bg-muted/20 p-3 outline-none transition-colors focus-visible:border-primary focus-visible:ring-2 focus-visible:ring-primary/20 sm:grid-cols-[160px_minmax(0,1fr)]"
      >
        <div className="relative flex aspect-[4/3] items-center justify-center overflow-hidden rounded-md border bg-background">
          {previewUrl ? (
            <Image
              key={imagePath}
              src={previewUrl}
              alt={productName ? `${productName} 产品图片` : "已上传的产品图片"}
              width={320}
              height={240}
              unoptimized
              className="size-full object-contain"
              onError={() => setPreviewFailed(true)}
            />
          ) : (
            <ImagePlus className="size-8 text-muted-foreground" aria-hidden="true" />
          )}
        </div>
        <div className="flex min-w-0 flex-col justify-center gap-2">
          <div>
            <div className="text-sm font-medium">
              {imagePath ? "图片已保存到服务器" : "上传真实产品图片"}
            </div>
            <p className="mt-1 text-xs leading-5 text-muted-foreground">
              支持 JPEG、PNG、WebP，最大 25 MB。也可以先点击此区域，再按 Ctrl+V
              粘贴剪贴板图片。
            </p>
          </div>
          <div className="flex flex-wrap gap-2">
            <input
              ref={inputRef}
              type="file"
              accept="image/jpeg,image/png,image/webp"
              className="sr-only"
              disabled={disabled || uploading}
              onChange={(event) => {
                const file = event.target.files?.[0];
                if (file) void uploadFile(file);
              }}
            />
            <Button
              type="button"
              size="sm"
              variant="outline"
              disabled={disabled || uploading}
              onClick={() => inputRef.current?.click()}
            >
              {uploading ? <Loader2 className="animate-spin" /> : <Upload />}
              {uploading ? "正在上传" : imagePath ? "更换图片" : "选择本地图片"}
            </Button>
            <span className="inline-flex items-center gap-1.5 px-1 text-xs text-muted-foreground">
              <ClipboardPaste className="size-4" aria-hidden="true" />
              支持直接粘贴
            </span>
            {imagePath && (
              <Button
                type="button"
                size="sm"
                variant="ghost"
                disabled={disabled || uploading}
                onClick={() => {
                  onUploaded("");
                  setPreviewFailed(false);
                  setFeedback("图片引用已移除；保存产品后生效。");
                  setUploadError("");
                }}
              >
                <X />
                移除图片
              </Button>
            )}
          </div>
          {feedback && <p className="text-xs text-emerald-700">{feedback}</p>}
          {uploadError && <p className="text-xs text-destructive">{uploadError}</p>}
        </div>
      </div>
    </div>
  );
}

export function ArticleProductsStep({
  taskId,
  products,
  productsDirty,
  busy,
  hasActiveJob,
  canUpdate,
  canRevalidateAssets,
  onAdd,
  onRemove,
  onUpdate,
  onAutoFetch,
  onRevalidateAssets,
  onSave,
}: ArticleProductsStepProps) {
  return (
    <div className="grid gap-3">
      <div className="flex flex-wrap gap-2">
        <Button variant="outline" onClick={onAdd} disabled={busy}>
          <Plus />
          添加产品
        </Button>
        <Button
          variant="secondary"
          onClick={onAutoFetch}
          disabled={busy || hasActiveJob || !canUpdate}
        >
          {busy ? <Loader2 className="animate-spin" /> : <WandSparkles />}
          自动抓取产品与官网资产
        </Button>
        <Button
          variant="outline"
          onClick={onRevalidateAssets}
          disabled={busy || !canRevalidateAssets || !canUpdate}
        >
          {busy ? <Loader2 className="animate-spin" /> : <RefreshCw />}
          重新核验官网资产并 AI 选图
        </Button>
        <Button
          className="ml-auto"
          onClick={onSave}
          disabled={busy || !productsDirty || !canUpdate}
        >
          <Save />
          保存产品
        </Button>
      </div>
      <div className="text-xs text-muted-foreground">
        Tavily 只负责发现官网详情页；系统会从每个详情页归档对应产品资产，再由视觉模型按资产编号选图。每篇最多使用 3 张不同图片，证据不足的产品会跳过，不要求操作人员猜图。
      </div>
      <ScrollArea className="h-[520px] pr-3">
        <div className="grid gap-3">
          {products.map((product, index) => (
            <div key={index} className="rounded-lg border p-3">
              <div className="mb-3 flex flex-wrap items-center justify-between gap-2">
                <div className="flex flex-wrap items-center gap-2">
                  <div className="font-medium">产品 {index + 1}</div>
                  {product.detail_page_verified && <Badge variant="outline">官网详情页已核验</Badge>}
                  {product.discovery_source === "tavily" && <Badge variant="outline">Tavily 发现</Badge>}
                  {(product.asset_count ?? 0) > 0 && (
                    <Badge variant="secondary">官网资产 {product.asset_count}</Badge>
                  )}
                  {product.selected_asset_id && (
                    <Badge>
                      已选图
                      {product.selection_confidence != null
                        ? ` ${Math.round(product.selection_confidence * 100)}%`
                        : ""}
                    </Badge>
                  )}
                  {product.url && (
                    <a
                      href={product.canonical_url || product.url}
                      target="_blank"
                      rel="noreferrer"
                      className="inline-flex items-center gap-1 text-xs text-primary hover:underline"
                    >
                      <ExternalLink className="size-3.5" />
                      打开官网详情页
                    </a>
                  )}
                </div>
                <Button
                  size="icon-sm"
                  variant="ghost"
                  onClick={() => onRemove(index)}
                  disabled={products.length === 1}
                  aria-label={`删除产品 ${index + 1}`}
                >
                  <X />
                </Button>
              </div>
              <div className="grid gap-3 md:grid-cols-2">
                <WorkbenchField
                  label="产品名"
                  value={product.name}
                  onChange={(value) => onUpdate(index, "name", value)}
                />
                <WorkbenchField
                  label="产品 URL"
                  value={product.url}
                  onChange={(value) => onUpdate(index, "url", value)}
                />
                <ProductImageUploader
                  taskId={taskId}
                  imagePath={product.image_path}
                  productName={product.name}
                  disabled={busy || !canUpdate}
                  onUploaded={(imagePath) => onUpdate(index, "image_path", imagePath)}
                />
                <div className="grid gap-2 md:col-span-2">
                  <Label>描述</Label>
                  <Textarea
                    value={product.description}
                    onChange={(event) => onUpdate(index, "description", event.target.value)}
                    className="h-[86px] resize-none overflow-y-auto"
                  />
                </div>
                {(product.reference_summary || product.selection_reason) && (
                  <div className="grid gap-2 rounded-md bg-muted/45 p-3 text-xs md:col-span-2">
                    {product.reference_summary && (
                      <div>
                        <span className="font-medium">官网资料摘要：</span>
                        <span className="text-muted-foreground">{product.reference_summary}</span>
                      </div>
                    )}
                    {product.selection_reason && (
                      <div>
                        <span className="font-medium">选图依据：</span>
                        <span className="text-muted-foreground">{product.selection_reason}</span>
                      </div>
                    )}
                  </div>
                )}
                {product.asset_error && (
                  <div className="text-xs text-destructive md:col-span-2">
                    官网资产未采用：{product.asset_error}
                  </div>
                )}
              </div>
            </div>
          ))}
        </div>
      </ScrollArea>
    </div>
  );
}
