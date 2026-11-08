# astrbot_plugin_hindsight_memory

这是一个 AstrBot 插件，用于接入 Hindsight Cloud，为私聊和群聊提供按会话隔离的长期记忆能力。

Hindsight 是一个面向 AI 应用的长期记忆服务，可以把对话中的信息沉淀到 Memory Bank，并在后续请求中按语义检索相关记忆。Hindsight Cloud 提供托管版 API 和后台管理界面，适合在不自建向量数据库、不维护记忆管理 WebUI 的情况下，为 Bot 增加可持续积累和召回的记忆能力。

插件不修改 AstrBot 核心，也不提供自定义 WebUI。配置仍然通过 AstrBot 根据 `_conf_schema.json` 生成的插件配置界面完成，记忆管理使用 Hindsight Cloud 自带后台。

## 功能特性

- 在每次 LLM 请求前，从 Hindsight Cloud 召回相关记忆。
- 通过 `extra_user_content_parts` 注入临时 `<hindsight_memory>` 内容，不写入 AstrBot 持久会话历史。
- 在 LLM 回复后，按写入判定策略将值得长期保存的内容写入 Hindsight Cloud。
- 私聊和群聊严格按 scope 隔离，群聊使用“群公共记忆 + 群成员个人记忆”双层召回，避免同群不同用户的个人记忆互相串扰。
- 发送到 Hindsight 的 sender ID、group ID、`unified_msg_origin` 都会先做 hash。
- 可启用统一的 `memory_ai_*` 配置，让小模型负责判断是否写入、生成自然事实句，并为自动召回和 `/hindsight recall` 拓展搜索 query。
- 提供 `/hindsight` 命令，用于状态检查、手动召回和当前会话临时开关。

## 平台兼容性

已测试 AstrBot 平台：

- `aiocqhttp`
- `qq_official`
- `qq_official_webhook`

其他 AstrBot 平台理论兼容，未逐一测试。

## 安装与配置

1. 在 Hindsight Cloud 创建一个 Memory Bank。
2. 为该 Bank 创建 Bank-scoped API Key。
3. 将本仓库安装到 AstrBot 插件目录。
4. 在 AstrBot WebUI 的插件配置中填写两个必填项：
   - “Hindsight API Key（必填）”（`api_key`）：第 2 步创建的 Key
   - “Memory Bank ID（必填）”（`bank_id`）：第 1 步创建的 Memory Bank 的 ID

   其余配置保持默认即可使用。如果想让记忆更精准，可以开启“用 AI 整理和查找记忆”（`memory_ai_enabled`），并在“整理记忆使用的模型”（`memory_ai_provider_id`）中选一个便宜、快速的小模型。
5. 保存配置后，重载插件或重启 AstrBot。

插件依赖只包含 `httpx`，AstrBot 通常会根据 `requirements.txt` 自动安装。

如果需要手动安装依赖，可在 AstrBot 环境中执行：

```bash
pip install -r data/plugins/astrbot_plugin_hindsight_memory/requirements.txt
```

## 配置项说明

下表中的“页面名称”是 AstrBot WebUI 插件配置页中显示的名称，括号内是对应的配置 key。

### 连接

| 页面名称 | 默认值 | 说明 |
|---|---|---|
| 启用长期记忆（`enabled`） | 开启 | 总开关。关闭后插件不再读取或写入任何记忆，已保存的记忆不受影响。 |
| 接入陪伴插件（`companion_provider_enabled`） | 开启 | 向陪伴插件（`astrbot_plugin_private_companion` 7.0.7+）注册为外部记忆 Provider，由本插件承接模型侧记忆，并接收它产生的日记、梦境与主动消息。没装陪伴插件时此项无效果。 |
| Hindsight 服务地址（`api_base`） | `https://api.hindsight.vectorize.io` | 保持默认即可。目前只支持 Hindsight Cloud，不支持自行部署的 Hindsight。 |
| Hindsight API Key（必填）（`api_key`） | 空 | 在 Hindsight Cloud 后台创建。建议创建只能访问对应 Memory Bank 的 Key（Bank-scoped），更安全。 |
| Memory Bank ID（必填）（`bank_id`） | 空 | Memory Bank 是存放记忆的“仓库”。先在 Hindsight Cloud 后台创建一个，再把它的 ID 填到这里。 |
| 连接 Hindsight 的超时时间（秒）（`request_timeout_seconds`） | `8` | 超过这个时间没有响应就放弃本次读写，机器人照常回复。网络较慢时可适当调大。 |

### 记忆范围

| 页面名称 | 默认值 | 说明 |
|---|---|---|
| 私聊中使用记忆（`enable_private_memory`） | 开启 | 机器人会记住私聊对象的信息，并在之后的私聊中想起来。每个用户的记忆相互隔离。 |
| 群聊中使用记忆（`enable_group_memory`） | 开启 | 机器人会记住群规则、群项目等群内公共信息，以及群成员在该群里的个人信息。不同群的记忆相互隔离。 |

### 召回

| 页面名称 | 默认值 | 说明 |
|---|---|---|
| 每次回复最多参考几条记忆（`recall_limit`） | `5` | 回复前会从记忆库中找出相关的几条，附加给 AI 作参考。条数越多参考越全，但会消耗更多 token。 |
| 每条记忆最多附加多少字（`recall_item_max_chars`） | `360` | 过长的记忆会被截断后再交给 AI，用来控制 token 消耗。一般无需修改。 |
| 参考最近几轮对话来理解指代（`context_turns`） | `3` | 用户说“就用那个”“改成周五”时，插件会结合最近几轮对话判断“那个”“周五”指什么，再决定记什么、查什么。这些旧对话只用来理解当前这句话，不会被再次写入。填 `0` 关闭。 |
| 记忆结果解析深度（高级）（`memory_extract_max_depth`） | `4` | 解析 Hindsight 返回数据时最多向下查找几层。一般无需修改。 |

### 写入

| 页面名称 | 默认值 | 说明 |
|---|---|---|
| 自动保存新记忆（`retain_enabled`） | 开启 | 机器人每次回复后会判断这轮对话里有没有值得长期记住的内容，有就保存。关闭后只读取已有记忆，不再新增。 |
| 自动保存的严格程度（`retain_decision_mode`） | 均衡（`balanced`） | 均衡：只保存偏好、身份、约定这类长期有用的信息，忽略闲聊。严格（`strict`）：只有用户明确说“记住……”时才保存。全部保存（`all`）：每轮对话原样保存，记忆库增长快、噪音多，仅用于兼容旧版本。 |
| 过短的消息不保存（字数）（`retain_min_chars`） | `8` | 用户消息的有效字数少于此值时，视为信息量不足，不会自动保存。用户明确说“记住”时不受此限制。 |
| 隐私信息需用户明确要求才保存（`retain_sensitive_requires_explicit`） | 开启 | 包含手机号、邮箱、身份证号等隐私信息的消息，只有用户明确说“记住”才会保存。密码、API Key、Token、私钥等密钥无论如何都不会保存。 |
| 保存对话原文而不是整理后的要点（`retain_write_raw_conversation`） | 关闭 | 默认只保存整理后的简洁事实（如“用户喜欢喝美式咖啡”）。开启后改为保存这轮对话的原文，细节更全，但记忆更冗长、更难被准确查到。“全部保存”模式下始终保存原文。 |
| 保存原文时包含用户的消息（`retain_user_message`） | 开启 | 仅在保存对话原文时生效：原文里是否包含用户说的话。一般保持开启。 |
| 保存原文时包含机器人的回复（`retain_assistant_message`） | 开启 | 仅在保存对话原文时生效：原文里是否包含机器人的回复。关闭可减少冗余内容。 |

### 去重

| 页面名称 | 默认值 | 说明 |
|---|---|---|
| 跳过重复的记忆（`retain_dedupe_enabled`） | 开启 | 保存前先查一下已有记忆，内容几乎相同的就不再重复保存。用户纠正旧信息时（如“我搬到上海了”）始终会保存。 |
| 相似度达到多少算重复（0～1）（`retain_dedupe_threshold`） | `0.85` | 新记忆与已有记忆的相似度达到此值时视为重复并跳过。调低会跳过更多相近内容；调高则只跳过几乎一模一样的内容。 |
| 查重时比对的已有记忆条数（`retain_dedupe_limit`） | `5` | 保存前取出最相关的几条已有记忆做比对。条数越多查重越全，但请求稍慢。 |

### AI 记忆分析

| 页面名称 | 默认值 | 说明 |
|---|---|---|
| 用 AI 整理和查找记忆（`memory_ai_enabled`） | 关闭 | 由 AI 判断哪些内容值得记、把对话整理成简洁的事实，并在查找记忆时补充更多相关搜索词，效果更好。代价是每轮对话会额外调用 1～2 次 LLM，建议选一个便宜、快速的小模型。 |
| 整理记忆使用的模型（`memory_ai_provider_id`） | 空 | 仅在开启“用 AI 整理和查找记忆”后生效。推荐选择便宜、快速的小模型；留空则使用当前对话正在用的模型。 |
| 所选模型出错时改用当前对话模型（`memory_ai_fallback_to_current_provider`） | 开启 | 上一项选择的模型调用失败时，是否改用当前对话的模型重试。关闭后本次跳过 AI，改用不调用 AI 的默认方式处理。 |
| AI 把握度低于此值时不保存（`memory_ai_min_confidence`） | `0.7` | AI 判断每条记忆时会给出 0～1 的把握度。数值越高，保存得越谨慎、记忆越少越准；数值越低，保存得越多。 |
## 写入判定策略

写入流程现在采用 AI 优先、规则兜底：

1. 如果 `memory_ai_enabled=true`，本地只做空文本、命令、硬敏感信息等必要过滤，然后调用 AI 从本轮对话中提取 0 到 5 条关于用户或群的长期记忆，每条带 scope、记忆类型（`preference`/`profile`/`event`/`project`/`rule`/`correction`）和置信度。AI 被要求只记用户本人说出或确认的信息，不记助手回复中的通用知识、用户的一次性提问和闲聊，并结合当前时间把“下周五”等相对时间换算成绝对日期。
2. AI 必须返回结构化 JSON（`{"memories": [...]}`，同时兼容旧的单条格式）；插件解析失败或调用失败时，不影响聊天流程，会回退到本地精简规则。置信度低于 `memory_ai_min_confidence` 的单条记忆会被单独丢弃。
3. 如果未启用 AI，本地规则只保留明确记忆意图、基础稳定事实、群规则/项目事实、个人偏好等几类，不再依赖大量手写锚点。
4. 写入前仍会用 `memory_text` 在当前 scope recall，若相似度超过 `retain_dedupe_threshold` 则跳过；纠正类记忆会继续写入，并在 metadata 中标记为 `correction`。
5. AI 判定时会附带最近 `context_turns` 轮对话（来自 AstrBot 当前请求的上下文），但只从当前这一轮提取记忆；群聊上下文中其他成员的发言不会被归到当前用户。工具调用的中间响应不会触发写入。
6. 每条写入都会附带 Hindsight 的 `context`（例如“私聊中关于用户的事实 (event)”）和消息发生时间 `timestamp`，帮助 Hindsight 抽取事实和按时间理解记忆。

AI 生成的 `memory_text` 会作为自然、独立、可检索的事实句写入，不再强制添加人工锚点前缀。默认不会额外调用 LLM；只有开启 `memory_ai_enabled` 后才会调用小模型。需要兼容旧行为时，可设置 `retain_decision_mode=all`。

## 召回拓展策略

默认召回只使用用户原始 query。开启 `memory_ai_enabled` 后，每次自动召回和手动 `/hindsight recall` 都会先让 AI 将原始 query 改写成 1 到 4 条搜索 query，插件会并发检索，再用倒数排名融合（RRF）合并各条 query 和各个 scope 的结果并去重，最后按 `recall_limit` 注入。这样扩展 query 和群成员个人记忆不会被第一条 query 或群公共记忆的结果挤掉；部分请求失败时仍使用其余结果。

召回改写同样会参考最近 `context_turns` 轮对话来补全指代。未开启 `memory_ai_enabled` 时，如果当前消息很短（如“那个呢？”），会额外用“上一条用户消息 + 当前消息”检索一次。“谢谢”“好的”等闲聊和命令不会触发自动召回。

自动注入的记忆块首行带有使用说明（按需参考、不逐条复述、以用户当前说法为准、无关则忽略），每条记忆前标注发生或记录日期，群公共记忆额外标注 `[群]`。手动 `/hindsight recall` 的输出不带使用说明。AI 只负责拓展搜索词，不允许回答用户问题；如果 AI 调用失败或 JSON 解析失败，则直接回退为只使用原 query。

Hindsight 仍固定使用原有 recall API 和 `tags_match: all_strict`，本版不引入阈值配置。

## 命令

- `/hindsight status`：检查配置完整性和 Hindsight Cloud 连通性。
- `/hindsight recall <query>`：在当前会话 scope 下手动检索记忆。
- `/hindsight on`：启用当前会话记忆。
- `/hindsight off`：关闭当前会话记忆。
- `/hindsight help`：显示命令帮助。

`/hindsight on` 和 `/hindsight off` 的状态保存在 AstrBot 插件数据目录中，只影响当前会话 scope，不会修改 Hindsight Cloud 中已有的记忆。

## 与陪伴插件（private_companion）协同

[陪伴插件](https://github.com/menglimi/astrbot_plugin_private_companion)（`astrbot_plugin_private_companion`）从 7.0.7 起对外提供 `companion.external-memory.v1` 记忆 Provider 接口。本插件检测到它的 `extension_api` 后会自动注册：

- **记忆归属**：注册时声明 `handles_model_memory=True`。本插件的记忆注入与保存本来就在 AstrBot 的 LLM 钩子里完成，因此由本插件承担模型侧记忆，陪伴插件让自己的内置记忆注入与记忆工具让位，模型只会看到一份记忆叙事。
- **接收角色记录**：陪伴插件产生的日记、梦境（`kind=persona_life`）和主动消息（`kind=proactive_message`）会写进 Hindsight，按 `memory_id` 幂等，同一条记录重复推送不会写两遍。这类记录落在 `scope:persona` 标签下，不按某个私聊用户召回。
- **撤销**：关闭「接入陪伴插件」，或在 AstrBot 里停用/卸载本插件时，会自动注销注册。

没有做的部分：不注册 `read` 回调（召回仍走本插件自己的 LLM 钩子），也不注册 `summary`（Hindsight Cloud 没有对应的计数接口）。同时装了「我会牢牢记住你」时，两个记忆插件都符合承接条件，建议只保留一个。

## Scope 隔离策略

私聊使用以下 tags：

```text
scope:private
platform:<platform_id>
sender:<sender_id_hash>
umo:<umo_hash>
```

群聊使用双层 tags。

群公共记忆：

```text
scope:group
scope:group_shared
platform:<platform_id>
group:<group_id_hash>
umo:<umo_hash>
```

群成员个人记忆：

```text
scope:group
scope:group_member
platform:<platform_id>
group:<group_id_hash>
sender:<sender_id_hash>
umo:<umo_hash>
```

召回时固定使用 `tags_match: all_strict`。私聊只召回当前私聊 scope 的记忆；群聊会同时召回当前群的公共记忆和当前发言成员在该群内的个人记忆。启用 `memory_ai_enabled` 时由 AI 在允许范围内选择 `group_shared` 或 `group_member`；未启用 AI 或 AI 失败时，本地规则会把群规则、群公告、项目约定等内容写入群公共层，把个人偏好、称呼、个人资料等内容写入群成员个人层。`retain_decision_mode=all` 时保持旧行为，同时写入群公共层和群成员个人层。

写入 metadata 会附带 `retention_reason`、`retention_type`、`retention_source`、`retention_confidence` 和 `retention_action`，便于在 Hindsight Cloud 后台排查某条记忆来自规则、AI 判断还是纠正/补充写入。

## ID 稳定性与迁移注意事项

正常重启 AstrBot 通常不会改变记忆 scope。插件生成 tags 时会使用：

- `platform_id`：AstrBot 平台名，例如 `aiocqhttp`、`qq_official`。
- `sender_id`：平台提供的用户 ID。
- `group_id`：平台提供的群 ID。
- `unified_msg_origin`：AstrBot 的会话来源标识。
- 本地 `salt`：插件首次运行时生成，并保存在 AstrBot 插件数据目录中。

只要平台适配器、机器人账号和插件数据目录不变，重启后 hash 出来的 tags 应保持稳定，旧记忆可以继续召回。

以下情况可能导致同一个用户或群生成不同 scope，从而召回不到旧记忆：

- 删除或迁移时丢失插件数据目录，导致 `salt.txt` 重新生成。
- 更换平台适配器，例如从 `aiocqhttp` 切换到 `qq_official`。
- 更换机器人账号、QQ 官方应用或平台配置，导致平台侧用户 ID 变化。
- 平台或 AstrBot 适配器更新后改变了 `sender_id`、`group_id`、`unified_msg_origin` 的生成方式。
- 群聊事件暂时拿不到 `group_id`，插件会降级为私聊 scope；之后如果又能拿到 `group_id`，scope 会发生变化。
- 群聊事件拿不到当前发言人的 `sender_id`，插件会跳过本轮记忆操作，避免多个未知用户被归入同一个个人记忆层。

迁移 AstrBot 或插件时，建议同时备份插件数据目录中的 `salt.txt` 和 `scope_state.json`。其中 `salt.txt` 会影响历史记忆是否还能被同一 scope 召回，`scope_state.json` 保存 `/hindsight on` 和 `/hindsight off` 的当前会话开关状态。

## Hindsight API

插件直接调用 Hindsight Cloud REST API，不依赖官方 SDK：

- Recall：`POST /v1/default/banks/{bank_id}/memories/recall`
- Retain：`POST /v1/default/banks/{bank_id}/memories`
- 状态检查：`GET /v1/default/banks/{bank_id}/tags?limit=1`

Retain 使用 item-level `tags`，并固定使用 `async: true`，减少对聊天流程延迟的影响。

## 测试方法

### 本地单元测试

在仓库根目录执行：

```bash
python -B -m unittest discover -s tests -v
```

### 语法检查

```bash
python -B -m py_compile main.py commands.py hindsight_client.py memory_formatter.py scope.py retention_policy.py memory_ai.py
```

### AstrBot 手动验收

1. 在 AstrBot WebUI 中确认插件已启用，且 `api_key`、`bank_id` 已配置。
2. 在聊天中发送：

   ```text
   /hindsight status
   ```

   期望看到配置完整，并且 Hindsight Cloud 连接正常。

3. 发送一条需要记忆的内容，例如：

   ```text
   我最喜欢的饮料是冰美式，请记住。
   ```

4. 等待几秒到几十秒后，再问：

   ```text
   我最喜欢喝什么？
   ```

   如果 Hindsight 已完成异步处理，模型应能通过 recall 回答出相关记忆。

5. 测试当前会话开关：

   ```text
   /hindsight off
   /hindsight on
   ```

6. 在不同私聊、不同群聊之间分别测试，确认记忆不会跨 scope 召回。
