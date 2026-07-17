"use client";

import { useMemo } from "react";

import { Badge } from "@/components/ui/badge";
import { cn } from "@/lib/utils";

type DiffLine = {
  number: number;
  text: string;
};

type DiffRow = {
  kind: "equal" | "changed" | "deleted" | "added";
  local?: DiffLine;
  server?: DiffLine;
};

type RawOperation = {
  kind: "equal" | "deleted" | "added";
  line: DiffLine;
};

const MAX_LCS_CELLS = 400_000;

function numberedLines(value: string): DiffLine[] {
  return value.replace(/\r\n/g, "\n").split("\n").map((text, index) => ({
    number: index + 1,
    text,
  }));
}

function fallbackRows(local: DiffLine[], server: DiffLine[]): DiffRow[] {
  return Array.from({ length: Math.max(local.length, server.length) }, (_, index) => {
    const left = local[index];
    const right = server[index];
    if (left && right) {
      return {
        kind: left.text === right.text ? "equal" : "changed",
        local: left,
        server: right,
      };
    }
    return left
      ? { kind: "deleted", local: left }
      : { kind: "added", server: right };
  });
}

function buildOperations(local: DiffLine[], server: DiffLine[]): RawOperation[] {
  if (local.length * server.length > MAX_LCS_CELLS) {
    return [];
  }

  const matrix = Array.from(
    { length: local.length + 1 },
    () => new Uint16Array(server.length + 1),
  );
  for (let left = local.length - 1; left >= 0; left -= 1) {
    for (let right = server.length - 1; right >= 0; right -= 1) {
      matrix[left][right] = local[left].text === server[right].text
        ? matrix[left + 1][right + 1] + 1
        : Math.max(matrix[left + 1][right], matrix[left][right + 1]);
    }
  }

  const operations: RawOperation[] = [];
  let left = 0;
  let right = 0;
  while (left < local.length && right < server.length) {
    if (local[left].text === server[right].text) {
      operations.push({ kind: "equal", line: local[left] });
      left += 1;
      right += 1;
    } else if (matrix[left + 1][right] >= matrix[left][right + 1]) {
      operations.push({ kind: "deleted", line: local[left] });
      left += 1;
    } else {
      operations.push({ kind: "added", line: server[right] });
      right += 1;
    }
  }
  while (left < local.length) {
    operations.push({ kind: "deleted", line: local[left] });
    left += 1;
  }
  while (right < server.length) {
    operations.push({ kind: "added", line: server[right] });
    right += 1;
  }
  return operations;
}

function alignOperations(operations: RawOperation[]): DiffRow[] {
  const rows: DiffRow[] = [];
  let deleted: DiffLine[] = [];
  let added: DiffLine[] = [];

  const flush = () => {
    const count = Math.max(deleted.length, added.length);
    for (let index = 0; index < count; index += 1) {
      const local = deleted[index];
      const server = added[index];
      rows.push({
        kind: local && server ? "changed" : local ? "deleted" : "added",
        local,
        server,
      });
    }
    deleted = [];
    added = [];
  };

  for (const operation of operations) {
    if (operation.kind === "equal") {
      flush();
      rows.push({
        kind: "equal",
        local: operation.line,
        server: operation.line,
      });
    } else if (operation.kind === "deleted") {
      deleted.push(operation.line);
    } else {
      added.push(operation.line);
    }
  }
  flush();
  return rows;
}

function diffRows(localValue: string, serverValue: string): DiffRow[] {
  const local = numberedLines(localValue);
  const server = numberedLines(serverValue);
  const operations = buildOperations(local, server);
  return operations.length ? alignOperations(operations) : fallbackRows(local, server);
}

function DiffCell({
  line,
  tone,
}: {
  line?: DiffLine;
  tone: "neutral" | "removed" | "added";
}) {
  return (
    <div
      className={cn(
        "grid min-h-7 grid-cols-[3rem_minmax(0,1fr)] border-b text-xs last:border-b-0",
        tone === "removed" && "bg-red-50 text-red-950",
        tone === "added" && "bg-emerald-50 text-emerald-950",
        tone === "neutral" && "bg-background text-foreground",
        !line && "bg-muted/30",
      )}
    >
      <span className="select-none border-r px-2 py-1 text-right font-mono text-muted-foreground">
        {line?.number ?? ""}
      </span>
      <pre className="min-w-0 whitespace-pre-wrap break-words px-2 py-1 font-mono">
        {line?.text ?? ""}
      </pre>
    </div>
  );
}

export function LineDiffView({
  localValue,
  serverValue,
}: {
  localValue: string;
  serverValue: string;
}) {
  const rows = useMemo(
    () => diffRows(localValue, serverValue),
    [localValue, serverValue],
  );
  const changed = rows.filter((row) => row.kind !== "equal").length;

  return (
    <div className="grid gap-2">
      <div className="flex flex-wrap items-center gap-2 text-xs text-muted-foreground">
        <Badge variant="outline">{changed} 行有差异</Badge>
        <span><span className="mr-1 inline-block size-2 rounded-full bg-red-300" />本地删除或修改</span>
        <span><span className="mr-1 inline-block size-2 rounded-full bg-emerald-300" />服务器新增或修改</span>
      </div>
      <div className="overflow-hidden rounded-lg border">
        <div className="grid grid-cols-2 border-b bg-muted/60 text-xs font-medium">
          <div className="border-r px-3 py-2">本地未保存内容</div>
          <div className="px-3 py-2">服务器最新内容</div>
        </div>
        <div className="max-h-[480px] overflow-auto">
          {rows.map((row, index) => (
            <div className="grid min-w-[760px] grid-cols-2" key={`${index}-${row.kind}`}>
              <div className="border-r">
                <DiffCell
                  line={row.local}
                  tone={row.kind === "changed" || row.kind === "deleted" ? "removed" : "neutral"}
                />
              </div>
              <DiffCell
                line={row.server}
                tone={row.kind === "changed" || row.kind === "added" ? "added" : "neutral"}
              />
            </div>
          ))}
        </div>
      </div>
    </div>
  );
}
