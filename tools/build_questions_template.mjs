import fs from "node:fs/promises";
import { fileURLToPath } from "node:url";
import { SpreadsheetFile, Workbook } from "@oai/artifact-tool";

const projectRoot = fileURLToPath(new URL("..", import.meta.url));
const outputPath = `${projectRoot}questions.xlsx`;

const statuses = [
  ["status", "meaning"],
  ["pending", "待采集"],
  ["running", "正在采集"],
  ["success", "完成截图和结果保存"],
  ["manual_required", "登录/验证/风控，需要人工处理"],
  ["failed", "流程失败"],
];

const taskHeaders = [
  "id",
  "question",
  "platform",
  "round",
  "status",
  "screenshot_path",
  "answer_text_path",
  "remark",
  "updated_at",
];

const sampleRows = [
  [
    "Q001",
    "推荐几个适合学生用的 AI 搜索工具，并说明各自适合什么场景。",
    "deepseek",
    1,
    "pending",
    "",
    "",
    "",
    "",
  ],
  [
    "Q002",
    "国内有哪些适合写论文的 AI 工具？请按资料检索、写作辅助、润色三类整理。",
    "deepseek",
    1,
    "pending",
    "",
    "",
    "",
    "",
  ],
  [
    "Q003",
    "请比较 AI 搜索工具和传统搜索引擎在学习场景中的优缺点。",
    "deepseek",
    1,
    "pending",
    "",
    "",
    "",
    "",
  ],
];

const workbook = Workbook.create();
const tasks = workbook.worksheets.add("Tasks");
const guide = workbook.worksheets.add("Guide");

tasks.getRange("A1:I1").values = [taskHeaders];
tasks.getRange(`A2:I${sampleRows.length + 1}`).values = sampleRows;
tasks.getRange("A6:I105").values = Array.from({ length: 100 }, () => Array(9).fill(""));

guide.getRange("A1:B1").values = [["Yingdao GEO Collector MVP", "DeepSeek only"]];
guide.getRange("A3:B8").values = statuses;
guide.getRange("A10:B17").values = [
  ["Input workbook", "questions.xlsx"],
  ["Output directory", "yingdao_results/YYYYMMDD/"],
  ["Screenshot naming", "{id}_{platform}_round{round}_{HHMMSS}.png"],
  ["Answer text naming", "{id}_{platform}_round{round}_{HHMMSS}.txt"],
  ["Supported platform", "deepseek"],
  ["DeepSeek URL", "https://chat.deepseek.com/"],
  ["Safety rule", "Do not solve or bypass CAPTCHA; mark manual_required."],
  ["Resume rule", "After manual handling, set status back to pending and rerun."],
];
guide.getRange("A19:B26").values = [
  ["Main step 1", "Read rows in Tasks where status = pending and platform = deepseek."],
  ["Main step 2", "Set status = running and updated_at = current time."],
  ["Main step 3", "Open real Chrome and navigate to DeepSeek."],
  ["Main step 4", "If login or verification is detected, screenshot and set manual_required."],
  ["Main step 5", "Create a new chat, paste question from clipboard, and send."],
  ["Main step 6", "Wait for generation to finish; fallback wait is 90 seconds."],
  ["Main step 7", "Save screenshot and answer text when available."],
  ["Main step 8", "Set status = success or failed with remark."],
];

for (const sheet of [tasks, guide]) {
  sheet.getRange("A1:I1").format.fill.color = "#1f2937";
  sheet.getRange("A1:I1").format.font.color = "#ffffff";
  sheet.getRange("A1:I1").format.font.bold = true;
}

tasks.getRange("A:I").format.autofitColumns();
guide.getRange("A:B").format.autofitColumns();
tasks.getRange("A:A").format.columnWidthPx = 78;
tasks.getRange("B:B").format.columnWidthPx = 560;
tasks.getRange("C:E").format.columnWidthPx = 96;
tasks.getRange("F:G").format.columnWidthPx = 230;
tasks.getRange("H:H").format.columnWidthPx = 220;
tasks.getRange("I:I").format.columnWidthPx = 160;
guide.getRange("A:A").format.columnWidthPx = 190;
guide.getRange("B:B").format.columnWidthPx = 560;
tasks.getRange("B:B").format.wrapText = true;
tasks.getRange("A1:I105").format.wrapText = true;
guide.getRange("A1:B26").format.wrapText = true;
tasks.getRange("A1:I105").format.autofitRows();
guide.getRange("A1:B26").format.autofitRows();

await fs.mkdir(projectRoot, { recursive: true });
const output = await SpreadsheetFile.exportXlsx(workbook);
await output.save(outputPath);
console.log(outputPath);
