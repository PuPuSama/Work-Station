文章工具便携版使用说明
========================

1. 将整个文件夹解压到本机磁盘，例如 D:\ArticleAgent。不要直接在 ZIP 压缩包里运行。
2. 将分配给自己的 Excel 话题文件放入 data\topic-library。
3. 如果项目有知识库资料，放入 data\knowledge\官网域名\，例如：
   data\knowledge\www.example.com\产品资料.docx
4. 双击 start.cmd。浏览器会自动打开 http://127.0.0.1:3000。
5. 完成的文章、图片和交付包位于 data\workspace\官网域名\topic_NNN。
6. 不使用时可以双击 stop.cmd。
7. 如果启动失败，将 logs 文件夹发给技术人员。

注意：
- 本软件包已包含模型和 Tavily 密钥，请勿发送给无关人员或上传公共网盘。
- 每个人的任务状态独立保存在 data\state\tasks.json。
- ZeroGPT 检测仍然需要人工操作。
- Word 文件可以使用 Microsoft Word 或 WPS 打开。
