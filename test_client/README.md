# WebSocket 测试客户端

本目录提供一个最小测试客户端，用于连接本项目的 WebSocket 服务端，持续收发消息，并观察 skill 流中的 JSON 格式是否符合预期。

## 先看这个

**普通用户消息时，你只需要输入 `content` 文本，不需要自己手写完整 JSON。**

客户端会自动封装成下面的格式再发送：

```json
{
  "sessionId": "...",
  "projectid": "...",
  "type": "message",
  "body": {
    "role": "user",
    "content": "你输入的文本"
  }
}
```

**只有在收到 `tool-calls` 后，客户端提示你填写的才是 `tool-results.result` 的内容。**

## 适用场景

- 服务端已按默认方式启动：`python main.py`
- 需要手工观察 `message`、`tool-calls`、`tool-results`、`error` 的实际格式
- 需要在 skill 流中手动给工具调用回包，验证后续步骤是否继续推进

## 启动服务端

项目根目录执行：

```powershell
python main.py
```

默认监听：

- Host: `0.0.0.0`
- Port: `8765`

测试客户端默认连接：

- URL: `ws://127.0.0.1:8765`

这与当前 [ws_server.py](/H:/Program/Agent-tool/ws_server.py) 的默认端口保持一致。

## 启动客户端

项目根目录执行：

```powershell
python test_client/ws_test_client.py
```

可选参数：

```powershell
python test_client/ws_test_client.py --host 127.0.0.1 --port 8765 --session-id s1 --project-id proj123 --content "请帮我进行BGA逃逸"
```

## 交互说明

客户端启动后会先发送一条用户消息：

```json
{
  "sessionId": "...",
  "projectid": "...",
  "type": "message",
  "body": {
    "role": "user",
    "content": "..."
  }
}
```

这里的 `body.content` 来自你在客户端里输入的普通文本。

**结论：普通对话时只输入一句话即可，客户端自动包装成完整 `message` JSON。**

之后会持续监听服务端消息：

- 收到 `tool-calls` 时，会打印完整 JSON，并提示输入 `tool-results.result`
- 直接回车：使用内置默认结果
- 输入 `json:...`：按 JSON 解析后作为结果回传
- 输入 `skip`：跳过本次工具回包

内置默认工具结果：

- `getProjectData`：返回一段简化的版图字符串
- `route`：返回一个简化的布线结果对象
- `GetSelectedElements`：返回一个简化的选择列表

客户端回包格式如下：

```json
{
  "type": "tool-results",
  "body": {
    "role": "tool",
    "content": {
      "id": "...",
      "result": "..."
    }
  }
}
```

收到服务端的 `message` 或 `error` 后，客户端也会打印完整 JSON，并允许继续输入下一条用户消息，以便持续观察 skill 恢复流程。

## 建议用法

如果要观察 BGA skill 流，建议：

1. 先启动服务端：`python main.py`
2. 再启动客户端：`python test_client/ws_test_client.py --project-id proj123 --content "请帮我做BGA逃逸"`
3. 当服务端发出 `getProjectData` 时，直接回车使用默认 mock 数据
4. 当服务端返回 `notice` 或 `fanout_params` 后，再根据提示输入下一条用户消息
5. 当服务端发出 `route` 时，继续回车使用默认 mock 数据
6. 观察最终 `routing_result` 的输出格式

## 依赖

客户端依赖项目现有的 `websockets` 包。若未安装，请先安装项目依赖：

```powershell
pip install -r requirements.txt
```
