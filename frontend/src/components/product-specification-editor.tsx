"use client";

import { Columns3, Plus, Rows3, Trash2 } from "lucide-react";

import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import type { KnowledgeProductSummary } from "@/types";

export type SpecificationTable = KnowledgeProductSummary["specification_tables"][number];

type SpecificationEditorProps = {
  tables: SpecificationTable[];
  disabled?: boolean;
  onChange: (tables: SpecificationTable[]) => void;
};

function columnCount(table: SpecificationTable) {
  return Math.max(
    2,
    table.headers?.length || 0,
    ...(table.rows || []).map((row) => row.length),
  );
}

function rectangularTable(table: SpecificationTable): SpecificationTable {
  const columns = columnCount(table);
  return {
    caption: table.caption || "",
    headers: Array.from({ length: columns }, (_, index) => table.headers?.[index] || ""),
    rows: (table.rows || []).map((row) =>
      Array.from({ length: columns }, (_, index) => row[index] || ""),
    ),
  };
}

export function editableSpecificationTables(
  tables: SpecificationTable[],
): SpecificationTable[] {
  return tables.map(rectangularTable);
}

export function ProductSpecificationEditor({
  tables,
  disabled = false,
  onChange,
}: SpecificationEditorProps) {
  const updateTable = (tableIndex: number, table: SpecificationTable) => {
    onChange(tables.map((current, index) => (index === tableIndex ? table : current)));
  };

  const addTable = () => {
    onChange([
      ...tables,
      {
        caption: "",
        headers: ["参数", "数值"],
        rows: [["", ""]],
      },
    ]);
  };

  if (!tables.length) {
    return (
      <div className="grid justify-items-center gap-3 rounded-xl border border-dashed px-4 py-8 text-center">
        <p className="text-sm text-muted-foreground">当前没有规格表，可以手动新建。</p>
        <Button type="button" variant="outline" onClick={addTable} disabled={disabled}>
          <Plus /> 新建规格表
        </Button>
      </div>
    );
  }

  return (
    <div className="grid gap-4">
      {tables.map((rawTable, tableIndex) => {
        const table = rectangularTable(rawTable);
        const columns = columnCount(table);
        return (
          <section key={tableIndex} className="grid gap-3 rounded-xl border p-3">
            <div className="flex flex-wrap items-center gap-2">
              <Input
                aria-label={`规格表 ${tableIndex + 1} 标题`}
                value={table.caption || ""}
                placeholder={`规格表 ${tableIndex + 1} 标题（可选）`}
                disabled={disabled}
                className="min-w-64 flex-1"
                onChange={(event) =>
                  updateTable(tableIndex, { ...table, caption: event.target.value })
                }
              />
              <Button
                type="button"
                size="sm"
                variant="outline"
                disabled={disabled || columns >= 40}
                onClick={() =>
                  updateTable(tableIndex, {
                    ...table,
                    headers: [...(table.headers || []), ""],
                    rows: (table.rows || []).map((row) => [...row, ""]),
                  })
                }
              >
                <Columns3 /> 加一列
              </Button>
              <Button
                type="button"
                size="sm"
                variant="outline"
                disabled={disabled || (table.rows?.length || 0) >= 500}
                onClick={() =>
                  updateTable(tableIndex, {
                    ...table,
                    rows: [...(table.rows || []), Array.from({ length: columns }, () => "")],
                  })
                }
              >
                <Rows3 /> 加一行
              </Button>
              <Button
                type="button"
                size="icon-sm"
                variant="ghost"
                aria-label={`删除规格表 ${tableIndex + 1}`}
                disabled={disabled}
                onClick={() => onChange(tables.filter((_, index) => index !== tableIndex))}
              >
                <Trash2 />
              </Button>
            </div>

            <div className="overflow-auto rounded-lg border">
              <table className="w-full min-w-max border-collapse text-sm">
                <thead className="bg-muted/50">
                  <tr>
                    {(table.headers || []).map((header, cellIndex) => (
                      <th key={cellIndex} className="min-w-44 border-b border-r p-2 last:border-r-0">
                        <Input
                          aria-label={`规格表 ${tableIndex + 1} 第 ${cellIndex + 1} 列标题`}
                          value={header}
                          placeholder={`第 ${cellIndex + 1} 列`}
                          disabled={disabled}
                          onChange={(event) =>
                            updateTable(tableIndex, {
                              ...table,
                              headers: (table.headers || []).map((value, index) =>
                                index === cellIndex ? event.target.value : value,
                              ),
                            })
                          }
                        />
                      </th>
                    ))}
                    <th className="w-12 border-b p-2">
                      <span className="sr-only">操作</span>
                    </th>
                  </tr>
                </thead>
                <tbody>
                  {(table.rows || []).map((row, rowIndex) => (
                    <tr key={rowIndex} className="border-b last:border-b-0">
                      {row.map((cell, cellIndex) => (
                        <td key={cellIndex} className="min-w-44 border-r p-2 last:border-r-0">
                          <Input
                            aria-label={`规格表 ${tableIndex + 1} 第 ${rowIndex + 1} 行第 ${cellIndex + 1} 列`}
                            value={cell}
                            disabled={disabled}
                            onChange={(event) =>
                              updateTable(tableIndex, {
                                ...table,
                                rows: (table.rows || []).map((currentRow, currentRowIndex) =>
                                  currentRowIndex === rowIndex
                                    ? currentRow.map((value, currentCellIndex) =>
                                        currentCellIndex === cellIndex
                                          ? event.target.value
                                          : value,
                                      )
                                    : currentRow,
                                ),
                              })
                            }
                          />
                        </td>
                      ))}
                      <td className="w-12 p-2 text-center">
                        <Button
                          type="button"
                          size="icon-sm"
                          variant="ghost"
                          aria-label={`删除规格表 ${tableIndex + 1} 第 ${rowIndex + 1} 行`}
                          disabled={disabled}
                          onClick={() =>
                            updateTable(tableIndex, {
                              ...table,
                              rows: (table.rows || []).filter((_, index) => index !== rowIndex),
                            })
                          }
                        >
                          <Trash2 />
                        </Button>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>

            <div className="flex flex-wrap items-center justify-between gap-2">
              <p className="text-xs text-muted-foreground">
                {table.rows?.length || 0} 行 · {columns} 列
              </p>
              <div className="flex gap-2">
                <Button
                  type="button"
                  size="sm"
                  variant="ghost"
                  disabled={disabled || columns <= 1}
                  onClick={() =>
                    updateTable(tableIndex, {
                      ...table,
                      headers: (table.headers || []).slice(0, -1),
                      rows: (table.rows || []).map((row) => row.slice(0, -1)),
                    })
                  }
                >
                  删除末列
                </Button>
                <Button
                  type="button"
                  size="sm"
                  variant="outline"
                  disabled={disabled || (table.rows?.length || 0) >= 500}
                  onClick={() =>
                    updateTable(tableIndex, {
                      ...table,
                      rows: [...(table.rows || []), Array.from({ length: columns }, () => "")],
                    })
                  }
                >
                  <Plus /> 添加参数行
                </Button>
              </div>
            </div>
          </section>
        );
      })}
      <Button type="button" variant="outline" onClick={addTable} disabled={disabled || tables.length >= 20}>
        <Plus /> 添加另一张规格表
      </Button>
    </div>
  );
}
