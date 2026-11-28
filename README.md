# 教学诊改协同台

教师、教研室主任和教务处在课堂观摩后填写教学反馈、制定改进方案、跟踪实施进度并在学期末完成复盘归档。

这是一个前后端分离的管理平台：前端 Vue 3 + Vite + TypeScript，后端 FastAPI（Python）。
两边各自独立启动，前端 dev server 已关掉自动打开页面，启动后按终端打印的地址手工打开。

## 目录结构

```text
.
├── frontend/                 Vue 3 + Vite + TypeScript 前端
│   ├── src/views/WorkbenchView.vue|tsx  全部模块共享工作台
│   ├── src/data/modules.ts              18个业务模块配置
│   ├── src/api/              统一请求封装
│   └── vite.config.ts        dev server 配置（open: false）
├── backend/                  FastAPI（Python） 后端
│   ├── app/routers/resource.py  通用查询/导出/登记/动作接口（原业务入口，保持不变）
│   ├── app/routers/cutover.py   持久化切换与工作流接口（迁移/回执/分发/待办/认领/回滚）
│   ├── app/services/<key>.py    每个模块独立领域逻辑
│   ├── app/services/base.py     共享服务骨架
│   ├── app/services/migration.py 存量迁移与切换回执
│   ├── app/services/workflow.py  幂等分发、待办收件箱、认领资格
│   ├── app/database.py       SQLite 连接、建表与底层读写
│   └── app/store.py          数据仓库：写回式容器 + 切换台账，底层落 SQLite
├── .gitignore
└── docker-compose.yml        backend 挂载 workbench-data 卷持久化 SQLite
```

## 启动

### 后端

```bash
cd backend
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
./run.sh
```

健康检查：`curl http://127.0.0.1:8000/api/health`

### 前端

```bash
cd frontend
npm ci
npm run dev
```

前端默认监听 `http://127.0.0.1:5173/`，dev server 不会自动打开浏览器，
需要自己访问。`/api` 由 vite 代理到后端 `http://127.0.0.1:8000`。

## 业务模块

| 模块 | 目录 | 业务对象 | 主要字段 |
| --- | --- | --- | --- |
| 课堂观摩 | `observation` | 观摩记录 | 教师姓名、课程名称、授课日期 |
| 教学反馈 | `feedback` | 反馈单 | 反馈对象、反馈要点、改进建议 |
| 改进方案 | `improve_plan` | 改进方案 | 关联反馈、改进目标、改进措施 |
| 实施进展 | `progress_log` | 进展记录 | 关联方案、当前阶段、完成情况 |
| 同行评议 | `peer_review` | 评议表 | 被评教师、评议维度、得分 |
| 教学档案 | `teach_record` | 档案条目 | 教师姓名、档案类型、内容摘要 |
| 学期目标 | `semester_goal` | 学期目标 | 目标描述、量化指标、责任教师 |
| 复盘会议 | `review_meeting` | 会议纪要 | 会议主题、参会人员、召开日期 |
| 诊改任务 | `improve_task` | 诊改任务 | 任务描述、承接方案、执行人员 |
| 外部评估 | `external_eval` | 评估报告 | 评估机构、评估指标、评估结论 |
| 课程组编排 | `course_group` | 课程组 | 组名、覆盖课程、组长教师 |
| 大纲修订 | `syllabus_rev` | 修订稿 | 课程名称、修订章节、原内容 |
| 教学成果申报 | `teach_award` | 成果申报 | 成果名称、完成团队、成果概要 |
| 集体备课 | `lesson_prep` | 备课记录 | 备课章节、主备教师、参与教师 |
| 师徒结对 | `mentor_pair` | 结对记录 | 指导教师、青年教师、结对目标 |
| 试卷分析 | `exam_analysis` | 分析记录 | 考试名称、任课教师、参考人数 |
| 排课编排 | `class_schedule` | 排课单 | 课程名称、授课教师、上课时间 |
| 教学资源申请 | `resource_apply` | 资源申请 | 申请资源、用途说明、使用时间 |
| 质量简报 | `quality_report` | 质量简报 | 覆盖学期、教学质量概况、突出问题 |
| 教材选用 | `textbook_select` | 选用单 | 教材名称、主编、出版社 |

## 约定

- 全部模块共享`frontend/src/views/WorkbenchView.vue|tsx`和一套资源路由。
- 每个模块仍保留独立`backend/app/services/<模块>.py`；纯前端保留
  `frontend/src/domain/<模块>.ts`，用于承载真实业务差异并维持36题位容量。
- 列表接口统一返回 `{ items, total, page, size }`，动作接口统一返回 `{ ok, message }`。
- 状态流转只允许在 `app/services` 里改，路由层不做业务判断。

## 持久化与存量切换

数据仓库已从进程内内存台账迁移到 SQLite（仅用标准库 `sqlite3`，无新增依赖）。
原业务入口 `/api/{module}`、领域服务与前端调用方式保持不变；`store.py` 用写回式
容器把服务里的原地修改自动落库，并新增四张切换台账表。

- 数据库位置：环境变量 `APP_DB_PATH`，默认 `backend/data/workbench.sqlite3`；
  切换回执与备份目录：`APP_RECEIPT_DIR`，默认 `backend/data/migration/`。
- 首次启动且数据库为空时自动灌入示例数据；删除数据库文件即可回到全新状态。

### 切换接口（均在 `/api` 下，与资源路由并存）

| 接口 | 作用 |
| --- | --- |
| `POST /api/migration/imports` | 导入旧工作台导出的存量记录，返回可核对的切换回执；验证不通过自动回滚 |
| `GET  /api/migration/batches` | 迁移批次列表 |
| `GET  /api/migration/batches/{id}` | 查询某批次切换回执（记录数、关联结果、来源保留情况） |
| `POST /api/migration/batches/{id}/verify` | 对已导入批次重新核对记录数与关联 |
| `POST /api/migration/batches/{id}/rollback` | 按批次回滚，恢复服务；已受理的认领/评议台账保留 |
| `POST /api/dispatch` | 带 `dispatch_key` 的幂等动作分发，中断重试取回原结果，不重复派单 |
| `GET  /api/dispatch` | 分发台账查询 |
| `GET  /api/todos` | 待办收件箱（带认领资格、业务状态和原出处） |
| `GET  /api/{module}/{id}/eligibility` | 任务入口与待办入口共用的认领资格 |
| `POST /api/claims` | 幂等受理；已受理重试返回原结果，两边入口状态同步 |
| `GET  /api/claims` | 认领台账查询 |

### 切换日在途问题的处理约定

- 导入保留纪要、诊改任务、备课评议的**原编号与来源**（`source_code`/`source_ref` 落列），
  回执给出每个模块的记录数对照与 `meeting:`/`lesson_prep:` 等跨模块关联解析结果。
- 待认领事项按确定性幂等键写入 `todo_inbox`，重复导入或重复触发只更新不新增，
  不会因停用旧内存服务而全部重发。
- 分发与认领分别记 `dispatch_ledger`/`claim_ledger`：分发者中断后用同一幂等键重试，
  直接取回首次处理结果；同一份待办受理后，任务入口与待办入口同步变为不可再领。
- 导入前做 SQLite 在线备份；验证不通过自动回滚，也可按批次人工回滚。
  回滚只撤回本批业务记录，已经向操作员确认受理的诊改任务和评议邀请台账保留，
  重新导入后自动接续，仍可继续办理。

## 测试

```bash
cd backend
.venv/bin/python -m pytest -q
```

测试使用独立临时 SQLite 库（见 `tests/conftest.py`），不污染工作区数据。

领域：应用开发
子方向：web前端/全栈开发
