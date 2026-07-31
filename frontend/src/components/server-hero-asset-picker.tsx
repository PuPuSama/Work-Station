"use client";

import { Check, ImageOff, Loader2, RefreshCw } from "lucide-react";
import { useEffect, useState } from "react";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { apiGet } from "@/lib/api";
import { cn } from "@/lib/utils";
import type {
  ProjectAssetDownload,
  ServerCatalogImageAsset,
} from "@/types";

type PreviewState = Record<string, string>;
type PreviewRequestState = {
  key: string;
  urls: PreviewState;
};

function dimensions(asset: ServerCatalogImageAsset) {
  return asset.width && asset.height
    ? `${asset.width} × ${asset.height}`
    : "尺寸未知";
}

function byteSize(value: number) {
  if (value < 1024) return `${value} B`;
  if (value < 1024 * 1024) return `${Math.round(value / 1024)} KB`;
  return `${(value / (1024 * 1024)).toFixed(1)} MB`;
}

export function ServerHeroAssetPicker({
  assets,
  disabled,
  onSelect,
  projectApi,
  selectedAssetId,
}: {
  assets: ServerCatalogImageAsset[];
  disabled: boolean;
  onSelect: (assetId: string) => void;
  projectApi: string;
  selectedAssetId: string;
}) {
  const [previewState, setPreviewState] = useState<PreviewRequestState>({
    key: "",
    urls: {},
  });
  const [previewRevision, setPreviewRevision] = useState(0);
  const previewKey = `${previewRevision}:${assets
    .map((asset) => asset.asset_id)
    .join("|")}`;
  const loading = Boolean(assets.length && previewState.key !== previewKey);
  const previews =
    previewState.key === previewKey ? previewState.urls : {};

  useEffect(() => {
    let active = true;
    if (!assets.length) return () => undefined;
    void Promise.allSettled(
      assets.map(async (asset) => {
        const result = await apiGet<ProjectAssetDownload>(
          `${projectApi}/assets/${encodeURIComponent(asset.asset_id)}/download?expires_seconds=300`,
        );
        return [asset.asset_id, result.url] as const;
      }),
    ).then((results) => {
      if (!active) return;
      setPreviewState({
        key: previewKey,
        urls: Object.fromEntries(
          results.flatMap((result) =>
            result.status === "fulfilled" && result.value[1]
              ? [result.value]
              : [],
          ),
        ),
      });
    });
    return () => {
      active = false;
    };
  }, [assets, previewKey, projectApi]);

  if (!assets.length) {
    return (
      <div className="rounded-xl border border-dashed p-5 text-sm leading-6 text-muted-foreground">
        当前已发布快照没有可用图片。先完成资料抓取、人工审阅与发布，再返回选择
        Hero；未发布或旧快照图片不会出现在这里。
      </div>
    );
  }

  return (
    <div className="grid gap-3">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <p className="text-sm text-muted-foreground">
          仅显示当前已发布快照中的图片，共 {assets.length} 张。
        </p>
        <Button
          type="button"
          variant="outline"
          size="sm"
          className="min-h-11"
          disabled={loading}
          onClick={() => setPreviewRevision((value) => value + 1)}
        >
          {loading ? (
            <Loader2 className="animate-spin" />
          ) : (
            <RefreshCw />
          )}
          刷新短时预览
        </Button>
      </div>
      <div className="grid gap-3 sm:grid-cols-2">
        {assets.map((asset) => {
          const selected = selectedAssetId === asset.asset_id;
          const preview = previews[asset.asset_id];
          return (
            <button
              key={asset.asset_id}
              type="button"
              aria-pressed={selected}
              disabled={disabled}
              className={cn(
                "group overflow-hidden rounded-xl border bg-card text-left transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 disabled:cursor-not-allowed disabled:opacity-60",
                selected
                  ? "border-primary ring-1 ring-primary"
                  : "hover:border-foreground/30 hover:bg-accent/30",
              )}
              onClick={() => onSelect(asset.asset_id)}
            >
              <div className="relative flex aspect-[16/10] items-center justify-center overflow-hidden bg-muted">
                {preview ? (
                  // The URL is a short-lived, authorized object-store URL and
                  // intentionally stays in component memory only.
                  // eslint-disable-next-line @next/next/no-img-element
                  <img
                    src={preview}
                    alt=""
                    referrerPolicy="no-referrer"
                    className="size-full object-contain transition-transform duration-200 group-hover:scale-[1.02]"
                  />
                ) : (
                  <ImageOff
                    className={cn(
                      "size-8 text-muted-foreground",
                      loading && "animate-pulse",
                    )}
                    aria-hidden="true"
                  />
                )}
                {selected && (
                  <span className="absolute right-2 top-2 grid size-8 place-items-center rounded-full bg-primary text-primary-foreground shadow-sm">
                    <Check className="size-4" />
                    <span className="sr-only">已选择</span>
                  </span>
                )}
              </div>
              <span className="grid gap-2 p-3">
                <span className="line-clamp-2 text-sm font-medium leading-5">
                  {asset.label}
                </span>
                <span className="flex flex-wrap gap-1.5">
                  <Badge variant="secondary">{dimensions(asset)}</Badge>
                  <Badge variant="outline">{byteSize(asset.byte_size)}</Badge>
                  <Badge variant="outline">{asset.evidence_kind}</Badge>
                </span>
                <span className="truncate font-mono text-[11px] text-muted-foreground">
                  {asset.asset_id}
                </span>
              </span>
            </button>
          );
        })}
      </div>
    </div>
  );
}
