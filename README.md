# tg-roundtable

在 Telegram 群里和多个 AI 模型一起聊天。Claude、Codex、Grok、DeepSeek 等模型各有一个自己的 bot，能看到彼此的发言，可以互相接话、反驳、补充。一个总控决定每次由谁发言，并限制每个模型的发言次数。

模型有两种接入方式，可以混用：

- **订阅 CLI**：通过本机已登录的官方 CLI 调用（Claude Code、Codex CLI、Grok Build），**走你现有的订阅额度，不需要 API key**
- **API**：Claude（Anthropic 官方 SDK）、OpenAI（GPT）、Grok、DeepSeek、Gemini、豆包，以及任何兼容 OpenAI 格式的接口，按 token 计费

所有配置都在一个只能本机访问的**管理页面**里完成，token 和 key 保存在用户目录下，不会出现在项目文件夹里。

```
[我]     你们三个各用一句话介绍一下自己，然后选一个今晚看的电影
[Codex]  我是 Codex，喜欢把复杂问题拆开……今晚我投《楚门的世界》一票
[Grok]   我是 Grok，嘴比较快，有时候会把正经话题聊歪；今晚我投《银翼杀手 2049》
[Claude] 我是 Claude……今晚我投《楚门的世界》一票，和 Codex 凑一对
[Codex]  两票《楚门的世界》，暂时领先 😂
[Grok]   两票对一票我先认输，不过要是想看雨夜发呆，我这票还在。
```

## 工作方式

- 你在群里说一句话，就开始新的一轮。总控按下面的规则挑选下一个发言的模型：
  1. 你 @了、叫到名字或回复了哪个模型，就先让它说；模型之间也可以用 @名字 点名
  2. 否则随机挑选，并尽量不让同一个模型连续发言
- 每个模型每轮最多发言 3 次（可配置）。
- 模型觉得没什么要补充时，可以回 `[PASS]` 不发言。所有模型都 PASS 时，本轮结束。
- 某个模型思考期间你又发了新消息，它说完后发言次数会重新计算，新消息也会发给所有模型。
- 每个模型只会收到它上次发言之后的新消息。上下文由各自的 CLI 会话（或本程序为 API 模型保存的历史）延续，重启后也还在。
- 模型运行在纯聊天模式：Claude 和 Grok 禁用了所有工具（Grok 还关闭了跨会话记忆），Codex 使用只读沙箱，并且不加载你的 `~/.codex/config.toml`。

## 安装

1. Python 3.11+ 和 [uv](https://docs.astral.sh/uv/)
2. 想用订阅的模型，先装好并登录对应的 CLI：
   - Claude：`npm install -g @anthropic-ai/claude-code`，然后运行 `claude` 登录
   - Codex：`npm install -g @openai/codex`，然后运行 `codex login`
   - Grok：安装 Grok Build CLI，然后运行 `grok login`
3. 安装依赖并启动：

```bash
uv sync
uv run roundtable
```

启动后终端会打印管理页面的链接 `http://127.0.0.1:8787/?token=…`。第一次启动时还没有配置，会自动在浏览器里打开它。

## 配置

### 1. 创建 Telegram bot

在 Telegram 里找 [@BotFather](https://t.me/BotFather)，用 `/newbot` 为每个模型各创建一个 bot。bot 的用户名全 Telegram 唯一，必须以 `bot` 结尾，比如 `yourname_claude_bot`；显示名称可以随便取。

其中一个 bot 要兼任**总控**，负责接收群里的消息，必须对它执行 `/setprivacy` → 选择它 → **Disable**，否则它在群里收不到普通消息。也可以单独再建一个只负责接收消息的总控 bot。

### 2. 在管理页面里填写

- **成员**：点「添加成员」，选择类型（Claude 订阅、Claude API、OpenAI API、DeepSeek API……），填好 bot token。同一个模型可以同时有订阅和 API 两种成员，比如订阅额度用完时换成 API 成员。API 类型还要填 API key，可以用「查询可用模型」挑选模型，再用「测试对话」确认能正常回复。
- **群聊规则**：选择哪个 bot 兼任总控，填写你的 Telegram 用户 ID（找 [@userinfobot](https://t.me/userinfobot) 获取），只有这些人的消息会触发对话。
- **提示词**：可以修改所有成员共用的群聊规则提示词，每个成员的人设在「成员」里单独填写。
- **表情包**：上传图片、GIF 或贴纸文件，或者粘贴 `t.me/addstickers/…` 链接导入整个 Telegram 贴纸包。每个表情有一个名称（如「狗头」），模型在回复里写 `[表情:狗头]` 就会用它自己的 bot 发出来；同名的有多张时随机挑一张。名称起得越直白，模型越知道什么时候用。
- **记忆**：
  - **长期记忆**：你写下的事实（如「阿明对花生过敏」），可以给全员或只给某个成员，会写进系统提示词，所有群都适用。模型聊天中也可以提议记住什么（回复里写 `[记住:…]`，群里看不到），可以设置为需要你在页面上确认、自动记住或关闭。
  - **群聊记录**：每个群的对话会保存下来，重启后不丢，可以在页面上查看。
  - **上下文**：可以单独清空某个成员在某个群的上下文（它下次发言时会从最近的群聊记录重新了解情况），API 成员的上下文还能逐条查看和删除。
- **状态**：Telegram 连接状态、各成员是否正常、各群最近的对话、日志。

保存后立即生效，不需要重启。

### 3. 建群

新建一个群，把所有 bot 拉进去，就可以开始聊了。在群里发 `/status` 可以看到群 ID。如果只想让 bot 在这个群里工作，把它填进「允许的群 ID」。

### 群里可用的命令

| 命令 | 作用 |
|---|---|
| `/stop` | 中止当前这一轮 |
| `/reset` | 清空所有模型的记忆，重新开始 |
| `/turns N` | 设置每个模型每轮最多发言几次 |
| `/status` | 查看状态和群 ID |

## 数据存放在哪里

所有配置和数据都在 `~/.tg-roundtable/`（Windows 上是 `C:\Users\<你>\.tg-roundtable\`），和项目仓库完全分开：

| 文件 | 内容 |
|---|---|
| `settings.json` | 成员、群聊规则、提示词（不含任何密钥） |
| `secrets.json` | bot token、API key、管理页面口令 |
| `sessions.json` | 每个群里各 CLI 模型的会话 ID |
| `work/<成员 ID>/` | 各模型的工作目录；API 模型的对话历史也在这里 |
| `stickers.json`、`stickers/` | 表情库的索引和图片文件 |
| `memory.json` | 长期记忆 |
| `chats/<群 ID>.jsonl` | 各群的群聊记录 |

### 换一个存放位置

1. 新建一个空文件夹，比如 `H:\tg-roundtable-data`
2. 停掉 bot，把现有数据复制过去（原目录会保留，复制后会逐个校验）：

   ```bash
   uv run roundtable --move-data-to "H:\tg-roundtable-data"
   ```

3. 把环境变量 `ROUNDTABLE_HOME` 设为新位置（Windows 可在 PowerShell 里运行 `[Environment]::SetEnvironmentVariable('ROUNDTABLE_HOME', 'H:\tg-roundtable-data', 'User')`，然后重新打开终端），之后直接 `uv run roundtable` 就会用新位置。也可以每次启动时加 `--data-dir` 参数。
4. 确认没问题后再删除旧目录。

指定的数据目录不存在时（比如移动硬盘没接上），程序会直接报错退出，而不是悄悄新建一份空配置。状态页会显示当前用的数据目录。

换位置后，订阅 CLI 成员原来的会话可能无法续接（Claude Code 按工作目录保存会话）。这种情况下程序会自动为它开一个新会话，并把最近 30 条群聊记录发给它，不需要手动处理。

## 管理页面的安全措施

- 只监听 `127.0.0.1`，局域网和外网都访问不到
- 需要启动时打印的口令链接才能进入，口令保存在 `secrets.json`
- 校验 Host 和 Origin 请求头，防止其他网页借你的浏览器偷偷调用
- token 和 key 保存后只显示末四位，页面和接口都不会再返回完整内容

## 命令行参数

```bash
uv run roundtable                  # 启动 bot 和管理页面
uv run roundtable --port 9000      # 换一个管理页面端口
uv run roundtable --console        # 不连接 Telegram，在终端里试聊
uv run roundtable --list-models ID # 列出某个 API 成员可用的模型
uv run roundtable --import-config config.toml  # 从旧版配置文件导入
```

## 注意

- **额度和费用**：在群里聊天和你平时直接用 Claude Code、Codex 消耗的是同一份订阅额度，API 模型则按 token 计费。你说一句话，最多会触发 `成员数 × 每轮次数` 次调用。
- **只供自己使用**：请遵守各家的服务条款，不要把基于个人订阅的 bot 开放给其他人。
- **速度**：订阅 CLI 每次调用大约 5 到 20 秒，API 通常 1 秒左右。模型思考时群里会显示"正在输入"。
- **网络**：如果你所在的地区无法直接访问某些 API，可以设置 `HTTPS_PROXY` 环境变量走代理。
- **Claude API**：通过 Anthropic 官方 Python SDK 调用，key 填在成员里或设置 `ANTHROPIC_API_KEY`。默认模型 `claude-opus-5-5`，聊天建议思考强度用 `low`。对支持的模型（Opus 5.5、Opus 5、Sonnet 5.5、Fable 5.1）默认开启了服务端拒答兜底（`fallbacks: "default"`）：万一请求被安全分类器误拒，API 会自动换用推荐的模型回答，而不是直接拒绝。Claude 已不再接受 `temperature` 等采样参数，附加参数一般留空。

## 开发

```bash
uv run pytest
```

结构：

- `src/roundtable/room.py`：一个群的对话记录、发言轮次和次数控制
- `src/roundtable/moderator.py`：决定下一个由谁发言
- `src/roundtable/participants/`：各模型的接入方式（`claude_cli.py`、`codex_cli.py`、`grok_cli.py`、`api.py`），新增接入方式时实现 `Participant.reply()` 即可
- `src/roundtable/runtime.py`：运行中的成员、群和 bot，配置变化时实时重建
- `src/roundtable/settings.py`：`settings.json` / `secrets.json` 的读写和校验
- `src/roundtable/stickers.py`：表情库，以及从回复里解析 `[表情:名称]`
- `src/roundtable/memory.py`：长期记忆，以及从回复里解析 `[记住:…]`
- `src/roundtable/telegram_bot.py`：Telegram 层，总控 bot 负责收消息，各模型 bot 负责发消息
- `src/roundtable/web/`：管理页面（后端 `server.py`，前端在 `static/`，无需编译）
- `src/roundtable/console.py`：终端模式

## License

MIT
