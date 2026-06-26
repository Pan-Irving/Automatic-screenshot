# 影刀 DeepSeek GEO 采集 MVP

本方案用于搭建第一版独立 DeepSeek 采集流程。当前提供两种入口：

- 本地 Web 后台：推荐给运营团队使用，Excel 只负责导入和导出，运行状态保存到 `runtime/tasks.json`。
- 影刀流程：保留原有影刀代码流程作为备用入口。

采集通过本地 Chrome 的 CDP 端口复用真实登录态。当前使用原手写 CDP 采集器。

## 本地 Web 后台

### 启动步骤

1. 双击或运行 `start_web_console.command`。
2. 浏览器打开 `http://127.0.0.1:8765`。
3. 在“账号准备”里选择本轮账号数量，默认 2 个账号。
4. 点击“打开账号窗口”，后台会打开对应数量的专用 Chrome。
5. 在打开的 Chrome 窗口中分别登录 DeepSeek。
6. 回到后台，点击“检测状态”确认账号窗口可用。
7. 点击“导入默认 Excel”或上传 `.xlsx`。
8. 点击“开始采集”。

### 页面能力

- 当前任务：显示正在采集的问题、开始时间、状态和执行次数。
- 待执行动作：显示所有 `pending` 任务。一个 Excel 问题行就是一个动作。
- 已执行任务：显示 `success`、`failed`、`manual_required` 任务。
- 任务详情：显示截图路径、回答文本路径、DeepSeek 对话链接、链接文本文件、备注、错误、耗时等数据情况。
- 暂停：不会强行中断当前 DeepSeek 问答，只会阻止继续执行下一条任务。
- 重试：可将 `failed`、`manual_required` 或已完成任务重新排回 `pending`。
- 导出 Excel：根据 `runtime/tasks.json` 导出最新结果。

### 运行态文件

- `runtime/tasks.json`：Web 后台的主状态文件。
- `runtime/events.jsonl`：导入、启动、完成、暂停、重试、导出等事件日志。
- `runtime/exported_tasks.xlsx`：最近一次导出的结果 Excel。
- `yingdao_results/YYYYMMDD/{question}/`：每轮保存回答文本、对话 URL、截图和可选 HTML 页面。

第一版不使用数据库。Excel 是任务导入/结果导出格式，不再作为采集过程中的实时状态文件。

## 采集器配置

默认使用 CDP 采集器：

```bash
YINGDAO_COLLECTOR=cdp python3 server.py
```

采集器连接 Web 后台打开的专用 Chrome 账号池。每条任务都会打开新的 DeepSeek 页面，避免 GEO 前置探测的上下文污染。

账号池默认包含 3 个槽位，每个账号最多 2 条并发生成：

```bash
deepseek_a:9222:chrome_cdp_profile_deepseek_a:2
deepseek_b:9223:chrome_cdp_profile_deepseek_b:2
deepseek_c:9224:chrome_cdp_profile_deepseek_c:2
```

可通过 `YINGDAO_ACCOUNTS` 调整账号池，格式为 `account_id:cdp_port:profile_dir:max_concurrency`，多个账号用英文逗号分隔：

```bash
YINGDAO_ACCOUNTS="deepseek_a:9222:chrome_cdp_profile_deepseek_a:2,deepseek_b:9223:chrome_cdp_profile_deepseek_b:2" python3 server.py
```

`YINGDAO_MAX_WORKERS` 可作为全局上限；默认等于账号池总容量。例如 2 个账号、每个 2 并发时，默认总并发为 4。

```bash
YINGDAO_MAX_WORKERS=4 python3 server.py
```

`YINGDAO_DEFAULT_ACCOUNT_COUNT` 控制后台默认选择几个账号，默认是 2：

```bash
YINGDAO_DEFAULT_ACCOUNT_COUNT=2 python3 server.py
```

## 文件约定

- 任务表：`/Users/pan/Documents/思阳/geo-evidence-collector/yingdao_mvp/questions.xlsx`
- 任务 Sheet：`Tasks`
- 输出目录：`yingdao_results/YYYYMMDD/`
- 输出子目录：`{question}/`
- 截图文件名：`{id}_{platform}_round{round}_{HHMMSS}_{task_uid后6位}.png`
- 回答文件名：`{id}_{platform}_round{round}_{HHMMSS}_{task_uid后6位}.txt`，内容包含本轮问题和 DeepSeek 回答。
- 链接文件名：`{id}_{platform}_round{round}_{HHMMSS}_{task_uid后6位}_url.txt`
- 搜索结果文件名：`{id}_{platform}_round{round}_{HHMMSS}_{task_uid后6位}_search_results.json`，内容为 DeepSeek 页面已展示的搜索结果证据。

## 本机影刀应用

- 应用名称：`GEO - DeepSeek 影刀 MVP`
- 影刀项目目录：`/Users/pan/Library/Application Support/Shadowbot/users/957807004805722114/apps/a6a87d1f-0034-4d5f-ad0b-78b512510fba/xbot_robot`
- 启动流程：`main`
- 入口文件：`main.py`
- 采集逻辑：`collector.py`

当前实现直接把 `main` 配置为代码流程，由 `main.py` 调用 `collector.main(args)`。这样不依赖可视化流程里“调用模块”控件的下拉识别；旧的 `主流程.flow` 文件仍保留在影刀项目中作为备份，不作为当前启动入口。

`Tasks` 固定列：

| 列名 | 用途 |
| --- | --- |
| `id` | 问题编号，例如 `Q001` |
| `question` | 要提交给 DeepSeek 的问题 |
| `platform` | 第一版固定为 `deepseek` |
| `round` | 轮次，第一版默认 `1` |
| `status` | `pending`、`running`、`success`、`manual_required`、`failed` |
| `screenshot_path` | 影刀保存截图后写回的绝对路径 |
| `answer_text_path` | 影刀保存回答文本后写回的绝对路径 |
| `answer_url` | DeepSeek 当前对话链接 |
| `url_text_path` | 保存对话链接的文本文件路径 |
| `search_results_path` | 保存搜索结果 JSON 的绝对路径 |
| `search_result_count` | JSON 中解析到的搜索结果条数 |
| `search_read_count` | DeepSeek 页面显示的已阅读网页数 |
| `account_id` | DeepSeek 账号槽位 |
| `cdp_port` | 当前任务使用的 Chrome CDP 端口 |
| `profile_dir` | 当前任务使用的 Chrome profile 目录 |
| `remark` | 错误、验证或人工接管说明 |
| `updated_at` | 当前处理时间 |

## 主流程 Main

1. 打开 Excel 文件 `questions.xlsx`。
2. 读取 `Tasks` 已使用区域。
3. 循环每一行，只有满足以下条件才处理：
   - `status = pending`
   - `platform = deepseek`
   - `question` 不为空
4. 处理当前行前，写回：
   - `status = running`
   - `remark = 正在采集`
   - `updated_at = 当前时间`
5. 调用子流程 `RunDeepSeek`。
6. 子流程返回后写回当前行。
7. 每个问题完成后等待 3 到 8 秒。
8. 继续下一行。

## 子流程 RunDeepSeek

建议变量：

| 变量 | 含义 |
| --- | --- |
| `task_id` | Excel 当前行 `id` |
| `question` | Excel 当前行 `question` |
| `platform` | 固定 `deepseek` |
| `round` | Excel 当前行 `round` |
| `date_dir` | 当前日期，格式 `YYYYMMDD` |
| `time_tag` | 当前时间，格式 `HHMMSS` |
| `output_dir` | `yingdao_results/{date_dir}/` |
| `base_name` | `{task_id}_{platform}_round{round}_{time_tag}` |
| `screenshot_path` | `{output_dir}/{base_name}.png` |
| `answer_text_path` | `{output_dir}/{base_name}.txt` |
| `search_results_path` | `{output_dir}/{base_name}_search_results.json` |

流程步骤：

1. 创建 `output_dir`。
2. 使用真实 Chrome 打开 `https://chat.deepseek.com/`。
3. 等待页面加载完成。
4. 获取页面文本，检查是否命中人工接管关键字：
   - `登录`
   - `验证码`
   - `验证`
   - `安全检测`
   - `人机验证`
   - `访问受限`
   - `captcha`
   - `verify`
5. 如果命中：
   - 保存当前页面截图到 `screenshot_path`
   - 返回 `status = manual_required`
   - 返回 `remark = DeepSeek 触发登录/验证/风控，需要人工处理`
   - 不继续提问
6. 如果页面可用，点击“新对话”。如果元素捕获失败，允许用图像识别兜底，不建议长期使用固定坐标。
7. 点击输入框。
8. 将 `question` 写入剪贴板。
9. 粘贴到输入框。
10. 点击发送按钮。
11. 等待回答完成：
    - 优先等待“停止生成”按钮出现后消失。
    - 默认最长等待 150 秒，期间每 2 秒检查一次回答文本是否稳定。
12. 再次检查人工接管关键字。如果命中，截图并返回 `manual_required`。
13. 尽量获取最后一条回答文本，保存到 `answer_text_path`。
14. 从页面 DOM 提取右侧“搜索结果/已阅读网页”数据，保存到 `search_results_path`。
15. 保存当前页面截图到 `screenshot_path`。
16. 返回：
    - `status = success`
    - `screenshot_path`
    - `answer_text_path`
    - `search_results_path`
    - `search_result_count`
    - `search_read_count`
    - `remark = 正常完成`

搜索结果 JSON 只记录 DeepSeek 页面已经暴露的数据，包括已阅读网页数、搜索结果标题、来源站点、日期、URL、摘要，以及回答正文里的引用链接。DeepSeek 内部搜索关键词、排序权重和未展示候选网页不会稳定暴露，当前流程不采集这类内部信息。

## 异常处理

任意步骤异常时：

1. 尝试保存当前页面截图到 `screenshot_path`。
2. 写回：
   - `status = failed`
   - `screenshot_path = 截图路径`
   - `answer_text_path = 空`
   - `remark = 失败阶段 + 错误原因`
   - `updated_at = 当前时间`

推荐失败阶段命名：

- `open_deepseek_failed`
- `page_not_ready`
- `new_chat_failed`
- `input_not_found`
- `send_failed`
- `timeout_waiting_answer`
- `screenshot_failed`
- `write_excel_failed`

## 人工接管规则

影刀不要自动处理验证码或安全验证。

当状态为 `manual_required`：

1. 打开 `screenshot_path` 查看停在哪个页面。
2. 人工在 Chrome 中完成登录或验证。
3. 回到 `questions.xlsx`，把该行 `status` 改回 `pending`。
4. 重新运行影刀流程。

## 低风控运行参数

- 默认使用多 Chrome profile、多 DeepSeek 账号池并发处理问题。
- Web 后台先选择本轮账号数量，再打开对应账号窗口。
- 每个账号默认最多 2 条并发生成，不把单账号压到 3 条。
- 每个 worker 都会打开自己的 DeepSeek 新对话页面。
- 任务启动之间有轻微错峰，避免同一瞬间打开多个页面。
- 某个账号触发登录/验证/忙碌时，只暂停该账号继续接任务，其他账号继续收尾。
- 不在验证后连续重试。
- 不做自动绕验证或自动风控规避。

## 验收步骤

1. 只运行 `start_web_console.command`，不要提前启动 Chrome 脚本。
2. 在后台选择 `2 个账号`，点击“打开账号窗口”，确认只打开两个专用 Chrome。
3. 分别登录两个 DeepSeek 账号后点击“检测状态”，确认账号显示可用或至少不是未启动。
4. 在 `questions.xlsx` 保留 6 条 `pending` 任务。
5. 运行 Web 后台采集，确认任务只分配给本轮选择的账号。
6. 检查任务详情里的 `account_id`、`cdp_port`、`cdp_target_id` 正确。
7. 检查 `screenshot_path`、`answer_text_path`、`search_results_path` 对应文件存在。
8. 选择 `3 个账号` 但只登录两个，确认第三个未登录账号不会在未选择时抢任务。
