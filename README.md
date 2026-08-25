# 织梦台：AI 长篇小说生成器

织梦台是一款面向长篇小说创作的本地 AI 写作工具，目标是让 AI 负责辅助规划和起草，由作者掌握设定、情节与最终文本。

当前项目仍处于 MVP / 测试阶段，适合体验、验证流程和参与开发，暂不建议把生成结果不经人工审核直接用于正式发布。

> 新建作品 → 故事档案 → 人物与世界观 → 分卷及章节大纲 → AI 生成正文 → 一致性检查 → 作者修改和保存

## 下载 Windows 安装版

不想配置 Python、Node.js 或 Docker 的 Windows 用户，可以直接下载 `.exe` 安装程序：

**[前往 GitHub Releases 下载最新版](https://github.com/HardRain123/ai-novel-generator/releases/latest)**

在 Release 的 `Assets` 中选择类似下面的文件：

```text
WeDream-Setup-0.2.5-x64.exe
```

请不要下载以下文件作为安装程序：

- `*.exe.blockmap`：供应用自动增量更新使用，不能单独安装。
- `latest.yml`：自动更新元数据。
- `Source code (zip)` / `Source code (tar.gz)`：源代码压缩包，需要自行配置开发环境。

安装版目前面向 Windows 10/11 x64。安装并启动“织梦台”后，在“模型设置”中配置模型服务即可使用；安装包不包含任何 API Key。
<img width="2523" height="1098" alt="bce52e3f-454e-4d61-9dd9-8a9e8915788e" src="https://github.com/user-attachments/assets/396ce41e-5bc8-4508-bfcf-0418e0324727" />
<img width="2493" height="1109" alt="3ffca8c1-bf87-4d7c-bf27-12644447fd9f" src="https://github.com/user-attachments/assets/bcf0f9f4-64f5-48ba-b3a5-ab2c56018688" />
<img width="2442" height="1089" alt="a38bb7aa-fe5d-4d5d-a80b-47f5bf172bb3" src="https://github.com/user-attachments/assets/c6f379d2-953f-4a92-a9ea-10ba63b32533" />
<img width="2484" height="1137" alt="25fefbe9-3333-402f-b566-1d607d2c72c6" src="https://github.com/user-attachments/assets/21cd01b7-6393-4baf-8ec1-45b9d25653d4" />
<img width="2484" height="1232" alt="318a3c8e-cb80-48ab-affc-a3a70df57700" src="https://github.com/user-attachments/assets/2e20da0c-e269-4e6c-b559-c45024737d08" />

## 当前可以做什么

- 创建和管理小说作品。
- 分步骤建立故事契约、角色、世界观和主线资产。
- 生成分卷主线、章节大纲与章节正文。
- 编辑和保存规划内容、章节正文及历史版本。
- 管理人物状态、时间线、别名和伏笔。
- 对角色、一致性、重复内容和伏笔进行基础检查。
- 保存模型配置，为不同作品选择不同模型和推理强度。
- 支持 DeepSeek、通义千问、Kimi 及其他 OpenAI 兼容接口。
- 支持本机 Codex CLI 登录桥接（`codex login` + `codex exec`）。
- 将作品和完整大纲导出为 Markdown。
- 使用 SQLite 在本地保存数据。

## 当前不足

请在使用前了解以下限制：

1. **热点分析目前基本无效。** 番茄、起点、晋江等公开榜单的数据获取容易受到页面变化、访问限制和缓存质量影响，当前搜索与趋势分析结果可能为空、过期或缺少参考价值，暂时不能作为可靠的选题依据。
2. **生成文章的质量还不够好。** 正文可能出现模板化表达、重复描写、节奏平淡、人物声音相似、情绪铺垫不足或“AI 味”明显等问题，需要作者进行较多修改。
3. **长篇连续性仍然有限。** 章节增多后，模型可能遗忘较早设定，角色状态、时间线、伏笔和因果关系仍可能发生漂移。
4. **规划完整不等于成文质量高。** 故事档案和大纲可以约束方向，但目前从规划到正文的执行能力仍依赖所选模型，不能保证生成内容准确落实全部要求。
5. **质检能力偏基础。** 当前不少检查依赖规则和结构化数据，只能发现部分重复、缺失与冲突，不能替代作者审稿或专业编辑。
6. **状态提取需要人工确认。** 自动提取的人物变化和时间线事件只是候选结果，不会直接视为已确认事实；错误接受可能影响后续生成。
7. **模型效果、速度和费用不可控。** 实际表现取决于第三方模型、提示词、上下文长度和网络状态，并可能产生 API 调用费用。
8. **桌面版仍属于测试构建。** 当前主要验证 Windows x64，尚未完成正式代码签名、多平台安装包和大规模用户环境测试。

## 建议的使用方式

- 把 AI 生成内容当作草稿，而不是最终稿。
- 先完成故事档案和阶段性大纲，再逐章生成。
- 每章生成后人工检查人物动机、事实、时间线和伏笔。
- 及时修改模板化、重复和空泛的段落。
- 长篇项目定期导出 Markdown，保留额外备份。
- 正式创作前先用少量章节测试所选模型的质量与费用。

## 模型与隐私

首次启动后，可在页面“模型设置”中保存模型地址、模型名称和 API Key。密钥在本地加密保存，作品数据保存在本机 SQLite 数据库中。

使用模型服务时，相关提示词和作品上下文会发送到你配置的模型提供商，请自行了解对应服务的隐私政策和数据使用规则。

`Codex Auth` 模式调用本机已登录的 `codex exec`，不读取或回传 ChatGPT 浏览器 Cookie，也不使用未公开的 Codex HTTP 接口。使用前需要在同一台机器安装 Codex CLI 并运行：

```bash
codex login
```

## 使用源码运行

### Docker 启动

推荐先安装 Docker Desktop：

- Windows：双击 `start.bat`。
- macOS：双击 `start.command`；首次被系统拦截时可右键选择“打开”。

启动脚本会构建并启动前端、后端和生成 Worker，然后打开 <http://localhost:3000>。首次运行需要下载依赖和构建镜像，因此耗时会更长。

### 本机开发环境

需要 Python 3.11+、Node.js 22+ 和 [uv](https://docs.astral.sh/uv/)。

安装 Python 依赖并启动后端：

```bash
uv sync --dev
uv run uvicorn main:app --reload --port 8000
```

另开一个终端启动异步生成 Worker：

```bash
uv run python -m app.worker
```

再启动前端：

```bash
cd web
npm install
npm run dev
```

打开 <http://localhost:3000>。生成故事档案、大纲和章节时必须保持 Worker 运行。

如果页面中还没有模型配置，可以通过环境变量提供初始配置：

```env
LLM_API_KEY=...
LLM_BASE_URL=https://api.deepseek.com
LLM_MODEL=deepseek-v4-flash
```

## 本地构建 Windows 安装包

安装项目依赖后，在 Windows PowerShell 中运行：

```powershell
.\scripts\build-windows.ps1
```

安装程序会生成到：

```text
desktop/dist/WeDream-Setup-<version>-x64.exe
```

推送与 `desktop/package.json` 版本一致的 `v*` Git 标签，会触发 GitHub Actions 自动构建并上传到 Releases。

## 后续改进方向

1. 修复并重新设计热点数据源、缓存和有效性提示。
2. 增加正文生成后的批评、修订和去模板化流程，提高可读性。
3. 加强人物声音、情绪曲线、场景目标和章节节奏控制。
4. 支持从指定章节向后重放角色状态、时间线和伏笔。
5. 为一致性问题增加来源定位和基于模型的编辑建议。
6. 完善备份恢复、异常诊断、代码签名和多平台安装包。

## 主要技术栈

- 后端：FastAPI、Python、SQLite
- 前端：Next.js、React、TypeScript
- 桌面端：Electron、electron-builder
- 后台任务：持久化任务队列与独立 Worker
- 打包发布：PyInstaller、NSIS、GitHub Actions

欢迎提交 Issue，特别是可复现的生成质量问题、模型配置问题和桌面版运行日志。请勿在 Issue 中公开 API Key、作品隐私内容或其他敏感信息。
