# M0–M2 接口契约

此阶段只开放聊天、模型设置、运行详情与本地配对。不展示虚假的研究/代码执行或 Agent 图。

所有 API `/api/v1`；cookie 鉴权，同源。JSON 错误 `{detail: {code, message}}`。POST/PUT/DELETE 必须携带 `X-Omni-Request: 1`。模型 Key 仅提交，响应永不返回。

- GET `/bootstrap` -> `{version, authenticated, pairing_required, demo_enabled, stage: "M0-M2"}`
- POST `/auth/pair` `{code}` -> `{authenticated:true}`，设置 HttpOnly cookie。
- POST `/auth/logout` -> 204。
- GET `/model-configs` -> `ModelConfig[]`。
- POST `/model-configs` -> ModelConfig，body `{name, adapter_type: "openai"|"anthropic"|"gemini"|"openai-compatible"|"mock", model_name, base_url?:string, api_key?:string, enabled?:boolean, parameters?:object, timeout_seconds?:number, max_output_tokens?:number}`。
- PUT `/model-configs/{id}` 同字段，省略 api_key 表示保留；空串表示清除。
- DELETE `/model-configs/{id}` 禁用（204）。
- POST `/model-configs/{id}/test` `{probes?:["chat","streaming","tools","structured"]}` -> `{model_config_id, results:{[probe]: {status:"supported"|"unsupported"|"error", message:string}}, tested_at}`。
- GET `/configuration/export` -> `{schema_version:1, model_configs:[不含key的配置]}`。
- POST `/configuration/import` 同上 -> `{imported:number}`，导入不覆盖既有配置，新凭据为空；mock 明确标识。
- GET `/conversations` -> `Conversation[]`。
- POST `/conversations` `{title?:string}` -> Conversation。
- GET `/conversations/{id}/messages` -> `Message[]`。
- POST `/conversations/{id}/messages` `{content,model_config_id,client_request_id,workflow_type?:"chat"|"agent"}` -> `{conversation_id,run_id,message_id}`；省略工作流类型与 `chat` 都是原有聊天。`agent` 目前返回 `503 AGENT_RUNTIME_UNAVAILABLE`，不会入队或创建消息，直到真正的 Agent 运行与恢复路径接通。
- GET `/runs?conversation_id=...` -> Run[]。
- GET `/runs/{id}` -> Run。
- GET `/runs/{id}/agent-runs` -> 实际保存在数据库中的 AgentRun 元数据数组；没有节点时是空数组，不为聊天构造虚拟节点。
- POST `/runs/{id}/cancel` -> Run。
- GET `/runs/{id}/events?after=0` -> SSE，id 为该 Run 的 sequence_number；data 为 Event。支持 Last-Event-ID；terminal 后关闭。
- GET `/plugins` -> 插件列表（声明接口及状态）。

ModelConfig: `{id,name,adapter_type,model_name,base_url,enabled,key_configured,version,parameters,timeout_seconds,max_output_tokens,capabilities,tested_at,created_at}`。
Conversation: `{id,title,created_at,updated_at}`。
Message: `{id,conversation_id,run_id,role:"user"|"assistant",content,status,created_at}`。
Run: `{id,conversation_id,model_config_id,model_label,status,mode:"live"|"mock",error_code,error_message,usage,created_at,started_at,finished_at}`。
Run.status: queued/running/cancel_requested/completed/cancelled/failed/interrupted。
Event: `{schema_version:1,event_id,run_id,sequence_number,event_type,created_at,payload}`。
事件：run.created、run.started、message.delta（payload 含 message_id、delta）、run.completed（usage）、run.cancelled、run.failed（code,message）、run.interrupted。

密钥与参数快照不经 Run 查询暴露。usage 缺失字段为 null；未知费用为 null。Mock 的 Run/文本/事件均明确区分，缺真实Key不自动切换Mock。

Provider Python 契约见同阶段 providers.py：RuntimeModelConfig、ModelDelta、ProviderError、stream_chat、probe_model。服务层只向适配器传递解密后的运行期配置，禁止记录其 repr。
