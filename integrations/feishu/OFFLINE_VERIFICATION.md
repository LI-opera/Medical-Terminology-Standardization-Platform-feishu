# 飞书接入离线验证记录

日期：2026-10-21（Asia/Shanghai）。

初始离线验证状态：IMPLEMENTED_NOT_ACCEPTED。下文为该阶段记录。

2026-10-22 发布准备复验：26 项离线测试通过，SDK 1.7.5 离线检查通过。后续操作者截图已证明单聊和群聊 @ 帮助消息收发；完整医学分析链路仍未记录端到端验收。

## 用户安装 SDK 后的修复与复验

用户截图显示 SDK 离线检查触发 `AssertionError: network forbidden`：SDK 导入时创建 Windows asyncio 事件循环，其本机 socketpair 被检查程序的 connect 拦截器误拦。修复为先建立事件循环的 OS 本机内部通道，然后启用连接/DNS 拦截，再导入 SDK 并检查 builder 和事件序列化，退出时关闭循环。

已在用户安装的 SDK 1.7.5 环境实际执行 `python -m integrations.feishu.sdk_check`，结果 `OFFLINE SDK CHECK PASS: 1.7.5`。新增回归用例验证事件循环可正常执行、直接本机/远端连接和 DNS 仍被阻断，以及真实 SDK 构造/序列化离线检查。完整离线测试 26 tests / OK（含真实 SDK 离线检查）；第三方 protobuf 发出一条 datetime 弃用警告，不影响通过。未执行 discover/run，不读取真实凭证，不操作用户已运行的进程。

## 执行环境与命令

已用本机既有 Python 创建独立 `integrations/feishu/.venv`，没有联网安装依赖。24 个测试使用标准库 unittest，未导入后端模型、Milvus 或实际 SDK。

```powershell
Set-Location 'E:\Work\B_RAG\Try\medical-nlp'
.\integrations\feishu\.venv\Scripts\python.exe -m unittest discover -s integrations/feishu/tests -q
.\integrations\feishu\.venv\Scripts\python.exe -m compileall -q integrations/feishu
.\integrations\feishu\.venv\Scripts\python.exe -m integrations.feishu.main --help
git check-ignore integrations/feishu/.env integrations/feishu/.venv/pyvenv.cfg integrations/feishu/runtime/state.sqlite3
```

结果：24 tests / OK；Python 编译检查通过；CLI 帮助能运行；凭证文件、独立环境和状态目录忽略规则生效。

新增配置验证测试曾因 Windows 短路径与长路径字符串比较失败，修正测试期望值为解析后的路径，再在独立环境完整运行通过。未通过修改业务路径逻辑掩盖问题。

## 已覆盖的行为

- 拒绝错误 app/tenant、机器人发送者、未获准人员或群。
- 群聊仅通过 mentions 中机器人 open_id 与正文占位符双重校验后触发；单纯打名字无效。
- 帮助、空文本、中文无英文字符、超长文本和不支持类型不调用分析。
- 并发重投去重、单用户在途限制、等待队列上限与回复积压上限。
- 不同原消息回复不串线；保留 WITHHELD 等失败状态，不显示未确认候选编码。
- 所有业务状态、未知状态、空结果，以及尖括号转义和 UTF-8 分块后重组。
- 网络超时不重跑分析；发送限频使用同一 uuid 重试，权限错误不重试。
- 进程恢复将 RUNNING 标为结果不确定，已完成结果仅投递；排队过期不执行。
- 发送后正文清空，失败内容到期清空，去重元数据到期清理。
- 随机口令身份登记仅返回身份元数据。
- 医学 HTTP 请求格式与安全错误包装、飞书 reply builder/限频分类的模拟契约。
- 配置缺失拒绝运行、地址与通配符校验、单实例锁、后台线程处理和正常停止。

测试在运行期间替换 socket 创建、连接和 DNS 为抛错函数；HTTP opener 与飞书发送方均为替身。官方文档查询属于实现前的资料核对，不是联网运行测试。

## 仍需后续验证

1. SDK 1.7.5 构造与事件序列化检查已在上述复验通过；SDK 真实连接行为仍待联调。
2. 登记模式真实接收事件、实际 tenant/user/bot/chat ID、权限与发布状态。
3. 单聊帮助回复、群聊 @ 帮助回复。
4. 现有后端在实际数据集上的分析、耗时与资源占用。
5. 飞书消息大小限制、断线恢复、uuid 去重时窗与真实限频行为。

未做：真实消息收发、API/模型调用、服务启动、Git 暂存/提交/推送、数据库重建或后台自动任务。
