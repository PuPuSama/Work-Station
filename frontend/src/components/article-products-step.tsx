"use client";

import { ExternalLink, Loader2, Plus, RefreshCw, Save, WandSparkles, X } from "lucide-react";

import { WorkbenchField } from "@/components/article-workbench-ui";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Label } from "@/components/ui/label";
import { ScrollArea } from "@/components/ui/scroll-area";
import { Textarea } from "@/components/ui/textarea";
import type { Product } from "@/types";

type EditableProductField = "name" | "url" | "image_path" | "description";

type ArticleProductsStepProps = {
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

export function ArticleProductsStep({
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
                <WorkbenchField
                  label="图片路径"
                  value={product.image_path}
                  onChange={(value) => onUpdate(index, "image_path", value)}
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
