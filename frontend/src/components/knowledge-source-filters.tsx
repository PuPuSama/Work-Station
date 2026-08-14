"use client";

import { ArrowDownUp, Search } from "lucide-react";

import { Input } from "@/components/ui/input";
import type { KnowledgeSourceSort } from "@/lib/knowledge-source-filters";

type KnowledgeSourceFiltersProps = {
  query: string;
  sort: KnowledgeSourceSort;
  includeLocal: boolean;
  includeWebsite: boolean;
  localCount: number;
  websiteCount: number;
  totalCount: number;
  visibleCount: number;
  onQueryChange: (value: string) => void;
  onSortChange: (value: KnowledgeSourceSort) => void;
  onIncludeLocalChange: (value: boolean) => void;
  onIncludeWebsiteChange: (value: boolean) => void;
};

export function KnowledgeSourceFilters({
  query,
  sort,
  includeLocal,
  includeWebsite,
  localCount,
  websiteCount,
  totalCount,
  visibleCount,
  onQueryChange,
  onSortChange,
  onIncludeLocalChange,
  onIncludeWebsiteChange,
}: KnowledgeSourceFiltersProps) {
  return (
    <div className="grid gap-3 rounded-xl border bg-muted/10 p-3 md:grid-cols-[minmax(0,1fr)_190px]">
      <div className="relative">
        <Search
          className="pointer-events-none absolute left-3 top-1/2 size-4 -translate-y-1/2 text-muted-foreground"
          aria-hidden="true"
        />
        <Input
          value={query}
          className="pl-9"
          placeholder="搜索来源名称、编号或网址"
          aria-label="搜索知识库来源"
          onChange={(event) => onQueryChange(event.target.value)}
        />
      </div>
      <label className="grid items-center gap-1.5 text-sm">
        <span className="flex items-center gap-1.5 text-xs font-medium text-muted-foreground">
          <ArrowDownUp className="size-3.5" />
          时间排序
        </span>
        <select
          value={sort}
          className="min-h-10 rounded-md border bg-background px-3 text-sm"
          aria-label="来源时间排序"
          onChange={(event) =>
            onSortChange(event.target.value as KnowledgeSourceSort)
          }
        >
          <option value="newest">最新入库优先</option>
          <option value="oldest">最早入库优先</option>
        </select>
      </label>
      <fieldset className="flex flex-wrap items-center gap-x-5 gap-y-2 text-sm md:col-span-2">
        <legend className="mr-1 text-xs font-medium text-muted-foreground">
          来源类型
        </legend>
        <label className="inline-flex cursor-pointer items-center gap-2">
          <input
            type="checkbox"
            className="size-4 accent-primary"
            checked={includeLocal}
            onChange={(event) => onIncludeLocalChange(event.target.checked)}
          />
          本地上传
          <span className="text-xs text-muted-foreground">{localCount}</span>
        </label>
        <label className="inline-flex cursor-pointer items-center gap-2">
          <input
            type="checkbox"
            className="size-4 accent-primary"
            checked={includeWebsite}
            onChange={(event) => onIncludeWebsiteChange(event.target.checked)}
          />
          网站抓取
          <span className="text-xs text-muted-foreground">{websiteCount}</span>
        </label>
        <span className="ml-auto text-xs text-muted-foreground">
          显示 {visibleCount} / {totalCount}
        </span>
      </fieldset>
    </div>
  );
}
