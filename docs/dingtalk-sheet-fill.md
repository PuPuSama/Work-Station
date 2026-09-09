# 钉钉自动填表 Skill 使用说明

这个 Skill 将 Article Agent 成品交付包中的 `metadata.json` 转成填表预览，再由 Codex 操作你已登录的钉钉/Alidocs 在线表格，核对并写入文章完成信息。

**Article Agent 负责生成和交付，Skill 负责交付后的登记。** 它不是网站里的自动回调，也不需要安装到 `myserver` 才能使用。服务部署在远端时，在本机下载交付 ZIP、解压，再让本机 Codex 填表即可；如果 Codex 在远端运行，JSON 文件和可控制的浏览器也必须在该执行环境可用。

## 1. 安装

仓库路径：[skills/dingtalk-sheet-fill](../skills/dingtalk-sheet-fill/)。需要 Python 3.10+（项目使用 3.12）、Codex 可用的浏览器控制工具，以及目标表格的编辑权限。转换脚本只用 Python 标准库，无需 LLM、Embedding、MinerU 或钉钉 API 密钥，也不会调用模型接口。

推荐直接向 Codex 发送：

```text
请用 skill-installer 安装 PuPuSama/Work-Station 仓库 main 分支中的
skills/dingtalk-sheet-fill。
如果已安装同名 Skill，保留现有 workbook.json 和 project_sheet_map.json，
先比较版本，不要覆盖我的表格配置。
```

安装位置默认是 `~/.codex/skills/dingtalk-sheet-fill`；设置过 `CODEX_HOME` 时在其 `skills/` 子目录。安装后在下一轮对话调用；未显示时重新打开会话确认。私有仓库需要已有 GitHub 访问权限，不要把访问令牌贴进使用说明或聊天提示词。

已克隆仓库时，也可在 **Windows PowerShell、仓库根目录**手动复制：

```powershell
$skillRoot = if ($env:CODEX_HOME) { $env:CODEX_HOME } else { Join-Path $env:USERPROFILE '.codex' }
$skillDestination = Join-Path $skillRoot 'skills/dingtalk-sheet-fill'
if (Test-Path -LiteralPath $skillDestination) { throw '已安装同名 Skill，请先比较版本并保留个人配置。' }
New-Item -ItemType Directory -Path (Split-Path $skillDestination -Parent) -Force | Out-Null
Copy-Item -LiteralPath './skills/dingtalk-sheet-fill' -Destination $skillDestination -Recurse
```

更新已安装版本时，只替换 `SKILL.md`、`agents/`、`scripts/` 和示例/映射说明；保留自己的两份 JSON 配置。仓库更新不会自动更新本机安装目录。本次仓库收录也不会改变已有个人安装。

## 2. 首次配置表格与项目

在**安装后的 Skill 目录**的 `references/` 中：

1. 复制 `workbook.example.json` 为 `workbook.json`，填写实际 `workbook_url` 和浏览器可见的完整 `expected_title`。普通在线表格保留 `workbook_kind: "spreadsheetv2"`；`sheet_map` 保留 `project_sheet_map.json`。
2. 复制 `project_sheet_map.example.json` 为 `project_sheet_map.json`，删除示例项目，填写自己的映射。
3. 在 Codex 可控制的浏览器中打开表格并登录，确认有编辑权限。

项目映射示例：

```json
{
  "example.com": { "sheet_name": "示例项目" },
  "another-example.com": { "sheet_name": "另一个示例项目" }
}
```

键必须逐字对应交付 `metadata.json` 的 `project_id`，值必须对应表格底部实际 Sheet 名称，包括空格。不要把显示名称、域名猜测或选题序号当作项目 ID。多个项目可对应同一工作簿中的不同 Sheet；**当前配置只支持一个工作簿**，切换工作簿需同步核对 URL、标题和项目映射。

真实工作簿链接和项目映射不随仓库分发。Skill 自带 `.gitignore` 忽略两份真实配置和临时输出；不要强制提交它们。配置中无需存钉钉密码、Cookie 或 MCP 网关密钥。

## 3. 搭配 Article Agent 完成登记

1. 在 Article Agent 完成文章复检、必要的 AI 率确认及交付。进入项目的**交付记录页**，点击“打包”（尚未生成时），再点 **ZIP** 下载。
2. 解压交付 ZIP，找到 `metadata.json`。单篇包中它与 Word、D.docx 等交付物同包；批量包应分别使用每篇文章目录里的 JSON，不混用不同文章的统计。
3. 正文或检测结果更新后，使用重新生成的交付包，不用旧下载文件代替当前结果。脚本不会访问服务器检查版本。
4. 将本地 JSON 路径和目标行规则告诉 Codex。Skill 会检查字段、匹配项目 Sheet、读取当前单元格，然后给出修改预览。
5. 预览包含正确项目、Sheet、行号与字段后授权写入。完成报告应列出实际回读核对的单元格；仅产生 JSON 不等于已填表。

单篇提示词：

```text
使用 $dingtalk-sheet-fill，读取 D:/Downloads/article-001/metadata.json，
按 project_id 找到配置的 Sheet，按标题精确匹配已有行。
先显示目标行、各单元格原值和拟写入值，我确认后再填写并回读核对。
```

新增登记提示词：

```text
使用 $dingtalk-sheet-fill，读取 D:/Downloads/article-001/metadata.json。
在对应 Sheet 先检查有无同标题记录；没有时，选最后一条记录之后的
第一条真正空白行，先预览，不插入或删除结构行。
```

批量提示词：

```text
使用 $dingtalk-sheet-fill，处理 D:/Downloads/article-batch 中每篇文章的 metadata.json。
逐篇按 project_id 映射 Sheet，按标题匹配已有行；没有或重名的行先单独列出。
先汇总可写入的单元格及原值供我确认。已填内容相同就跳过，
只对已授权且验证通过的行执行，最后分别报告成功、失败与待处理文章。
```

批量操作也可走浏览器。仅在需要 API 模式时评估兼容的钉钉 MCP：它必须能访问同一在线工作簿、列出 Sheet 并读写范围。只有 Base/table/record 能力的“AI 表格 MCP”不等于支持 `spreadsheetv2`/`axls`，安装了也不能直接假定可用。不要把私人网关链接当成通用安装地址。

## 4. 写入字段与限制

完整规则见 [字段映射](../skills/dingtalk-sheet-fill/references/mapping.md)。七项为：撰写日期、关键词、关键词密度、标题、AI%、锚文本、知识库引用率。

- 当前模板的“关键词密度”填**出现次数**，不是密度百分比；如需改指标，先明确表格约定再同步修改映射和测试。
- AI 率必须已确认；知识库引用率必须为 `available`，`stale` 也不能填。不能将未检测视为 0%，也不能手改确认字段来通过脚本。
- 锚文本只填文字，多个锚文本换行放在同一格。知识库引用率按表头定位，不能固定写 K 列。
- 未确认 AI、无锚文本、资料不足导致引用率不可用等情况会停止该篇；回项目处理并重新导出后重试。不会因一篇失败就自动重写已完成行。
- 只修改预览中的七项，不会填写作者、网址、上传日期等其他字段，也不会发布文章到 WordPress。无浏览器控制能力时只能生成预览。

## 5. 不连接真实表格的自检

在仓库根目录运行（也可用自己的 Python 3.10+ 替换解释器）：

```powershell
backend/.venv/Scripts/python.exe -X utf8 skills/dingtalk-sheet-fill/scripts/metadata_to_patch.py skills/dingtalk-sheet-fill/references/metadata.example.json
backend/.venv/Scripts/python.exe -m unittest discover -s skills/dingtalk-sheet-fill/tests -q
```

示例是合成数据，预期得到日期 `9.9`、关键词次数 `4`、AI 值 `20`、引用率 `16`，以及含两行文本的单个锚文本值。输出中的列字母仅为模板提示，真实地址在浏览器核对表头后确定。脚本退出码 `0` 表示预览生成成功；`2` 表示数据/路径校验失败，未进行任何填表操作。

这些检查验证数据转换，不验证真实钉钉登录、表头、权限或保存。首次实际使用仍需对一条经授权的记录完成浏览器写入和回读。
