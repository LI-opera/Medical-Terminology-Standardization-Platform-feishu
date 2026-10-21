# 飞书接入程序：从填写配置到启动

状态：IMPLEMENTED_NOT_ACCEPTED（完整业务链路尚待验收）。离线测试与 SDK 检查已通过；操作者已提供单聊和群聊真正 @ 后的帮助回复截图。英文文本经医学后端分析并在飞书返回的完整链路仍待验收。

使用前需创建飞书企业自建应用、添加机器人能力并开通接收与回复消息所需权限。具体权限以飞书开放平台对事件和接口的提示为准。

## 0. 当前已交付什么

- 官方 Python SDK 长连接入口；`run` 才启动业务服务。
- 单聊英文文本、群聊真正 @ 当前机器人；群内发送者和群 ID 必须同时获准。
- `帮助` / `/help` 不调用模型；图片、文件、富文本、空内容和超长文本只提示。
- 单分析 worker、有界持久队列、同一用户最多一个在途分析、SQLite 消息去重。
- 回复关联原消息，逐项保留 CODED / WITHHELD / NOT_EXPANDED / ABSTAIN 等状态，不把概念 ID 当作概念代码。
- 请求超时不自动再分析；重启后在途分析标为结果不确定；已保存结果仅继续发送。
- 飞书回复使用稳定 uuid，限频/网络错误最多尝试三次；永久错误不重试。
- `discover` 身份登记只在本机显示匹配随机口令的 ID，不发送飞书消息，不调用后端。
- `check`、`status`、离线测试不联网。

## 1. 文件与环境位置

实际配置文件由每位部署者从 `.env.example` 复制生成，不随仓库分发。以下绝对路径为原开发机示例，请替换为你的克隆目录：

`E:\Work\B_RAG\Try\medical-nlp\integrations\feishu\.env`

可提交的配置模板：同目录 `.env.example`。实际 `.env` 被 Git 忽略。

独立虚拟环境：`E:\Work\B_RAG\Try\medical-nlp\integrations\feishu\.venv`。

运行状态默认位置：`E:\Work\B_RAG\Try\medical-nlp\integrations\feishu\runtime\state.sqlite3`，包含临时任务正文，整目录已忽略。不要上传或截图状态库内容。

下列命令均在 **PowerShell** 执行。无需激活虚拟环境，也无需调整 ExecutionPolicy。

```powershell
Set-Location 'E:\Work\B_RAG\Try\medical-nlp'
```

## 2. 安装独立依赖（将来由你执行，会联网下载包）

若目录已经存在，不要重复创建；新机器可使用 Python 3.12 创建：

```powershell
py -3.12 -m venv integrations\feishu\.venv
```

安装仅接入所需的 SDK；不把 PyTorch、Milvus 等后端依赖装进此环境：

```powershell
.\integrations\feishu\.venv\Scripts\python.exe -m pip install -r integrations\feishu\requirements.txt
```

主依赖锁定 `lark-oapi==1.7.5`；传递依赖由 pip 解析，本轮没有生成传递依赖锁文件。安装成功后，先做不联网的 SDK 兼容性检查：

```powershell
.\integrations\feishu\.venv\Scripts\python.exe -m integrations.feishu.sdk_check
```

预期 `OFFLINE SDK CHECK PASS: 1.7.5`。若失败，停止在这一步，将错误告诉 Codex。用户安装 SDK 后，已修复检查程序在 Windows 上误拦截事件循环本机 socketpair 的问题，并实际通过 SDK 1.7.5 离线检查；这不表示飞书长连接已验证。

## 3. 先填写两项应用凭证

用编辑器打开：

```powershell
notepad 'E:\Work\B_RAG\Try\medical-nlp\integrations\feishu\.env'
```

先填写这两行并保存，其余身份字段暂时留空：

```dotenv
FEISHU_APP_ID=你在飞书后台看到的cli_开头应用ID
FEISHU_APP_SECRET=你在飞书后台看到的AppSecret
```

值不要带注释，不要发到聊天中。配置优先级：同名进程环境变量优先于 `.env`；如反复读到旧值，检查终端环境变量。程序不会自动读取后端的 `.env`。

离线检查填写格式：

```powershell
.\integrations\feishu\.venv\Scripts\python.exe -m integrations.feishu.main check --discovery
```

“检查通过”只表示字段齐全，不表示凭证有效或权限生效。空模板检查失败是正常的。

## 4. 启动身份登记（此步骤会连接飞书）

```powershell
.\integrations\feishu\.venv\Scripts\python.exe -m integrations.feishu.main discover
```

终端会显示本次随机口令，例如 `/register a1b2...`。不要复制本 README 的示例字符，要使用你终端这一次显示的完整口令。有效期 10 分钟；过期后 Ctrl+C，再重新启动获取新口令。过期只停止接受登记消息，不自动退出连接。

配合飞书后台完成：

1. 进入 **事件与回调 / 事件配置**。
2. 选择 **使用长连接接收事件**。保持此终端开启，然后保存。
3. 添加 **接收消息 v2.0**，事件代码 `im.message.receive_v1`。
4. 检查事件要求的单聊、群聊 @ 权限和回复接口所需权限，按后台提示补齐。
5. 创建/发布应用版本，可用范围先限定自己及明确测试成员。
6. 如果提示先发布才能连接，就先发布当前版本，返回本步骤重启登记；事件/权限变更后按后台提示再发布。

“正在尝试连接”不代表连接成功；你发送口令后能在本机看到身份记录才是接收通道的证据。SDK 原始日志可能包含内容，因此已抑制；失败时终端只显示安全错误类型，优先检查密钥、网络、发布和权限。

### 4.1 取得你自己的用户 ID 和租户 ID

在飞书找到机器人单聊，发送终端显示的 `/register 随机口令`。

终端会出现类似下面的记录（仅示例）：

```json
{"tenant_key":"tenant_example","user_open_id":"ou_example","chat_id":"oc_private","chat_type":"p2p","mentioned_open_ids":[]}
```

将真实输出中的 `tenant_key` 填入 `FEISHU_ALLOWED_TENANT`，将 `user_open_id` 填入 `FEISHU_ALLOWED_USERS`。私聊的 `chat_id` **不要**填到允许群列表。

### 4.2 取得测试群 ID 和机器人 ID

1. 把这个已发布的应用机器人加入同组织内部测试群。
2. 输入 `@` 并从候选列表选择本机器人，**只 @ 它一个**。
3. 后面粘贴同一个 `/register 随机口令`，发送。
4. 查看终端记录，确认 `chat_type` 是 `group`。
5. 将这一条记录的 `chat_id` 填入 `FEISHU_ALLOWED_CHATS`。
6. 将 `mentioned_open_ids` 中唯一的 `ou_...` 填入 `FEISHU_BOT_OPEN_ID`。

若列表为空或含多个 ID，重新发送只 @ 此机器人的登记消息；不要猜哪个是机器人。登记模式的 ID 需要你人工确认来自刚才自己操作的会话，不自动写入允许列表。随机口令仅在本地展示，勿转发给不参与测试的人。

所有字段填写好后 **Ctrl+C 停止登记进程**。不要同时运行登记和业务模式。

## 5. 完整配置填写说明

| 字段 | 填什么 |
|---|---|
| FEISHU_APP_ID | 应用 ID，cli_ 开头 |
| FEISHU_APP_SECRET | 后台 App Secret |
| FEISHU_ALLOWED_TENANT | 登记得到的 tenant_key |
| FEISHU_BOT_OPEN_ID | 群登记中仅 @ 本机器人时得到的唯一 mentioned_open_ids 值 |
| FEISHU_ALLOWED_USERS | 测试人员 open_id，多个用英文逗号分隔 |
| FEISHU_ALLOWED_CHATS | 测试群 chat_id，多个用英文逗号分隔 |
| MEDICAL_API_BASE_URL | 后端实际地址，如 http://127.0.0.1:8000；不带 /app |

其余参数先保留默认。运行模式要求上述允许列表非空，不支持 `*`，也不会因空值放行全部。一个用户在群中发消息时，用户和群都必须在名单内。增加其他测试人员时，让他们在登记模式发送本次口令，再由你人工添加身份。

```powershell
.\integrations\feishu\.venv\Scripts\python.exe -m integrations.feishu.main check
```

## 6. 确认后端可用

沿用原项目已有启动方式；本接入不会启动、重建或清理后端。

如果项目网页为 `http://127.0.0.1:8000/app`，基础地址填 `http://127.0.0.1:8000`；若原来用 8001 则填 8001。后端 `/health` 仅说明 API 在线，不能证明模型和 Milvus 可用。

真实分析会使用现有模型服务，可能产生费用；请在明确开始真实联调时，再用虚构文本验证 Analyze 页面。只测试帮助不需要启动后端。

## 7. 正式启动与停止（此命令会接收并回复消息）

从项目根目录启动：

```powershell
.\integrations\feishu\.venv\Scripts\python.exe -m integrations.feishu.main run
```

先向机器人发送 `帮助`。成功后，再发送虚构英文 `The patient has SOB and CP.`；群聊要真正 @。

停止：在此终端按 **Ctrl+C**。程序停止接收新消息并等待正在执行的 HTTP 请求结束；根据超时设置可能需要数分钟。未发送的持久化结果在下次 `run` 时会继续发送，**所以重启 run 也属于允许发送消息的操作**。强制结束时，下次运行会把中断中的分析标为 OUTCOME_UNKNOWN，不重复执行模型。

此程序没有注册开机自启、后台服务或计划任务。终端保持运行即可；不要使用关闭/删除数据库的方式停止。

只查看脱敏状态统计，不联网：

```powershell
.\integrations\feishu\.venv\Scripts\python.exe -m integrations.feishu.main status
```

## 8. 离线测试命令

测试使用标准库 unittest，不要求安装 SDK、pytest 或后端依赖，且阻断 socket 创建、连接和 DNS。所有消息和病例均为合成样例；MedicalClient 使用假的 HTTP opener，飞书发送使用替身。

```powershell
.\integrations\feishu\.venv\Scripts\python.exe -m unittest discover -s integrations\feishu\tests -v
```

实际 SDK 安装后，额外运行第 2 节的 `sdk_check`，其网络同样被阻断。Windows 事件循环初始化会先创建本机内部通信 socketpair，随后才加载 SDK，并阻断直接连接与 DNS；不连接飞书或后端。模拟 SDK builder 的测试不能替代真实 SDK 兼容性检查，更不能证明飞书权限已生效。

## 9. 限制、状态与排查

- 默认正文 4,000 字符，等待队列 10，单人最多一个在途任务，排队 300 秒，后端 socket 超时 180 秒。socket 超时不是后端任务取消或严格总执行时限。
- 首版仅一个进程和一个分析 worker；状态库文件锁防止同一状态库启动两个进程。禁止换另一个状态库启动同一机器人做多实例。
- 总待回复任务达到 50 时，新消息会被丢弃并在本机记录 `reply_backlog`，不继续无限积压；恢复后用户需要重新发送。普通分析队列满但回复队列未满时会返回“队列已满”。
- 回复为纯文本，按 UTF-8 大小编号拆分；可能跨段落/映射项拆分，但不丢字符。内容中的尖括号转为全角，避免被当成 @ 标签。
- 回复失败最多三次，重试保持同一 uuid；平台去重时窗和网络故障下的绝对恰好一次未验证。权限类错误记录失败，不无限循环。失败分块保留状态供排查，不自动重跑分析。
- 医学分析超时/网络故障标记 OUTCOME_UNKNOWN；重新发送另一条消息仍可能与未结束的后端请求重叠，应先由维护者检查。
- 任务正文完成后清空；发送成功的分块清空；失败正文超过 24 小时清除；去重元数据 7 天。清理由运行中的 worker 执行，停机期间不会自动清理。SQLite WAL/文件系统/备份残留不等于安全擦除，状态目录仅给维护者访问。
- 控制台日志不含正文或密钥，不写额外日志文件。现有后端自身的日志策略未在本次改动；真实病例使用前需要核对。
- 不处理富文本 `post`，不增加文件上传或多轮对话，不改变原医学逻辑。

`FAILED` 说明业务处理或排队失败；`DELIVERY_FAILED` 表示结果投递未完成；`OUTCOME_UNKNOWN` 表示后端是否完成不确定。`status` 同时显示任务和投递统计，以免把“处理失败信息已送达”误读成处理成功。

排查顺序：配置 check → 应用发布及事件订阅 → discover 口令能否到达 → 允许列表 → 帮助回复 → 后端真实请求 → 状态统计。登记能收到但帮助不能回复，重点检查发送权限和应用发布状态。

## 10. 来源与回退

官方参考：[Python SDK](https://github.com/larksuite/oapi-sdk-python)、[1.7.5 发布页](https://pypi.org/project/lark-oapi/1.7.5/)、[接收消息](https://open.feishu.cn/document/server-docs/im-v1/message/events/receive)、[回复消息](https://open.feishu.cn/document/server-docs/im-v1/message/reply)。SDK 源码中的请求 builder、uuid、timeout 和事件处理接口已用于静态对照；此前已完成 SDK 离线检查，帮助收发有操作者截图；完整医学链路仍待验收。

回退只需停止接入程序；原网页和后端未改动。保留本机配置和状态库可避免丢失去重记录。不要清空 Milvus、词典或 Docker 数据卷。新仓库为脱敏源码快照，本机配置和运行状态不在发布范围。
