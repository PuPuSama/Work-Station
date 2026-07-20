# Lab 01：文档变成规范化文本

## 目标

理解知识入库的第一步不是 Embedding，而是先把来源转换成稳定、可重复处理的文本结构。

本实验只处理已经读入内存的 TXT/Markdown 字符串，不处理 DOCX/PDF，也不调用 API。

## TODO 顺序

### TODO 1：`normalize_text()`（当前任务）

输入可能混有 `\r\n`、`\r`、多余行尾空格和过多空行。输出要求：

- 所有换行统一为 `\n`。
- 删除每一行开头和结尾的空格、Tab。
- 连续空行最多保留一个。
- 删除整段文本开头和结尾的空行。
- 非字符串输入抛出 `TypeError`。

示例：

```text
输入："  # Guide  \r\n\r\n\r\n  First line  \rSecond line\t "
输出："# Guide\n\nFirst line\nSecond line"
```

### TODO 2：`extract_title()`（暂时不要做）

优先读取第一个 Markdown H1；没有 H1 时使用来源文件名。

### TODO 3：`parse_text_document()`（暂时不要做）

组合前两个函数，返回统一的 `ParsedDocument`，并保存基础元数据。

## 当前测试命令

```powershell
backend\.venv\Scripts\python.exe -m unittest learning_labs.lab01_document_to_text.test_starter.NormalizeTextTests -v
```

