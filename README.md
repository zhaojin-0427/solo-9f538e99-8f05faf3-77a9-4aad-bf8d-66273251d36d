# 风机振动分区更正 · 补算清单与确认追溯系统

风机振动数据按"日分区"存储。某日分区被更正后，数据员需要：

1. 排出**补算清单**：哪些三日特征窗口、哪些告警评分切片需要重算；
2. 事后能**查明每个评分切片为何入选**（命中了哪些窗口、对应哪些源分区修订号）。

本服务用 **FastAPI + SQLite** 实现，提供 REST 接口与单页操作界面，支持 `docker compose` 一键启动。

## 领域模型与核心规则

### 版本化、不可变的三类事实记录

| 记录 | 唯一键 | 说明 |
| --- | --- | --- |
| 分区修订 `partition_rev` | 风机 + UTC 日期 + 修订号 | 同一风机同一日期修订号严格递增，再次更正用更大的修订号 |
| 特征窗口 `feature_window` | 风机 + 特征名 + 结束日 + 版本 | 三日窗口，覆盖**结束日及前两日** |
| 评分切片 `scoring_slice` | 风机 + 切片名 + 版本 | 通过 `slice_window_ref` 按所见版本引用若干窗口，仅限同风机 |

同一条同版记录可被多张确认单通过关联表**共用**，后续更正/换版不会修改或删除旧记录。

### 影响面计算（按当前已提交状态）

- **命中窗口**：最新版窗口的三日覆盖与任一更正日（同风机）相交；
- **命中切片**：最新版切片引用了任一命中窗口；
- **任务排序**：同一特征的连续结束日窗口**合并为一个特征任务**，去重后先列特征任务、再列切片任务；
- 每个窗口附三日源分区修订号，每个切片任务附全部命中窗口及其源修订号，回答"为何入选"。

### 预览、确认与失效

- `POST /api/previews` 生成预览，同时**留存**源分区/窗口/切片引用与"所见版本指纹"；
- `POST /api/previews/{id}/confirm` 按所见版本确认：
  - 相关分区修订、窗口、切片或切片-窗口引用发生变化（含**新增命中**的窗口/切片）→ **409 拒绝旧预览**，响应给出差异；
  - 无关风机、或同风机但不覆盖更正日的改动 → 仍可确认；
  - 成功才发确认号 `CF-YYYYMMDD-000001`（发号器在同一事务内消耗，保存失败不发号、不留单，整体回滚）。

### 按确认号追溯（含重启、换版之后）

`GET /api/orders/{confirmation_no}` **不依赖当前最新版本**，仅凭留存的源修订、窗口、切片引用复算原清单：窗口/切片仍是旧版本、源修订号仍是旧号，并逐切片列出命中窗口与源修订号。

## 接口一览

| 方法 | 路径 | 用途 |
| --- | --- | --- |
| POST | `/api/partitions` | 上传风机 + UTC 日期 + 递增修订号 |
| POST | `/api/windows` | 登记三日特征窗口版本（version 留空自动递增） |
| POST | `/api/slices` | 登记评分切片版本及窗口版本引用 |
| POST | `/api/previews` | 按更正点生成补算清单预览 |
| POST | `/api/previews/{id}/confirm` | 按所见版本确认，成功返回确认号 |
| GET | `/api/previews/{id}` | 打开预览单（留存引用复算） |
| GET | `/api/orders/{confirmation_no}` | 按确认号打开旧单并复算 |
| GET | `/api/partitions` `/api/windows` `/api/slices` `/api/previews` | 登记数据浏览 |
| GET | `/` | 单页操作界面；`/docs` 为 OpenAPI 文档 |

## 启动方式

### docker compose

```bash
docker compose up --build
# 访问 http://localhost:8000 ，SQLite 持久化在命名卷 vibration-data (/data/vibration.db)
```

### 本地运行

```bash
pip install -r requirements.txt
export APP_DB_PATH=/data/vibration.db   # 可选，默认 /data/vibration.db
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

## 测试与冒烟

```bash
python -m pytest tests/ -q               # 15 个 HTTP 链路用例
python scripts/smoke.py                  # 对运行中的服务做端到端剧情验证
```

测试覆盖：递增修订号约束、三日命中与连续日期合并、先特征后切片排序、
源分区再更正/窗口换版/切片换版拒绝旧预览、无关改动放行、
真实重启后按确认号复算、同版记录多单共用、保存失败不留单不发号。

## 目录结构

```
app/
  database.py   # SQLite 连接与建表（版本化事实表 + 确认单留存引用表）
  schemas.py    # Pydantic 入参
  services.py   # 影响面计算、指纹比对、预览确认与留存复算
  errors.py     # 统一业务错误
  main.py       # FastAPI 路由
  static/index.html  # 单页操作界面
tests/          # pytest 全链路用例
scripts/smoke.py    # 端到端冒烟脚本（仅标准库）
Dockerfile / docker-compose.yml / requirements.txt
```
