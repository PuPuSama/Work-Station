# WordPress 草稿上传

当前实现：每个项目连接一个站点，使用该项目自己的账号将单篇成品上传为标准 WordPress `post` 草稿。用户在 WordPress 后台检查后发布；上传成功不等于文章已公开。

## 配置与操作

1. 在 Article Agent 服务器设置稳定的 `ARTICLE_AGENT_WORDPRESS_CREDENTIALS_KEY`，至少 32 字符；保持私密并备份。
2. 为目标站点准备可编辑文章、上传媒体的账号及 **Application Password**。网页登录密码与 Application Password 用途不同。
3. 在项目设置中填写站点根地址，例如 `https://blog.example.com`，以及用户名、Application Password，保存后测试连接。不要填写 `/wp-admin` 或 `/wp-json/wp/v2`。
4. 在交付记录页选择已有成品的文章，点击“上传 WordPress 草稿”；正文和图片必须满足当前准备条件。
5. 成功后打开返回的后台编辑链接。预览草稿通常需要在同一浏览器登录 WordPress，未登录时可能显示 404。

生产连接要求 HTTPS 与可达的 REST API；证书校验、站点地址限制和项目权限检查仍保留。只允许本地隔离调试对 loopback WordPress 使用 HTTP。

项目地址和凭据分开保存；Application Password 只以密文存入专用凭据表，读取接口不会返回密码或密文。密码框留空时，仅在账号未变化的情况下保留旧密码；改用户名要同时填写新密码。凭据有未保存修改时应先保存再测试。

服务端可按项目使用 `ARTICLE_AGENT_WORDPRESS_PROJECT_CREDENTIALS` 的环境变量引用映射；项目已保存凭据优先。全局地址和账号只用于兼容回退，见[配置指南](configuration.md)。不要把测试站账号用于正式业务站。

## 上传内容与恢复

| 内容 | 当前行为 |
| --- | --- |
| 标题、正文 | 转换为受限 HTML，支持标题、段落、列表、表格、链接、加粗与图片 |
| 图片 | 上传准备好的文件，正文使用 WordPress 持久媒体 URL，插入位置复用 Word 导出规则 |
| 首图 | 设置 `featured_media`，正文仍保留原插图布局 |
| TDK | 写入 `article_agent_seo_*` 自定义 meta；不等于已经适配 Yoast/Rank Math |
| 重复上传 | 通过站点、文章身份和内容哈希判断已有结果；不自动覆盖不同内容的同名来源 |
| 失败恢复 | 保存媒体检查点和状态，允许重试；不会因为上传而重新调用模型写文章 |

当前上传是**同步请求**，没有后台上传 Job、逐阶段排队进度或批量 WordPress 上传功能。已实现的防重复机制不能被描述成所有超时场景都不会留下孤立媒体。

当前转换器保留正文原有 H1；部分主题又显示文章标题和特色图片，因此可能重复显示标题或首图。主题布局与 SEO 插件适配需要针对站点验收，不能承诺任意主题都与 Word 排版一致。

TDK/source meta 需要目标 WordPress 注册对应可写字段；测试站通过 [article-agent-meta.php](../deploy/wordpress-test/article-agent-meta.php) 注册。正式业务站应由站点维护者提供相同字段或明确的 SEO 插件适配。标准分类、标签虽然存在于 WordPress API，但当前项目界面没有分类/标签映射配置。

## 两个测试环境

| 环境 | 入口和启动方式 |
| --- | --- |
| 本机模板站 | 仓库根目录运行 `backend/.venv/Scripts/python.exe scripts/setup_wordpress_test.py`，入口 `http://127.0.0.1:8088` |
| 服务器独立站 | `https://43.154.92.36/wp-admin/`，运行和证书维护见[测试站说明](../deploy/wordpress-test/README.md) |

本机 WordPress 凭据保存在忽略的 `outputs/wordpress-test/credentials.json`；远端测试站凭据由运维在服务器私有目录保管。`web_password` 用于网页，`application_password` 用于 Article Agent。不要输出、提交凭据文件。

本机 Article Agent 的免登录调试与 WordPress 登录是两个系统；免登录调试不会让 WordPress 草稿变成公开页面。Article Agent 本机环境见[README](../README.md)。

## 验收

使用测试文章创建草稿，回读并检查标题、正文、图片、链接、TDK 和草稿状态；实际打开后台及登录态预览。重复上传应复用同一来源结果；修改源文或切换站点时检查提示与目标身份，不能静默覆盖人工编辑。

实现入口：`backend/services/wordpress_publisher.py`、`backend/services/server_wordpress_credentials.py`、`backend/server_project_http.py` 和 `frontend/src/components/project-delivery-records.tsx`。
