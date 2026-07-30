# 履约效率与质量看板

每周从飞书邮件附件读取数据，生成履约效率与质量看板。

GitHub Pages 发布文件为根目录的 `index.html`。运行 `agent.py` 后会更新该文件，并在 Git 仓库已配置远程地址与认证时自动提交、推送。

## 飞书数据问答机器人

`chatbot.py` 是按需启动的本地服务：它读取 `dashboard_output/history_data.json`，在本机聚合近六周大盘、品类、新人/老人及异常数据，再发送聚合结果给智谱 GLM-4-Flash 回答问题。不会发送订单明细、用户 UID 或工程师姓名。

1. 将 `chatbot.env.example` 中的键名补充至本机 `.env`，至少设置 `ZHIPUAI_API_KEY`；可选设置 `FEISHU_VERIFICATION_TOKEN`。API Key 在 [智谱开放平台](https://open.bigmodel.cn) 申请。
2. 安装并启动：

   ```bash
   .venv/bin/pip install -r requirements-chatbot.txt
   .venv/bin/python chatbot.py
   ```

3. 新开终端启动内网穿透：

   ```bash
   ngrok http 8080
   ```

4. 在飞书开放平台的应用「事件订阅」中填入 `https://<ngrok域名>/feishu/events`，订阅「接收消息 v2.0（`im.message.receive_v1`）」。如配置了 Verification Token，将同一个值填到 `.env`。
5. 确保机器人能力已开启，并发布接收私聊消息权限 `im:message.p2p_msg`（群聊按需再开 `im:message.group_at_msg`）。

健康检查地址为 `http://127.0.0.1:8080/health`。停止 `chatbot.py` 或 ngrok 后，机器人不会继续接收消息。
